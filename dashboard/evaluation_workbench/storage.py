"""工作台 SQLite 与文件存储。保持与现有业务模块隔离。"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from dashboard.evaluation_workbench.prompt_templates import (
    PROMPT_TEMPLATE_SETTING, PROMPT_TEMPLATES, default_template, template_presentation,
)


MAX_BID_DOCUMENTS = 12
# 排队上限：每项目最多 3 个排队任务，全局最多 12 个。计数只统计 queued，
# 运行中的任务不计入；入队检查由 create_task 用 BEGIN IMMEDIATE 短事务包住，
# 保证并发到达的请求不会同时读到未满额度而突破上限。与单 worker、全局 FIFO、
# 2 核 2 GB 服务器匹配：只增加可排队数量，不增加模型并发、OCR 并发或常驻内存。
MAX_QUEUED_TASKS_PER_PROJECT = 3
MAX_QUEUED_TASKS_GLOBAL = 12
# 分块写盘，避免大文件上传时占用整份内存；生产环境可按磁盘容量通过环境变量下调。
MAX_UPLOAD_MB = max(1, int(os.environ.get("EVALUATION_WORKBENCH_MAX_UPLOAD_MB", "500")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
GLOBAL_RULE_CATEGORIES = {"qualification", "compliance", "substantive", "other"}
VISION_ENABLED_SETTING = "evaluation_workbench_vision_enabled"
# OCR 与多模态图片识别是两项独立能力。保留旧的 VISION_ENABLED_SETTING
# 仅表示“是否允许发送图片给多模态模型”，避免文字模型也被迫具备图片能力。
OCR_ENABLED_SETTING = "evaluation_workbench_ocr_enabled"
DEFAULT_VISION_MODEL_SETTING = "default_vision_model_profile_id"
TENCENT_OCR_CONFIGURATION_SETTING = "evaluation_workbench_tencent_ocr_configuration"
# 本地 OCR 验收样本统计只用于配置页提示；加短 TTL 缓存，避免每次 OCR 额度预留
# 都执行一次全表聚合（见 _local_ocr_readiness）。
_LOCAL_OCR_READINESS_CACHE: dict[str, object] = {}
_LOCAL_OCR_READINESS_TTL_SECONDS = 300
_LOCAL_OCR_READINESS_LOCK = threading.Lock()
TENCENT_OCR_SERVICES = {
    "accurate": {"label": "通用文字识别（高精度版）", "action": "GeneralAccurateOCR", "default_limit": 900,
                 "usage": "证书编号、小字、复杂背景及关键字段"},
    "basic": {"label": "通用印刷体识别", "action": "GeneralBasicOCR", "default_limit": 900,
              "usage": "清晰的普通印刷文字"},
    # 两个旧接口仍可能向老账号分别发放月度免费包；新账号默认关闭，实际可用时再启用。
    "fast": {"label": "通用印刷体识别（高速版）", "action": "GeneralFastOCR", "default_limit": 900,
             "legacy": True, "usage": "清晰且批量的普通印刷文字"},
    "efficient": {"label": "通用印刷体识别（精简版）", "action": "GeneralEfficientOCR", "default_limit": 900,
                  "legacy": True, "usage": "低强度、版面简单的清晰文字"},
    "table": {"label": "表格识别 V3", "action": "RecognizeTableAccurateOCR", "default_limit": 900,
              "usage": "评分表、参数表、明细表及清单"},
    "biz_license": {"label": "营业执照识别", "action": "BizLicenseOCR", "default_limit": 900,
                    "usage": "营业执照结构化字段"},
}

_SCORE_TOTAL_PATTERN = re.compile(r"(?:总计|共计|合计|最高(?:得)?|最多(?:得)?|满分(?:为)?)\s*(\d+(?:\.\d+)?)\s*分")
_SCORE_VALUE_PATTERN = re.compile(r"(?:得|扣)\s*(\d+(?:\.\d+)?)\s*分")
_LEGACY_EXTRACT_RULES_USER_SHA256 = "4fe464136f54fb033ac1824271f0d942a3d7f3d13b53c04acdf498ac152ff3d2"
# 该值是 2026-07-22 之前随系统同步到云端、但没有人工编辑过的默认模板。
# 保留它可让本次规则质量升级实际作用于既有部署，同时绝不覆盖用户手动编辑的内容。
_PREVIOUS_DEFAULT_EXTRACT_RULES_USER_SHA256 = "a4bb928f79c5e954c155a344ae817231ade404c684da1afd7f111bdb284ab578"
# 2026-07-27 v16 部署时的默认模板。云端将默认内容保存成了 override；只有哈希
# 完全一致时才升级，任何人工编辑（哪怕一个字符）都会被保留。
_V16_DEFAULT_EXTRACT_RULES_USER_SHA256 = "faca26909a0098c11c32ee238dbaa167e52c8bc85a767922abbdb87f9027cd29"
_V16_DEFAULT_EXTRACT_RULES_CONTINUE_SHA256 = "922d2d7c6e8df3fa6851cd214d7e36d8949e5c02d194f9ad95850a78be5ba5d1"
_RUNTIME_RELEASE_CACHE: str | None = None
_RUNTIME_CODE_CACHE: str | None = None


def _runtime_project_root() -> Path:
    """返回运行代码根目录，便于测试与版本事实读取保持同一入口。"""
    return Path(__file__).resolve().parents[2]


def _read_release_marker(path: Path) -> tuple[bool, str]:
    """读取镜像构建标记，区分“文件缺失”和“文件存在但不可用”。"""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False, ""
    value = raw[:40]
    return True, value if re.fullmatch(r"[0-9a-fA-F]{7,40}", value) else ""


def runtime_release_fingerprint() -> str:
    """返回当前运行代码的可公开版本标识，用于任务复现与缓存隔离。

    镜像内 ``.build-commit`` 是运行代码的唯一生产事实。镜像已带该文件但内容无效时，
    不能退回宿主机部署记录或挂载目录 Git HEAD 冒充运行版本，只能明确返回 ``unknown``；
    本地开发没有镜像标记时才读取 Git HEAD。不会调用 git 子进程，也不读取任何凭据。
    """
    global _RUNTIME_RELEASE_CACHE
    if _RUNTIME_RELEASE_CACHE is not None:
        return _RUNTIME_RELEASE_CACHE
    root = _runtime_project_root()
    image_marker_exists, image_commit = _read_release_marker(root / ".build-commit")
    if image_marker_exists:
        _RUNTIME_RELEASE_CACHE = image_commit or "unknown"
        return _RUNTIME_RELEASE_CACHE

    value = str(os.environ.get("DEPLOY_COMMIT") or "").strip()[:40]
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
        value = ""
    if not value:
        try:
            value = (root / ".deploy-commit").read_text(encoding="utf-8").strip()[:40]
        except OSError:
            pass
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
        value = ""
    if not value:
        git_dir = root / ".git"
        try:
            if git_dir.is_file():
                raw = git_dir.read_text(encoding="utf-8").strip()
                if raw.startswith("gitdir:"):
                    target = raw.split(":", 1)[1].strip()
                    git_dir = (Path(target) if Path(target).is_absolute() else root / target).resolve()
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref:"):
                ref = head.split(":", 1)[1].strip()
                value = (git_dir / ref).read_text(encoding="utf-8").strip()[:40]
            else:
                value = head[:40]
        except OSError:
            pass
    _RUNTIME_RELEASE_CACHE = value or "unknown"
    return _RUNTIME_RELEASE_CACHE


def runtime_code_fingerprint() -> str:
    """返回核心运行源码指纹，补足开发期未提交修改的缓存失效。

    部署提交号便于人工追溯，但同一提交下的本地测试改动也会改变结果；这里只散列
    工作台核心模块的源码，不读取业务文件、数据库、提示词覆盖或敏感配置。
    """
    global _RUNTIME_CODE_CACHE
    if _RUNTIME_CODE_CACHE is not None:
        return _RUNTIME_CODE_CACHE
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256(runtime_release_fingerprint().encode("utf-8"))
    for relative in (
        "dashboard/evaluation_workbench/storage.py",
        "dashboard/evaluation_workbench/worker.py",
        "dashboard/evaluation_workbench/ai_gateway.py",
        "dashboard/evaluation_workbench/prompt_templates.py",
        "dashboard/evaluation_workbench/local_ocr_gateway.py",
    ):
        try:
            digest.update((root / relative).read_bytes())
        except OSError:
            digest.update(relative.encode("utf-8"))
    _RUNTIME_CODE_CACHE = digest.hexdigest()
    return _RUNTIME_CODE_CACHE


def compare_pipeline_metadata(app, profile_id: str | None = None, input_fingerprint: str = "") -> dict:
    """返回纯文字查重的可复现身份，不包含正文、密钥或 OCR 配置。"""
    # 延迟导入避免 storage 初始化时与 worker/collusion_signals 形成循环引用。
    from dashboard.evaluation_workbench.collusion_signals import ANALYSIS_VERSION
    from dashboard.utils.comparator import ALGORITHM_VERSION

    profile = get_model_profile(app, profile_id, "deepseek-v4-flash")
    source_digest = hashlib.sha256()
    root = _runtime_project_root()
    for relative in (
        "dashboard/utils/comparator.py",
        "dashboard/evaluation_workbench/collusion_signals.py",
        "dashboard/evaluation_workbench/worker.py",
        "dashboard/evaluation_workbench/prompt_templates.py",
    ):
        source_digest.update(relative.encode("utf-8"))
        try:
            source_digest.update((root / relative).read_bytes())
        except OSError:
            source_digest.update(b"missing")
    value = {
        "analysis_version": ANALYSIS_VERSION,
        "comparator_version": ALGORITHM_VERSION,
        "runtime_release": runtime_release_fingerprint(),
        "compare_source": source_digest.hexdigest(),
        "prompt_templates": task_prompt_template_fingerprint(app, "compare_documents"),
        "model": {
            "profile_id": profile.get("profile_id"),
            "model_name": profile.get("model_name"),
            "base_url": profile.get("base_url"),
            "updated_at": profile.get("updated_at"),
            "json_mode": profile.get("json_mode"),
            "thinking_mode": profile.get("thinking_mode"),
        },
        "input_fingerprint": str(input_fingerprint or ""),
        "text_only": True,
    }
    value["fingerprint"] = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return value


def _validate_api_key_characters(api_key: str) -> None:
    """API Key 会被放入 HTTP Header，必须是可安全编码的单行 ASCII 文本。"""
    if any(not (0x21 <= ord(character) <= 0x7E) for character in api_key):
        raise ValueError(
            "API Key 含有中文、全角符号、空格或不可见字符；请只粘贴服务商控制台生成的纯文本 Key"
        )


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _prompt_template_overrides(app) -> dict[str, str]:
    with connection(app) as conn:
        row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = ?", (PROMPT_TEMPLATE_SETTING,)).fetchone()
    if not row:
        return {}
    try:
        values = json.loads(row["setting_value"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return {key: value for key, value in values.items() if key in PROMPT_TEMPLATES and isinstance(value, str)} if isinstance(values, dict) else {}


def _effective_prompt_template_content(template_id: str, override: object = None) -> str:
    """兼容旧自定义模板缺少后续新增的系统输出字段。

    用户自定义内容保持原样；只有旧配置没有包含新引入且不可缺的协议字段时，才在
    实际生效内容末尾补充对应的系统短协议。列表接口也使用这里的结果，确保页面可见
    的提示词与运行时一致，用户保存后即可把协议纳入自己的模板。
    """
    meta = PROMPT_TEMPLATES[template_id]
    content = str(override if isinstance(override, str) else meta["content"])
    suffix = str(meta.get("system_required_suffix") or "").strip()
    required = tuple(str(value) for value in meta.get("required_literals", ()) if str(value).strip())
    if suffix and any(literal not in content for literal in required):
        return content.rstrip() + "\n\n【系统必需输出协议】\n" + suffix
    return content


def list_prompt_templates(app) -> list[dict]:
    overrides = _prompt_template_overrides(app)
    values = [
        {"template_id": template_id, "name": meta["name"], "description": meta["description"],
         "content": _effective_prompt_template_content(template_id, overrides.get(template_id)), "is_custom": template_id in overrides,
         "placeholders": list(meta.get("placeholders", ())), **template_presentation(template_id)}
        for template_id, meta in PROMPT_TEMPLATES.items()
    ]
    return sorted(values, key=lambda item: (item["sort_order"], item["name"]))


def prompt_template(app, template_id: str) -> str:
    if template_id not in PROMPT_TEMPLATES:
        raise ValueError("不支持的提示词流程")
    return _effective_prompt_template_content(template_id, _prompt_template_overrides(app).get(template_id))


def render_prompt_template(app, template_id: str, **values: object) -> str:
    """渲染用户可编辑模板；仅替换显式 {{占位符}}，不解释 JSON 花括号。"""
    content = prompt_template(app, template_id)
    required = PROMPT_TEMPLATES[template_id].get("placeholders", ())
    missing = [name for name in required if name not in values]
    if missing:
        raise ValueError(f"提示词模板“{template_id}”缺少运行时变量：{', '.join(missing)}")
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda match: str(values.get(match.group(1), match.group(0))), content)


def prompt_template_fingerprint(app, template_ids: set[str] | tuple[str, ...] | list[str] | None = None) -> str:
    """生成提示词指纹；可限定到一个任务真正使用的模板集合。"""
    selected = set(template_ids) if template_ids is not None else None
    values = {
        item["template_id"]: item["content"]
        for item in list_prompt_templates(app)
        if selected is None or item["template_id"] in selected
    }
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def task_prompt_template_fingerprint(app, task_type: str) -> str | None:
    """只让会实际参与该任务的提示词影响缓存，避免无关编辑触发昂贵重跑。"""
    templates_by_task = {
        "compare_documents": {"compare_ai_assessment", "compare_ai_assessment_user", "json_repair", "json_repair_user"},
        "extract_rules": {
            "extract_rules", "extract_rules_guidance", "extract_rules_validation_guidance", "extract_rules_user",
            "extract_rules_continue_user",
            "extract_rules_supplement_user",
            "extract_rules_qualification_supplement_user",
            "extract_rules_hard_anchor_supplement_user",
            "extract_rules_rejection_ledger_user",
            "extract_rules_dedupe_adjudication_user",
            "extract_rules_obligation_compile_user",
            "extract_rules_scoring_structure_repair_user",
            "json_repair", "json_repair_user",
        },
        "extract_price_rules": {
            "extract_rules", "extract_rules_guidance", "extract_rules_scoring_assembly_user",
            "extract_rules_scoring_supplement_user", "json_repair", "json_repair_user",
        },
        "calculate_price_scores": {"price_score_calculation", "price_score_calculation_user", "json_repair", "json_repair_user"},
        "review_documents": {"review_documents", "review_documents_user", "json_repair", "json_repair_user"},
        "score_objective": {"score_objective", "score_objective_user", "json_repair", "json_repair_user"},
        "score_subjective": {"score_subjective", "score_subjective_user", "json_repair", "json_repair_user"},
        "evaluate_all": {
            "evaluate_all", "evaluate_all_guidance", "evaluate_all_highlights", "evaluate_all_scope_profile", "evaluate_all_scope_profile_user",
            "evaluate_all_scope_anomaly_guidance", "evaluate_all_full_scan_user", "evaluate_all_review_user", "evaluate_all_objective_user",
            "evaluate_all_subjective_user", "evaluate_all_cross_bid_subjective_shadow_user", "evaluate_all_highlights_user",
            "evaluate_all_visual_user", "evaluate_all_ocr_user", "evaluate_all_visual_contract", "evaluate_all_ocr_contract",
            "evaluate_all_ocr_batch_user",
            "evaluate_all_visual_locator_user",
            "evaluate_all_output_contract",
            "json_repair", "json_repair_user",
        },
    }
    template_ids = templates_by_task.get(task_type)
    return prompt_template_fingerprint(app, template_ids) if template_ids else None


def update_prompt_template(app, template_id: str, content: object) -> dict:
    if template_id not in PROMPT_TEMPLATES:
        raise ValueError("不支持的提示词流程")
    value = str(content or "").strip()
    if not 20 <= len(value) <= 12_000:
        raise ValueError("提示词长度应在 20 到 12000 个字符之间")
    missing = [name for name in PROMPT_TEMPLATES[template_id].get("placeholders", ()) if f"{{{{{name}}}}}" not in value]
    if missing:
        raise ValueError(f"提示词不能删除运行时变量：{', '.join('{{' + name + '}}' for name in missing)}")
    required_literals = PROMPT_TEMPLATES[template_id].get("required_literals", ())
    absent = [literal for literal in required_literals if literal not in value]
    if absent:
        raise ValueError(f"提示词不能删除系统结果字段或证据约束：{', '.join(absent)}")
    overrides = _prompt_template_overrides(app)
    overrides[template_id] = value
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
            (PROMPT_TEMPLATE_SETTING, json.dumps(overrides, ensure_ascii=False), now_iso()),
        )
    return next(item for item in list_prompt_templates(app) if item["template_id"] == template_id)


def reset_prompt_template(app, template_id: str) -> dict:
    if template_id not in PROMPT_TEMPLATES:
        raise ValueError("不支持的提示词流程")
    overrides = _prompt_template_overrides(app)
    overrides.pop(template_id, None)
    with connection(app) as conn:
        if overrides:
            conn.execute(
                "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
                (PROMPT_TEMPLATE_SETTING, json.dumps(overrides, ensure_ascii=False), now_iso()),
            )
        else:
            conn.execute("DELETE FROM ew_settings WHERE setting_key = ?", (PROMPT_TEMPLATE_SETTING,))
    return next(item for item in list_prompt_templates(app) if item["template_id"] == template_id)


def data_dir(app) -> Path:
    configured = app.config.get("EVALUATION_WORKBENCH_DATA_DIR")
    if configured:
        path = Path(configured)
    else:
        path = Path(app.root_path).parent / "data" / "evaluation_workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path(app) -> Path:
    return data_dir(app).parent / "evaluation_workspace.db"


@contextmanager
def connection(app, *, immediate: bool = False):
    conn = sqlite3.connect(str(database_path(app)), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if immediate:
            # 需要“先检查、再写入”原子性的短事务（如入队）必须显式加写锁：
            # 普通连接在 WAL 下并发读不互斥，两个请求可能同时看到未满额度。
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_database(app) -> None:
    marker = str(database_path(app).resolve())
    if app.extensions.get("evaluation_workbench_database") == marker and Path(marker).exists():
        return
    with connection(app) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ew_projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                project_number TEXT NOT NULL DEFAULT '',
                section_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_documents (
                document_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('tender', 'tender_attachment', 'bid')),
                bidder_name TEXT NOT NULL DEFAULT '',
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                page_count INTEGER,
                text_length INTEGER,
                parse_status TEXT NOT NULL DEFAULT 'pending',
                parse_error TEXT,
                parsed_path TEXT,
                quote_value TEXT,
                quote_source TEXT NOT NULL DEFAULT '',
                quote_excerpt TEXT NOT NULL DEFAULT '',
                quote_candidates_json TEXT NOT NULL DEFAULT '[]',
                quote_status TEXT NOT NULL DEFAULT 'pending',
                quote_fingerprint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_documents_project ON ew_documents(project_id, role, created_at);
            CREATE TABLE IF NOT EXISTS ew_price_entries (
                price_entry_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                document_id TEXT REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                bidder_name TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('document', 'manual')),
                extracted_quote TEXT,
                manual_quote TEXT,
                evaluation_price TEXT,
                included INTEGER NOT NULL DEFAULT 1,
                exclusion_reason TEXT NOT NULL DEFAULT '',
                quote_source TEXT NOT NULL DEFAULT '',
                quote_excerpt TEXT NOT NULL DEFAULT '',
                extraction_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(extraction_status IN ('pending', 'found', 'ambiguous', 'missing', 'unavailable')),
                extraction_fingerprint TEXT NOT NULL DEFAULT '',
                manual_scores_json TEXT NOT NULL DEFAULT '{}',
                adjustment_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ew_price_entries_document
                ON ew_price_entries(project_id, document_id) WHERE document_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_ew_price_entries_project
                ON ew_price_entries(project_id, source_type, created_at);
            CREATE TABLE IF NOT EXISTS ew_price_rule_sets (
                price_rule_set_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                task_id TEXT REFERENCES ew_tasks(task_id) ON DELETE SET NULL,
                profile_id TEXT,
                rules_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_price_rule_sets_project
                ON ew_price_rule_sets(project_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS ew_price_score_runs (
                price_score_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                task_id TEXT REFERENCES ew_tasks(task_id) ON DELETE SET NULL,
                profile_id TEXT,
                input_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_price_score_runs_project
                ON ew_price_score_runs(project_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS ew_tasks (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'success', 'error', 'cancelled', 'interrupted')),
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_tasks_queue ON ew_tasks(status, created_at);
            CREATE TABLE IF NOT EXISTS ew_model_calls (
                call_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                document_id TEXT REFERENCES ew_documents(document_id) ON DELETE SET NULL,
                phase TEXT NOT NULL,
                profile_id TEXT,
                context_mode TEXT NOT NULL DEFAULT 'full',
                input_chars INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cache_hit_tokens INTEGER,
                requested_max_tokens INTEGER,
                finish_reason TEXT,
                response_chars INTEGER,
                parse_status TEXT,
                parse_error_kind TEXT,
                local_json_repaired INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_model_calls_project ON ew_model_calls(project_id, created_at);
            CREATE TABLE IF NOT EXISTS ew_output_risk_observations (
                observation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                document_id TEXT REFERENCES ew_documents(document_id) ON DELETE SET NULL,
                phase TEXT NOT NULL,
                context_mode TEXT NOT NULL DEFAULT '',
                input_chars INTEGER NOT NULL DEFAULT 0,
                rule_count INTEGER NOT NULL DEFAULT 0,
                requested_max_tokens INTEGER,
                predicted_risk_score INTEGER NOT NULL DEFAULT 0,
                predicted_risk_level TEXT NOT NULL DEFAULT 'low',
                shadow_split_recommended INTEGER NOT NULL DEFAULT 0,
                actual_format_error INTEGER NOT NULL DEFAULT 0,
                actual_finish_reason TEXT NOT NULL DEFAULT '',
                actual_error_kind TEXT NOT NULL DEFAULT '',
                recovery_action TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_output_risk_project ON ew_output_risk_observations(project_id, phase, created_at);
            CREATE TABLE IF NOT EXISTS ew_local_ocr_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES ew_tasks(task_id) ON DELETE SET NULL,
                project_id TEXT REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                document_id TEXT REFERENCES ew_documents(document_id) ON DELETE SET NULL,
                requested_pages INTEGER NOT NULL DEFAULT 0,
                recognized_pages INTEGER NOT NULL DEFAULT 0,
                empty_pages INTEGER NOT NULL DEFAULT 0,
                failed_pages INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                peak_rss_kb INTEGER,
                status TEXT NOT NULL DEFAULT 'unknown',
                error_kind TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_local_ocr_runs_project ON ew_local_ocr_runs(project_id, created_at);
            CREATE TABLE IF NOT EXISTS ew_evaluation_scan_cache (
                cache_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                scan_key TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                chunk_hash TEXT NOT NULL,
                findings_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(document_id, scan_key, chunk_id, chunk_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_scan_cache_document ON ew_evaluation_scan_cache(document_id, scan_key);
            CREATE INDEX IF NOT EXISTS idx_ew_scan_cache_chunk ON ew_evaluation_scan_cache(document_id, chunk_id, chunk_hash, updated_at);
            CREATE TABLE IF NOT EXISTS ew_document_evidence_manifests (
                manifest_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                document_sha256 TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(document_id, document_sha256, parser_version)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_evidence_manifest_document ON ew_document_evidence_manifests(document_id, updated_at);
            CREATE TABLE IF NOT EXISTS ew_evidence_packs (
                evidence_pack_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                rule_id TEXT NOT NULL REFERENCES ew_rules(rule_id) ON DELETE CASCADE,
                component TEXT NOT NULL CHECK(component IN ('review', 'objective', 'subjective')),
                document_sha256 TEXT NOT NULL,
                rule_fingerprint TEXT NOT NULL,
                material_key TEXT NOT NULL DEFAULT '',
                pack_version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, document_id, rule_id, component)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_evidence_packs_document ON ew_evidence_packs(document_id, rule_id, updated_at);
            CREATE TABLE IF NOT EXISTS ew_evaluation_unit_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                rule_set_id TEXT NOT NULL REFERENCES ew_rule_sets(rule_set_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                component TEXT NOT NULL CHECK(component IN ('review', 'objective', 'subjective')),
                rule_id TEXT NOT NULL REFERENCES ew_rules(rule_id) ON DELETE CASCADE,
                execution_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(rule_set_id, document_id, component, rule_id, execution_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_unit_checkpoint_lookup ON ew_evaluation_unit_checkpoints(project_id, rule_set_id, document_id, execution_fingerprint);
            CREATE TABLE IF NOT EXISTS ew_project_scope_cache (
                scope_cache_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                scope_key TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, scope_key)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_scope_cache_project ON ew_project_scope_cache(project_id, scope_key);
            CREATE TABLE IF NOT EXISTS ew_compare_pairs (
                pair_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                document_a_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                document_b_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_compare_signal_reviews (
                signal_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                human_disposition TEXT NOT NULL DEFAULT 'pending'
                    CHECK(human_disposition IN ('pending', 'verified', 'dismissed', 'needs_more_evidence')),
                human_note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_signal_reviews_task ON ew_compare_signal_reviews(task_id);
            CREATE TABLE IF NOT EXISTS ew_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_model_profiles (
                profile_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                protocol TEXT NOT NULL DEFAULT 'openai-compatible',
                base_url TEXT NOT NULL,
                model_name TEXT NOT NULL,
                api_key_env TEXT NOT NULL,
                api_key_encrypted TEXT,
                context_limit INTEGER,
                timeout_seconds INTEGER NOT NULL DEFAULT 600,
                json_mode INTEGER NOT NULL DEFAULT 1,
                thinking_mode TEXT NOT NULL DEFAULT 'default',
                supports_vision INTEGER NOT NULL DEFAULT 0,
                vision_protocol TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_rule_sets (
                rule_set_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                source_task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, version)
            );
            CREATE TABLE IF NOT EXISTS ew_rules (
                rule_id TEXT PRIMARY KEY,
                rule_set_id TEXT NOT NULL REFERENCES ew_rule_sets(rule_set_id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                check_rule TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL DEFAULT '',
                source_page INTEGER,
                check_mode TEXT NOT NULL DEFAULT 'auto',
                scoring_json TEXT,
                execution_meta_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_global_rules (
                global_rule_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                check_rule TEXT NOT NULL,
                source_text TEXT NOT NULL DEFAULT '',
                check_mode TEXT NOT NULL DEFAULT 'auto',
                execution_meta_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_global_rules_enabled ON ew_global_rules(enabled, category, sort_order);
            CREATE TABLE IF NOT EXISTS ew_review_runs (
                review_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                rule_set_id TEXT NOT NULL REFERENCES ew_rule_sets(rule_set_id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                profile_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_review_results (
                review_result_id TEXT PRIMARY KEY,
                review_run_id TEXT NOT NULL REFERENCES ew_review_runs(review_run_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                rule_id TEXT NOT NULL REFERENCES ew_rules(rule_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                requirement_relation TEXT NOT NULL DEFAULT 'uncertain',
                final_status TEXT,
                evidence TEXT NOT NULL DEFAULT '',
                page_hint TEXT,
                reason TEXT NOT NULL DEFAULT '',
                conclusion_summary TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT 'medium',
                coverage_status TEXT NOT NULL DEFAULT 'covered',
                vision_status TEXT NOT NULL DEFAULT 'not_requested',
                ocr_status TEXT NOT NULL DEFAULT 'not_requested',
                multimodal_status TEXT NOT NULL DEFAULT 'not_requested',
                vision_pages_json TEXT NOT NULL DEFAULT '[]',
                vision_evidence_pages_json TEXT NOT NULL DEFAULT '[]',
                evidence_layers_json TEXT NOT NULL DEFAULT '[]',
                vision_model TEXT NOT NULL DEFAULT '',
                vision_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                UNIQUE(review_run_id, document_id, rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_review_results_run ON ew_review_results(review_run_id, document_id);
            CREATE TABLE IF NOT EXISTS ew_score_runs (
                score_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                rule_set_id TEXT NOT NULL REFERENCES ew_rule_sets(rule_set_id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                score_type TEXT NOT NULL CHECK(score_type IN ('objective', 'subjective')),
                profile_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ew_score_results (
                score_result_id TEXT PRIMARY KEY,
                score_run_id TEXT NOT NULL REFERENCES ew_score_runs(score_run_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                rule_id TEXT NOT NULL REFERENCES ew_rules(rule_id) ON DELETE CASCADE,
                suggested_score REAL,
                final_score REAL,
                max_score REAL,
                evidence TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                conclusion_summary TEXT NOT NULL DEFAULT '',
                confidence TEXT,
                coverage_status TEXT NOT NULL DEFAULT 'covered',
                vision_status TEXT NOT NULL DEFAULT 'not_requested',
                ocr_status TEXT NOT NULL DEFAULT 'not_requested',
                multimodal_status TEXT NOT NULL DEFAULT 'not_requested',
                vision_pages_json TEXT NOT NULL DEFAULT '[]',
                vision_evidence_pages_json TEXT NOT NULL DEFAULT '[]',
                evidence_layers_json TEXT NOT NULL DEFAULT '[]',
                vision_model TEXT NOT NULL DEFAULT '',
                vision_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(score_run_id, document_id, rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_score_results_run ON ew_score_results(score_run_id, document_id);
            CREATE TABLE IF NOT EXISTS ew_evaluation_current_documents (
                project_id TEXT NOT NULL REFERENCES ew_projects(project_id) ON DELETE CASCADE,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                rule_set_id TEXT NOT NULL REFERENCES ew_rule_sets(rule_set_id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ew_tasks(task_id) ON DELETE CASCADE,
                profile_id TEXT,
                document_sha256 TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL DEFAULT '',
                review_run_id TEXT,
                objective_score_run_id TEXT,
                subjective_score_run_id TEXT,
                highlights_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, document_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_current_evaluation_rule_set
                ON ew_evaluation_current_documents(project_id, rule_set_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS ew_ocr_page_cache (
                cache_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES ew_documents(document_id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,
                image_hash TEXT NOT NULL,
                service TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(document_id, page_number, image_hash, service)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_ocr_page_cache_document ON ew_ocr_page_cache(document_id, page_number);
            CREATE TABLE IF NOT EXISTS ew_ocr_usage_ledger (
                usage_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES ew_tasks(task_id) ON DELETE SET NULL,
                project_id TEXT REFERENCES ew_projects(project_id) ON DELETE SET NULL,
                service TEXT NOT NULL,
                month_key TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                billed_units INTEGER NOT NULL DEFAULT 1,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_ocr_usage_month ON ew_ocr_usage_ledger(month_key, service);
            """
        )
        _ensure_column(conn, "ew_review_results", "final_status", "TEXT")
        _ensure_column(conn, "ew_review_results", "requirement_relation", "TEXT NOT NULL DEFAULT 'uncertain'")
        _ensure_column(conn, "ew_review_results", "confirmed_at", "TEXT")
        _ensure_column(conn, "ew_model_profiles", "api_key_encrypted", "TEXT")
        _ensure_column(conn, "ew_model_profiles", "supports_vision", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "ew_model_profiles", "vision_protocol", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_review_results", "confidence", "TEXT")
        _ensure_column(conn, "ew_review_results", "evidence_quality", "TEXT")
        _ensure_column(conn, "ew_review_results", "coverage_status", "TEXT NOT NULL DEFAULT 'covered'")
        _ensure_column(conn, "ew_review_results", "automation_status", "TEXT NOT NULL DEFAULT 'needs_review'")
        _ensure_column(conn, "ew_review_results", "requires_review", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "ew_review_results", "review_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_review_results", "vision_status", "TEXT NOT NULL DEFAULT 'not_requested'")
        _ensure_column(conn, "ew_review_results", "ocr_status", "TEXT NOT NULL DEFAULT 'not_requested'")
        _ensure_column(conn, "ew_review_results", "multimodal_status", "TEXT NOT NULL DEFAULT 'not_requested'")
        _ensure_column(conn, "ew_review_results", "vision_pages_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_review_results", "vision_evidence_pages_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_review_results", "evidence_layers_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_review_results", "vision_model", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_review_results", "vision_message", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_review_results", "conclusion_summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_score_results", "effective_score", "REAL")
        _ensure_column(conn, "ew_score_results", "automation_status", "TEXT NOT NULL DEFAULT 'needs_review'")
        _ensure_column(conn, "ew_score_results", "requires_review", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "ew_score_results", "review_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_score_results", "coverage_status", "TEXT NOT NULL DEFAULT 'covered'")
        _ensure_column(conn, "ew_score_results", "vision_status", "TEXT NOT NULL DEFAULT 'not_requested'")
        _ensure_column(conn, "ew_score_results", "ocr_status", "TEXT NOT NULL DEFAULT 'not_requested'")
        _ensure_column(conn, "ew_score_results", "multimodal_status", "TEXT NOT NULL DEFAULT 'not_requested'")
        _ensure_column(conn, "ew_score_results", "vision_pages_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_score_results", "vision_evidence_pages_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_score_results", "evidence_layers_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_score_results", "vision_model", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_score_results", "vision_message", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_score_results", "conclusion_summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_rules", "source_type", "TEXT")
        _ensure_column(conn, "ew_rules", "source_task_id", "TEXT")
        _ensure_column(conn, "ew_rules", "check_rule", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_rules", "execution_meta_json", "TEXT")
        _ensure_column(conn, "ew_global_rules", "execution_meta_json", "TEXT")
        _ensure_column(conn, "ew_evidence_packs", "material_key", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_price_entries", "adjustment_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "ew_projects", "price_profile_id", "TEXT")
        # 文件清单报价缓存：解析完成时提取一次并落库，价格工作表直接复用同一结果。
        _ensure_column(conn, "ew_documents", "quote_value", "TEXT")
        _ensure_column(conn, "ew_documents", "quote_source", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_documents", "quote_excerpt", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_documents", "quote_candidates_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "ew_documents", "quote_status", "TEXT NOT NULL DEFAULT 'pending'")
        _ensure_column(conn, "ew_documents", "quote_fingerprint", "TEXT NOT NULL DEFAULT ''")
        # 旧数据库先补列，再建索引；把索引放在 CREATE TABLE 脚本里会导致升级时
        # 因旧表尚无 material_key 而中断整个初始化。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ew_evidence_packs_material ON ew_evidence_packs(document_id, material_key, updated_at)")
        _ensure_column(conn, "ew_model_calls", "requested_max_tokens", "INTEGER")
        _ensure_column(conn, "ew_model_calls", "finish_reason", "TEXT")
        _ensure_column(conn, "ew_model_calls", "response_chars", "INTEGER")
        _ensure_column(conn, "ew_model_calls", "parse_status", "TEXT")
        _ensure_column(conn, "ew_model_calls", "parse_error_kind", "TEXT")
        _ensure_column(conn, "ew_model_calls", "local_json_repaired", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE ew_rules SET check_rule = title WHERE check_rule IS NULL OR check_rule = ''")
        conn.execute("UPDATE ew_rules SET source_type = CASE WHEN rule_set_id IN (SELECT rule_set_id FROM ew_rule_sets WHERE source_task_id IS NOT NULL) THEN 'ai' ELSE 'manual' END WHERE source_type IS NULL OR source_type = ''")
        _migrate_known_legacy_prompt_override(conn)
        _seed_ocr_feature_setting(conn)
        _seed_default_profiles(conn)
        _seed_default_model_setting(conn)
    app.extensions["evaluation_workbench_database"] = marker


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_known_legacy_prompt_override(conn: sqlite3.Connection) -> None:
    """只升级已确认的历史默认覆盖，不改写用户后来编辑过的提示词。"""
    row = conn.execute(
        "SELECT setting_value FROM ew_settings WHERE setting_key = ?", (PROMPT_TEMPLATE_SETTING,)
    ).fetchone()
    if not row:
        return
    try:
        overrides = json.loads(row["setting_value"])
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(overrides, dict):
        return
    known_defaults = {
        "extract_rules_user": {
            _LEGACY_EXTRACT_RULES_USER_SHA256,
            _PREVIOUS_DEFAULT_EXTRACT_RULES_USER_SHA256,
            _V16_DEFAULT_EXTRACT_RULES_USER_SHA256,
        },
        "extract_rules_continue_user": {_V16_DEFAULT_EXTRACT_RULES_CONTINUE_SHA256},
    }
    changed = False
    for template_id, known_hashes in known_defaults.items():
        current = overrides.get(template_id)
        if not isinstance(current, str):
            continue
        if hashlib.sha256(current.encode("utf-8")).hexdigest() not in known_hashes:
            continue
        overrides[template_id] = default_template(template_id)
        changed = True
    if not changed:
        return
    # 仅升级已知历史默认值；用户真正编辑过的提示词哈希不同，会原样保留。
    conn.execute(
        "UPDATE ew_settings SET setting_value = ?, updated_at = ? WHERE setting_key = ?",
        (json.dumps(overrides, ensure_ascii=False), now_iso(), PROMPT_TEMPLATE_SETTING),
    )


def _seed_default_profiles(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM ew_model_profiles").fetchone()[0]
    if count:
        return
    timestamp = now_iso()
    rows = [
        (str(uuid.uuid4()), "DeepSeek V4 Flash", "openai-compatible", "https://api.deepseek.com", "deepseek-v4-flash", "DEEPSEEK_API_KEY", 1000000, 600, 1, "disabled", 1, timestamp, timestamp),
        (str(uuid.uuid4()), "DeepSeek V4 Pro", "openai-compatible", "https://api.deepseek.com", "deepseek-v4-pro", "DEEPSEEK_API_KEY", 1000000, 600, 1, "enabled", 1, timestamp, timestamp),
    ]
    conn.executemany(
        """INSERT INTO ew_model_profiles
        (profile_id, display_name, protocol, base_url, model_name, api_key_env, context_limit,
         timeout_seconds, json_mode, thinking_mode, enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )


def _seed_default_model_setting(conn: sqlite3.Connection) -> None:
    """将旧版的 Flash 优先策略迁移为可持久化的全局默认模型。"""
    if conn.execute("SELECT 1 FROM ew_settings WHERE setting_key = 'default_model_profile_id'").fetchone():
        return
    row = conn.execute(
        "SELECT profile_id FROM ew_model_profiles WHERE enabled = 1 AND model_name = 'deepseek-v4-flash' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute("SELECT profile_id FROM ew_model_profiles WHERE enabled = 1 ORDER BY created_at LIMIT 1").fetchone()
    if row:
        conn.execute(
            "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES ('default_model_profile_id', ?, ?)",
            (row["profile_id"], now_iso()),
        )


def _seed_ocr_feature_setting(conn: sqlite3.Connection) -> None:
    """首次升级时继承旧图片总开关，之后 OCR 与多模态独立维护。"""
    if conn.execute("SELECT 1 FROM ew_settings WHERE setting_key=?", (OCR_ENABLED_SETTING,)).fetchone():
        return
    legacy = conn.execute(
        "SELECT setting_value FROM ew_settings WHERE setting_key=?", (VISION_ENABLED_SETTING,)
    ).fetchone()
    inherited = "1" if legacy and str(legacy["setting_value"]) == "1" else "0"
    conn.execute(
        "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?)",
        (OCR_ENABLED_SETTING, inherited, now_iso()),
    )


def project_dir(app, project_id: str) -> Path:
    path = data_dir(app) / project_id
    for name in ("source", "parsed", "results", "reports"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _model_key_path(app) -> Path:
    return data_dir(app).parent / ".evaluation_workspace.key"


def _model_fernet(app) -> Fernet:
    configured = str(app.config.get("EVALUATION_WORKBENCH_SECRET_KEY") or "").strip()
    key_path = _model_key_path(app)
    if configured:
        key = configured.encode("utf-8")
    elif key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key + b"\n")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise ValueError("工作台密钥文件无效，无法读取已保存的模型 API Key") from exc


def _encrypt_model_api_key(app, api_key: str) -> str:
    return _model_fernet(app).encrypt(api_key.encode("utf-8")).decode("ascii")


def _decrypt_model_api_key(app, encrypted: str) -> str:
    try:
        return _model_fernet(app).decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ValueError("已保存的模型 API Key 无法解密，请重新配置该模型") from exc


def _ocr_month_key() -> str:
    """OCR 免费包按中国自然月发放；固定按 UTC+8 计算，不依赖容器或系统时区。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m")


def _ocr_configuration_raw(app) -> dict:
    with connection(app) as conn:
        row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key=?", (TENCENT_OCR_CONFIGURATION_SETTING,)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row["setting_value"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _ocr_service_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    result = {}
    for service, meta in TENCENT_OCR_SERVICES.items():
        item = raw.get(service) if isinstance(raw.get(service), dict) else {}
        default_enabled = not meta.get("legacy")
        result[service] = {
            "enabled": bool(item.get("enabled", default_enabled)),
            "monthly_limit": max(1, min(1000, int(item.get("monthly_limit", meta["default_limit"]) or meta["default_limit"]))),
        }
    return result


def _local_ocr_settings(value: object) -> dict:
    """本地 OCR 是稳定兜底，不再暴露为可关闭的业务开关。

    腾讯云开关关闭时它承担直接 OCR；腾讯云优先路径不可用时它承担回退。旧版本
    保存的 ``local.enabled=false`` 仅为历史配置，不能让关闭腾讯后系统没有 OCR。
    """
    return {
        "enabled": True,
        "mode": "fallback_or_primary",
        "engine": "RapidOCR / PP-OCRv5 mobile / ONNX CPU",
        # find_spec 不导入模型，不会让 ONNX Runtime 在 Web 服务常驻。
        "runtime_available": bool(importlib.util.find_spec("rapidocr") and importlib.util.find_spec("onnxruntime")),
    }


def _local_ocr_readiness(app) -> dict:
    """仅提示是否应人工发起第二阶段验收，绝不自动切换 OCR 优先级。

    该指标只用于配置页提示，允许 5 分钟 TTL 缓存。否则每次 OCR 额度预留都会
    触发一次全表聚合，评审页数多时成为无谓的 SQLite 负载。
    """
    now = time.time()
    with _LOCAL_OCR_READINESS_LOCK:
        cached = _LOCAL_OCR_READINESS_CACHE.get("value")
        if cached is not None and cached[0] > now:
            return cached[1]
    with connection(app) as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT c.document_id || ':' || c.page_number) AS page_count,
                      COUNT(DISTINCT d.project_id) AS project_count
               FROM ew_ocr_page_cache c JOIN ew_documents d ON d.document_id=c.document_id
               WHERE c.service='rapidocr_local'"""
        ).fetchone()
    pages = int(row["page_count"] or 0) if row else 0
    projects = int(row["project_count"] or 0) if row else 0
    value = {
        "sample_pages": pages, "sample_projects": projects,
        # 这只是“提醒人工验收”的最低客观门槛；准确率和内存指标仍须人工确认。
        "ready_for_manual_validation": pages >= 30 and projects >= 3,
    }
    with _LOCAL_OCR_READINESS_LOCK:
        _LOCAL_OCR_READINESS_CACHE["value"] = (time.time() + _LOCAL_OCR_READINESS_TTL_SECONDS, value)
    return value


def ocr_configuration(app) -> dict:
    """公开 OCR 路由配置；绝不返回 SecretId/SecretKey 明文或密文。"""
    raw = _ocr_configuration_raw(app)
    services = _ocr_service_settings(raw.get("services"))
    local = {**_local_ocr_settings(raw.get("local")), "readiness": _local_ocr_readiness(app)}
    month_key = _ocr_month_key()
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT service, COALESCE(SUM(billed_units), 0) AS used FROM ew_ocr_usage_ledger WHERE month_key=? GROUP BY service",
            (month_key,),
        ).fetchall()
    used_by_service = {row["service"]: int(row["used"] or 0) for row in rows}
    values = []
    for service, meta in TENCENT_OCR_SERVICES.items():
        setting = services[service]
        used = used_by_service.get(service, 0)
        values.append({
            "service": service, "label": meta["label"], "action": meta["action"],
            "usage": meta.get("usage", ""),
            "legacy": bool(meta.get("legacy")), "enabled": setting["enabled"],
            "monthly_limit": setting["monthly_limit"], "used": used,
            "remaining": max(0, setting["monthly_limit"] - used),
        })
    manual_id = bool(raw.get("secret_id_encrypted"))
    manual_key = bool(raw.get("secret_key_encrypted"))
    env_ready = bool(os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip() and os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip())
    tencent_enabled = bool(raw.get("enabled", False))
    return {
        # enabled 保留给既有前端/API 调用方；tencent_enabled 是当前更明确的语义字段。
        "enabled": tencent_enabled,
        "tencent_enabled": tencent_enabled,
        "ocr_enabled": ocr_feature_configuration(app)["enabled"],
        "region": str(raw.get("region") or "ap-guangzhou"),
        "credentials_configured": (manual_id and manual_key) or env_ready,
        "credentials_source": "manual" if manual_id and manual_key else "environment" if env_ready else "none",
        "month_key": month_key, "services": values, "local": local,
    }


def ocr_feature_configuration(app) -> dict:
    """本地 RapidOCR 是评审的固定基础能力。

    ``enabled`` 保留给已发布的 API 调用方；从本版本起它恒为 True，避免配置
    状态意外关闭全部图片文字取证。腾讯 OCR 与多模态仍是独立、可关闭的增强层。
    """
    return {"enabled": True, "fixed": True, "provider": "local_rapidocr"}


def update_ocr_feature_configuration(app, payload: dict) -> dict:
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("OCR 文字识别总开关必须为布尔值")
    # 兼容旧客户端的 PATCH 请求：接受 false，但本地基础能力不允许被关闭。
    # 同时把历史库中的 0 迁移为 1，避免旧任务指纹、管理脚本读到相反状态。
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
            (OCR_ENABLED_SETTING, "1", now_iso()),
        )
    return ocr_feature_configuration(app)


def update_ocr_configuration(app, payload: dict) -> dict:
    existing = _ocr_configuration_raw(app)
    enabled = payload.get("tencent_enabled", payload.get("enabled", existing.get("enabled", False)))
    if not isinstance(enabled, bool):
        raise ValueError("腾讯 OCR 总开关必须为布尔值")
    region = str(payload.get("region", existing.get("region", "ap-guangzhou")) or "").strip()
    if not re.fullmatch(r"[a-z0-9-]{2,40}", region):
        raise ValueError("腾讯云地域格式不正确")
    secret_id = str(payload.get("secret_id", "")).strip()
    secret_key = str(payload.get("secret_key", "")).strip()
    encrypted_id = existing.get("secret_id_encrypted")
    encrypted_key = existing.get("secret_key_encrypted")
    if secret_id:
        _validate_api_key_characters(secret_id)
        encrypted_id = _encrypt_model_api_key(app, secret_id)
    if secret_key:
        _validate_api_key_characters(secret_key)
        encrypted_key = _encrypt_model_api_key(app, secret_key)
    service_input = payload.get("services", existing.get("services", {}))
    if not isinstance(service_input, dict):
        raise ValueError("OCR 接口设置格式不正确")
    services = _ocr_service_settings(service_input)
    if enabled and not ((encrypted_id and encrypted_key) or (
        os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip() and os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    )):
        raise ValueError("开启腾讯 OCR 前，请配置 SecretId 和 SecretKey，或在运行环境设置 TENCENTCLOUD_SECRET_ID/KEY")
    # 本地 RapidOCR 是固定的直接/回退路径，不接受前端关闭；保留 local 配置对象只为
    # 已有数据库和 API 响应兼容，实际运行语义统一由 _local_ocr_settings 决定。
    value = {"enabled": enabled, "region": region, "secret_id_encrypted": encrypted_id,
             "secret_key_encrypted": encrypted_key, "services": services, "local": {"enabled": True}}
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
            (TENCENT_OCR_CONFIGURATION_SETTING, json.dumps(value, ensure_ascii=False), now_iso()),
        )
    return ocr_configuration(app)


def tencent_ocr_credentials(app, *, require_enabled: bool = True) -> tuple[str, str, str] | None:
    """读取腾讯云 OCR 凭据。

    业务调用保持默认 ``require_enabled=True``；配置页测试可显式忽略运行开关，
    这样用户先关闭腾讯云改用本地 OCR 后，仍能验证并维护已保存的云端配置。
    """
    raw = _ocr_configuration_raw(app)
    if require_enabled and not raw.get("enabled"):
        return None
    secret_id = _decrypt_model_api_key(app, raw["secret_id_encrypted"]) if raw.get("secret_id_encrypted") else os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
    secret_key = _decrypt_model_api_key(app, raw["secret_key_encrypted"]) if raw.get("secret_key_encrypted") else os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    if not secret_id or not secret_key:
        return None
    return secret_id, secret_key, str(raw.get("region") or "ap-guangzhou")


def reserve_ocr_request(app, task: dict, service: str) -> str | None:
    """每个真实外发请求先保守记为一次，避免失败/重试突破免费安全额度。"""
    if service not in TENCENT_OCR_SERVICES:
        return None
    config = ocr_configuration(app)
    item = next((value for value in config["services"] if value["service"] == service), None)
    if not config["enabled"] or not config["credentials_configured"] or not item or not item["enabled"] or item["remaining"] <= 0:
        return None
    usage_id = str(uuid.uuid4())
    with connection(app) as conn:
        # SQLite 的立即写锁让并行文档任务在额度临界点也不能同时越过上限。
        conn.execute("BEGIN IMMEDIATE")
        used = conn.execute("SELECT COALESCE(SUM(billed_units), 0) AS value FROM ew_ocr_usage_ledger WHERE month_key=? AND service=?", (config["month_key"], service)).fetchone()["value"]
        if int(used or 0) >= int(item["monthly_limit"]):
            return None
        conn.execute(
            "INSERT INTO ew_ocr_usage_ledger(usage_id, task_id, project_id, service, month_key, status, billed_units, created_at) VALUES (?, ?, ?, ?, ?, 'requested', 1, ?)",
            (usage_id, task.get("task_id"), task.get("project_id"), service, config["month_key"], now_iso()),
        )
    return usage_id


def complete_ocr_request(app, usage_id: str, *, status: str, request_id: str = "", detail: str = "") -> None:
    with connection(app) as conn:
        conn.execute("UPDATE ew_ocr_usage_ledger SET status=?, request_id=?, detail=? WHERE usage_id=?", (status[:40], request_id[:120], detail[:500], usage_id))


def get_ocr_page_cache(app, document_id: str, page_number: int, image_hash: str, service: str) -> dict | None:
    with connection(app) as conn:
        row = conn.execute("SELECT result_json FROM ew_ocr_page_cache WHERE document_id=? AND page_number=? AND image_hash=? AND service=?", (document_id, page_number, image_hash, service)).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def list_ocr_cached_page_texts(app, document_id: str) -> list[dict]:
    """列出一份文件在页级缓存中已识别的非空文字页，供本地事实兜底复用。

    只读已有缓存，绝不触发新的 OCR 调用；同页存在多个服务结果时按更新时间
    取最新一条，与“精确复核覆盖同页基础识别”的口径一致。
    """
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT page_number, result_json FROM ew_ocr_page_cache "
            "WHERE document_id=? ORDER BY page_number, updated_at",
            (document_id,),
        ).fetchall()
    by_page: dict[int, dict] = {}
    for row in rows:
        try:
            value = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        text = str(value.get("text") or "").strip()
        if not text or value.get("empty"):
            continue
        by_page[int(row["page_number"])] = {"page": int(row["page_number"]), "text": text,
                                            "service": str(value.get("service") or "")}
    return [by_page[page] for page in sorted(by_page)]


def save_ocr_page_cache(app, document_id: str, page_number: int, image_hash: str, service: str, value: dict) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_ocr_page_cache(cache_id, document_id, page_number, image_hash, service, result_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(document_id, page_number, image_hash, service) DO UPDATE SET result_json=excluded.result_json, updated_at=excluded.updated_at",
            (str(uuid.uuid4()), document_id, page_number, image_hash, service, json.dumps(value, ensure_ascii=False), timestamp, timestamp),
        )


def create_project(app, name: str, project_number: str = "", section_name: str = "") -> dict:
    project_id = str(uuid.uuid4())
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_projects(project_id, name, project_number, section_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, name.strip(), project_number.strip(), section_name.strip(), timestamp, timestamp),
        )
        _import_global_rules(conn, project_id, timestamp)
    project_dir(app, project_id)
    return get_project(app, project_id)


def _import_global_rules(conn: sqlite3.Connection, project_id: str, timestamp: str) -> None:
    """新项目复制全部通用规则；模板开关只决定项目内的默认勾选状态。"""
    templates = conn.execute(
        "SELECT * FROM ew_global_rules ORDER BY category, sort_order, created_at"
    ).fetchall()
    if not templates:
        return
    rule_set_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, created_at, updated_at) VALUES (?, ?, 1, 'draft', ?, ?)",
        (rule_set_id, project_id, timestamp, timestamp),
    )
    for position, template in enumerate(templates):
        conn.execute(
            """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
               source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'global', NULL, NULL, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), rule_set_id, template["category"], template["title"], template["check_rule"],
             template["source_text"], template["check_mode"], template["execution_meta_json"],
             int(bool(template["enabled"])), position, timestamp, timestamp),
        )


def _rule_signature(category: object, title: object, check_rule: object) -> tuple[str, str, str]:
    """用于规则集内的精确去重；不以近似标题覆盖人工已有规则。"""
    return (
        str(category or ""),
        re.sub(r"\s+", "", str(title or "")).casefold(),
        re.sub(r"\s+", "", str(check_rule or title or "")).casefold(),
    )


def _sync_new_global_rule_to_drafts(conn: sqlite3.Connection, template: dict, timestamp: str) -> int:
    """把新增模板补入所有当前待确认规则集，不改已确认或历史版本。

    没有规则集的早期项目也会创建一个仅包含此通用规则的待确认版本；这样用户不必
    先重新提取规则才能看见新基线。若项目内已有完全相同的 AI 或人工规则，则保留
    既有规则并跳过插入，避免同一检查点重复执行。
    """
    signature = _rule_signature(template.get("category"), template.get("title"), template.get("check_rule"))
    synced_count = 0
    projects = conn.execute("SELECT project_id FROM ew_projects").fetchall()
    for project in projects:
        project_id = project["project_id"]
        current = conn.execute(
            "SELECT * FROM ew_rule_sets WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,),
        ).fetchone()
        # 已确认项目只能经“重新提取规则”获得新的模板，避免新增全局规则悄悄改变
        # 已完成或正在复核的评审口径。
        if current and current["status"] != "draft":
            continue
        if current is None:
            rule_set_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, created_at, updated_at) VALUES (?, ?, 1, 'draft', ?, ?)",
                (rule_set_id, project_id, timestamp, timestamp),
            )
        else:
            rule_set_id = current["rule_set_id"]
        rows = conn.execute(
            "SELECT category, title, check_rule FROM ew_rules WHERE rule_set_id=?", (rule_set_id,),
        ).fetchall()
        if any(_rule_signature(row["category"], row["title"], row["check_rule"]) == signature for row in rows):
            continue
        sort_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM ew_rules WHERE rule_set_id=?", (rule_set_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
               source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'global', NULL, NULL, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), rule_set_id, template["category"], template["title"], template["check_rule"],
             template["source_text"], template["check_mode"], template["execution_meta_json"], int(bool(template["enabled"])), sort_order,
             timestamp, timestamp),
        )
        conn.execute("UPDATE ew_rule_sets SET updated_at=? WHERE rule_set_id=?", (timestamp, rule_set_id))
        synced_count += 1
    return synced_count


def _global_rule_payload(payload: dict, existing: dict | None = None) -> dict:
    base = existing or {}
    category = str(payload.get("category", base.get("category", "substantive"))).strip()
    if category not in GLOBAL_RULE_CATEGORIES:
        raise ValueError("通用规则仅支持资格性、符合性、实质性/废标项和其他规则")
    title = str(payload.get("title", base.get("title", ""))).strip()
    check_rule = str(payload.get("check_rule", base.get("check_rule", ""))).strip()
    if not title:
        raise ValueError("规则名称不能为空")
    if not check_rule:
        raise ValueError("检查规则不能为空")
    ocr_required = payload.get("ocr_required", base.get("check_mode") == "ocr")
    check_mode = "ocr" if ocr_required or payload.get("check_mode") == "ocr" else "auto"
    execution_payload = {**payload, "check_mode": check_mode}
    return {
        "category": category,
        "title": title,
        "check_rule": check_rule,
        "source_text": str(payload.get("source_text", base.get("source_text", ""))).strip(),
        "check_mode": check_mode,
        "execution_meta_json": _execution_meta_json(execution_payload, fallback=base),
        "enabled": 1 if bool(payload.get("enabled", base.get("enabled", True))) else 0,
        "sort_order": int(payload.get("sort_order", base.get("sort_order", 0)) or 0),
    }


def list_global_rules(app) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT * FROM ew_global_rules ORDER BY category, sort_order, created_at"
        ).fetchall()
    return [_rule_public_value(dict(row)) for row in rows]


def create_global_rule(app, payload: dict) -> dict:
    rule = _global_rule_payload(payload)
    rule.update({"global_rule_id": str(uuid.uuid4()), "created_at": now_iso(), "updated_at": now_iso()})
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_global_rules(global_rule_id, category, title, check_rule, source_text, check_mode, execution_meta_json, enabled, sort_order, created_at, updated_at)
               VALUES (:global_rule_id, :category, :title, :check_rule, :source_text, :check_mode, :execution_meta_json, :enabled, :sort_order, :created_at, :updated_at)""",
            rule,
        )
        # 新增立即同步到所有待确认项目；修改/删除只影响规则库自身，避免破坏
        # 用户正在人工核对的项目规则内容。
        rule["synced_draft_rule_sets"] = _sync_new_global_rule_to_drafts(conn, rule, rule["created_at"])
    return rule


def update_global_rule(app, global_rule_id: str, payload: dict) -> dict:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_global_rules WHERE global_rule_id = ?", (global_rule_id,)).fetchone()
        if not row:
            raise ValueError("通用规则不存在")
        rule = _global_rule_payload(payload, dict(row))
        rule.update({"global_rule_id": global_rule_id, "updated_at": now_iso()})
        conn.execute(
            """UPDATE ew_global_rules SET category=:category, title=:title, check_rule=:check_rule, source_text=:source_text,
               check_mode=:check_mode, execution_meta_json=:execution_meta_json, enabled=:enabled, sort_order=:sort_order, updated_at=:updated_at WHERE global_rule_id=:global_rule_id""",
            rule,
        )
    return rule


def delete_global_rule(app, global_rule_id: str) -> None:
    with connection(app) as conn:
        if not conn.execute("DELETE FROM ew_global_rules WHERE global_rule_id = ?", (global_rule_id,)).rowcount:
            raise ValueError("通用规则不存在")


def delete_project(app, project_id: str) -> None:
    root = data_dir(app).resolve()
    target = (root / project_id).resolve()
    if target.parent != root or target.name != project_id:
        raise ValueError("项目文件目录无效")
    with connection(app) as conn:
        active = conn.execute(
            "SELECT 1 FROM ew_tasks WHERE project_id = ? AND status IN ('queued', 'running') LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            raise ValueError("项目存在排队中或运行中的任务，暂不能删除")
        exists = conn.execute("SELECT 1 FROM ew_projects WHERE project_id = ?", (project_id,)).fetchone()
        if not exists:
            raise ValueError("评标项目不存在")
        conn.execute("DELETE FROM ew_projects WHERE project_id = ?", (project_id,))
    if target.exists():
        shutil.rmtree(target)


def get_project(app, project_id: str) -> dict | None:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_projects WHERE project_id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def set_project_price_profile(app, project_id: str, profile_id: str | None) -> None:
    """记住用户最后明确选择的价格计算模型，不保存密钥也不影响其他任务模型。"""
    with connection(app) as conn:
        conn.execute(
            "UPDATE ew_projects SET price_profile_id=?, updated_at=? WHERE project_id=?",
            (profile_id or None, now_iso(), project_id),
        )


def list_projects(app) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT p.*,
               (SELECT COUNT(*) FROM ew_documents d WHERE d.project_id = p.project_id) AS document_count,
               (SELECT COUNT(*) FROM ew_documents d WHERE d.project_id = p.project_id AND d.role = 'bid') AS bid_count,
               (SELECT MAX(t.updated_at) FROM ew_tasks t WHERE t.project_id = p.project_id) AS task_updated_at
               FROM ew_projects p ORDER BY p.updated_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_documents(app, project_id: str) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT * FROM ew_documents WHERE project_id = ? ORDER BY role, created_at", (project_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def update_document_quote(app, document_id: str, fields: dict) -> dict:
    """保存文件清单报价提取结果；与价格工作表报价缓存共用同一份数据。"""
    allowed = {"quote_value", "quote_source", "quote_excerpt", "quote_candidates_json", "quote_status", "quote_fingerprint"}
    updates = {key: fields[key] for key in allowed if key in fields}
    if not updates:
        return {}
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connection(app) as conn:
        conn.execute(
            f"UPDATE ew_documents SET {assignments} WHERE document_id=?",
            [*updates.values(), document_id],
        )
        row = conn.execute("SELECT * FROM ew_documents WHERE document_id=?", (document_id,)).fetchone()
    return dict(row) if row else {}


def _list_price_entries(conn, project_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT entry.*, document.sha256 AS document_sha256,
                  document.parsed_path, document.parse_status, document.original_name,
                  document.quote_candidates_json AS document_quote_candidates_json
           FROM ew_price_entries entry
           LEFT JOIN ew_documents document ON document.document_id=entry.document_id
           WHERE entry.project_id=?
           ORDER BY CASE entry.source_type WHEN 'document' THEN 0 ELSE 1 END,
                    entry.created_at, entry.bidder_name""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_price_entries(app, project_id: str) -> list[dict]:
    """纯读取价格台账；供 GET 使用，绝不创建或更新时间戳。"""
    with connection(app) as conn:
        return _list_price_entries(conn, project_id)


def save_price_rule_set(app, project_id: str, task_id: str | None, profile_id: str | None, rules: list[dict]) -> dict:
    """保存独立价格规则提取结果，不改写项目的完整评审规则集。"""
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_price_rule_sets(price_rule_set_id, project_id, task_id, profile_id, rules_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), project_id, task_id or None, profile_id or None,
             json.dumps(rules, ensure_ascii=False, separators=(",", ":")), timestamp, timestamp),
        )
    return current_price_rule_set(app, project_id) or {}


def current_price_rule_set(app, project_id: str) -> dict | None:
    with connection(app) as conn:
        row = conn.execute(
            "SELECT * FROM ew_price_rule_sets WHERE project_id=? ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    if not row:
        return None
    value = dict(row)
    try:
        rules = json.loads(value.get("rules_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        rules = []
    value["rules"] = [item for item in rules if isinstance(item, dict)]
    return value


def save_price_score_run(app, project_id: str, task_id: str | None, profile_id: str | None,
                         input_fingerprint: str, result: dict) -> dict:
    """保存一次 AI 价格分计算；旧结果保留，以便输入变化后自然失效而非覆盖历史。"""
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_price_score_runs(
                   price_score_run_id, project_id, task_id, profile_id, input_fingerprint,
                   result_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), project_id, task_id or None, profile_id or None, input_fingerprint,
             json.dumps(result, ensure_ascii=False, separators=(",", ":")), timestamp, timestamp),
        )
    return current_price_score_run(app, project_id, input_fingerprint) or {}


def current_price_score_run(app, project_id: str, input_fingerprint: str | None = None) -> dict | None:
    """返回指定输入版本的最新 AI 计算，避免旧报价或旧规则的分数混入当前页面。"""
    query = "SELECT * FROM ew_price_score_runs WHERE project_id=?"
    values: list[object] = [project_id]
    if input_fingerprint:
        query += " AND input_fingerprint=?"
        values.append(input_fingerprint)
    query += " ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT 1"
    with connection(app) as conn:
        row = conn.execute(query, values).fetchone()
    if not row:
        return None
    value = dict(row)
    try:
        result = json.loads(value.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        result = {}
    value["result"] = result if isinstance(result, dict) else {}
    return value


def sync_price_document_entries(app, project_id: str) -> list[dict]:
    """按需建立投标文件价格台账，仅供明确的刷新写操作调用。"""
    documents = [item for item in list_documents(app, project_id) if item.get("role") == "bid"]
    timestamp = now_iso()
    with connection(app) as conn:
        for document in documents:
            bidder_name = document.get("bidder_name") or document.get("original_name") or "未命名投标人"
            conn.execute(
                """INSERT INTO ew_price_entries(
                       price_entry_id, project_id, document_id, bidder_name, source_type,
                       extraction_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'document', 'pending', ?, ?)
                   ON CONFLICT(project_id, document_id) WHERE document_id IS NOT NULL DO UPDATE SET
                       bidder_name=excluded.bidder_name, updated_at=excluded.updated_at
                   WHERE ew_price_entries.bidder_name IS NOT excluded.bidder_name""",
                (str(uuid.uuid4()), project_id, document["document_id"], bidder_name, timestamp, timestamp),
            )
        return _list_price_entries(conn, project_id)


def get_price_entry(app, project_id: str, price_entry_id: str) -> dict | None:
    with connection(app) as conn:
        row = conn.execute(
            "SELECT * FROM ew_price_entries WHERE project_id=? AND price_entry_id=?",
            (project_id, price_entry_id),
        ).fetchone()
    return dict(row) if row else None


def create_manual_price_entry(app, project_id: str, bidder_name: str) -> dict:
    bidder_name = str(bidder_name or "").strip()
    if not bidder_name:
        raise ValueError("请填写未上传文件投标人的名称")
    timestamp = now_iso()
    value = {
        "price_entry_id": str(uuid.uuid4()), "project_id": project_id,
        "bidder_name": bidder_name[:200], "created_at": timestamp, "updated_at": timestamp,
    }
    with connection(app) as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM ew_price_entries WHERE project_id=? AND lower(trim(bidder_name))=lower(?) LIMIT 1",
            (project_id, bidder_name),
        ).fetchone()
        if duplicate:
            raise ValueError("价格工作表中已存在同名投标人")
        conn.execute(
            """INSERT INTO ew_price_entries(
                   price_entry_id, project_id, document_id, bidder_name, source_type,
                   extraction_status, created_at, updated_at)
               VALUES (:price_entry_id, :project_id, NULL, :bidder_name, 'manual',
                       'unavailable', :created_at, :updated_at)""",
            value,
        )
    return get_price_entry(app, project_id, value["price_entry_id"])


def update_price_entry(app, project_id: str, price_entry_id: str, fields: dict) -> dict:
    entry = get_price_entry(app, project_id, price_entry_id)
    if not entry:
        raise ValueError("价格工作表中的投标人不存在")
    allowed = {
        "manual_quote", "evaluation_price", "included", "exclusion_reason",
        "manual_scores_json", "extracted_quote", "quote_source", "quote_excerpt",
        "extraction_status", "extraction_fingerprint",
    }
    updates = {key: fields[key] for key in allowed if key in fields}
    if "bidder_name" in fields:
        if entry.get("source_type") != "manual":
            raise ValueError("已上传投标人的名称请在文件信息中维护")
        bidder_name = str(fields.get("bidder_name") or "").strip()
        if not bidder_name:
            raise ValueError("投标人名称不能为空")
        updates["bidder_name"] = bidder_name[:200]
    if not updates:
        return entry
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connection(app) as conn:
        conn.execute(
            f"UPDATE ew_price_entries SET {assignments} WHERE project_id=? AND price_entry_id=?",
            [*updates.values(), project_id, price_entry_id],
        )
    return get_price_entry(app, project_id, price_entry_id)


def delete_manual_price_entry(app, project_id: str, price_entry_id: str) -> None:
    entry = get_price_entry(app, project_id, price_entry_id)
    if not entry:
        raise ValueError("价格工作表中的投标人不存在")
    if entry.get("source_type") != "manual":
        raise ValueError("已上传投标人只能移出价格计算，不能在价格工作表中删除")
    with connection(app) as conn:
        conn.execute(
            "DELETE FROM ew_price_entries WHERE project_id=? AND price_entry_id=?",
            (project_id, price_entry_id),
        )


def apply_price_entry_batch(
    app, project_id: str, *, updates: list[dict], new_entries: list[dict], delete_manual_entry_ids: list[str],
) -> None:
    """一次事务保存价格工作表；不计算评审得分，也不触发任务。"""
    timestamp = now_iso()
    with connection(app, immediate=True) as conn:
        existing = {
            row["price_entry_id"]: dict(row)
            for row in conn.execute("SELECT * FROM ew_price_entries WHERE project_id=?", (project_id,)).fetchall()
        }
        deleted = {str(value) for value in delete_manual_entry_ids}
        if len(deleted) != len(delete_manual_entry_ids):
            raise ValueError("删除列表中存在重复投标人")
        for entry_id in deleted:
            entry = existing.get(entry_id)
            if not entry:
                raise ValueError("价格工作表中的投标人不存在")
            if entry.get("source_type") != "manual":
                raise ValueError("已上传投标人只能移出价格计算，不能删除")
        update_ids = [str(item.get("price_entry_id") or "") for item in updates]
        if len(set(update_ids)) != len(update_ids) or any(not value for value in update_ids):
            raise ValueError("每条价格修改必须对应唯一投标人")
        for entry_id in update_ids:
            if entry_id not in existing or entry_id in deleted:
                raise ValueError("价格工作表中的投标人不存在")

        # 删除手工行后按最终名称统一校验，允许同批“删除旧行、补录同名行”或
        # 两条手工行互换名称，且不允许与已上传投标人重名。
        update_map = {str(item["price_entry_id"]): item for item in updates}
        final_names = []
        for entry_id, item in existing.items():
            if entry_id in deleted:
                continue
            update = update_map.get(entry_id) or {}
            raw_name = update.get("bidder_name") if item.get("source_type") == "manual" and "bidder_name" in update else item.get("bidder_name")
            name = str(raw_name or "").strip()
            if not name:
                raise ValueError("未上传投标人名称不能为空")
            final_names.append(name.casefold())
        for item in new_entries:
            name = str(item.get("bidder_name") or "").strip()
            if not name:
                raise ValueError("请填写未上传文件投标人的名称")
            final_names.append(name.casefold())
        if len(set(final_names)) != len(final_names):
            raise ValueError("价格工作表中已存在同名投标人")

        for entry_id in deleted:
            conn.execute("DELETE FROM ew_price_entries WHERE project_id=? AND price_entry_id=?", (project_id, entry_id))
        for entry_id, item in update_map.items():
            entry = existing[entry_id]
            assignments = {
                "manual_quote": item.get("manual_quote"),
                "evaluation_price": item.get("evaluation_price"),
                "included": 1 if item.get("included") else 0,
                "exclusion_reason": str(item.get("exclusion_reason") or "")[:300],
                "manual_scores_json": str(item.get("manual_scores_json") or "{}"),
                "adjustment_json": str(item.get("adjustment_json") or "{}"),
                "updated_at": timestamp,
            }
            if entry.get("source_type") == "manual":
                assignments["bidder_name"] = str(item.get("bidder_name") or "").strip()[:200]
            sql = ", ".join(f"{key}=?" for key in assignments)
            conn.execute(
                f"UPDATE ew_price_entries SET {sql} WHERE project_id=? AND price_entry_id=?",
                [*assignments.values(), project_id, entry_id],
            )
        for item in new_entries:
            conn.execute(
                """INSERT INTO ew_price_entries(
                       price_entry_id, project_id, document_id, bidder_name, source_type,
                       manual_quote, evaluation_price, included, exclusion_reason,
                       extraction_status, manual_scores_json, adjustment_json, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, 'manual', ?, ?, ?, ?, 'unavailable', ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), project_id, str(item.get("bidder_name") or "").strip()[:200],
                    item.get("manual_quote"), item.get("evaluation_price"), 1 if item.get("included") else 0,
                    str(item.get("exclusion_reason") or "")[:300], str(item.get("manual_scores_json") or "{}"),
                    str(item.get("adjustment_json") or "{}"), timestamp, timestamp,
                ),
            )


def document_path(app, document: dict) -> Path:
    return project_dir(app, document["project_id"]) / "source" / document["stored_name"]


def store_upload(app, project_id: str, role: str, bidder_name: str, upload) -> dict:
    bidder_name = str(bidder_name or "").strip()
    original_name = Path(upload.filename or "").name
    extension = Path(original_name).suffix.lower()
    if extension not in {".pdf", ".docx"}:
        raise ValueError("工作台目前仅支持 PDF 和 DOCX 文件")
    if role not in {"tender", "tender_attachment", "bid"}:
        raise ValueError("不支持的文件角色")
    if role == "bid" and not bidder_name:
        raise ValueError("上传投标文件时必须填写投标人名称")
    existing_documents = list_documents(app, project_id)
    if role == "bid" and sum(item["role"] == "bid" for item in existing_documents) >= MAX_BID_DOCUMENTS:
        raise ValueError(f"每个项目最多上传 {MAX_BID_DOCUMENTS} 份投标文件")
    if role == "tender" and any(item["role"] == "tender" for item in existing_documents):
        raise ValueError("每个项目只能保留一份主招标文件，请先移除或替换原文件")

    document_id = str(uuid.uuid4())
    stored_name = f"{document_id}{extension}"
    target = project_dir(app, project_id) / "source" / stored_name
    digest_builder = hashlib.sha256()
    size_bytes = 0
    try:
        with target.open("xb") as output:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOAD_BYTES:
                    raise ValueError(f"单个文件不能超过 {MAX_UPLOAD_MB} MB")
                digest_builder.update(chunk)
                output.write(chunk)
        if not size_bytes:
            raise ValueError("上传文件为空")
        digest = digest_builder.hexdigest()
        if any(item["sha256"] == digest for item in existing_documents):
            raise ValueError("该项目中已存在内容相同的文件")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    timestamp = now_iso()
    document = {
        "document_id": document_id,
        "project_id": project_id,
        "role": role,
        "bidder_name": bidder_name,
        "original_name": original_name,
        "stored_name": stored_name,
        "extension": extension,
        "size_bytes": size_bytes,
        "sha256": digest,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        with connection(app) as conn:
            conn.execute(
                """INSERT INTO ew_documents(document_id, project_id, role, bidder_name, original_name, stored_name,
                extension, size_bytes, sha256, created_at, updated_at)
                VALUES (:document_id, :project_id, :role, :bidder_name, :original_name, :stored_name,
                :extension, :size_bytes, :sha256, :created_at, :updated_at)""",
                document,
            )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return document


def delete_document(app, project_id: str, document_id: str) -> None:
    with connection(app) as conn:
        active = conn.execute("SELECT 1 FROM ew_tasks WHERE project_id = ? AND status IN ('queued', 'running') LIMIT 1", (project_id,)).fetchone()
        if active:
            raise ValueError("项目存在排队中或运行中的任务，暂不能删除文件")
        row = conn.execute("SELECT * FROM ew_documents WHERE project_id = ? AND document_id = ?", (project_id, document_id)).fetchone()
        if not row:
            raise ValueError("文件不存在")
        conn.execute("DELETE FROM ew_documents WHERE document_id = ?", (document_id,))
    document = dict(row)
    document_path(app, document).unlink(missing_ok=True)
    if document.get("parsed_path"):
        Path(document["parsed_path"]).unlink(missing_ok=True)


def create_task(app, project_id: str, task_type: str, payload: dict | None = None) -> dict:
    init_database(app)
    task_id = str(uuid.uuid4())
    timestamp = now_iso()
    # 入队必须用 BEGIN IMMEDIATE 写锁覆盖“检查数量 + 插入任务”：并发到达的请求
    # 不会同时读到未满额度而突破每项目/全局上限。该锁只覆盖很短的入队事务，
    # 不阻塞长任务执行（长任务运行时不持有任何入队锁）。
    with connection(app, immediate=True) as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM ew_tasks WHERE project_id = ? AND task_type = ? AND status IN ('queued', 'running') LIMIT 1",
            (project_id, task_type),
        ).fetchone()
        if duplicate:
            raise ValueError("相同任务已经在排队或运行中，请勿重复提交")
        project_queued = conn.execute(
            "SELECT COUNT(*) FROM ew_tasks WHERE project_id = ? AND status = 'queued'", (project_id,)
        ).fetchone()[0]
        if project_queued >= MAX_QUEUED_TASKS_PER_PROJECT:
            raise ValueError(f"该项目最多允许 {MAX_QUEUED_TASKS_PER_PROJECT} 个排队任务，请等待已有任务完成")
        global_queued = conn.execute("SELECT COUNT(*) FROM ew_tasks WHERE status = 'queued'").fetchone()[0]
        if global_queued >= MAX_QUEUED_TASKS_GLOBAL:
            raise ValueError(f"全局排队任务已达 {MAX_QUEUED_TASKS_GLOBAL} 个上限，请等待已有任务完成")
        conn.execute(
            """INSERT INTO ew_tasks(task_id, project_id, task_type, status, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
            (task_id, project_id, task_type, json.dumps(payload or {}, ensure_ascii=False), timestamp, timestamp),
        )
    return get_task(app, task_id)


def task_input_fingerprint(app, project_id: str, task_type: str, profile_id: str | None, prompt_version: str,
                           document_ids: list[str] | None = None) -> str:
    """仅由文件指纹、规则版本和公开模型配置构成；不包含正文或 API Key。"""
    documents = list_documents(app, project_id)
    rule_set = current_rule_set(app, project_id)
    profile = get_model_profile(app, profile_id, "deepseek-v4-flash")
    vision_profile = resolve_vision_model_profile(app, profile) if task_type == "evaluate_all" else None
    ocr_config = ocr_configuration(app) if task_type == "evaluate_all" else {}
    relevant_roles = {
        "compare_documents": {"tender", "bid"},
        "extract_rules": {"tender", "tender_attachment"},
        "extract_price_rules": {"tender", "tender_attachment"},
        "calculate_price_scores": {"bid"},
        "review_documents": {"bid"},
        "score_objective": {"bid"},
        "score_subjective": {"bid"},
        # 综合评审的项目范围画像和全文核验均依赖招标文件及其附件；遗漏这些
        # 输入会使“招标文件已变、投标文件未变”的任务错误复用历史结果。
        "evaluate_all": {"tender", "tender_attachment", "bid"},
    }.get(task_type, {"tender", "tender_attachment", "bid"})
    uses_rules = task_type in {"review_documents", "score_objective", "score_subjective", "evaluate_all"}
    selected_document_ids = {str(value) for value in document_ids or [] if str(value)}
    # 全选与历史“未传 document_ids”的评审输入完全等价，保持其严格复用能力；
    # 仅局部选择才进入指纹，避免把一次 UI 升级误判为所有全量结果均需重跑。
    if task_type == "evaluate_all":
        all_bid_document_ids = {str(item["document_id"]) for item in documents if item["role"] == "bid"}
        if selected_document_ids == all_bid_document_ids:
            selected_document_ids = set()
    value = {
        "task_type": task_type,
        # 代码逻辑变更同样会改变结果。此前只有提示词版本参与键，导致未改提示词的
        # 修复可能错误复用旧任务结果，掩盖真实差异。
        "runtime_release": runtime_release_fingerprint(),
        "runtime_code": runtime_code_fingerprint(),
        "prompt_version": prompt_version,
        "documents": sorted(
            (item["document_id"], item["sha256"], item.get("updated_at"), item.get("parse_status"))
            for item in documents
            if item["role"] in relevant_roles
            and not (task_type == "evaluate_all" and item["role"] == "bid" and selected_document_ids and item["document_id"] not in selected_document_ids)
        ),
        "selected_document_ids": sorted(selected_document_ids) if task_type == "evaluate_all" else None,
        "rule_set": (rule_set or {}).get("rule_set_id") if uses_rules else None,
        "rule_set_updated_at": (rule_set or {}).get("updated_at") if uses_rules else None,
        "profile": (profile.get("profile_id"), profile.get("model_name"), profile.get("base_url"), profile.get("updated_at"), profile.get("json_mode"), profile.get("thinking_mode")),
        # 查重结果必须同时绑定比较器、线索规则、AI 提示词与模型公开配置；不能只靠
        # 一个手工版本号判断结果是否仍对应当前链路。
        "comparison_pipeline": compare_pipeline_metadata(app, profile_id) if task_type == "compare_documents" else None,
        "ocr_feature_configuration": ocr_feature_configuration(app) if task_type == "evaluate_all" else None,
        # 仅记录会改变实际取证路径的公开配置，不记录凭据或随每次调用变化的额度余额。
        "ocr_execution_configuration": {
            "tencent_enabled": bool(ocr_config.get("tencent_enabled")),
            "region": str(ocr_config.get("region") or ""),
            "services": sorted((item.get("service"), bool(item.get("enabled")), int(item.get("monthly_limit") or 0))
                               for item in ocr_config.get("services", []) if isinstance(item, dict)),
            "local_runtime_available": bool((ocr_config.get("local") or {}).get("runtime_available")),
        } if task_type == "evaluate_all" else None,
        "vision_configuration": vision_configuration(app) if task_type == "evaluate_all" else None,
        "vision_profile": (vision_profile.get("profile_id"), vision_profile.get("model_name"), vision_profile.get("base_url"), vision_profile.get("updated_at")) if vision_profile else None,
        "prompt_templates": task_prompt_template_fingerprint(app, task_type),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def find_reusable_task(app, project_id: str, task_type: str, input_fingerprint: str) -> dict | None:
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT * FROM ew_tasks WHERE project_id = ? AND task_type = ? AND status = 'success'
               ORDER BY finished_at DESC LIMIT 20""", (project_id, task_type)
        ).fetchall()
    for row in rows:
        task = task_to_dict(row)
        if task.get("payload", {}).get("input_fingerprint") == input_fingerprint:
            return task
    return None


def get_task(app, task_id: str) -> dict | None:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_tasks WHERE task_id = ?", (task_id,)).fetchone()
    return task_to_dict(row) if row else None


def list_tasks(app, project_id: str) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute("SELECT * FROM ew_tasks WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 50", (project_id,)).fetchall()
    return [task_to_dict(row) for row in rows]


def list_task_summaries(app, project_id: str) -> list[dict]:
    """供轮询使用；综合评审仅携带很小的已完成投标人清单。"""
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT task_id, project_id, task_type, status, progress, message, error, created_at, started_at, finished_at, updated_at,
                      CASE WHEN task_type = 'evaluate_all' OR status = 'running' THEN result_json ELSE NULL END AS result_json,
                      CASE WHEN task_type = 'evaluate_all' AND status = 'running' THEN payload_json ELSE NULL END AS payload_json
               FROM ew_tasks WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 50""",
            (project_id,),
        ).fetchall()
    values = []
    for row in rows:
        value = dict(row)
        raw_result = value.pop("result_json", None)
        if raw_result:
            try:
                result = json.loads(raw_result)
            except (TypeError, json.JSONDecodeError):
                result = {}
            completed = result.get("completed_documents") if isinstance(result, dict) else None
            if isinstance(completed, list):
                value["completed_documents"] = [
                    item for item in completed
                    if isinstance(item, dict) and item.get("document_id")
                ]
        raw_payload = value.pop("payload_json", None)
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            value["cancel_requested"] = bool(payload.get("cancel_requested")) if isinstance(payload, dict) else False
        values.append(value)
    return values


def task_queue_contexts(app, project_id: str) -> dict[str, dict]:
    """返回当前项目排队任务的全局队列说明。

    工作台只有一个按需 worker，会从全局 FIFO 队列逐个取任务。这里刻意只暴露
    正在运行任务的项目名称、阶段和进度，不携带文件名、评审结论或模型输出；前端
    可据此说明“为什么还在排队”，而不改变既有 tasks 字段的兼容语义。
    """
    with connection(app) as conn:
        running = conn.execute(
            """SELECT t.task_id, t.project_id, t.task_type, t.progress, t.message, t.started_at,
                      p.name AS project_name, p.section_name
               FROM ew_tasks t JOIN ew_projects p ON p.project_id = t.project_id
               WHERE t.status = 'running'
               ORDER BY t.started_at, t.created_at LIMIT 1"""
        ).fetchone()
        queued = conn.execute(
            """SELECT task_id, project_id FROM ew_tasks
               WHERE status = 'queued' ORDER BY created_at, task_id"""
        ).fetchall()
    active = dict(running) if running else None
    contexts: dict[str, dict] = {}
    running_count = 1 if active else 0
    for queue_index, row in enumerate(queued):
        if row["project_id"] != project_id:
            continue
        contexts[row["task_id"]] = {
            # waiting_count 把正在运行的任务也计入“前方”，更符合用户直觉。
            "waiting_count": running_count + queue_index,
            "queue_position": running_count + queue_index + 1,
            "active_task": active,
        }
    return contexts


def has_queued_tasks(app) -> bool:
    with connection(app) as conn:
        return conn.execute("SELECT 1 FROM ew_tasks WHERE status = 'queued' LIMIT 1").fetchone() is not None


def has_running_tasks(app) -> bool:
    with connection(app) as conn:
        return conn.execute("SELECT 1 FROM ew_tasks WHERE status = 'running' LIMIT 1").fetchone() is not None


def interrupt_stale_running_tasks(app) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            """UPDATE ew_tasks SET status='interrupted', message='上次工作进程意外中断',
               error='工作进程退出前未完成任务', finished_at=?, updated_at=? WHERE status='running'""",
            (timestamp, timestamp),
        )


def task_to_dict(row) -> dict:
    value = dict(row)
    for field in ("payload_json", "result_json"):
        if value.get(field):
            value[field[:-5]] = json.loads(value[field])
        value.pop(field, None)
    return value


def next_queued_task(app) -> dict | None:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_tasks WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        timestamp = now_iso()
        updated = conn.execute(
            "UPDATE ew_tasks SET status = 'running', started_at = ?, updated_at = ? WHERE task_id = ? AND status = 'queued'",
            (timestamp, timestamp, row["task_id"]),
        ).rowcount
        if not updated:
            return None
    return get_task(app, row["task_id"])


def update_task(app, task_id: str, *, progress: int | None = None, message: str | None = None,
                status: str | None = None, result: dict | None = None, error: str | None = None) -> None:
    fields, values = [], []
    if progress is not None:
        fields.append("progress = ?")
        values.append(max(0, min(100, int(progress))))
    if message is not None:
        fields.append("message = ?")
        values.append(message)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
        if status in {"success", "error", "cancelled", "interrupted"}:
            fields.append("finished_at = ?")
            values.append(now_iso())
    if result is not None:
        fields.append("result_json = ?")
        values.append(json.dumps(result, ensure_ascii=False))
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    fields.append("updated_at = ?")
    values.append(now_iso())
    values.append(task_id)
    with connection(app) as conn:
        conn.execute(f"UPDATE ew_tasks SET {', '.join(fields)} WHERE task_id = ?", values)


def request_task_cancellation(app, project_id: str, task_id: str) -> dict:
    """对综合评审发出协作式终止请求；不强杀进程或正在进行的外部请求。"""
    timestamp = now_iso()
    with connection(app, immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM ew_tasks WHERE task_id=? AND project_id=?",
            (task_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError("任务不存在或不属于当前项目")
        if row["task_type"] != "evaluate_all":
            raise ValueError("当前仅支持安全终止综合评审任务")
        if row["status"] == "queued":
            conn.execute(
                """UPDATE ew_tasks SET status='cancelled', message='已取消排队', error=NULL,
                          finished_at=?, updated_at=? WHERE task_id=? AND status='queued'""",
                (timestamp, timestamp, task_id),
            )
        elif row["status"] == "running":
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["cancel_requested"] = True
            payload["cancel_requested_at"] = timestamp
            conn.execute(
                """UPDATE ew_tasks SET payload_json=?, message='已收到终止请求，正在等待当前调用结束后安全停止…',
                          updated_at=? WHERE task_id=? AND status='running'""",
                (json.dumps(payload, ensure_ascii=False), timestamp, task_id),
            )
        elif row["status"] not in {"cancelled", "interrupted"}:
            raise ValueError("任务已经结束，无需终止")
    return get_task(app, task_id)


def task_cancellation_requested(app, task_id: str) -> bool:
    """读取综合评审的协作式终止标记；高频调用方只在自然检查点使用。"""
    with connection(app) as conn:
        row = conn.execute(
            "SELECT status, payload_json FROM ew_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    if not row or row["status"] in {"cancelled", "interrupted"}:
        return True
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return bool(payload.get("cancel_requested")) if isinstance(payload, dict) else False


def finalize_task(app, task_id: str, *, status: str, progress: int | None = None,
                  message: str = "", result: dict | None = None, error: str | None = None) -> dict | None:
    """原子完成任务；若终止请求与正常完成竞态，终止优先且保留已发布清单。"""
    if status not in {"success", "error", "cancelled", "interrupted"}:
        raise ValueError("任务最终状态不正确")
    timestamp = now_iso()
    with connection(app, immediate=True) as conn:
        row = conn.execute("SELECT * FROM ew_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        if row["status"] in {"success", "error", "cancelled", "interrupted"}:
            return task_to_dict(row)
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        cancellation_requested = isinstance(payload, dict) and bool(payload.get("cancel_requested"))
        final_status = "cancelled" if cancellation_requested or status == "cancelled" else status
        if final_status == "cancelled":
            try:
                final_result = json.loads(row["result_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                final_result = {}
            if not isinstance(final_result, dict):
                final_result = {}
            final_result["completion_state"] = "cancelled"
            final_result["cancelled_at"] = timestamp
            final_message = "任务已安全终止；已完整完成的投标人结果已保留"
            final_error = None
            final_progress = int(row["progress"] or 0)
        else:
            final_result = result
            final_message = message
            final_error = error
            final_progress = max(0, min(100, int(progress if progress is not None else row["progress"] or 0)))
        conn.execute(
            """UPDATE ew_tasks SET status=?, progress=?, message=?, result_json=?, error=?,
                      finished_at=?, updated_at=? WHERE task_id=? AND status='running'""",
            (
                final_status, final_progress, final_message,
                json.dumps(final_result, ensure_ascii=False) if final_result is not None else row["result_json"],
                final_error, timestamp, timestamp, task_id,
            ),
        )
    return get_task(app, task_id)


def _safe_positive_int(value: object) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_model_call(app, task_id: str, project_id: str, phase: str, profile_id: str | None,
                      *, document_id: str | None = None, input_chars: int = 0,
                      context_mode: str = "full", usage: dict | None = None,
                      response_metadata: dict | None = None) -> None:
    """保存供应商返回的用量；不保存提示词、正文或密钥。"""
    usage = usage or {}
    response_metadata = response_metadata or {}

    def number(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return None

    def nested_number(*paths: tuple[str, ...]) -> int | None:
        """兼容服务商把缓存命中量放在 usage 的嵌套统计字段中。"""
        for path in paths:
            value: object = usage
            for key in path:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return None

    prompt_tokens = number("prompt_tokens", "input_tokens")
    completion_tokens = number("completion_tokens", "output_tokens")
    total_tokens = number("total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_model_calls(call_id, task_id, project_id, document_id, phase, profile_id,
               context_mode, input_chars, prompt_tokens, completion_tokens, total_tokens, cache_hit_tokens,
               requested_max_tokens, finish_reason, response_chars, parse_status, parse_error_kind,
               local_json_repaired, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id, project_id, document_id, phase, profile_id, context_mode,
             max(0, int(input_chars)), prompt_tokens, completion_tokens, total_tokens,
             number("prompt_cache_hit_tokens", "cache_hit_tokens", "cached_tokens") or nested_number(
                 ("prompt_tokens_details", "cached_tokens"),
                 ("usage", "prompt_tokens_details", "cached_tokens"),
                 ("cache_read_input_tokens",),
             ),
             _safe_positive_int(response_metadata.get("requested_max_tokens")),
             str(response_metadata.get("finish_reason") or "")[:64] or None,
             _safe_positive_int(response_metadata.get("response_chars")),
             str(response_metadata.get("parse_status") or "")[:32] or None,
             str(response_metadata.get("parse_error_kind") or "")[:96] or None,
             1 if response_metadata.get("local_json_repaired") else 0, now_iso()),
        )


def get_evaluation_scan_checkpoint(app, document_id: str, scan_key: str, chunk_id: str, chunk_hash: str) -> object | None:
    """读取可复用的全文扫描页块；只保存候选证据，不保存模型原始输出。"""
    with connection(app) as conn:
        row = conn.execute(
            """SELECT findings_json FROM ew_evaluation_scan_cache
               WHERE document_id=? AND scan_key=? AND chunk_id=? AND chunk_hash=?""",
            (document_id, scan_key, chunk_id, chunk_hash),
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["findings_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, (list, dict)) else None


def save_evaluation_scan_checkpoint(app, project_id: str, document_id: str, scan_key: str,
                                    chunk_id: str, chunk_hash: str, findings: object) -> None:
    """每个成功页块立即落库，工作进程中断后可继续使用。"""
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_evaluation_scan_cache
               (cache_id, project_id, document_id, scan_key, chunk_id, chunk_hash, findings_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(document_id, scan_key, chunk_id, chunk_hash) DO UPDATE SET
               findings_json=excluded.findings_json, updated_at=excluded.updated_at""",
            (str(uuid.uuid4()), project_id, document_id, scan_key, chunk_id, chunk_hash,
              json.dumps(findings, ensure_ascii=False), timestamp, timestamp),
        )


def get_document_evidence_manifest(app, document_id: str, document_sha256: str, parser_version: str) -> list[dict] | None:
    """读取轻量页块清单；不存正文，正文始终从已有解析文件按需读取。"""
    with connection(app) as conn:
        row = conn.execute(
            """SELECT manifest_json FROM ew_document_evidence_manifests
               WHERE document_id=? AND document_sha256=? AND parser_version=?""",
            (document_id, document_sha256, parser_version),
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["manifest_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def save_document_evidence_manifest(app, document_id: str, document_sha256: str, parser_version: str,
                                    manifest: list[dict]) -> None:
    """保存页块边界和摘要哈希，为后续规则关联与缓存校验提供稳定基础。"""
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_document_evidence_manifests
               (manifest_id, document_id, document_sha256, parser_version, manifest_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(document_id, document_sha256, parser_version) DO UPDATE SET
               manifest_json=excluded.manifest_json, updated_at=excluded.updated_at""",
            (str(uuid.uuid4()), document_id, document_sha256, parser_version,
             json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), timestamp, timestamp),
        )


def record_output_risk_observation(app, task_id: str, project_id: str, phase: str, *,
                                   document_id: str | None = None, context_mode: str = "",
                                   input_chars: int = 0, rule_count: int = 0,
                                   requested_max_tokens: int | None = None,
                                   predicted_risk_score: int = 0,
                                   shadow_split_recommended: bool = False,
                                   actual_format_error: bool = False,
                                   actual_finish_reason: str = "", actual_error_kind: str = "",
                                   recovery_action: str = "none") -> None:
    """记录全文扫描输出风险的影子预测；不保存正文，也不改变真实拆分行为。"""
    score = max(0, min(100, int(predicted_risk_score or 0)))
    level = "high" if score >= 70 else "medium" if score >= 45 else "low"
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_output_risk_observations
               (observation_id, task_id, project_id, document_id, phase, context_mode,
                input_chars, rule_count, requested_max_tokens, predicted_risk_score,
                predicted_risk_level, shadow_split_recommended, actual_format_error,
                actual_finish_reason, actual_error_kind, recovery_action, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id, project_id, document_id, phase, str(context_mode or "")[:160],
             max(0, int(input_chars or 0)), max(0, int(rule_count or 0)),
             _safe_positive_int(requested_max_tokens), score, level,
             1 if shadow_split_recommended else 0, 1 if actual_format_error else 0,
             str(actual_finish_reason or "")[:64], str(actual_error_kind or "")[:96],
             str(recovery_action or "none")[:64], now_iso()),
        )


def record_local_ocr_run(app, *, task_id: str | None, project_id: str | None,
                         document_id: str | None, requested_pages: int,
                         recognized_pages: int, empty_pages: int, failed_pages: int,
                         elapsed_ms: int, peak_rss_kb: int | None,
                         status: str, error_kind: str = "") -> None:
    """保存本地 OCR 的轻量性能与状态指标；不保存图片或识别文字。"""
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_local_ocr_runs
               (run_id, task_id, project_id, document_id, requested_pages, recognized_pages,
                empty_pages, failed_pages, elapsed_ms, peak_rss_kb, status, error_kind, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id or None, project_id or None, document_id or None,
             max(0, int(requested_pages or 0)), max(0, int(recognized_pages or 0)),
             max(0, int(empty_pages or 0)), max(0, int(failed_pages or 0)),
             max(0, int(elapsed_ms or 0)), _safe_positive_int(peak_rss_kb),
             str(status or "unknown")[:40], str(error_kind or "")[:80], now_iso()),
        )


_SCOPE_PAGE_RANGE_PATTERN = re.compile(r"第?\s*(\d+)\s*(?:[-—–~至]\s*(\d+))?\s*页?")


def _scope_candidate_page_overlap(candidate: dict, page_start: int, page_end: int) -> bool:
    """候选页码（page_range 或 page_hint）与目标页区间是否重叠。

    page_range 为系统生成的"第N-M页"标签；page_hint 为模型填写的单页，二者均可缺失，
    无法定位页码时按不重叠处理（由证据包含度校验另行兜底）。
    """
    match = _SCOPE_PAGE_RANGE_PATTERN.search(str(candidate.get("page_range") or ""))
    if match:
        start = int(match.group(1))
        end = int(match.group(2) or start)
        return not (end < page_start or start > page_end)
    hint = re.search(r"\d+", str(candidate.get("page_hint") or ""))
    if hint:
        page = int(hint.group(0))
        return page_start <= page <= page_end
    return False


def previous_scope_anomalies(app, document_id: str, page_start: int, page_end: int,
                             *, limit: int = 32) -> list[dict]:
    """按页码区间读取历史扫描发现的范围候选，供新一轮重新判断。

    候选以"页码 + 原文证据"为稳定锚点，不再绑定 chunk_id/chunk_hash：分块策略调整
    （如 11K→14K）不会让已发现的高价值原文线索静默消失。这里只复用可定位的候选线索，
    不复用最终结论；当前项目范围与指南仍由最终评审重新判断。
    """
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT findings_json FROM ew_evaluation_scan_cache
               WHERE document_id=?
               ORDER BY updated_at DESC LIMIT 400""",
            (document_id,),
        ).fetchall()
    values: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["findings_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload.get("scope_anomalies") if isinstance(payload, dict) else None
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("candidate_priority") not in {"high", "medium"}:
                continue
            if not _scope_candidate_page_overlap(candidate, page_start, page_end):
                continue
            signature = re.sub(r"\s+", "", str(candidate.get("evidence") or ""))[:180]
            if not signature or signature in seen:
                continue
            seen.add(signature)
            values.append(dict(candidate))
            if len(values) >= max(1, limit):
                return values
    return values


def save_evidence_packs(app, project_id: str, task_id: str, document_id: str, document_sha256: str,
                        packs: list[dict]) -> None:
    """保存可追溯 EvidencePack。

    EvidencePack 不保存 OCR 原文，也不保存可直接复用的最终结论；material_key 只用于
    后续同一文件、同一材料事实的候选页优先级，避免旧结论污染新一轮评审。
    """
    timestamp = now_iso()
    with connection(app) as conn:
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            rule_id = str(pack.get("rule_id") or "")
            component = str(pack.get("component") or "")
            fingerprint = str(pack.get("rule_fingerprint") or "")
            material_key = str(pack.get("material_key") or "")[:360]
            payload = pack.get("payload")
            if not rule_id or component not in {"review", "objective", "subjective"} or not fingerprint or not isinstance(payload, dict):
                continue
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            # EvidencePack 是诊断和未来灰度依据，不保存 OCR 原文；限制单条大小以保护小规格服务器磁盘。
            if len(encoded) > 96_000:
                encoded = json.dumps({
                    "pack_version": payload.get("pack_version"), "mode": "shadow_only",
                    "truncated": True, "rule_id": rule_id, "component": component,
                    "page_provenance": payload.get("page_provenance", [])[:80],
                    "result_snapshot": payload.get("result_snapshot", {}),
                }, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """INSERT INTO ew_evidence_packs
                   (evidence_pack_id, project_id, task_id, document_id, rule_id, component, document_sha256,
                    rule_fingerprint, material_key, pack_version, payload_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id, document_id, rule_id, component) DO UPDATE SET
                   document_sha256=excluded.document_sha256, rule_fingerprint=excluded.rule_fingerprint,
                   material_key=excluded.material_key, pack_version=excluded.pack_version,
                   payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), project_id, task_id, document_id, rule_id, component, document_sha256,
                 fingerprint, material_key, str(payload.get("pack_version") or "shadow-v1"), encoded, timestamp, timestamp),
            )


def list_evidence_packs(app, task_id: str, document_id: str) -> list[dict]:
    """内部验证/测试使用；暂不暴露为工作台 API。"""
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT * FROM ew_evidence_packs WHERE task_id=? AND document_id=? ORDER BY component, rule_id",
            (task_id, document_id),
        ).fetchall()
    values = []
    for row in rows:
        value = dict(row)
        try:
            value["payload"] = json.loads(value.pop("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            value["payload"] = {}
        values.append(value)
    return values


def evidence_pack_pages(app, document_id: str, document_sha256: str, material_key: str, *, limit: int = 12) -> list[int]:
    """返回同一文件、同一材料事实在历史运行中已直接命中的页。

    这是候选页的保守提示而不是结论复用：仅接受 OCR/图片层明确标记为 evidence 的页，
    且要求文件哈希与材料键均一致。旧版没有 material_key 的影子包自动忽略。
    """
    if not document_id or not document_sha256 or not material_key:
        return []
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT payload_json FROM ew_evidence_packs
               WHERE document_id=? AND document_sha256=? AND material_key=?
               ORDER BY updated_at DESC LIMIT 24""",
            (document_id, document_sha256, material_key),
        ).fetchall()
    pages: list[int] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for collection in (payload.get("ocr_findings"), payload.get("vision_findings")):
            if not isinstance(collection, list):
                continue
            for finding in collection:
                if not isinstance(finding, dict):
                    continue
                values = finding.get("evidence_pages")
                if not isinstance(values, list):
                    continue
                for value in values:
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 0:
                        page = int(value)
                        if page not in pages:
                            pages.append(page)
                            if len(pages) >= max(1, limit):
                                return pages
    return pages


def save_evaluation_unit_checkpoints(app, project_id: str, rule_set_id: str, document_id: str,
                                     execution_fingerprint: str, values: dict[str, dict[str, dict]]) -> None:
    """保存已完整结束的单规则结果，供“仅重跑失败项”复用。"""
    if not execution_fingerprint:
        return
    timestamp = now_iso()
    with connection(app) as conn:
        for component, by_rule in values.items():
            if component not in {"review", "objective", "subjective"} or not isinstance(by_rule, dict):
                continue
            for rule_id, result in by_rule.items():
                if not rule_id or not isinstance(result, dict):
                    continue
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """INSERT INTO ew_evaluation_unit_checkpoints
                       (checkpoint_id, project_id, rule_set_id, document_id, component, rule_id,
                        execution_fingerprint, result_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(rule_set_id, document_id, component, rule_id, execution_fingerprint) DO UPDATE SET
                       result_json=excluded.result_json, updated_at=excluded.updated_at""",
                    (str(uuid.uuid4()), project_id, rule_set_id, document_id, component, rule_id,
                     execution_fingerprint, encoded, timestamp, timestamp),
                )


def get_evaluation_unit_checkpoints(app, project_id: str, rule_set_id: str, document_id: str,
                                    execution_fingerprint: str) -> dict[str, dict[str, dict]]:
    if not execution_fingerprint:
        return {"review": {}, "objective": {}, "subjective": {}}
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT component, rule_id, result_json FROM ew_evaluation_unit_checkpoints
               WHERE project_id=? AND rule_set_id=? AND document_id=? AND execution_fingerprint=?""",
            (project_id, rule_set_id, document_id, execution_fingerprint),
        ).fetchall()
    values = {"review": {}, "objective": {}, "subjective": {}}
    for row in rows:
        try:
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, dict) and str(result.get("rule_id") or "") == str(row["rule_id"] or ""):
            values[str(row["component"])][str(row["rule_id"])] = result
    return values


def delete_evaluation_unit_checkpoints(app, project_id: str, rule_set_id: str, document_id: str,
                                       execution_fingerprint: str, values: dict[str, set[str]]) -> None:
    """删除本轮未完成规则的旧快照，确保“仅重跑失败项”不会误复用人工兜底结果。"""
    if not execution_fingerprint:
        return
    with connection(app) as conn:
        for component, rule_ids in values.items():
            if component not in {"review", "objective", "subjective"} or not rule_ids:
                continue
            placeholders = ",".join("?" for _ in rule_ids)
            conn.execute(
                f"""DELETE FROM ew_evaluation_unit_checkpoints
                    WHERE project_id=? AND rule_set_id=? AND document_id=? AND execution_fingerprint=?
                      AND component=? AND rule_id IN ({placeholders})""",
                (project_id, rule_set_id, document_id, execution_fingerprint, component, *sorted(rule_ids)),
            )


def clear_evaluation_results(app, project_id: str) -> None:
    """删除项目上一轮综合评审产物，使重新运行不展示或复用旧结论。

    文件解析、页级 OCR 缓存等确定性基础数据不在这里删除；它们不是评审结论，且
    保留可避免重新解析文件。规则、评审结果、评分结果、证据包和失败续跑快照则
    全部清空，确保新的综合评审从规则到结论完全独立。
    """
    with connection(app) as conn:
        conn.execute("DELETE FROM ew_evaluation_current_documents WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_review_runs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_score_runs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_evidence_packs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_evaluation_unit_checkpoints WHERE project_id=?", (project_id,))


def prune_superseded_evaluation_runs(app, project_id: str, rule_set_id: str, keep_task_id: str) -> bool:
    """全量评审成功后清理旧运行产物；当前文件未全部指向新任务时拒绝清理。"""
    with connection(app, immediate=True) as conn:
        expected = conn.execute(
            "SELECT COUNT(*) FROM ew_documents WHERE project_id=? AND role='bid'",
            (project_id,),
        ).fetchone()[0]
        published = conn.execute(
            """SELECT COUNT(*) FROM ew_evaluation_current_documents current
               JOIN ew_documents document ON document.document_id=current.document_id
               WHERE current.project_id=? AND current.rule_set_id=? AND current.task_id=?
                 AND document.role='bid' AND current.document_sha256=document.sha256""",
            (project_id, rule_set_id, keep_task_id),
        ).fetchone()[0]
        if expected <= 0 or published != expected:
            return False
        conn.execute(
            "DELETE FROM ew_review_runs WHERE project_id=? AND task_id<>?",
            (project_id, keep_task_id),
        )
        conn.execute(
            "DELETE FROM ew_score_runs WHERE project_id=? AND task_id<>?",
            (project_id, keep_task_id),
        )
        conn.execute(
            "DELETE FROM ew_evidence_packs WHERE project_id=? AND task_id<>?",
            (project_id, keep_task_id),
        )
    return True


def get_project_scope_checkpoint(app, project_id: str, scope_key: str) -> dict | None:
    with connection(app) as conn:
        row = conn.execute(
            "SELECT scope_json FROM ew_project_scope_cache WHERE project_id=? AND scope_key=?",
            (project_id, scope_key),
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["scope_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def save_project_scope_checkpoint(app, project_id: str, scope_key: str, scope: dict) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_project_scope_cache(scope_cache_id, project_id, scope_key, scope_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, scope_key) DO UPDATE SET scope_json=excluded.scope_json, updated_at=excluded.updated_at""",
            (str(uuid.uuid4()), project_id, scope_key, json.dumps(scope, ensure_ascii=False), timestamp, timestamp),
        )


def project_token_usage(app, project_id: str) -> dict:
    with connection(app) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS call_count, COALESCE(SUM(input_chars), 0) AS input_chars,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens,
               SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END) AS metered_calls
               FROM ew_model_calls WHERE project_id = ?""", (project_id,)
        ).fetchone()
        family_rows = conn.execute(
            """SELECT CASE
                        WHEN context_mode LIKE 'vision%' THEN 'vision'
                        WHEN context_mode = 'tencent_ocr' THEN 'tencent_ocr'
                        WHEN context_mode = 'local_ocr' THEN 'local_ocr'
                        ELSE 'text' END AS family,
                      COUNT(*) AS call_count,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(input_chars), 0) AS input_chars
               FROM ew_model_calls WHERE project_id = ?
               GROUP BY family""", (project_id,),
        ).fetchall()
        cache_phase_rows = conn.execute(
            """SELECT phase, COUNT(*) AS call_count,
                      COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens
               FROM ew_model_calls
               WHERE project_id=? AND prompt_tokens IS NOT NULL
               GROUP BY phase
               ORDER BY cache_hit_tokens DESC, prompt_tokens DESC
               LIMIT 12""",
            (project_id,),
        ).fetchall()
        ocr_row = conn.execute(
            "SELECT COALESCE(SUM(billed_units), 0) AS ocr_requests FROM ew_ocr_usage_ledger WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        local_ocr_row = conn.execute(
            """SELECT COUNT(DISTINCT c.document_id || ':' || c.page_number) AS local_ocr_pages
               FROM ew_ocr_page_cache c JOIN ew_documents d ON d.document_id=c.document_id
               WHERE d.project_id=? AND c.service='rapidocr_local'""",
            (project_id,),
        ).fetchone()
        local_ocr_performance_row = conn.execute(
            """SELECT COUNT(*) AS run_count, COALESCE(SUM(requested_pages), 0) AS requested_pages,
                      COALESCE(SUM(recognized_pages), 0) AS recognized_pages,
                      COALESCE(SUM(empty_pages), 0) AS empty_pages,
                      COALESCE(SUM(failed_pages), 0) AS failed_pages,
                      COALESCE(SUM(elapsed_ms), 0) AS elapsed_ms,
                      MAX(peak_rss_kb) AS peak_rss_kb
               FROM ew_local_ocr_runs WHERE project_id=?""",
            (project_id,),
        ).fetchone()
        output_risk_row = conn.execute(
            """SELECT COUNT(*) AS observation_count,
                      COALESCE(SUM(shadow_split_recommended), 0) AS recommended_count,
                      COALESCE(SUM(actual_format_error), 0) AS format_error_count,
                      COALESCE(SUM(CASE WHEN shadow_split_recommended=1 AND actual_format_error=1 THEN 1 ELSE 0 END), 0) AS true_positive_count,
                      COALESCE(SUM(CASE WHEN shadow_split_recommended=1 AND actual_format_error=0 THEN 1 ELSE 0 END), 0) AS false_positive_count,
                      COALESCE(SUM(CASE WHEN shadow_split_recommended=0 AND actual_format_error=1 THEN 1 ELSE 0 END), 0) AS missed_count
               FROM ew_output_risk_observations WHERE project_id=?""",
            (project_id,),
        ).fetchone()
    usage = dict(row)
    # 图片识别与腾讯 OCR 的独立消耗分项，帮助用户评估识图预算；OCR 页数来自
    # 额度台账，不计入模型 token。
    usage["families"] = {
        str(item["family"]): {
            "call_count": item["call_count"],
            "total_tokens": item["total_tokens"],
            "input_chars": item["input_chars"],
        }
        for item in family_rows
    }
    usage["ocr_requests"] = int(ocr_row["ocr_requests"] or 0) if ocr_row else 0
    # 本地 OCR 没有额度台账；以已缓存的去重页数呈现真实处理规模（含空白页缓存）。
    usage["local_ocr_pages"] = int(local_ocr_row["local_ocr_pages"] or 0) if local_ocr_row else 0
    performance = dict(local_ocr_performance_row) if local_ocr_performance_row else {}
    performance["average_ms_per_page"] = round(
        int(performance.get("elapsed_ms") or 0) / max(1, int(performance.get("requested_pages") or 0)), 1,
    ) if performance.get("requested_pages") else 0
    usage["local_ocr_performance"] = performance
    risk_values = dict(output_risk_row) if output_risk_row else {}
    usage["output_risk_shadow"] = {
        "observations": int(risk_values.get("observation_count") or 0),
        "recommended": int(risk_values.get("recommended_count") or 0),
        "format_errors": int(risk_values.get("format_error_count") or 0),
        "true_positives": int(risk_values.get("true_positive_count") or 0),
        "false_positives": int(risk_values.get("false_positive_count") or 0),
        "missed": int(risk_values.get("missed_count") or 0),
    }
    # 不依赖特定厂商字段。若模型返回缓存 token，即按环节汇总，供页面和后续优化判断
    # 哪些调用真正具备前缀复用空间；未返回该字段的模型保持 0，不误报失败。
    usage["cache_by_phase"] = [
        {
            "phase": str(item["phase"] or ""), "call_count": int(item["call_count"] or 0),
            "prompt_tokens": int(item["prompt_tokens"] or 0),
            "cache_hit_tokens": int(item["cache_hit_tokens"] or 0),
        }
        for item in cache_phase_rows
    ]
    return usage


def latest_evaluation_run_usage(app, project_id: str) -> dict | None:
    """最近一次有模型/OCR 消耗的成功任务的用量、结束时间与运行依托版本。"""
    with connection(app) as conn:
        task_row = conn.execute(
            """SELECT t.task_id, t.task_type, t.payload_json, t.started_at, t.finished_at
               FROM ew_tasks t
               WHERE t.project_id=? AND t.status='success'
                 AND (EXISTS (SELECT 1 FROM ew_model_calls m WHERE m.task_id=t.task_id)
                      OR EXISTS (SELECT 1 FROM ew_ocr_usage_ledger o WHERE o.task_id=t.task_id)
                      OR EXISTS (SELECT 1 FROM ew_local_ocr_runs r WHERE r.task_id=t.task_id))
               ORDER BY t.finished_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        if not task_row:
            return None
        task_id = task_row["task_id"]
        row = conn.execute(
            """SELECT COUNT(*) AS call_count, COALESCE(SUM(input_chars), 0) AS input_chars,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens,
               SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END) AS metered_calls
               FROM ew_model_calls WHERE task_id = ?""", (task_id,)
        ).fetchone()
        family_rows = conn.execute(
            """SELECT CASE
                        WHEN context_mode LIKE 'vision%' THEN 'vision'
                        WHEN context_mode = 'tencent_ocr' THEN 'tencent_ocr'
                        WHEN context_mode = 'local_ocr' THEN 'local_ocr'
                        ELSE 'text' END AS family,
                      COUNT(*) AS call_count,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(input_chars), 0) AS input_chars
               FROM ew_model_calls WHERE task_id = ?
               GROUP BY family""", (task_id,),
        ).fetchall()
        ocr_row = conn.execute(
            "SELECT COALESCE(SUM(billed_units), 0) AS ocr_requests FROM ew_ocr_usage_ledger WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        local_ocr_row = conn.execute(
            """SELECT COUNT(DISTINCT c.document_id || ':' || c.page_number) AS local_ocr_pages
               FROM ew_ocr_page_cache c
               JOIN ew_documents d ON d.document_id = c.document_id
               JOIN ew_tasks t ON t.task_id = ?
               WHERE d.project_id=? AND c.service='rapidocr_local'
                 AND c.created_at >= COALESCE(t.started_at, t.created_at)
                 AND c.created_at <= COALESCE(t.finished_at, c.created_at)""",
            (task_id, project_id),
        ).fetchone()
    try:
        payload = json.loads(task_row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    usage = dict(row)
    usage["families"] = {
        str(item["family"]): {
            "call_count": item["call_count"],
            "total_tokens": item["total_tokens"],
            "input_chars": item["input_chars"],
        }
        for item in family_rows
    }
    usage["ocr_requests"] = int(ocr_row["ocr_requests"] or 0) if ocr_row else 0
    usage["local_ocr_pages"] = int(local_ocr_row["local_ocr_pages"] or 0) if local_ocr_row else 0
    usage["task_type"] = str(task_row["task_type"] or "")
    usage["finished_at"] = task_row["finished_at"]
    usage["deploy_commit"] = str(payload.get("deploy_commit") or "")
    usage["prompt_version"] = str(payload.get("prompt_version") or "")
    return usage


def task_recovery_summary(app, task_id: str) -> dict[str, int]:
    """按实际模型调用区分结构化恢复路径，供任务结果和运行监控使用。"""
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT phase, context_mode FROM ew_model_calls WHERE task_id = ?", (task_id,)
        ).fetchall()
    summary = {"json_repair_count": 0, "compact_retry_count": 0, "missing_rule_retry_count": 0}
    for row in rows:
        phase = str(row["phase"] or "")
        context_mode = str(row["context_mode"] or "")
        if phase.endswith("_json_repair"):
            summary["json_repair_count"] += 1
        if phase.endswith("_compact_retry"):
            summary["compact_retry_count"] += 1
        if "/缺失补评" in context_mode:
            summary["missing_rule_retry_count"] += 1
    return summary


def save_compare_pair(app, task_id: str, document_a_id: str, document_b_id: str, result: dict) -> None:
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_compare_pairs(pair_id, task_id, document_a_id, document_b_id, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, document_a_id, document_b_id, json.dumps(result, ensure_ascii=False), now_iso()),
        )


def list_compare_pairs(app, task_id: str) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute("SELECT * FROM ew_compare_pairs WHERE task_id = ? ORDER BY created_at", (task_id,)).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json"))
        result.append(value)
    return result


def compare_analysis(app, task_id: str) -> dict | None:
    """返回查重分析结果。人工处置已停用（设计基线：不保存人工处置状态），
    仅返回任务中保存的 AI 判定与线索本身；历史表数据保留可读但不再参与。"""
    task = get_task(app, task_id)
    analysis = (task or {}).get("result", {}).get("cross_bid_analysis")
    if not isinstance(analysis, dict):
        return None
    # 不改写历史任务结果；仅在读取时补充“当前运行链路是否仍一致”的诊断信息。
    value = dict(analysis)
    payload = (task or {}).get("payload") or {}
    stored = value.get("pipeline")
    if not isinstance(stored, dict) or not stored.get("fingerprint"):
        value["pipeline_status"] = {
            "current": False,
            "reason": "历史结果未记录完整查重链路指纹，请重新运行后再作当前版本判断。",
        }
        return value
    try:
        current_input = task_input_fingerprint(
            app,
            str(task.get("project_id") or ""),
            "compare_documents",
            payload.get("profile_id"),
            str(payload.get("prompt_version") or ""),
        )
        current = compare_pipeline_metadata(app, payload.get("profile_id"), current_input)
        is_current = current.get("fingerprint") == stored.get("fingerprint")
        value["pipeline_status"] = {
            "current": is_current,
            "reason": "当前版本结果" if is_current else "文件、模型、提示词或查重代码已变化；该结果仅供历史参考，建议重新运行。",
            "stored_fingerprint": str(stored.get("fingerprint"))[:12],
            "current_fingerprint": str(current.get("fingerprint"))[:12],
        }
    except (ValueError, TypeError):
        value["pipeline_status"] = {
            "current": False,
            "reason": "无法核对当前查重链路身份，请重新运行后再作当前版本判断。",
        }
    return value


def latest_compare_results(app, project_id: str) -> tuple[dict | None, list[dict]]:
    with connection(app) as conn:
        task = conn.execute(
            "SELECT * FROM ew_tasks WHERE project_id = ? AND task_type = 'compare_documents' AND status = 'success' ORDER BY finished_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if not task:
            return None, []
        rows = conn.execute(
            """SELECT p.*, a.bidder_name AS bidder_a, a.original_name AS filename_a,
               b.bidder_name AS bidder_b, b.original_name AS filename_b
               FROM ew_compare_pairs p JOIN ew_documents a ON a.document_id = p.document_a_id
               JOIN ew_documents b ON b.document_id = p.document_b_id
               WHERE p.task_id = ? ORDER BY p.created_at""", (task["task_id"],)
        ).fetchall()
    pairs = []
    for row in rows:
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json"))
        pairs.append(value)
    task_value = task_to_dict(task)
    analysis = compare_analysis(app, task_value["task_id"])
    if analysis:
        task_value.setdefault("result", {})["cross_bid_analysis"] = analysis
    return task_value, pairs


def _public_model_profile(profile: dict) -> dict:
    value = dict(profile)
    encrypted = bool(value.pop("api_key_encrypted", ""))
    env_configured = bool(value.get("api_key_env") and os.environ.get(value["api_key_env"], "").strip())
    value["api_key_configured"] = encrypted or env_configured
    value["api_key_source"] = "manual" if encrypted else "environment" if env_configured else "none"
    value["supports_vision"] = bool(value.get("supports_vision"))
    value["vision_protocol"] = str(value.get("vision_protocol") or "") if value.get("supports_vision") else ""
    return value


def _clear_vision_default_for_profile(conn: sqlite3.Connection, profile_id: str) -> None:
    """避免删除/禁用默认图片模型后留下一个看似开启、实际不可用的全局开关。"""
    default_row = conn.execute(
        "SELECT setting_value FROM ew_settings WHERE setting_key = ?", (DEFAULT_VISION_MODEL_SETTING,)
    ).fetchone()
    if not default_row or default_row["setting_value"] != profile_id:
        return
    conn.execute("DELETE FROM ew_settings WHERE setting_key = ?", (DEFAULT_VISION_MODEL_SETTING,))
    conn.execute(
        "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
        (VISION_ENABLED_SETTING, "0", now_iso()),
    )


def list_model_profiles(app) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute("SELECT * FROM ew_model_profiles ORDER BY created_at").fetchall()
        default_row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = 'default_model_profile_id'").fetchone()
        vision_default_row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = ?", (DEFAULT_VISION_MODEL_SETTING,)).fetchone()
    default_id = default_row["setting_value"] if default_row else None
    vision_default_id = vision_default_row["setting_value"] if vision_default_row else None
    profiles = []
    for row in rows:
        profile = _public_model_profile(dict(row))
        profile["is_default"] = profile["profile_id"] == default_id
        profile["is_default_vision"] = profile["profile_id"] == vision_default_id
        profiles.append(profile)
    return profiles


def vision_configuration(app) -> dict:
    """全局视觉功能默认关闭；开关关闭时必须保持纯文字评审行为。"""
    with connection(app) as conn:
        enabled_row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = ?", (VISION_ENABLED_SETTING,)).fetchone()
        profile_row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = ?", (DEFAULT_VISION_MODEL_SETTING,)).fetchone()
    return {
        "enabled": str(enabled_row["setting_value"] if enabled_row else "0") == "1",
        "default_profile_id": profile_row["setting_value"] if profile_row else None,
    }


def update_vision_configuration(app, payload: dict) -> dict:
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("图片识别总开关必须为布尔值")
    requested_profile = payload.get("default_profile_id")
    with connection(app) as conn:
        profile_id = str(requested_profile or "").strip()
        if profile_id:
            row = conn.execute("SELECT * FROM ew_model_profiles WHERE profile_id=? AND enabled=1", (profile_id,)).fetchone()
            if not row:
                raise ValueError("默认图片识别模型不存在或已禁用")
            profile = dict(row)
            has_key = bool(profile.get("api_key_encrypted")) or bool(profile.get("api_key_env") and os.environ.get(profile["api_key_env"], "").strip())
            if not profile.get("supports_vision"):
                raise ValueError("所选默认图片识别模型未标记为多模态模型")
            if not has_key:
                raise ValueError("默认图片识别模型必须已配置 API Key")
        elif enabled:
            existing = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key=?", (DEFAULT_VISION_MODEL_SETTING,)).fetchone()
            if not existing:
                raise ValueError("开启图片识别前，请先选择默认图片识别模型")
            row = conn.execute("SELECT * FROM ew_model_profiles WHERE profile_id=? AND enabled=1", (existing["setting_value"],)).fetchone()
            if not row or not row["supports_vision"]:
                raise ValueError("默认图片识别模型已不可用，请重新选择已标记为多模态的模型")
            existing_profile = dict(row)
            if not (existing_profile.get("api_key_encrypted") or (
                existing_profile.get("api_key_env") and os.environ.get(existing_profile["api_key_env"], "").strip()
            )):
                raise ValueError("默认图片识别模型未配置 API Key，请先完善模型档案")
        conn.execute(
            "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
            (VISION_ENABLED_SETTING, "1" if enabled else "0", now_iso()),
        )
        if profile_id:
            conn.execute(
                "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
                (DEFAULT_VISION_MODEL_SETTING, profile_id, now_iso()),
            )
    return vision_configuration(app)


def get_default_vision_model_profile(app) -> dict | None:
    configuration = vision_configuration(app)
    if not configuration["enabled"] or not configuration.get("default_profile_id"):
        return None
    try:
        profile = get_model_profile(app, configuration["default_profile_id"])
    except ValueError:
        return None
    return profile if profile.get("supports_vision") else None


def resolve_vision_model_profile(app, primary_profile: dict | None = None) -> dict | None:
    """优先复用当前评审模型；否则使用独立默认图片模型。"""
    if not vision_configuration(app)["enabled"]:
        return None
    if primary_profile and primary_profile.get("supports_vision"):
        return primary_profile
    return get_default_vision_model_profile(app)


def default_model_profile_id(app) -> str | None:
    with connection(app) as conn:
        row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = 'default_model_profile_id'").fetchone()
    return row["setting_value"] if row else None


def set_default_model_profile(app, profile_id: str) -> dict:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_model_profiles WHERE profile_id = ? AND enabled = 1", (profile_id,)).fetchone()
        if not row:
            raise ValueError("只能将已启用的模型设为默认模型")
        profile = dict(row)
        has_key = bool(profile.get("api_key_encrypted")) or bool(profile.get("api_key_env") and os.environ.get(profile["api_key_env"], "").strip())
        if not has_key:
            raise ValueError("默认模型必须已配置 API Key")
        conn.execute(
            "INSERT INTO ew_settings(setting_key, setting_value, updated_at) VALUES ('default_model_profile_id', ?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
            (profile_id, now_iso()),
        )
    return _public_model_profile(profile)


def get_model_profile(app, profile_id: str | None, preferred_model: str = "") -> dict:
    with connection(app) as conn:
        if profile_id:
            row = conn.execute("SELECT * FROM ew_model_profiles WHERE profile_id = ? AND enabled = 1", (profile_id,)).fetchone()
        elif (row := conn.execute(
            """SELECT p.* FROM ew_model_profiles p JOIN ew_settings s ON s.setting_value=p.profile_id
               WHERE s.setting_key='default_model_profile_id' AND p.enabled=1"""
        ).fetchone()):
            pass
        elif preferred_model:
            row = conn.execute("SELECT * FROM ew_model_profiles WHERE model_name = ? AND enabled = 1 LIMIT 1", (preferred_model,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM ew_model_profiles WHERE enabled = 1 ORDER BY created_at LIMIT 1").fetchone()
    if not row:
        raise ValueError("未找到已启用的模型档案")
    profile = dict(row)
    if profile.get("api_key_encrypted"):
        profile["_api_key"] = _decrypt_model_api_key(app, profile["api_key_encrypted"])
    elif profile.get("api_key_env"):
        profile["_api_key"] = os.environ.get(profile["api_key_env"], "").strip()
    else:
        profile["_api_key"] = ""
    return profile


def _model_profile_values(app, payload: dict, *, existing: dict | None = None) -> dict:
    required = ("display_name", "base_url", "model_name")
    if any(not str(payload.get(key, "")).strip() for key in required):
        raise ValueError("模型名称、Base URL 和模型 ID 均不能为空")
    base_url = str(payload["base_url"]).strip().rstrip("/")
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Base URL 必须是有效的 HTTP 或 HTTPS 地址")
    api_key_env = str(payload.get("api_key_env", existing.get("api_key_env", "") if existing else "")).strip()
    if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("API Key 环境变量名格式不正确")
    raw_api_key = str(payload.get("api_key", "")).strip()
    encrypted = existing.get("api_key_encrypted") if existing else None
    if raw_api_key:
        _validate_api_key_characters(raw_api_key)
        encrypted = _encrypt_model_api_key(app, raw_api_key)
    if not existing and not raw_api_key and not api_key_env:
        raise ValueError("请填写 API Key，或指定已配置的 API Key 环境变量名")
    profile_id, timestamp = (existing["profile_id"] if existing else str(uuid.uuid4())), now_iso()
    enabled = payload.get("enabled", existing.get("enabled", 1) if existing else 1)
    if not isinstance(enabled, bool) and enabled not in {0, 1}:
        raise ValueError("模型启用状态格式不正确")
    supports_vision = payload.get("supports_vision", existing.get("supports_vision", 0) if existing else False)
    if not isinstance(supports_vision, bool) and supports_vision not in {0, 1}:
        raise ValueError("多模态能力标识格式不正确")
    vision_protocol = str(payload.get("vision_protocol", existing.get("vision_protocol", "") if existing else "") or "").strip()
    if supports_vision:
        # 首期仅实现通用 OpenAI-compatible 图片内容块；模型名称不参与能力判断。
        vision_protocol = vision_protocol or "openai-image-url"
        if vision_protocol not in {"openai-image-url"}:
            raise ValueError("暂不支持该图片识别接口协议")
    else:
        vision_protocol = ""
    values = {
        "profile_id": profile_id,
        "display_name": str(payload["display_name"]).strip(),
        "protocol": "openai-compatible",
        "base_url": base_url,
        "model_name": str(payload["model_name"]).strip(),
        "api_key_env": api_key_env,
        "api_key_encrypted": encrypted,
        "context_limit": int(payload.get("context_limit") or 0) or None,
        "timeout_seconds": min(1800, max(30, int(payload.get("timeout_seconds") or 600))),
        "json_mode": 1 if payload.get("json_mode", True) else 0,
        "thinking_mode": payload.get("thinking_mode") if payload.get("thinking_mode") in {"default", "enabled", "adaptive", "disabled"} else "default",
        "supports_vision": 1 if supports_vision else 0,
        "vision_protocol": vision_protocol,
        "enabled": 1 if enabled else 0,
        "created_at": existing["created_at"] if existing else timestamp,
        "updated_at": timestamp,
    }
    return values


def create_model_profile(app, payload: dict) -> dict:
    values = _model_profile_values(app, payload)
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_model_profiles(profile_id, display_name, protocol, base_url, model_name, api_key_env, api_key_encrypted,
            context_limit, timeout_seconds, json_mode, thinking_mode, supports_vision, vision_protocol, enabled, created_at, updated_at)
            VALUES (:profile_id, :display_name, :protocol, :base_url, :model_name, :api_key_env, :api_key_encrypted, :context_limit,
            :timeout_seconds, :json_mode, :thinking_mode, :supports_vision, :vision_protocol, :enabled, :created_at, :updated_at)""",
            values,
        )
    return _public_model_profile(values)


def update_model_profile(app, profile_id: str, payload: dict) -> dict:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_model_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
    if not row:
        raise ValueError("模型档案不存在")
    current = dict(row)
    merged = {**current, **{key: value for key, value in payload.items() if key != "api_key"}}
    if "api_key" in payload:
        merged["api_key"] = payload["api_key"]
    values = _model_profile_values(app, merged, existing=current)
    with connection(app) as conn:
        if not values["enabled"]:
            active_rows = conn.execute(
                "SELECT payload_json FROM ew_tasks WHERE status IN ('queued', 'running')"
            ).fetchall()
            for active in active_rows:
                try:
                    active_payload = json.loads(active["payload_json"] or "{}")
                except json.JSONDecodeError:
                    active_payload = {}
                if active_payload.get("profile_id") == profile_id:
                    raise ValueError("该模型正在被排队或运行中的任务使用，暂不能禁用")
        conn.execute(
            """UPDATE ew_model_profiles SET display_name=:display_name, protocol=:protocol, base_url=:base_url,
               model_name=:model_name, api_key_env=:api_key_env, api_key_encrypted=:api_key_encrypted,
               context_limit=:context_limit, timeout_seconds=:timeout_seconds, json_mode=:json_mode,
               thinking_mode=:thinking_mode, supports_vision=:supports_vision, vision_protocol=:vision_protocol,
               enabled=:enabled, updated_at=:updated_at WHERE profile_id=:profile_id""",
            values,
        )
        if not values["enabled"] or not values["supports_vision"]:
            _clear_vision_default_for_profile(conn, profile_id)
        if not values["enabled"]:
            default_row = conn.execute(
                "SELECT setting_value FROM ew_settings WHERE setting_key = 'default_model_profile_id'"
            ).fetchone()
            if default_row and default_row["setting_value"] == profile_id:
                candidates = conn.execute(
                    "SELECT profile_id, api_key_env, api_key_encrypted FROM ew_model_profiles "
                    "WHERE profile_id != ? AND enabled = 1 ORDER BY created_at",
                    (profile_id,),
                ).fetchall()
                replacement = next((candidate for candidate in candidates if candidate["api_key_encrypted"] or (
                    candidate["api_key_env"] and os.environ.get(candidate["api_key_env"], "").strip()
                )), None)
                if replacement:
                    conn.execute(
                        "UPDATE ew_settings SET setting_value = ?, updated_at = ? WHERE setting_key = 'default_model_profile_id'",
                        (replacement["profile_id"], now_iso()),
                    )
                else:
                    conn.execute("DELETE FROM ew_settings WHERE setting_key = 'default_model_profile_id'")
    return _public_model_profile(values)


def delete_model_profile(app, profile_id: str) -> None:
    with connection(app) as conn:
        row = conn.execute("SELECT 1 FROM ew_model_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
        if not row:
            raise ValueError("模型档案不存在")
        # 先检查任务引用，再处理默认模型/图片模型设置；删除被拒绝时不能产生任何配置副作用。
        active_rows = conn.execute(
            "SELECT payload_json FROM ew_tasks WHERE status IN ('queued', 'running')"
        ).fetchall()
        for active in active_rows:
            try:
                payload = json.loads(active["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if payload.get("profile_id") == profile_id:
                raise ValueError("该模型正在被排队或运行中的任务使用，暂不能删除")
        default_row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = 'default_model_profile_id'").fetchone()
        if default_row and default_row["setting_value"] == profile_id:
            candidates = conn.execute(
                "SELECT profile_id, api_key_env, api_key_encrypted FROM ew_model_profiles "
                "WHERE profile_id != ? AND enabled = 1 ORDER BY created_at",
                (profile_id,),
            ).fetchall()
            replacement = next((candidate for candidate in candidates if candidate["api_key_encrypted"] or (
                candidate["api_key_env"] and os.environ.get(candidate["api_key_env"], "").strip()
            )), None)
            if replacement:
                conn.execute(
                    "UPDATE ew_settings SET setting_value = ?, updated_at = ? WHERE setting_key = 'default_model_profile_id'",
                    (replacement["profile_id"], now_iso()),
                )
            else:
                conn.execute("DELETE FROM ew_settings WHERE setting_key = 'default_model_profile_id'")
        _clear_vision_default_for_profile(conn, profile_id)
        conn.execute("DELETE FROM ew_model_profiles WHERE profile_id = ?", (profile_id,))


def current_rule_set(app, project_id: str, create: bool = False) -> dict | None:
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_rule_sets WHERE project_id = ? ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
        if row or not create:
            return dict(row) if row else None
        timestamp = now_iso()
        rule_set = {"rule_set_id": str(uuid.uuid4()), "project_id": project_id, "version": 1, "status": "draft", "created_at": timestamp, "updated_at": timestamp}
        conn.execute("INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, created_at, updated_at) VALUES (:rule_set_id, :project_id, :version, :status, :created_at, :updated_at)", rule_set)
    return rule_set


_RULE_EXECUTION_STRATEGIES = {"point", "counting", "section", "consistency", "cross_bid", "visual", "external"}
# document/field 描述的是“需要什么证据”，而不是新的评审结论类型：
# document 用于材料存在性，field 用于材料内的编号、日期、金额等可读字段。
# 保留旧类型，确保已有规则及外部 API 的元数据可继续使用。
_RULE_EVIDENCE_TYPES = {"text", "document", "field", "visual", "cross_bid", "external"}
_VISION_TRIGGERS = {"off", "text_fallback", "required"}
_VISION_LEVELS = {"off", "low", "standard", "high"}
# image_mode 仅控制图片取证通道；保持 vision_trigger / vision_level 的旧字段，
# 使已有 API、历史规则与外部调用方无需迁移即可继续工作。
_IMAGE_MODES = {"auto", "ocr_only", "vision_only", "combined", "off"}
_BASELINE_OCR_MODES = {"auto", "text_only", "local_ocr"}
# acquisition_preset 是面向前台的业务级快捷设置。底层仍以 image_mode /
# vision_trigger / vision_level 执行，确保既有规则、历史任务和外部 API 完全兼容。
# always 是前台“每次执行”的简化语义：仍由 auto 通道根据规则证据类型决定
# OCR/多模态，而不是让用户自行组合两个技术通道。
_ACQUISITION_PRESETS = {"smart", "always", "text", "visual", "dual", "off", "custom"}


def _normalise_evidence_items(value: object) -> list[dict]:
    """保留复合规则的可独立取证子项，未知或旧格式安全忽略。

    子项只服务于候选页均衡和取证可追溯，不会拆分现有规则、改变其启用状态，
    更不会独立生成结论或分数。字段刻意保持通用，避免将某一类证书、参数或行业
    固化进存储结构。
    """
    if not isinstance(value, list):
        return []
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("title") or "").strip()[:180]
        requirement = str(raw.get("requirement") or raw.get("check_rule") or raw.get("criterion") or "").strip()[:600]
        if not name and not requirement:
            continue
        key = (re.sub(r"\s+", "", name).casefold(), re.sub(r"\s+", "", requirement).casefold())
        if key in seen:
            continue
        seen.add(key)
        page = raw.get("source_page")
        if isinstance(page, bool):
            page = None
        elif isinstance(page, (int, float)) and int(page) > 0:
            page = int(page)
        elif isinstance(page, str) and page.strip().isdigit() and int(page.strip()) > 0:
            page = int(page.strip())
        else:
            page = None
        requirements = raw.get("evidence_requirements")
        if not isinstance(requirements, list):
            requirements = []
        requirements = [str(item) for item in requirements if str(item) in _RULE_EVIDENCE_TYPES]
        items.append({
            "item_id": str(raw.get("item_id") or raw.get("id") or f"item_{index}").strip()[:80] or f"item_{index}",
            "name": name or requirement[:100],
            "requirement": requirement,
            "source_page": page,
            "evidence_requirements": list(dict.fromkeys(requirements)),
        })
        # 与规则提取提示词、候选页轮转上限保持一致，避免模型可输出但存储层静默
        # 截断不同数量的子项。
        if len(items) >= 12:
            break
    return items


def _preset_from_legacy_image_mode(image_mode: object) -> str:
    return {
        "ocr_only": "text",
        "vision_only": "visual",
        "combined": "dual",
        "off": "off",
    }.get(str(image_mode or "auto"), "smart")


RULE_VISUAL_SUGGESTION_TERMS = (
    # 建议层外观词汇全集：覆盖 worker 执行级 DECISIVE_VISUAL_FACT_PATTERN 的全部词
    # （图片外观/照片外观/版式外观由“外观”“照片”“版式”子串吸收），并补充图纸、
    # 截图等图像载体词。执行级判定保持更窄的集合不变；建议层取并集，保证凡是被
    # 标记 ocr_required 的规则，建议档位不会自相矛盾地落回“仅基础识别”。
    "签字", "签章", "盖章", "公章", "印章", "骑缝章", "手写", "指印", "勾选", "涂改",
    "外观", "版式", "照片", "截图", "图纸",
)


def rule_acquisition_recommendation(rule: dict) -> dict:
    """为前台提供可恢复的通用建议，不擅自改写当前规则的执行口径。

    recommendation 只根据证据类型（模型语义判断）和统一的外观词汇表判断，避免为
    某一行业、某一种证书或具体项目写补丁；模型已标记 ocr_required 的规则，建议
    档位至少为 smart，保持与规则自身标记一致。"""
    meta = rule_execution_meta(rule)
    requirements = set(meta.get("evidence_requirements") or [])
    text = "\n".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    has_visual_fact = "visual" in requirements or any(term in text for term in RULE_VISUAL_SUGGESTION_TERMS)
    has_document_field = bool(requirements & {"document", "field"}) or str(rule.get("check_mode") or "") == "ocr"
    ocr_marked = (
        str(rule.get("check_mode") or "") == "ocr"
        or bool(rule.get("ocr_required")) or bool(meta.get("ocr_required"))
    )
    if has_visual_fact and (has_document_field or "text" in requirements):
        preset = "smart"
    elif has_visual_fact:
        preset = "visual"
    elif ocr_marked:
        # 模型已判定决定性证据在 OCR/图像层（如证书编号、骑缝章等不含外观词的
        # 场景），建议不能低于“智能取证”，否则与规则自身的 ocr_required 矛盾。
        preset = "smart"
    else:
        preset = "off"
    # 本地 OCR 是基础层，不再等同于腾讯 OCR 增强。纯文字规则明确跳过 OCR；
    # 材料/字段规则采用 auto，仅在全文证据不足时才执行有限候选页 OCR。
    baseline_ocr_mode = "auto"
    return {
        "acquisition_preset": preset,
        "image_mode": {"smart": "auto", "text": "ocr_only", "visual": "vision_only", "dual": "combined", "off": "off"}[preset],
        "vision_trigger": "required" if preset == "visual" else "text_fallback" if preset in {"smart", "text"} else "off",
        # 非签章/外观类规则默认“快速”档（首轮 2 页、无补页），避免不必要的高清
        # 大图与多页调用；确需签字、盖章、勾选、照片、外观等视觉事实的规则保持
        # “标准”。只影响新规则与“恢复 AI 建议”，存量规则不受影响，可逐条调高。
        "vision_level": ("standard" if has_visual_fact else "low") if preset != "off" else "off",
        "baseline_ocr_mode": baseline_ocr_mode,
    }


def _normalise_rejection_clauses(value: object) -> list[dict]:
    """保存可追溯的 RC 台账摘要；只接受来源明确的可选元数据。"""
    rows = value if isinstance(value, list) else []
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows[:48]:
        if not isinstance(row, dict):
            continue
        clause_id = str(row.get("clause_id") or "").strip()
        if not clause_id.startswith("RC-") or clause_id in seen:
            continue
        source_units = row.get("source_unit_ids") if isinstance(row.get("source_unit_ids"), list) else []
        source_units = list(dict.fromkeys(str(item).strip() for item in source_units if str(item).strip()))[:24]
        quote = str(row.get("consequence_quote") or "").strip()[:500]
        if not source_units or not quote:
            continue
        source_pages = row.get("source_pages") if isinstance(row.get("source_pages"), list) else []
        source_pages = sorted({int(page) for page in source_pages if isinstance(page, int) and page > 0})[:24]
        scope = str(row.get("scope") or "").strip()
        if scope not in {"current_package", "all_packages"}:
            scope = "all_packages"
        result.append({
            "clause_id": clause_id,
            "source_unit_ids": source_units,
            "source_pages": source_pages,
            "trigger": str(row.get("trigger") or "").strip()[:500],
            "consequence_quote": quote,
            "consequence": str(row.get("consequence") or "").strip()[:500],
            "exceptions": str(row.get("exceptions") or "").strip()[:500],
            "scope": scope,
        })
        seen.add(clause_id)
    return result


def rule_execution_meta(rule: dict) -> dict:
    """读取可向后兼容的规则执行元数据；旧规则安全回退为空元数据。"""
    raw = rule.get("execution_meta_json")
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    strategy = str(value.get("execution_strategy") or "").strip()
    requirements = value.get("evidence_requirements")
    if not isinstance(requirements, list):
        requirements = []
    requirements = [str(item) for item in requirements if str(item) in _RULE_EVIDENCE_TYPES]
    if any(item in requirements for item in {"document", "field"}) and "text" not in requirements:
        requirements.append("text")
    applicability = value.get("applicability")
    if not isinstance(applicability, dict):
        applicability = {}
    legacy_visual = str(rule.get("check_mode") or "") == "ocr" or bool(rule.get("ocr_required"))
    trigger = str(value.get("vision_trigger") or "")
    if trigger not in _VISION_TRIGGERS:
        trigger = "required" if legacy_visual else "off"
    level = str(value.get("vision_level") or "")
    if level not in _VISION_LEVELS:
        level = "off"
    image_mode = str(value.get("image_mode") or "auto")
    if image_mode not in _IMAGE_MODES:
        image_mode = "auto"
    acquisition_preset = str(value.get("acquisition_preset") or "")
    if acquisition_preset not in _ACQUISITION_PRESETS:
        acquisition_preset = _preset_from_legacy_image_mode(image_mode)
    baseline_ocr_mode = str(value.get("baseline_ocr_mode") or "auto")
    if baseline_ocr_mode not in _BASELINE_OCR_MODES:
        baseline_ocr_mode = "auto"
    clause_ids = value.get("source_clause_ids")
    if not isinstance(clause_ids, list):
        clause_ids = []
    clause_ids = [str(item).strip() for item in clause_ids if str(item).strip()]
    # 来源事实 ID 是规则提取 V2 的稳定追溯键。它与评分条款 ID 不同：前者覆盖
    # 所有类别的直接招标原文事实，后者仅用于评分表守恒。旧规则没有该字段时保持空
    # 列表，不改变既有接口或执行行为。
    fact_ids = value.get("source_fact_ids")
    if not isinstance(fact_ids, list):
        fact_ids = []
    fact_ids = [str(item).strip() for item in fact_ids if str(item).strip()]
    source_unit_ids = value.get("source_unit_ids")
    if not isinstance(source_unit_ids, list):
        source_unit_ids = []
    source_unit_ids = [str(item).strip() for item in source_unit_ids if str(item).strip()]
    rejection_clause_ids = value.get("rejection_clause_ids")
    if not isinstance(rejection_clause_ids, list):
        rejection_clause_ids = []
    rejection_clause_ids = list(dict.fromkeys(
        item for raw in rejection_clause_ids if (item := str(raw).strip()).startswith("RC-")
    ))[:48]
    rejection_clauses = _normalise_rejection_clauses(value.get("rejection_clauses"))
    if rejection_clauses:
        rejection_clause_ids = [item["clause_id"] for item in rejection_clauses]
    decision_impact_source = str(value.get("decision_impact_source") or "").strip()
    if decision_impact_source not in {"", "rule_category", "legacy_source_fallback", "rc_ledger"}:
        decision_impact_source = ""
    verification_target = str(value.get("verification_target") or "").strip()
    verifiability = str(value.get("verifiability") or "").strip()
    if verifiability not in {"single_bid", "cross_bid", "external_procedure"}:
        verifiability = ""
    source_locations = value.get("source_locations")
    if not isinstance(source_locations, list):
        source_locations = []
    normalised_locations: list[dict] = []
    for location in source_locations:
        if not isinstance(location, dict):
            continue
        page = location.get("page")
        if not isinstance(page, int) or page <= 0:
            continue
        value_location = {"page": page}
        if value_location not in normalised_locations:
            normalised_locations.append(value_location)
    sections = value.get("score_sections")
    if not isinstance(sections, list):
        sections = []
    normalised_sections: list[dict] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "").strip()
        if not section_id or section_id in {item["section_id"] for item in normalised_sections}:
            continue
        try:
            max_score = float(section.get("max_score"))
        except (TypeError, ValueError):
            max_score = None
        page = section.get("source_page")
        normalised_sections.append({
            "section_id": section_id,
            "label": str(section.get("label") or "评分分部").strip() or "评分分部",
            "max_score": max_score if max_score is not None and max_score > 0 else None,
            "source_page": page if isinstance(page, int) and page > 0 else None,
        })
    compiled_group_id = str(value.get("compiled_group_id") or "").strip()[:80]
    compiled_categories = value.get("compiled_categories")
    if not isinstance(compiled_categories, list):
        compiled_categories = []
    compiled_categories = [
        str(item) for item in compiled_categories
        if str(item) in {"qualification", "compliance", "substantive", "rejection", "other"}
    ]
    compiled_children = value.get("compiled_child_requirements")
    if not isinstance(compiled_children, list):
        compiled_children = []
    normalised_children: list[dict] = []
    for child in compiled_children[:120]:
        if not isinstance(child, dict):
            continue
        title = str(child.get("title") or "").strip()[:120]
        check_rule = str(child.get("check_rule") or title).strip()[:520]
        if not title and not check_rule:
            continue
        source_units = child.get("source_unit_ids")
        if not isinstance(source_units, list):
            source_units = []
        child_requirements = child.get("evidence_requirements")
        if not isinstance(child_requirements, list):
            child_requirements = []
        child_requirements = [
            str(item) for item in child_requirements if str(item) in _RULE_EVIDENCE_TYPES
        ]
        child_baseline_ocr_mode = str(child.get("baseline_ocr_mode") or "auto")
        if child_baseline_ocr_mode not in _BASELINE_OCR_MODES:
            child_baseline_ocr_mode = "auto"
        page = child.get("source_page")
        normalised_children.append({
            "category": str(child.get("category") or "") if str(child.get("category") or "") in {"qualification", "compliance", "substantive", "rejection", "other"} else "",
            "title": title or check_rule[:120],
            "verification_target": str(child.get("verification_target") or "").strip()[:240],
            "check_rule": check_rule,
            "source_page": page if isinstance(page, int) and page > 0 else None,
            "source_unit_ids": list(dict.fromkeys(str(item).strip() for item in source_units if str(item).strip()))[:24],
            "rejection_clause_ids": list(dict.fromkeys(
                item for raw in child.get("rejection_clause_ids") or []
                if (item := str(raw).strip()).startswith("RC-")
            ))[:48],
            "rejection_clauses": _normalise_rejection_clauses(child.get("rejection_clauses")),
            "decision_impact_source": str(child.get("decision_impact_source") or "") if str(child.get("decision_impact_source") or "") in {"", "rule_category", "legacy_source_fallback", "rc_ledger"} else "",
            # 子项的取证语义必须随汇总规则一并落库。它当前主要供全文扫描和
            # 后续图片取证规划读取；缺失时仍按父规则安全回退。
            "evidence_requirements": list(dict.fromkeys(child_requirements)),
            "ocr_required": bool(child.get("ocr_required")),
            "baseline_ocr_mode": child_baseline_ocr_mode,
        })
    return {
        "execution_strategy": strategy if strategy in _RULE_EXECUTION_STRATEGIES else "",
        "evidence_requirements": list(dict.fromkeys(requirements)),
        "applicability": applicability,
        "vision_trigger": trigger,
        "vision_level": level,
        "image_mode": image_mode,
        "acquisition_preset": acquisition_preset,
        "baseline_ocr_mode": baseline_ocr_mode,
        "evidence_items": _normalise_evidence_items(value.get("evidence_items")),
        "source_clause_ids": list(dict.fromkeys(clause_ids)),
        "source_fact_ids": list(dict.fromkeys(fact_ids)),
        "source_unit_ids": list(dict.fromkeys(source_unit_ids)),
        "rejection_clause_ids": rejection_clause_ids,
        "rejection_clauses": rejection_clauses,
        "decision_impact_source": decision_impact_source,
        "verification_target": verification_target,
        "verifiability": verifiability,
        "source_locations": normalised_locations,
        "score_sections": normalised_sections,
        "compiled_group_id": compiled_group_id,
        "compiled_categories": list(dict.fromkeys(compiled_categories)),
        "compiled_child_requirements": normalised_children,
    }


def _execution_meta_json(payload: dict, *, fallback: dict | None = None) -> str | None:
    base = rule_execution_meta(fallback or {})
    strategy = str(payload.get("execution_strategy", base["execution_strategy"]) or "").strip()
    requirements = payload.get("evidence_requirements", base["evidence_requirements"])
    if not isinstance(requirements, list):
        requirements = base["evidence_requirements"]
    normalized = [str(item) for item in requirements if str(item) in _RULE_EVIDENCE_TYPES]
    # “材料本体/关键字段”通常先依赖可读文字或 OCR；显式补齐 text 只影响取证路由，
    # 不把它们误当成独立审查规则，也不改变旧 visual 选择。
    if any(item in normalized for item in {"document", "field"}) and "text" not in normalized:
        normalized.append("text")
    if payload.get("ocr_required") or payload.get("check_mode") == "ocr":
        if "visual" not in normalized:
            normalized.append("visual")
        # “需 OCR”不是纯图片外观结论：它至少还需要可读文字。保留 visual 以便
        # 用户要求时继续交给多模态核验，同时让腾讯关闭后的 RapidOCR 能正常接管。
        if "text" not in normalized:
            normalized.append("text")
    applicability = payload.get("applicability", base["applicability"])
    if not isinstance(applicability, dict):
        applicability = {}
    preset_value = payload.get("acquisition_preset")
    preset = str(preset_value if preset_value is not None else base["acquisition_preset"] or "smart")
    if preset not in _ACQUISITION_PRESETS:
        raise ValueError("图片取证策略不正确")
    # 用户明确选择“智能/扫描文字”策略时，将扫描件取证表述为通用的
    # “材料本体＋关键字段”需求。旧规则没有 acquisition_preset 时绝不迁移，
    # 从而避免升级后改变其既有视觉优先语义。
    explicit_text_policy = preset_value is not None and preset in {"smart", "always", "text", "dual"}
    explicit_ocr_path = payload.get("ocr_required") or payload.get("check_mode") == "ocr" or (
        preset in {"smart", "always", "text", "dual"} and str(payload.get("image_mode") or base["image_mode"] or "") in {"auto", "ocr_only", "combined"}
    )
    if explicit_text_policy and explicit_ocr_path:
        # 旧 OCR 勾选会先放入 visual/text；新策略还需要明确表达“材料本体＋关键字段”，
        # 让计划器能在不识别具体行业名的前提下保守地选择 OCR 优先。保留 visual
        # 是有意的：它仍允许在文字不足时回退到多模态外观核验。
        for requirement in ("document", "field", "text"):
            if requirement not in normalized:
                normalized.append(requirement)
    trigger_value = payload.get("vision_trigger")
    trigger = str(trigger_value if trigger_value is not None else base["vision_trigger"] or "off")
    # 新建或重新标记为 OCR 的规则，默认只表达“图片可能是决定性证据”；强度仍为 off，
    # 因而绝不会在升级后自动增加图片调用。人工可再选文字兜底/必须识图和具体强度。
    if trigger_value is None and trigger == "off" and (
        "visual" in normalized or payload.get("ocr_required") or payload.get("check_mode") == "ocr"
    ):
        # 兼容既有“需 OCR”规则的默认口径：只新增文字证据维度，不能改变它原先
        # “必须图片核验、待人工选择强度”的触发语义。
        trigger = "required" if (payload.get("ocr_required") or payload.get("check_mode") == "ocr") else (
            "text_fallback" if "text" in normalized else "required"
        )
    level = str(payload.get("vision_level", base["vision_level"]) or "off")
    image_mode = str(payload.get("image_mode", base["image_mode"]) or "auto")
    baseline_ocr_mode = str(payload.get("baseline_ocr_mode", base["baseline_ocr_mode"]) or "auto")
    # 前台选择业务预设时明确生成旧字段；仅保存旧字段的历史/API调用保持原样。
    if preset_value is not None and preset != "custom":
        image_mode = {"smart": "auto", "always": "auto", "text": "ocr_only", "visual": "vision_only", "dual": "combined", "off": "off"}[preset]
        if "vision_trigger" not in payload:
            trigger = "required" if preset in {"always", "visual", "dual"} else "text_fallback" if preset in {"smart", "text"} else "off"
        if "vision_level" not in payload:
            level = "standard" if preset != "off" else "off"
    if trigger not in _VISION_TRIGGERS:
        raise ValueError("图片识别条件不正确")
    if level not in _VISION_LEVELS:
        raise ValueError("图片识别强度不正确")
    if image_mode not in _IMAGE_MODES:
        raise ValueError("图片取证方式不正确")
    if baseline_ocr_mode not in _BASELINE_OCR_MODES:
        raise ValueError("基础核验方式不正确")
    if trigger == "off":
        level = "off"
    if image_mode == "off":
        trigger = "off"
        level = "off"
    clause_ids = payload.get("source_clause_ids", base.get("source_clause_ids"))
    if not isinstance(clause_ids, list):
        clause_ids = []
    clause_ids = [str(item).strip() for item in clause_ids if str(item).strip()]
    fact_ids = payload.get("source_fact_ids", base.get("source_fact_ids"))
    if not isinstance(fact_ids, list):
        fact_ids = []
    fact_ids = [str(item).strip() for item in fact_ids if str(item).strip()]
    source_unit_ids = payload.get("source_unit_ids", base.get("source_unit_ids"))
    if not isinstance(source_unit_ids, list):
        source_unit_ids = []
    source_unit_ids = [str(item).strip() for item in source_unit_ids if str(item).strip()]
    rejection_clause_ids = payload.get("rejection_clause_ids", base.get("rejection_clause_ids"))
    if not isinstance(rejection_clause_ids, list):
        rejection_clause_ids = []
    rejection_clause_ids = list(dict.fromkeys(
        item for raw in rejection_clause_ids if (item := str(raw).strip()).startswith("RC-")
    ))[:48]
    rejection_clauses = _normalise_rejection_clauses(payload.get("rejection_clauses", base.get("rejection_clauses")))
    if rejection_clauses:
        rejection_clause_ids = [item["clause_id"] for item in rejection_clauses]
    decision_impact_source = str(payload.get("decision_impact_source", base.get("decision_impact_source")) or "").strip()
    if decision_impact_source not in {"", "rule_category", "legacy_source_fallback", "rc_ledger"}:
        decision_impact_source = ""
    verification_target = str(payload.get("verification_target", base.get("verification_target")) or "").strip()
    verifiability = str(payload.get("verifiability", base.get("verifiability")) or "").strip()
    if verifiability not in {"single_bid", "cross_bid", "external_procedure"}:
        verifiability = ""
    source_locations = payload.get("source_locations", base.get("source_locations"))
    if not isinstance(source_locations, list):
        source_locations = []
    normalised_locations: list[dict] = []
    for location in source_locations:
        if not isinstance(location, dict):
            continue
        page = location.get("page")
        if not isinstance(page, int) or page <= 0:
            continue
        value_location = {"page": page}
        if value_location not in normalised_locations:
            normalised_locations.append(value_location)
    sections = payload.get("score_sections", base.get("score_sections"))
    if not isinstance(sections, list):
        sections = []
    normalised_sections: list[dict] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "").strip()
        if not section_id or section_id in {item["section_id"] for item in normalised_sections}:
            continue
        try:
            max_score = float(section.get("max_score"))
        except (TypeError, ValueError):
            max_score = None
        page = section.get("source_page")
        normalised_sections.append({
            "section_id": section_id,
            "label": str(section.get("label") or "评分分部").strip() or "评分分部",
            "max_score": max_score if max_score is not None and max_score > 0 else None,
            "source_page": page if isinstance(page, int) and page > 0 else None,
        })
    compiled_group_id = str(payload.get("compiled_group_id", base.get("compiled_group_id")) or "").strip()[:80]
    compiled_categories = payload.get("compiled_categories", base.get("compiled_categories"))
    if not isinstance(compiled_categories, list):
        compiled_categories = []
    compiled_categories = [
        str(item) for item in compiled_categories
        if str(item) in {"qualification", "compliance", "substantive", "rejection", "other"}
    ]
    compiled_children = payload.get("compiled_child_requirements", base.get("compiled_child_requirements"))
    if not isinstance(compiled_children, list):
        compiled_children = []
    normalised_children: list[dict] = []
    for child in compiled_children[:120]:
        if not isinstance(child, dict):
            continue
        title = str(child.get("title") or "").strip()[:120]
        check_rule = str(child.get("check_rule") or title).strip()[:520]
        if not title and not check_rule:
            continue
        source_units = child.get("source_unit_ids")
        if not isinstance(source_units, list):
            source_units = []
        child_requirements = child.get("evidence_requirements")
        if not isinstance(child_requirements, list):
            child_requirements = []
        child_requirements = [
            str(item) for item in child_requirements if str(item) in _RULE_EVIDENCE_TYPES
        ]
        child_baseline_ocr_mode = str(child.get("baseline_ocr_mode") or "auto")
        if child_baseline_ocr_mode not in _BASELINE_OCR_MODES:
            child_baseline_ocr_mode = "auto"
        page = child.get("source_page")
        normalised_children.append({
            "category": str(child.get("category") or "") if str(child.get("category") or "") in {"qualification", "compliance", "substantive", "rejection", "other"} else "",
            "title": title or check_rule[:120],
            "verification_target": str(child.get("verification_target") or "").strip()[:240],
            "check_rule": check_rule,
            "source_page": page if isinstance(page, int) and page > 0 else None,
            "source_unit_ids": list(dict.fromkeys(str(item).strip() for item in source_units if str(item).strip()))[:24],
            "rejection_clause_ids": list(dict.fromkeys(
                item for raw in child.get("rejection_clause_ids") or []
                if (item := str(raw).strip()).startswith("RC-")
            ))[:48],
            "rejection_clauses": _normalise_rejection_clauses(child.get("rejection_clauses")),
            "decision_impact_source": str(child.get("decision_impact_source") or "") if str(child.get("decision_impact_source") or "") in {"", "rule_category", "legacy_source_fallback", "rc_ledger"} else "",
            "evidence_requirements": list(dict.fromkeys(child_requirements)),
            "ocr_required": bool(child.get("ocr_required")),
            "baseline_ocr_mode": child_baseline_ocr_mode,
        })
    value = {
        "execution_strategy": strategy if strategy in _RULE_EXECUTION_STRATEGIES else "",
        "evidence_requirements": normalized,
        "applicability": applicability,
        "vision_trigger": trigger,
        "vision_level": level,
        "image_mode": image_mode,
        "acquisition_preset": preset,
        "baseline_ocr_mode": baseline_ocr_mode,
        "evidence_items": _normalise_evidence_items(payload.get("evidence_items", base.get("evidence_items"))),
        "source_clause_ids": list(dict.fromkeys(clause_ids)),
        "source_fact_ids": list(dict.fromkeys(fact_ids)),
        "source_unit_ids": list(dict.fromkeys(source_unit_ids)),
        "rejection_clause_ids": rejection_clause_ids,
        "rejection_clauses": rejection_clauses,
        "decision_impact_source": decision_impact_source,
        "verification_target": verification_target,
        "verifiability": verifiability,
        "source_locations": normalised_locations,
        "score_sections": normalised_sections,
        "compiled_group_id": compiled_group_id,
        "compiled_categories": list(dict.fromkeys(compiled_categories)),
        "compiled_child_requirements": normalised_children,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _rule_public_value(row: dict) -> dict:
    value = dict(row)
    meta = rule_execution_meta(value)
    value.update(meta)
    value["acquisition_recommendation"] = rule_acquisition_recommendation(value)
    return value


def list_rules(app, project_id: str) -> tuple[dict | None, list[dict]]:
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        return None, []
    with connection(app) as conn:
        rows = conn.execute("SELECT * FROM ew_rules WHERE rule_set_id = ? ORDER BY category, sort_order, created_at", (rule_set["rule_set_id"],)).fetchall()
    return rule_set, [_rule_public_value(dict(row)) for row in rows]


def add_rule(app, project_id: str, payload: dict) -> dict:
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("规则名称不能为空")
    rule_set = current_rule_set(app, project_id, create=True)
    if rule_set["status"] != "draft":
        rule_set = _clone_rule_set_as_draft(app, project_id, rule_set)
    category = str(payload.get("category", "substantive")).strip()
    if category not in {"qualification", "compliance", "substantive", "rejection", "other", "objective", "subjective"}:
        raise ValueError("不支持的规则分类")
    timestamp = now_iso()
    rule = {
        "rule_id": str(uuid.uuid4()), "rule_set_id": rule_set["rule_set_id"], "category": category,
        "title": title, "check_rule": str(payload.get("check_rule", "")).strip() or title,
        "source_text": str(payload.get("source_text", "")).strip(),
        "source_page": int(payload["source_page"]) if str(payload.get("source_page", "")).isdigit() else None,
        "check_mode": "ocr" if payload.get("ocr_required") or payload.get("check_mode") == "ocr" else "auto",
        "source_type": "manual", "source_task_id": None,
        "scoring_json": json.dumps(payload.get("scoring"), ensure_ascii=False) if payload.get("scoring") else None,
        "execution_meta_json": _execution_meta_json(payload),
        "enabled": 1, "sort_order": int(payload.get("sort_order") or 0), "created_at": timestamp, "updated_at": timestamp,
    }
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
            source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at)
            VALUES (:rule_id, :rule_set_id, :category, :title, :check_rule, :source_text, :source_page, :check_mode,
            :source_type, :source_task_id, :scoring_json, :execution_meta_json, :enabled, :sort_order, :created_at, :updated_at)""", rule,
        )
        conn.execute("UPDATE ew_rule_sets SET updated_at = ? WHERE rule_set_id = ?", (timestamp, rule_set["rule_set_id"]))
    return rule


def _clone_rule_set_as_draft(app, project_id: str, source_rule_set: dict) -> dict:
    timestamp = now_iso()
    with connection(app) as conn:
        version = (conn.execute("SELECT MAX(version) FROM ew_rule_sets WHERE project_id = ?", (project_id,)).fetchone()[0] or 0) + 1
        draft = {"rule_set_id": str(uuid.uuid4()), "project_id": project_id, "version": version, "status": "draft", "created_at": timestamp, "updated_at": timestamp}
        conn.execute("INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, created_at, updated_at) VALUES (:rule_set_id, :project_id, :version, :status, :created_at, :updated_at)", draft)
        rows = conn.execute("SELECT * FROM ew_rules WHERE rule_set_id = ? ORDER BY sort_order, created_at", (source_rule_set["rule_set_id"],)).fetchall()
        for row in rows:
            conn.execute(
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
                   source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), draft["rule_set_id"], row["category"], row["title"], row["check_rule"] or row["title"], row["source_text"], row["source_page"],
                 "ocr" if row["check_mode"] == "ocr" else "auto", row["source_type"] or "manual", row["source_task_id"], row["scoring_json"], row["execution_meta_json"], row["enabled"], row["sort_order"], timestamp, timestamp),
            )
    return draft


def delete_rule(app, project_id: str, rule_id: str) -> None:
    rule_set = current_rule_set(app, project_id)
    if not rule_set or rule_set["status"] != "draft":
        raise ValueError("只能删除待确认规则集中的规则")
    with connection(app) as conn:
        deleted = conn.execute("DELETE FROM ew_rules WHERE rule_set_id = ? AND rule_id = ?", (rule_set["rule_set_id"], rule_id)).rowcount
        if not deleted:
            raise ValueError("规则不存在")
        conn.execute("UPDATE ew_rule_sets SET updated_at = ? WHERE rule_set_id = ?", (now_iso(), rule_set["rule_set_id"]))


def infer_max_score(source_text: str) -> float | None:
    """只从规则原文中明确的总分/最高分提取，不猜测分档计算。"""
    text = str(source_text or "")
    values = [float(item) for item in _SCORE_TOTAL_PATTERN.findall(text)]
    if not values:
        values = [float(item) for item in _SCORE_VALUE_PATTERN.findall(text)]
    values = [item for item in values if math.isfinite(item) and item > 0]
    return max(values) if values else None


def _valid_max_score(scoring: dict | None) -> float | None:
    try:
        value = float((scoring or {}).get("max_score"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _score_items_total(scoring: dict | None) -> float | None:
    """评分叶子项正分合计；负分项（扣分项）不计入。"""
    items = scoring.get("items") if isinstance(scoring, dict) else None
    if not isinstance(items, list) or not items:
        return None
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            return None
        try:
            value = float(item.get("max_score"))
        except (TypeError, ValueError):
            return None
        if value > 0:
            total += value
    return total


def _is_counting_score_rule(scoring: dict | None) -> bool:
    """计数型评分豁免：每提供一份/一项得 X 分、最高 Y 分的规则，
    其叶子可能只写单份分值而小于满分，属合法结构，不做“截断”误报。"""
    items = scoring.get("items") if isinstance(scoring, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        return False
    criterion = str(items[0].get("criterion") or "") if isinstance(items[0], dict) else ""
    if "每" not in criterion or "得" not in criterion:
        return False
    return bool(re.search(r"(?:最高|最多|满分|上限)[^。；;]{0,8}\d+(?:\.\d+)?\s*分", criterion))


def complete_missing_rule_scores(app, rule_set_id: str) -> int:
    """为 AI 漏填但原文有明确总分的评分项补齐满分。"""
    updated = 0
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT rule_id, category, source_text, scoring_json FROM ew_rules WHERE rule_set_id = ? AND enabled = 1 AND category IN ('objective', 'subjective')",
            (rule_set_id,),
        ).fetchall()
        for row in rows:
            try:
                scoring = json.loads(row["scoring_json"] or "{}")
            except json.JSONDecodeError:
                scoring = {}
            if _valid_max_score(scoring) is not None:
                continue
            inferred = infer_max_score(row["source_text"])
            if inferred is None:
                continue
            scoring = {
                **(scoring if isinstance(scoring, dict) else {}),
                "max_score": inferred,
                "source": "source_text_inferred",
            }
            if row["category"] == "objective":
                scoring.setdefault("kind", "manual")
            conn.execute(
                "UPDATE ew_rules SET scoring_json = ?, updated_at = ? WHERE rule_id = ?",
                (json.dumps(scoring, ensure_ascii=False), now_iso(), row["rule_id"]),
            )
            updated += 1
    return updated


def update_rule(app, project_id: str, rule_id: str, payload: dict) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        raise ValueError("当前没有可修改的规则集")
    # 已确认规则集只开放“启用/停用”这一纯执行过滤操作：可逆、不改历史结论，
    # 且修改会刷新规则集版本，使后续综合评审不复用包含旧启用状态的结果缓存。
    # 规则内容、评分口径与取证策略仍必须回到待确认规则集修改。
    only_enabled = set(payload) <= {"enabled"}
    if rule_set["status"] not in {"draft", "confirmed"} or (
        rule_set["status"] == "confirmed" and not only_enabled
    ):
        raise ValueError("已确认规则集只能调整启用状态；修改规则内容请先重新提取规则")
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_rules WHERE rule_id = ? AND rule_set_id = ?", (rule_id, rule_set["rule_set_id"])).fetchone()
        if not row:
            raise ValueError("规则不存在")
        rule = dict(row)
        check_rule = None
        if "check_rule" in payload:
            check_rule = str(payload.get("check_rule") or "").strip()
            if not check_rule:
                raise ValueError("检查规则不能为空")
        enabled = None
        if "enabled" in payload:
            if not isinstance(payload.get("enabled"), bool):
                raise ValueError("启用状态必须为布尔值")
            enabled = 1 if payload["enabled"] else 0
        scoring_json = None
        if "scoring" in payload:
            if rule["category"] not in {"objective", "subjective"}:
                raise ValueError("只有评分规则可以修改满分")
            scoring = payload.get("scoring") if isinstance(payload.get("scoring"), dict) else {}
            max_score = _valid_max_score(scoring)
            if max_score is None:
                raise ValueError("请填写大于 0 的有效满分")
            current = json.loads(rule["scoring_json"] or "{}") if rule["scoring_json"] else {}
            current.update({"max_score": max_score, "source": "manual"})
            if rule["category"] == "objective":
                current["kind"] = "boolean" if scoring.get("kind") == "boolean" else "manual"
            scoring_json = json.dumps(current, ensure_ascii=False)
        # 规则内容与评分口径才属于“人工编辑的业务口径”。启用勾选和取证策略只是
        # 当前一轮的执行参数：重新提取后应由新规则的证据要求重新生成，不能因为
        # 调整过 OCR/图片策略就把整条 AI 规则永久固化为 ai_edited。后者会使历史
        # AI 规则跨版本不断继承，是重复规则累积的根源之一。
        execution_meta_keys = (
            "execution_strategy", "evidence_requirements", "applicability", "ocr_required", "check_mode",
            "image_mode", "vision_trigger", "vision_level", "acquisition_preset", "evidence_items",
            "baseline_ocr_mode",
        )
        execution_meta_changed = any(key in payload for key in execution_meta_keys)
        content_locked = rule.get("source_type") in {"ai", "ai_locked"} and (
            check_rule is not None or scoring_json is not None
        )
        if check_rule is not None:
            conn.execute(
                "UPDATE ew_rules SET check_rule = ?, source_type = CASE WHEN ? THEN 'ai_edited' ELSE source_type END, updated_at = ? WHERE rule_id = ?",
                (check_rule, 1 if content_locked else 0, now_iso(), rule_id),
            )
        if scoring_json is not None:
            conn.execute(
                "UPDATE ew_rules SET scoring_json = ?, source_type = CASE WHEN ? THEN 'ai_edited' ELSE source_type END, updated_at = ? WHERE rule_id = ?",
                (scoring_json, 1 if content_locked else 0, now_iso(), rule_id),
            )
        if enabled is not None:
            conn.execute(
                "UPDATE ew_rules SET enabled = ?, updated_at = ? WHERE rule_id = ?",
                (enabled, now_iso(), rule_id),
            )
        if execution_meta_changed:
            conn.execute(
                "UPDATE ew_rules SET execution_meta_json = ?, source_type = CASE WHEN ? THEN 'ai_edited' ELSE source_type END, updated_at = ? WHERE rule_id = ?",
                (_execution_meta_json(payload, fallback=rule), 1 if content_locked else 0, now_iso(), rule_id),
            )
        conn.execute("UPDATE ew_rule_sets SET updated_at = ? WHERE rule_set_id = ?", (now_iso(), rule_set["rule_set_id"]))
        updated = conn.execute("SELECT * FROM ew_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    return _rule_public_value(dict(updated))


def replace_rules_from_extraction(app, project_id: str, task_id: str, rules: list[dict]) -> dict:
    with connection(app) as conn:
        # “重新提取”就是一次全新 AI 规则生成：旧 AI 规则、AI 编辑、勾选、取证
        # 策略和模型结论都不参与本轮。只有人工补充规则属于独立业务口径，允许
        # 进入新草稿；历史 AI 规则保留在旧版本用于审计，但绝不混入当前草稿。
        current = conn.execute(
            """SELECT rule_set_id FROM ew_rule_sets WHERE project_id=?
               AND status IN ('confirmed', 'draft') ORDER BY version DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        manual_rules = []
        if current:
            manual_rules = conn.execute(
                """SELECT * FROM ew_rules WHERE rule_set_id=? AND source_type='manual'
                   ORDER BY sort_order, created_at""",
                (current["rule_set_id"],),
            ).fetchall()
        prior = conn.execute("SELECT MAX(version) FROM ew_rule_sets WHERE project_id = ?", (project_id,)).fetchone()[0] or 0
        timestamp = now_iso()
        rule_set = {"rule_set_id": str(uuid.uuid4()), "project_id": project_id, "version": prior + 1, "status": "draft", "source_task_id": task_id, "created_at": timestamp, "updated_at": timestamp}
        conn.execute("UPDATE ew_rule_sets SET status = 'superseded', updated_at = ? WHERE project_id = ? AND status != 'superseded'", (timestamp, project_id))
        # 规则集已更换，所有综合评审产物都属于旧规则语境；不保留在页面上混看。
        conn.execute("DELETE FROM ew_review_runs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_score_runs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_evidence_packs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM ew_evaluation_unit_checkpoints WHERE project_id=?", (project_id,))
        conn.execute("INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, source_task_id, created_at, updated_at) VALUES (:rule_set_id, :project_id, :version, :status, :source_task_id, :created_at, :updated_at)", rule_set)
        signatures = set()
        next_sort_order = 0
        preserved_rule_count = 0
        for row in manual_rules:
            signature = (
                row["category"], re.sub(r"\s+", "", row["title"]).casefold(),
                re.sub(r"\s+", "", row["check_rule"] or row["title"]).casefold(),
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            conn.execute(
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
                   source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], row["category"], row["title"], row["check_rule"] or row["title"],
                 row["source_text"], row["source_page"], "ocr" if row["check_mode"] == "ocr" else "auto",
                 row["source_task_id"], row["scoring_json"], row["execution_meta_json"], int(bool(row["enabled"])),
                 next_sort_order, timestamp, timestamp),
            )
            next_sort_order += 1
            preserved_rule_count += 1
        for item in rules:
            title = str(item.get("title", "")).strip()
            category = str(item.get("category", "")).strip()
            if not title or category not in {"qualification", "compliance", "substantive", "rejection", "other", "objective", "subjective"}:
                continue
            check_rule = str(item.get("check_rule", "")).strip() or title
            signature = (category, re.sub(r"\s+", "", title).casefold(), re.sub(r"\s+", "", check_rule).casefold())
            if signature in signatures:
                continue
            signatures.add(signature)
            conn.execute(
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode, source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], category, title, check_rule, str(item.get("source_text", "")).strip(),
                 item.get("source_page") if isinstance(item.get("source_page"), int) else None,
                 "ocr" if item.get("ocr_required") or item.get("check_mode") == "ocr" else "auto",
                 task_id, json.dumps(item.get("scoring"), ensure_ascii=False) if item.get("scoring") else None, _execution_meta_json(item),
                 0 if item.get("ocr_required") or item.get("check_mode") == "ocr" else 1,
                 next_sort_order, timestamp, timestamp),
            )
            next_sort_order += 1
        global_rule_count = 0
        global_rules = conn.execute(
            "SELECT * FROM ew_global_rules ORDER BY category, sort_order, created_at"
        ).fetchall()
        for template in global_rules:
            signature = (
                template["category"], re.sub(r"\s+", "", template["title"]).casefold(),
                re.sub(r"\s+", "", template["check_rule"]).casefold(),
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            conn.execute(
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
                   source_type, source_task_id, scoring_json, execution_meta_json, enabled, sort_order, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'global', NULL, NULL, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], template["category"], template["title"], template["check_rule"],
                 template["source_text"], template["check_mode"], template["execution_meta_json"],
                 int(bool(template["enabled"])), next_sort_order, timestamp, timestamp),
            )
            global_rule_count += 1
            next_sort_order += 1
        rule_set["global_rule_count"] = global_rule_count
        rule_set["preserved_rule_count"] = preserved_rule_count
    # 当前轮内部仍可能因模型分段输出产生评分副本；只对可证明重复的规则做可逆
    # 停用，避免同一评分事实执行两次。
    rule_set["auto_merged_score_rule_count"] = merge_draft_score_rule_duplicates(app, rule_set["rule_set_id"])
    return rule_set


def _rule_title_identity(value: object, category: object = "") -> str:
    """规则对象的稳定标题键，忽略章节导航与机器附注的条款编号。

    该键只用于“标题对象是否一致”的第一道门，不能单独认定同一规则；后续仍要求
    原文或条款 ID 同源，避免把不同的证书、参数或资格要求错误合并。
    """
    text = str(value or "")
    # 模型会把锚点写成“（SC-37-2，35分）”一类尾注；它是来源索引而不是规则对象。
    text = re.sub(
        r"[（(]\s*[A-Za-z]{1,16}[A-Za-z0-9_#-]*(?:\s*[-_]\s*[A-Za-z0-9_#-]+)*(?:\s*[,，;；:]\s*\d+(?:\.\d+)?\s*分)?\s*[)）]\s*$",
        "", text,
    )
    if str(category or "") in {"objective", "subjective"}:
        return _score_rule_title_core(text)
    text = re.sub(r"^\s*(?:(?:商务|技术|价格|服务|资格|符合性|实质性)部分|(?:资格性|符合性|实质性|废标|其他)审查)\s*[-—–:：_]\s*", "", text)
    return re.sub(r"[\s\W_]+", "", text).casefold()


def _rule_titles_compatible(left: object, right: object, category: object) -> bool:
    first = _rule_title_identity(left, category)
    second = _rule_title_identity(right, category)
    if not first or not second:
        return False
    return first == second or (min(len(first), len(second)) >= 6 and (first in second or second in first))


def confirm_rule_set(app, project_id: str) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        raise ValueError("当前没有可确认的规则集")
    # 先清理旧版本/异常模型输出中被误标为评分项的“评审过程规则”，再补齐真正
    # 评分条款的明确满分。这样不会让异常低价解释等事项以 0 分规则阻塞确认。
    disable_non_file_scoring_process_rules(app, rule_set["rule_set_id"])
    complete_missing_rule_scores(app, rule_set["rule_set_id"])
    # 确认前把“同一计分事实被评分表不同章节/截断副本重复成多条”的评分规则安全合并，
    # 避免重复计分导致总分虚高；只停用次规则、保留信息更全者，不删除记录。
    merge_draft_score_rule_duplicates(app, rule_set["rule_set_id"])
    # 总分、叶子合计和评分台账覆盖是重要的人工核对提醒，不应因模型提取未完整而
    # 阻断后续人工评审；但同一原文被重复占用或 AI 评分项完全无原文锚点会导致
    # 重复/无依据计分，仍须先在规则卡片中人工处理。
    validation = rule_set_acquisition_validation(app, project_id)
    blockers = [
        item for item in validation.get("issues", [])
        if item.get("code") in {"duplicate_score_rule", "score_source_conflict", "score_source_unmapped"}
    ]
    if blockers:
        details = "；".join(str(item.get("message") or "评分规则校验失败") for item in blockers[:3])
        if len(blockers) > 3:
            details += f"；另有{len(blockers) - 3}项"
        raise ValueError(f"当前评分规则集未通过确认校验：{details}")
    with connection(app) as conn:
        count = conn.execute("SELECT COUNT(*) FROM ew_rules WHERE rule_set_id = ? AND enabled = 1", (rule_set["rule_set_id"],)).fetchone()[0]
        if not count:
            raise ValueError("当前规则集没有可确认的规则")
        scoring_rows = conn.execute("SELECT title, scoring_json FROM ew_rules WHERE rule_set_id = ? AND enabled = 1 AND category IN ('objective', 'subjective')", (rule_set["rule_set_id"],)).fetchall()
        for row in scoring_rows:
            try:
                scoring = json.loads(row["scoring_json"] or "{}")
                max_score = float(scoring.get("max_score"))
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                raise ValueError(f"评分规则“{row['title']}”缺少有效满分，请在规则表中补充满分后再确认")
            if not math.isfinite(max_score) or max_score <= 0:
                raise ValueError(f"评分规则“{row['title']}”的满分必须大于 0")
        conn.execute("UPDATE ew_rule_sets SET status = 'superseded', updated_at = ? WHERE project_id = ? AND rule_set_id != ? AND status = 'confirmed'", (now_iso(), project_id, rule_set["rule_set_id"]))
        conn.execute("UPDATE ew_rule_sets SET status = 'confirmed', updated_at = ? WHERE rule_set_id = ?", (now_iso(), rule_set["rule_set_id"]))
    return current_rule_set(app, project_id)


def _score_rule_title_core(value: object) -> str:
    """预检用的评分对象名归一化：去分值括注与章节导航前缀，与提取去重口径一致。"""
    text = str(value or "")
    # “（SC-37-2，35分）”之类尾注是模型/分段提取附加的条款定位信息，不是评分
    # 对象本身。先剥离它，才能把同一评分条款的带锚点/不带锚点副本稳定归并。
    text = re.sub(
        r"[（(]\s*[A-Za-z]{1,16}[A-Za-z0-9_#-]*(?:\s*[-_]\s*[A-Za-z0-9_#-]+)*(?:\s*[,，;；:]\s*\d+(?:\.\d+)?\s*分)?\s*[)）]\s*$",
        "", text,
    )
    text = re.sub(r"[（(][^()（）]*\d+(?:\.\d+)?\s*分[^()（）]*[)）]\s*$", "", text)
    text = re.sub(r"(?:[-—–:：\s]*)(?:满分|最高(?:得)?分?)\s*\d+(?:\.\d+)?\s*分?", "", text)
    # 章节导航前缀必须带分隔符（如“商务部分-企业业绩评分”“技术部分：货物指标”）
    # 才剥离；“价格部分评分”“技术部分评分”这类以部分名开头的完整标题本身没有
    # 导航分隔符，保留原样，避免被剥成“评分”后因过短而无法参与合并/预检。
    text = re.sub(
        r"^\s*(?:(?:商务|技术|价格|服务|资格)部分|(?:客观|主观)?评分|评分项?)\s*[-—–:：_]\s*",
        "", text,
    )
    # “投标报价得分计算”“价格评分公式”只是同一评分对象的计算口径表述，不能
    # 因尾部动作词不同成为两条可执行评分规则。
    text = re.sub(r"(?:得分|评分)(?:计算|公式)\s*$", lambda match: match.group(0)[:2], text)
    return re.sub(r"[\s\W_]+", "", text).casefold()


def _score_sources_compatible(left: object, right: object) -> bool:
    """两段评分原文是否同源：一方为另一方去省略号后的完整前缀（截断副本）。

    与提取管线口径一致；确认前合并与提取去重共用同一守卫，避免把原文真正不同
    的评分项误并。
    """
    def normalised(value: object) -> str:
        text = re.sub(r"(?:…|\.\.\.|……)+", "", str(value or ""))
        return re.sub(r"[\s\W_]+", "", text).casefold()

    shorter, longer = sorted((normalised(left), normalised(right)), key=len)
    return len(shorter) >= 20 and longer.startswith(shorter)


def _normalise_rule_source(value: object) -> str:
    text = re.sub(r"(?:…|\.\.\.|……)+", "", str(value or ""))
    return re.sub(r"[\s\W_]+", "", text).casefold()


def _score_sources_contain(left: object, right: object) -> bool:
    """较短原文（≥30 归一字符）完整出现在较长原文中时视为同源。

    覆盖“模型给同一评分项加了章节前缀/截断中间段”的跨代表述漂移；调用方负责
    排除出现在多条规则中的公共模板句，避免把“上述方案无缺陷得X分…”类共用尾句
    误当成合并证据。
    """
    shorter, longer = sorted((_normalise_rule_source(left), _normalise_rule_source(right)), key=len)
    return len(shorter) >= 30 and shorter in longer


def _score_rule_object_core(value: object) -> str:
    """评分对象核心词：去“评分/评审/得分/方案/（含…）”等词尾与常见修饰。"""
    text = _score_rule_title_core(value)
    text = re.sub(r"(?:响应|与偏离|对照|核验|评审|评分|得分|方案|内容|情况|部分)$", "", text)
    text = re.sub(r"含[^（）()]*$", "", text)
    return text


def _score_object_core_compatible(left: str, right: str) -> bool:
    """对象核心词兼容：完全一致，或一方是另一方的完整前缀（措辞被扩展/缩写）。"""
    if not left or not right:
        return False
    return left == right or (len(left) >= 4 and (left.startswith(right) or right.startswith(left)))


def _score_scoring_structure_similar(left: dict | None, right: dict | None) -> bool:
    """叶子结构宽松匹配：归一叶子名后分值一致且至少半数命中，视为同一计分结构。"""
    def normalized_items(scoring: dict | None) -> list[tuple[str, float]]:
        items = scoring.get("items") if isinstance(scoring, dict) else None
        if not isinstance(items, list) or not items:
            return []
        values = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = re.sub(r"[\s\W_]+", "", str(item.get("name") or "")).casefold()
            name = re.sub(r"(?:评分|方案|括注|（[^）]*）|（[^)]*）|含[^（）()]*)$", "", name)
            try:
                score = float(item.get("max_score"))
            except (TypeError, ValueError):
                continue
            values.append((name, score))
        return values

    left_items = normalized_items(left)
    right_items = normalized_items(right)
    if not left_items or not right_items:
        return False
    right_map = dict(right_items)
    matched = sum(1 for name, score in left_items if name and right_map.get(name) is not None and abs(right_map[name] - score) <= 0.001)
    return matched >= max(1, (min(len(left_items), len(right_items)) + 1) // 2)


def _score_scoring_structure_key(scoring: dict | None) -> tuple | None:
    """评分叶子结构指纹：叶子名称、分值、条件归一后逐项一致才视为同一计分事实。"""
    items = scoring.get("items") if isinstance(scoring, dict) else None
    if not isinstance(items, list) or not items:
        return None
    values: list[tuple[str, float, str]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        name = re.sub(r"[\s\W_]+", "", str(item.get("name") or "")).casefold()
        try:
            score = float(item.get("max_score"))
        except (TypeError, ValueError):
            return None
        criterion = re.sub(r"[\s\W_]+", "", str(item.get("criterion") or "")).casefold()
        values.append((name, score, criterion))
    return tuple(values)


def _score_rule_richness(rule: dict) -> tuple[int, int, int]:
    try:
        scoring = json.loads(rule.get("scoring_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        scoring = {}
    items = scoring.get("items") if isinstance(scoring, dict) else []
    return (len(items) if isinstance(items, list) else 0,
            len(str(rule.get("check_rule") or "")),
            len(str(rule.get("source_text") or "")))


def _merge_rule_duplicate_fields(primary: dict, secondary: dict) -> dict:
    """合并同一评分事实的互补字段，选择信息更全者为主规则。"""
    merged = dict(primary)
    for key in ("check_rule", "source_text"):
        if len(str(secondary.get(key) or "")) > len(str(merged.get(key) or "")):
            merged[key] = secondary[key]
    if not merged.get("source_page"):
        merged["source_page"] = secondary.get("source_page")
    try:
        primary_scoring = json.loads(primary.get("scoring_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        primary_scoring = {}
    try:
        secondary_scoring = json.loads(secondary.get("scoring_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        secondary_scoring = {}
    if not isinstance(primary_scoring, dict):
        primary_scoring = {}
    if not isinstance(secondary_scoring, dict):
        secondary_scoring = {}
    if len(secondary_scoring.get("items") or []) > len(primary_scoring.get("items") or []):
        merged["scoring_json"] = json.dumps(secondary_scoring, ensure_ascii=False)
    return merged


def _rule_source_clause_ids(rule: dict) -> set[str]:
    try:
        meta = json.loads(rule.get("execution_meta_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    values = meta.get("source_clause_ids")
    return {str(item).strip() for item in values if str(item).strip()} if isinstance(values, list) else set()


def _score_source_ownership_conflicts(rules: list[dict]) -> list[dict]:
    """返回同一评分原文条款被多个启用评分规则占用的冲突。

    仅检查提取管线生成的 ``SC-`` 评分条款 ID。人工规则或资格条款不带此类
    锚点，不会被误报；同一条款落到多条启用规则则必然存在重复计分或父子分值
    归属不清，必须在确认前处理。
    """
    owners: dict[str, list[dict]] = {}
    for rule in rules:
        if not rule.get("enabled") or str(rule.get("category") or "") not in {"objective", "subjective"}:
            continue
        for clause_id in _rule_source_clause_ids(rule):
            if clause_id.startswith("SC-"):
                owners.setdefault(clause_id, []).append(rule)
    return [
        {"clause_id": clause_id, "rules": values}
        for clause_id, values in owners.items() if len(values) > 1
    ]


def _is_common_template_source(rule: dict, peers: list[dict]) -> bool:
    """较短原文是否在 3 条以上其他规则中作为子串出现（公共模板尾句）。"""
    source = _normalise_rule_source(rule.get("source_text"))
    if len(source) < 30:
        return False
    appearances = 0
    for peer in peers:
        if peer.get("rule_id") == rule.get("rule_id"):
            continue
        if source in _normalise_rule_source(peer.get("source_text")):
            appearances += 1
    return appearances >= 3


def merge_draft_score_rule_duplicates(app, rule_set_id: str) -> int:
    """安全合并重复评分规则（draft 与已确认规则集均可执行）。

    匹配采用多证据联合，全部为通用机制：
    1) 评分条款 ID 交集（提取时持久化的 source_clause_ids，同代确定性锚）；
    2) 原文同源（截断/完整前缀，或较短原文整体出现在较长原文中且非公共模板句）；
    3) 评分叶子结构逐项一致；
    4) 对象核心词相同且叶子结构半数以上匹配（覆盖标题措辞漂移）。
    命中任一证据即保留信息更全的一条、停用其余；不删除记录，可逆。
    """
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT * FROM ew_rules
               WHERE rule_set_id=? AND category IN ('objective', 'subjective') AND enabled=1
               ORDER BY sort_order, created_at""",
            (rule_set_id,),
        ).fetchall()
    rules = [dict(row) for row in rows]
    merged_count = 0
    with connection(app) as conn:
        for index, primary in enumerate(rules):
            if not primary.get("enabled"):
                continue
            primary_scoring = {}
            try:
                primary_scoring = json.loads(primary.get("scoring_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            if not isinstance(primary_scoring, dict):
                primary_scoring = {}
            primary_max = _valid_max_score(primary_scoring)
            primary_category = str(primary.get("category") or "")
            if primary_max is None:
                continue
            primary_ids = _rule_source_clause_ids(primary)
            primary_core = _score_rule_object_core(primary.get("title"))
            for other in rules[index + 1:]:
                if not other.get("enabled"):
                    continue
                try:
                    other_scoring = json.loads(other.get("scoring_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    other_scoring = {}
                if not isinstance(other_scoring, dict):
                    other_scoring = {}
                other_max = _valid_max_score(other_scoring)
                if other_max is None or abs(primary_max - other_max) > 0.001:
                    continue
                if str(other.get("category") or "") != primary_category:
                    continue
                other_ids = _rule_source_clause_ids(other)
                same_id = bool(primary_ids & other_ids)
                same_prefix = _score_sources_compatible(primary.get("source_text"), other.get("source_text"))
                contain_primary = _score_sources_contain(primary.get("source_text"), other.get("source_text"))
                contain_other = _score_sources_contain(other.get("source_text"), primary.get("source_text"))
                primary_common = _is_common_template_source(primary, rules)
                other_common = _is_common_template_source(other, rules)
                same_contain = (contain_primary and not other_common) or (contain_other and not primary_common)
                same_structure = (
                    _score_scoring_structure_key(primary_scoring) is not None
                    and _score_scoring_structure_key(primary_scoring) == _score_scoring_structure_key(other_scoring)
                )
                other_core = _score_rule_object_core(other.get("title"))
                same_core = _score_object_core_compatible(primary_core, other_core)
                similar_structure = _score_scoring_structure_similar(primary_scoring, other_scoring)
                if not (same_id or same_prefix or same_contain or same_structure or (same_core and similar_structure)):
                    continue
                primary, other = _merge_rule_pair(conn, primary, other)
                merged_count += 1
        # 无论是否实际合并都刷新规则集版本，使已确认规则集的后续评审缓存失效，
        # 避免“刚去重就复用旧结果”的误导。
        conn.execute(
            "UPDATE ew_rule_sets SET updated_at=? WHERE rule_set_id=?",
            (now_iso(), rule_set_id),
        )
    return merged_count


def _merge_rule_pair(conn, primary: dict, other: dict) -> tuple[dict, dict]:
    """合并两条规则：信息更全者为主，更新主规则字段并停用次规则。"""
    primary_edited = str(primary.get("source_type") or "") == "ai_edited"
    other_edited = str(other.get("source_type") or "") == "ai_edited"
    # 可证明重复时优先保留人工编辑过的规则；自动提取的较长文案只能补来源锚点，
    # 不得反向覆盖用户已经维护的检查口径或计分结构。
    if other_edited and not primary_edited:
        primary, other = other, primary
        primary_edited, other_edited = True, False
    elif not primary_edited and not other_edited and _score_rule_richness(other) > _score_rule_richness(primary):
        primary, other = other, primary
    if primary_edited:
        merged = dict(primary)
        if not merged.get("source_page"):
            merged["source_page"] = other.get("source_page")
    else:
        merged = _merge_rule_duplicate_fields(primary, other)
    try:
        primary_meta = json.loads(primary.get("execution_meta_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        primary_meta = {}
    try:
        other_meta = json.loads(other.get("execution_meta_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        other_meta = {}
    if not isinstance(primary_meta, dict):
        primary_meta = {}
    if not isinstance(other_meta, dict):
        other_meta = {}
    clause_ids = list(dict.fromkeys([
        *(primary_meta.get("source_clause_ids") or []),
        *(other_meta.get("source_clause_ids") or []),
    ]))
    primary_meta["source_clause_ids"] = [str(item) for item in clause_ids]
    conn.execute(
        """UPDATE ew_rules SET check_rule=?, source_text=?, source_page=?, scoring_json=?, execution_meta_json=?, updated_at=?
           WHERE rule_id=?""",
        (str(merged.get("check_rule") or ""), str(merged.get("source_text") or ""),
         merged.get("source_page"), str(merged.get("scoring_json") or primary.get("scoring_json") or "{}"),
         json.dumps(primary_meta, ensure_ascii=False), now_iso(), primary["rule_id"]),
    )
    conn.execute(
        "UPDATE ew_rules SET enabled=0, updated_at=? WHERE rule_id=?",
        (now_iso(), other["rule_id"]),
    )
    return merged, other


def _score_rule_max_value(rule: dict) -> float | None:
    scoring = rule.get("scoring")
    if not isinstance(scoring, dict):
        try:
            scoring = json.loads(rule.get("scoring_json") or "{}")
        except (AttributeError, TypeError, json.JSONDecodeError):
            return None
    try:
        value = float(scoring.get("max_score"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


_TENDER_TOTAL_SCORE_PATTERNS = (
    # “部分总分/条款满分”属于局部表述，用负向断言排除，只保留整体声明口径。
    re.compile(r"(?<![部项节则条])(?<!部分)总分\s*[为计（(]?\s*(\d+(?:\.\d+)?)\s*分"),
    re.compile(r"(?<![项条款节])满分\s*[为（(]?\s*(\d+(?:\.\d+)?)\s*分"),
)

_HARD_TENDER_ANCHOR_GROUPS = (
    # 期限数值必须由当前招标原文解释，不能把某个项目常见的 90 日历天写成
    # 通用锚点；这里只负责发现“投标有效期”这一类条款是否被规则承接。
    ("投标有效期", ("投标有效期",)),
    ("联合体", ("联合体", "联合投标")),
    ("法定代表人/授权委托", ("法定代表人身份证明", "授权委托书", "法定代表人证明")),
    ("串通投标", ("串通投标", "串通")),
    ("失信记录核查", ("信用中国", "失信", "政府采购网")),
    ("进口产品", ("进口产品", "进口")),
)

_TENDER_SCORE_PARENT_PATTERN = re.compile(
    r"第\s*[一二三四五六七八九十\d]+\s*部分[^（(]{0,40}?[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]"
)


def _tender_score_parent_ledger(text: str) -> list[dict]:
    """本地解析评分父项分值台账（如“第一部分价格部分（30分）…第四部分技术部分（30分）”）。

    纯本地正则、零模型成本；格式不规整时返回空列表，调用方静默跳过。
    """
    ledger: list[dict] = []
    for match in _TENDER_SCORE_PARENT_PATTERN.finditer(text):
        page = max(1, text[:match.start()].count("[第"))
        label = re.sub(r"\s+", "", match.group(0))
        try:
            score = float(match.group(1))
        except ValueError:
            continue
        if score <= 0:
            continue
        if ledger and ledger[-1]["page"] == page and ledger[-1]["score"] == score:
            continue
        ledger.append({"label": label, "score": score, "page": page})
    return ledger


def _score_parent_mismatches(tender_text: str, rules: list[dict]) -> list[dict]:
    """评分父项声明分值与下属评分规则满分合计对账。

    新规则优先使用提取阶段从原文继承的 ``score_sections``。评分表常跨页，
    用 source_page 切分会把上一分部的尾行错误归给下一分部；只有历史规则尚无
    台账元数据时才保留页码回退，保证老项目可读而不改变既有数据。
    """
    anchored: dict[str, dict] = {}
    for rule in rules:
        if not rule.get("enabled") or str(rule.get("category") or "") not in {"objective", "subjective"}:
            continue
        max_score = _score_rule_max_value(rule)
        if max_score is None:
            continue
        sections = rule_execution_meta(rule).get("score_sections") or []
        for section in sections:
            section_id = str(section.get("section_id") or "")
            if not section_id:
                continue
            group = anchored.setdefault(section_id, {
                "label": str(section.get("label") or "评分分部"),
                "score": section.get("max_score"), "rules": [],
            })
            group["rules"].append((str(rule.get("rule_id") or rule.get("title") or "未命名规则"), max_score, str(rule.get("title") or "未命名规则")))
    if anchored:
        issues: list[dict] = []
        for parent in anchored.values():
            try:
                declared = float(parent["score"])
            except (TypeError, ValueError):
                continue
            members: dict[str, tuple[float, str]] = {}
            for key, max_score, title in parent["rules"]:
                members[key] = (max_score, title)
            total = sum(item[0] for item in members.values())
            if abs(total - declared) <= 0.01:
                continue
            titles = "、".join(item[1] for item in members.values()) or "无评分规则"
            issues.append({
                "severity": "warning", "code": "score_parent_mismatch",
                "rule_id": "", "title": parent["label"],
                "message": (
                    f"招标评分分部“{parent['label']}”声明 {declared:g} 分，"
                    f"但其台账锚定的启用评分规则满分合计 {total:g} 分（{titles}）。"
                    f"差额 {declared - total:+.2g} 分，请核对是否缺项、重复或分部归属错误。"
                ),
            })
        return issues
    ledger = _tender_score_parent_ledger(tender_text)
    if len(ledger) < 2:
        return []
    issues: list[dict] = []
    for index, parent in enumerate(ledger):
        next_page = ledger[index + 1]["page"] if index + 1 < len(ledger) else None
        total = 0.0
        member_titles: list[str] = []
        for rule in rules:
            if not rule.get("enabled") or str(rule.get("category") or "") not in {"objective", "subjective"}:
                continue
            page = rule.get("source_page")
            if not isinstance(page, int) or page < parent["page"]:
                continue
            if next_page is not None and page >= next_page:
                continue
            scoring = {}
            try:
                scoring = json.loads(rule.get("scoring_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            max_score = _valid_max_score(scoring)
            if max_score is None:
                continue
            total += max_score
            member_titles.append(str(rule.get("title") or "未命名规则"))
        if abs(total - parent["score"]) > 0.01:
            issues.append({
                "severity": "warning", "code": "score_parent_mismatch",
                "rule_id": "", "title": parent["label"],
                "message": (
                    f"招标评分父项“{parent['label']}”声明 {parent['score']:g} 分，"
                    f"但该部分下启用评分规则满分合计 {total:g} 分（{'、'.join(member_titles) or '无评分规则'}）。"
                    f"差额 {parent['score'] - total:+.2g} 分，请核对是否缺项或存在重复。"
                ),
            })
    return issues


def _hard_tender_anchor_scan(app, project_id: str, rules: list[dict]) -> list[dict]:
    """首提取兜底：招标文件出现法律/政策硬性条款而规则集未覆盖时提示。

    仅在没有历史已确认规则集时运行（有历史集时由提取管线内部的硬性条款自动
    补漏保证完整，避免长期关键词清单噪音）。返回 issue 列表，不改写任何规则。
    """
    with connection(app) as conn:
        has_previous = conn.execute(
            "SELECT 1 FROM ew_rule_sets WHERE project_id=? AND status IN ('confirmed', 'superseded') LIMIT 1",
            (project_id,),
        ).fetchone()
    if has_previous:
        return []
    tender = next((item for item in list_documents(app, project_id) if item.get("role") == "tender"), None)
    parsed_path = str((tender or {}).get("parsed_path") or "")
    if not parsed_path:
        return []
    try:
        text = Path(parsed_path).read_text(encoding="utf-8", errors="ignore")[:500_000]
    except OSError:
        return []
    issues: list[dict] = []
    enabled_text = {
        "title": "、".join(str(rule.get("title") or "") for rule in rules if rule.get("enabled")),
        "check_rule": "、".join(str(rule.get("check_rule") or "") for rule in rules if rule.get("enabled")),
        "source_text": "、".join(str(rule.get("source_text") or "") for rule in rules if rule.get("enabled")),
    }
    disabled_text = {
        "title": "、".join(str(rule.get("title") or "") for rule in rules if not rule.get("enabled")),
        "check_rule": "、".join(str(rule.get("check_rule") or "") for rule in rules if not rule.get("enabled")),
        "source_text": "、".join(str(rule.get("source_text") or "") for rule in rules if not rule.get("enabled")),
    }
    for label, terms in _HARD_TENDER_ANCHOR_GROUPS:
        page_hits = [
            text[:position].count("[第") + 1
            for term in terms
            for position in [match.start() for match in re.finditer(re.escape(term), text)][:3]
        ]
        if not page_hits:
            continue
        enabled_hit = any(
            term in enabled_text["title"] or term in enabled_text["check_rule"] or term in enabled_text["source_text"]
            for term in terms
        )
        disabled_hit = any(
            term in disabled_text["title"] or term in disabled_text["check_rule"] or term in disabled_text["source_text"]
            for term in terms
        )
        if enabled_hit:
            continue
        pages = "、".join(str(page) for page in sorted(set(page_hits))[:6])
        if disabled_hit:
            issues.append({
                "severity": "warning", "code": "tender_requirement_disabled",
                "rule_id": "", "title": label,
                "message": f"招标文件出现“{label}”要求（第{pages}页），规则集中已有对应规则但处于停用状态，请确认是否需要启用。",
            })
        else:
            issues.append({
                "severity": "warning", "code": "tender_requirement_missing",
                "rule_id": "", "title": label,
                "message": f"招标文件出现“{label}”要求（第{pages}页），当前规则集未覆盖，建议补充对应审查规则。",
            })
    return issues





def _tender_declared_total_score(app, project_id: str) -> float | None:
    """从招标文件读取唯一、明确的整体总分声明。

    只采纳“总分 X 分”的显式表述且要求不小于 50 分。找不到招标文件、解析文本
    或出现多个不同总分时返回 None，预检保持静默。
    """
    tender = next((item for item in list_documents(app, project_id) if item.get("role") == "tender"), None)
    parsed_path = str((tender or {}).get("parsed_path") or "")
    if not parsed_path:
        return None
    try:
        text = Path(parsed_path).read_text(encoding="utf-8", errors="ignore")[:500_000]
    except OSError:
        return None
    # “某部分满分 X 分”在多包、分部评分表中极易出现，不能再作为全项目总分
    # 使用；否则会错误阻断规则确认或驱动错误的评分重组。只采纳“总分”明示，
    # 且全文存在不同总分时返回 None，交由人工确认而不是猜测多数值。
    values: set[float] = set()
    for match in _TENDER_TOTAL_SCORE_PATTERNS[0].finditer(text):
        value = float(match.group(1))
        if 50 <= value <= 1000:
            values.add(value)
    if len(values) != 1:
        return None
    return next(iter(values))


def rule_set_acquisition_validation(app, project_id: str) -> dict:
    """检查图片取证配置是否自洽，仅返回提示而不改写规则或阻断业务。

    该检查专门服务于前台确认前预检。历史规则允许保留“待人工选择强度”的状态，
    因此除无可用能力的显式单通道外均为 warning，避免升级后突然无法确认旧项目。
    """
    rule_set, rules = list_rules(app, project_id)
    if not rule_set:
        return {"rule_set_id": None, "issues": [], "summary": {"enabled": 0, "active": 0}}
    ocr_enabled = bool(ocr_feature_configuration(app).get("enabled"))
    ocr_settings = ocr_configuration(app)
    local = ocr_settings.get("local") if isinstance(ocr_settings, dict) else {}
    ocr_runtime = bool(ocr_settings.get("tencent_enabled") or (isinstance(local, dict) and local.get("runtime_available")))
    vision_enabled = bool(vision_configuration(app).get("enabled"))
    issues: list[dict] = []
    active = 0
    for rule in rules:
        if not rule.get("enabled"):
            continue
        meta = rule_execution_meta(rule)
        mode = meta["image_mode"]
        trigger = meta["vision_trigger"]
        level = meta["vision_level"]
        wants_image = mode != "off" and trigger != "off"
        key = str(rule.get("rule_id") or "")
        label = str(rule.get("title") or "检查规则")
        if wants_image and level == "off":
            issues.append({"severity": "warning", "code": "image_budget_off", "rule_id": key, "title": label,
                           "message": "已设置图片取证，但覆盖上限为关闭；本次不会调用 OCR 或多模态。"})
            continue
        if not wants_image or level == "off":
            continue
        active += 1
        if mode in {"ocr_only", "combined"} and (not ocr_enabled or not ocr_runtime):
            issues.append({"severity": "warning", "code": "ocr_unavailable", "rule_id": key, "title": label,
                           "message": "该规则需要 OCR，但当前没有可用 OCR 路径；将保留文字结论。"})
        if mode in {"vision_only", "combined"} and not vision_enabled:
            issues.append({"severity": "warning", "code": "vision_unavailable", "rule_id": key, "title": label,
                           "message": "该规则要求图片外观核验，但未启用可用的多模态模型；将保留其他已获得的证据。"})
        if mode == "auto" and not ocr_enabled and not vision_enabled:
            issues.append({"severity": "warning", "code": "all_image_capabilities_off", "rule_id": key, "title": label,
                           "message": "该规则设为智能取证，但 OCR 与多模态均未启用；本次只执行文字审查。"})
    # 疑似重复评分规则预检：同一评分对象、同一分值出现多条启用规则时，综合评审会
    # 重复计分导致总分虚高。只提示不自动停用，由人工核对后决定保留哪一条。
    score_groups: dict[tuple[str, str, float], list[dict]] = {}
    for rule in rules:
        if not rule.get("enabled") or str(rule.get("category") or "") not in {"objective", "subjective"}:
            continue
        core = _score_rule_title_core(rule.get("title"))
        max_value = _score_rule_max_value(rule)
        if len(core) < 4 or max_value is None:
            continue
        score_groups.setdefault((str(rule.get("category") or ""), core, max_value), []).append(rule)
    for (category, core, max_value), group in score_groups.items():
        if len(group) < 2:
            continue
        titles = "；".join(str(item.get("title") or "未命名规则") for item in group[:4])
        issues.append({
            "severity": "warning", "code": "duplicate_score_rule",
            "rule_id": str(group[0].get("rule_id") or ""),
            "rule_ids": [str(item.get("rule_id") or "") for item in group],
            "title": str(group[0].get("title") or "评分规则"),
            "message": f"疑似同一评分事实的重复规则（均 {max_value:g} 分）：{titles}。重复计分会导致总分虚高，请核对后仅保留一条。",
        })
    # 疑似重复审查规则预检：非评分规则同类别、同标题、检查指令高度相似时提示
    # 人工合并（不自动停用）。只提示最相似的一对，避免把同名但内容不同的规则
    # （如按章节/模块拆分的“逐项响应覆盖”）全部刷成噪音。
    review_groups: dict[tuple[str, str], list[dict]] = {}
    for rule in rules:
        if not rule.get("enabled") or str(rule.get("category") or "") in {"objective", "subjective"}:
            continue
        core = _score_rule_title_core(rule.get("title"))
        if len(core) < 4:
            continue
        review_groups.setdefault((str(rule.get("category") or ""), core), []).append(rule)
    for (category, core), group in review_groups.items():
        if len(group) < 2:
            continue
        best_pair: tuple[float, dict, dict] | None = None
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left = re.sub(r"[\s\W_]+", "", str(group[i].get("check_rule") or "")).casefold()
                right = re.sub(r"[\s\W_]+", "", str(group[j].get("check_rule") or "")).casefold()
                if not left or not right:
                    continue
                ratio = difflib.SequenceMatcher(None, left, right).ratio()
                if ratio >= 0.85 and (best_pair is None or ratio > best_pair[0]):
                    best_pair = (ratio, group[i], group[j])
        if best_pair is None:
            continue
        _, left_rule, right_rule = best_pair
        issues.append({
            "severity": "warning", "code": "duplicate_review_rule",
            "rule_id": str(left_rule.get("rule_id") or ""),
            "rule_ids": [str(left_rule.get("rule_id") or ""), str(right_rule.get("rule_id") or "")],
            "title": str(left_rule.get("title") or "审查规则"),
            "message": f"疑似同一审查事实的重复规则（{category}）：“{left_rule.get('title')}”与“{right_rule.get('title')}”"
                       "检查指令高度相似，重复执行会浪费调用并产生重复结论，请核对后仅保留一条。",
        })
    # 评分条款 ID 是提取阶段按原文位置生成的稳定台账锚点。同一 ID 同时落到两条
    # 启用评分规则时，即使标题或满分不同，也会造成父子项重复或续页片段被误当成
    # 独立分值。此处不替用户猜测该删哪一条，而是显式阻断确认，保留全部证据供编辑。
    for conflict in _score_source_ownership_conflicts(rules):
        group = conflict["rules"]
        titles = "；".join(str(item.get("title") or "未命名规则") for item in group[:4])
        issues.append({
            "severity": "warning", "code": "score_source_conflict",
            "rule_id": str(group[0].get("rule_id") or ""),
            "rule_ids": [str(item.get("rule_id") or "") for item in group],
            "title": str(group[0].get("title") or "评分规则"),
            "message": f"评分原文条款 {conflict['clause_id']} 同时被多个评分规则引用：{titles}。"
                       "请合并为一个包含完整叶子项的规则，或保留唯一归属，避免重复计分。",
        })
    # 新提取的评分规则应至少能锚定一条原文评分条款。人工补充和历史编辑规则可能
    # 天然没有机器台账，仍只提示；纯 AI 规则若无来源则不能可靠参与分部对账，确认
    # 后会把“同分项重复/漏项”直接带进综合评分，因此作为确认前硬阻断处理。
    for rule in rules:
        if not rule.get("enabled") or str(rule.get("category") or "") not in {"objective", "subjective"}:
            continue
        if str(rule.get("source_type") or "") in {"manual", "global"}:
            continue
        if rule_execution_meta(rule).get("source_clause_ids"):
            continue
        issues.append({
            "severity": "error" if str(rule.get("source_type") or "") == "ai" else "warning", "code": "score_source_unmapped",
            "rule_id": str(rule.get("rule_id") or ""), "title": str(rule.get("title") or "评分规则"),
            "message": "该 AI 评分规则未能唯一挂接到招标评分原文条款，不能可靠参与分部对账；请核对原文依据或重新提取。",
        })
    # 招标声明总分与启用评分规则满分合计交叉校验：提取可能把评分表标题分值
    # （如“商务评分标准（15分）”）误作规则满分，使合计偏离招标文件明示的
    # “（总分100分）”。只提示不阻断，由人工核对各规则满分是否与分值构成一致。
    declared_total = _tender_declared_total_score(app, project_id)
    score_total = 0.0
    score_rules: list[dict] = []
    for rule in rules:
        if not rule.get("enabled") or str(rule.get("category") or "") not in {"objective", "subjective"}:
            continue
        max_value = _score_rule_max_value(rule)
        if max_value is None:
            continue
        try:
            scoring = json.loads(rule.get("scoring_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            scoring = {}
        if not isinstance(scoring, dict):
            scoring = {}
        items_total = _score_items_total(scoring)
        # 只对“叶子合计低于满分”报警（疑似截断/漏项）；计数型规则（每份得X分、
        # 最高Y分）豁免；高于满分多为档位计分，不挂警告避免噪音。
        if (
            items_total is not None
            and max_value - items_total > 0.01
            and not _is_counting_score_rule(scoring)
        ):
            issues.append({
                "severity": "warning", "code": "score_leaf_total_below",
                "rule_id": str(rule.get("rule_id") or ""), "title": str(rule.get("title") or "评分规则"),
                "message": f"“{rule.get('title')}”满分 {max_value:g} 分，但评分子项合计 {items_total:g} 分，"
                           f"疑似跨页截断或漏项，请核对评分表原文。",
            })
        score_total += max_value
        score_rules.append(rule)
    if declared_total is not None and score_rules and abs(score_total - declared_total) > 0.01:
        titles = "；".join(str(rule.get("title") or "未命名规则") for rule in score_rules[:6])
        if len(score_rules) > 6:
            titles += f" 等{len(score_rules)}条"
        ledger = "、".join(
            f"{str(rule.get('title') or '未命名规则')}（{_score_rule_max_value(rule):g} 分）"
            for rule in score_rules
        )
        issues.append({"severity": "warning", "code": "score_total_mismatch",
                       "rule_id": str(score_rules[0].get("rule_id") or ""), "title": "评分满分合计",
                       "message": f"招标文件明示总分为 {declared_total:g} 分，但当前启用的评分规则满分合计为 {score_total:g} 分（{titles}）。"
                                  f"明细：{ledger}。请核对各评分规则满分是否与招标文件分值构成一致。"})
    tender = next((item for item in list_documents(app, project_id) if item.get("role") == "tender"), None)
    tender_path = str((tender or {}).get("parsed_path") or "")
    if tender_path:
        try:
            tender_text = Path(tender_path).read_text(encoding="utf-8", errors="ignore")[:500_000]
        except OSError:
            tender_text = ""
        if tender_text:
            issues.extend(_score_parent_mismatches(tender_text, rules))
    issues.extend(_hard_tender_anchor_scan(app, project_id, rules))
    return {
        "rule_set_id": rule_set["rule_set_id"], "issues": issues,
        "summary": {"enabled": sum(1 for rule in rules if rule.get("enabled")), "active": active},
    }


def create_review_run(app, project_id: str, task_id: str, profile_id: str | None) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始实质性审查")
    value = {"review_run_id": str(uuid.uuid4()), "project_id": project_id, "rule_set_id": rule_set["rule_set_id"], "task_id": task_id, "profile_id": profile_id, "created_at": now_iso()}
    with connection(app) as conn:
        conn.execute("INSERT INTO ew_review_runs(review_run_id, project_id, rule_set_id, task_id, profile_id, created_at) VALUES (:review_run_id, :project_id, :rule_set_id, :task_id, :profile_id, :created_at)", value)
    return value


def _pages_json(item: dict, public_key: str, storage_key: str) -> str:
    """兼容内存页码列表和数据库 JSON 字段，统一去重并过滤非法页码。"""
    values = item.get(public_key)
    if values is None:
        values = item.get(storage_key)
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, json.JSONDecodeError):
            values = []
    pages: list[int] = []
    if isinstance(values, list):
        for value in values:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 0:
                page = int(value)
                if page not in pages:
                    pages.append(page)
    return json.dumps(pages, ensure_ascii=False, separators=(",", ":"))


def _vision_pages_json(item: dict) -> str:
    return _pages_json(item, "vision_pages", "vision_pages_json")


def _vision_evidence_pages_json(item: dict) -> str:
    return _pages_json(item, "vision_evidence_pages", "vision_evidence_pages_json")


def _ocr_status_value(item: dict) -> str:
    """新增字段；没有它的历史结果仍可从旧 vision_status 推断 OCR 状态。"""
    value = str(item.get("ocr_status") or "")
    if value:
        return value
    legacy = str(item.get("vision_status") or "")
    return legacy if legacy.startswith("ocr_") else "not_requested"


def _multimodal_status_value(item: dict) -> str:
    value = str(item.get("multimodal_status") or "")
    if value:
        return value
    legacy = str(item.get("vision_status") or "")
    return "not_requested" if legacy.startswith("ocr_") else legacy


def _requirement_relation_value(item: dict) -> str:
    """审查事实关系是枚举契约；历史结果或异常调用统一安全回落。"""
    value = str(item.get("requirement_relation") or "")
    return value if value in {"supports", "contradicts", "uncertain"} else "uncertain"


def _evidence_layers_json(item: dict) -> str:
    """持久化可展示的证据层；异常输入退化为空数组，不能影响评审主流程。"""
    values = item.get("evidence_layers")
    if values is None:
        values = item.get("evidence_layers_json")
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, json.JSONDecodeError):
            values = []
    if not isinstance(values, list):
        values = []
    normalized: list[dict] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        source = str(value.get("source") or "").strip()
        if source not in {"text", "tencent_ocr", "local_ocr", "vision", "score_calculation"}:
            continue
        checked = json.loads(_pages_json(value, "checked_pages", "checked_pages_json"))
        evidence = json.loads(_pages_json(value, "evidence_pages", "evidence_pages_json"))
        normalized.append({
            "source": source,
            "summary": str(value.get("summary") or "").strip()[:1600],
            "checked_pages": checked,
            "evidence_pages": evidence,
            "service": str(value.get("service") or "").strip()[:160],
            "model": str(value.get("model") or "").strip()[:160],
        })
        # OCR/图片层可选地记录“事实方向—业务影响—外观依赖”。这是对旧 API 的
        # 兼容扩展，供报告、EvidencePack 和重跑审计复用；缺失时仍按旧证据层展示。
        if source in {"local_ocr", "tencent_ocr", "vision"}:
            relation = str(value.get("fact_relation") or "")
            adverse_impact = str(value.get("adverse_impact") or "")
            visual_dependency = str(value.get("visual_dependency") or "")
            rule_impact = str(value.get("rule_decision_impact") or "")
            if relation in {"supports", "contradicts", "uncertain"}:
                normalized[-1]["fact_relation"] = relation
            if adverse_impact in {"rejection", "material", "ordinary"}:
                normalized[-1]["adverse_impact"] = adverse_impact
            if visual_dependency in {"none", "confirmation_only", "decisive"}:
                normalized[-1]["visual_dependency"] = visual_dependency
            if rule_impact in {"rejection", "material", "ordinary"}:
                normalized[-1]["rule_decision_impact"] = rule_impact
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _public_vision_result(value: dict) -> dict:
    """将内部 JSON 列转换为向后兼容的新增公开字段。"""
    def public_pages(storage_key: str) -> list[int]:
        raw = value.pop(storage_key, "[]")
        try:
            pages = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            pages = []
        return [int(page) for page in pages if isinstance(page, (int, float)) and not isinstance(page, bool) and int(page) > 0] if isinstance(pages, list) else []

    value["vision_pages"] = public_pages("vision_pages_json")
    value["vision_evidence_pages"] = public_pages("vision_evidence_pages_json")
    value["ocr_status"] = _ocr_status_value(value)
    value["multimodal_status"] = _multimodal_status_value(value)
    raw_layers = value.pop("evidence_layers_json", "[]")
    try:
        layers = json.loads(raw_layers or "[]")
    except (TypeError, json.JSONDecodeError):
        layers = []
    value["evidence_layers"] = layers if isinstance(layers, list) else []
    return value


def _public_score_result(value: dict) -> dict:
    """为评分结果补充只读的评分分部元数据。

    评分对比视图只需要知道规则属于哪个原文评分分部，不应重新推断业务/技术
    归属，也不应读取或修改评分结论。旧结果没有 ``execution_meta_json`` 时返回
    空列表，前端会明确显示为“未归类”，保持向后兼容。
    """
    value = _public_vision_result(value)
    raw_meta = value.pop("execution_meta_json", None)
    try:
        meta = json.loads(raw_meta or "{}") if isinstance(raw_meta, str) else (raw_meta or {})
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    sections = meta.get("score_sections")
    value["score_sections"] = sections if isinstance(sections, list) else []
    value["rule_category"] = str(value.pop("rule_category", "") or "")
    try:
        value["rule_sort_order"] = int(value.pop("rule_sort_order", 0) or 0)
    except (TypeError, ValueError):
        value["rule_sort_order"] = 0
    return value


def save_review_results(app, review_run_id: str, document_id: str, results: list[dict]) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        for item in results:
            conn.execute(
                """INSERT INTO ew_review_results(review_result_id, review_run_id, document_id, rule_id, status, requirement_relation, evidence, page_hint, reason, conclusion_summary, risk_level,
                    confidence, evidence_quality, coverage_status, automation_status, requires_review, review_reason,
                    vision_status, ocr_status, multimodal_status, vision_pages_json, vision_evidence_pages_json, evidence_layers_json, vision_model, vision_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_run_id, document_id, rule_id) DO UPDATE SET
                status=excluded.status, requirement_relation=excluded.requirement_relation, evidence=excluded.evidence, page_hint=excluded.page_hint, reason=excluded.reason, conclusion_summary=excluded.conclusion_summary,
                risk_level=excluded.risk_level, confidence=excluded.confidence, evidence_quality=excluded.evidence_quality, coverage_status=excluded.coverage_status,
                automation_status=excluded.automation_status, requires_review=excluded.requires_review,
                review_reason=excluded.review_reason, vision_status=excluded.vision_status,
                ocr_status=excluded.ocr_status, multimodal_status=excluded.multimodal_status,
                vision_pages_json=excluded.vision_pages_json, vision_evidence_pages_json=excluded.vision_evidence_pages_json,
                evidence_layers_json=excluded.evidence_layers_json, vision_model=excluded.vision_model,
                vision_message=excluded.vision_message, created_at=excluded.created_at""",
                (str(uuid.uuid4()), review_run_id, document_id, item["rule_id"], item["status"], _requirement_relation_value(item), item.get("evidence", ""),
                 item.get("page_hint"), item.get("reason", ""), item.get("conclusion_summary", ""), item.get("risk_level", "medium"), item.get("confidence", "medium"),
                 item.get("evidence_quality", "limited"), item.get("coverage_status", "covered"), item.get("automation_status", "needs_review"),
                 1 if item.get("requires_review", True) else 0, item.get("review_reason", ""),
                 item.get("vision_status", "not_requested"), _ocr_status_value(item), _multimodal_status_value(item),
                 _vision_pages_json(item), _vision_evidence_pages_json(item), _evidence_layers_json(item),
                 item.get("vision_model", ""), item.get("vision_message", ""), timestamp),
            )


def publish_current_evaluation_document(app, project_id: str, document: dict, rule_set_id: str,
                                        task_id: str, profile_id: str | None, input_fingerprint: str,
                                        review_run_id: str | None, objective_score_run_id: str | None,
                                        subjective_score_run_id: str | None) -> None:
    """原子切换一份投标文件当前可展示的综合评审结果。

    运行明细仍保留在各自的 run 中；此索引只决定页面、报告读取哪一次成功完成的
    单文件结果。局部重评失败时不会调用本函数，因此旧结果天然保留。
    """
    document_id = str(document.get("document_id") or "")
    if not document_id:
        raise ValueError("投标文件标识不能为空")
    value = {
        "project_id": project_id,
        "document_id": document_id,
        "rule_set_id": rule_set_id,
        "task_id": task_id,
        "profile_id": profile_id,
        "document_sha256": str(document.get("sha256") or ""),
        "input_fingerprint": str(input_fingerprint or ""),
        "review_run_id": review_run_id,
        "objective_score_run_id": objective_score_run_id,
        "subjective_score_run_id": subjective_score_run_id,
        "highlights_json": "{}",
        "updated_at": now_iso(),
    }
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_evaluation_current_documents(
                   project_id, document_id, rule_set_id, task_id, profile_id, document_sha256,
                   input_fingerprint, review_run_id, objective_score_run_id, subjective_score_run_id,
                   highlights_json, updated_at)
               VALUES (:project_id, :document_id, :rule_set_id, :task_id, :profile_id, :document_sha256,
                       :input_fingerprint, :review_run_id, :objective_score_run_id, :subjective_score_run_id,
                       :highlights_json, :updated_at)
               ON CONFLICT(project_id, document_id) DO UPDATE SET
                   rule_set_id=excluded.rule_set_id, task_id=excluded.task_id, profile_id=excluded.profile_id,
                   document_sha256=excluded.document_sha256, input_fingerprint=excluded.input_fingerprint,
                   review_run_id=excluded.review_run_id, objective_score_run_id=excluded.objective_score_run_id,
                   subjective_score_run_id=excluded.subjective_score_run_id, highlights_json=excluded.highlights_json,
                   updated_at=excluded.updated_at""",
            value,
        )


def current_evaluation_document_states(app, project_id: str) -> dict[str, dict]:
    """返回当前规则集下每份投标文件最近已发布综合评审的轻量状态。"""
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        return {}
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT current.document_id, current.task_id, current.updated_at AS published_at,
                      task.status AS task_status, task.started_at AS task_started_at,
                      task.finished_at AS task_finished_at, task.updated_at AS task_updated_at
                 FROM ew_evaluation_current_documents current
                 JOIN ew_documents document ON document.document_id=current.document_id
                 JOIN ew_tasks task ON task.task_id=current.task_id
                WHERE current.project_id=? AND current.rule_set_id=? AND document.role='bid'
                  AND current.document_sha256=document.sha256
                ORDER BY current.updated_at DESC, task.rowid DESC""",
            (project_id, rule_set["rule_set_id"]),
        ).fetchall()
    values: dict[str, dict] = {}
    for row in rows:
        value = dict(row)
        document_id = str(value.pop("document_id") or "")
        if not document_id or document_id in values:
            continue
        value["last_run_at"] = (
            value.get("task_finished_at") or value.get("published_at")
            or value.get("task_updated_at") or value.get("task_started_at")
        )
        values[document_id] = value
    return values


def save_current_evaluation_highlights(app, project_id: str, task_id: str,
                                       document_ids: list[str], highlights: list[dict]) -> None:
    """保存本次实际重评文件的重点结论，未选投标人的既有摘要保持不变。"""
    allowed = {str(value) for value in document_ids if str(value)}
    if not allowed:
        return
    by_document = {
        str(item.get("document_id") or ""): item
        for item in highlights if isinstance(item, dict) and str(item.get("document_id") or "") in allowed
    }
    timestamp = now_iso()
    with connection(app) as conn:
        for document_id in allowed:
            conn.execute(
                """UPDATE ew_evaluation_current_documents
                   SET highlights_json=?, updated_at=?
                   WHERE project_id=? AND document_id=? AND task_id=?""",
                (json.dumps(by_document.get(document_id) or {}, ensure_ascii=False, separators=(",", ":")),
                 timestamp, project_id, document_id, task_id),
            )


def _current_evaluation_sources(app, project_id: str, component: str) -> list[dict]:
    """读取当前规则集下每份投标文件已发布的结果来源；无索引时保持旧查询路径。"""
    config = {
        "review": ("review_run_id", "ew_review_runs", "review_run_id"),
        "objective": ("objective_score_run_id", "ew_score_runs", "score_run_id"),
        "subjective": ("subjective_score_run_id", "ew_score_runs", "score_run_id"),
    }.get(component)
    if not config:
        return []
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        return []
    pointer_field, run_table, run_id_field = config
    score_type_filter = "" if component == "review" else " AND run.score_type=?"
    values: list[object] = [project_id, rule_set["rule_set_id"], rule_set["rule_set_id"]]
    if component != "review":
        values.append(component)
    with connection(app) as conn:
        rows = conn.execute(
            f"""SELECT current.project_id, current.document_id, current.rule_set_id, current.task_id,
                       current.profile_id, current.document_sha256, current.input_fingerprint,
                       current.review_run_id, current.objective_score_run_id, current.subjective_score_run_id,
                       current.highlights_json, current.updated_at AS published_at,
                       run.created_at AS source_run_created_at,
                       task.status AS task_status, task.error AS task_error,
                       task.progress AS task_progress, task.result_json AS task_result_json,
                       task.payload_json AS task_payload_json, task.rowid AS task_rowid
                FROM ew_evaluation_current_documents current
                JOIN {run_table} run ON run.{run_id_field}=current.{pointer_field}
                JOIN ew_tasks task ON task.task_id=current.task_id
                JOIN ew_documents document ON document.document_id=current.document_id
                WHERE current.project_id=? AND current.rule_set_id=? AND current.{pointer_field} IS NOT NULL
                  AND run.rule_set_id=? AND current.document_sha256=document.sha256{score_type_filter}
                ORDER BY current.updated_at DESC, task.rowid DESC""",
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def _current_evaluation_highlights(sources: list[dict]) -> list[dict]:
    values: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        document_id = str(source.get("document_id") or "")
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        try:
            item = json.loads(source.get("highlights_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item = {}
        if isinstance(item, dict) and item.get("document_id") == document_id:
            values.append(item)
    return values


def _current_evaluation_run_value(app, project_id: str, sources: list[dict]) -> dict | None:
    if not sources:
        return None
    value = dict(sources[0])
    # 当前结果由不同文件的独立运行组成，不能把某一来源任务的部分完成清单用于
    # 前端过滤其他投标人；全部已发布文件均可展示，任务状态仍保留供提示使用。
    value["completed_document_ids"] = sorted({str(item.get("document_id")) for item in sources if item.get("document_id")})
    value["mixed_sources"] = len({str(item.get("task_id") or "") for item in sources}) > 1
    value["source_task_ids"] = sorted({str(item.get("task_id") or "") for item in sources if item.get("task_id")})
    source_selection_modes: set[str] = set()
    for source in sources:
        try:
            payload = json.loads(source.get("task_payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        mode = str(payload.get("selection_mode") or "") if isinstance(payload, dict) else ""
        if mode in {"all", "selected"}:
            source_selection_modes.add(mode)
    value["selection_mode"] = "mixed" if len(source_selection_modes) > 1 or value["mixed_sources"] else next(iter(source_selection_modes), "all")
    value["highlights"] = _current_evaluation_highlights(sources)
    value["created_at"] = value.get("published_at") or value.get("source_run_created_at")
    value["updated_at"] = value.get("published_at") or value.get("source_run_created_at")
    # 未选择或尚未完成的投标文件不应被“有部分结果”掩盖；该统计只用于页面提示，
    # 不参与任务调度、模型调用或结果判断。
    current_document_ids = {
        str(item["document_id"]) for item in list_documents(app, project_id)
        if item.get("role") == "bid" and str(item.get("sha256") or "")
    }
    published_document_ids = set(value["completed_document_ids"])
    value["unassessed_document_ids"] = sorted(current_document_ids - published_document_ids)
    value["unassessed_document_count"] = len(value["unassessed_document_ids"])
    return value


def latest_review_results(app, project_id: str) -> tuple[dict | None, list[dict]]:
    current_sources = _current_evaluation_sources(app, project_id, "review")
    if current_sources:
        with connection(app) as conn:
            rows = conn.execute(
                """SELECT r.*, d.bidder_name, d.original_name, rule.category, rule.title, rule.check_rule
                   FROM ew_review_results r
                   JOIN ew_evaluation_current_documents current
                     ON current.review_run_id=r.review_run_id AND current.document_id=r.document_id
                   JOIN ew_documents d ON d.document_id=r.document_id
                   JOIN ew_rules rule ON rule.rule_id=r.rule_id
                   WHERE current.project_id=? AND current.rule_set_id=? AND current.document_sha256=d.sha256
                   ORDER BY d.bidder_name,
                       CASE WHEN r.status = 'ocr_required' THEN 1 ELSE 0 END,
                       CASE r.risk_level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC,
                       rule.category, rule.sort_order""",
                (project_id, current_sources[0]["rule_set_id"]),
            ).fetchall()
        results = [_public_vision_result(dict(row)) for row in rows]
        for result in results:
            if result.get("status") == "ocr_required":
                result["risk_level"] = "low"
        return _current_evaluation_run_value(app, project_id, current_sources), results
    with connection(app) as conn:
        run = conn.execute(
            """SELECT r.*, t.status AS task_status, t.error AS task_error, t.progress AS task_progress, t.result_json AS task_result_json
               FROM ew_review_runs r JOIN ew_tasks t ON t.task_id = r.task_id
               WHERE r.project_id = ? AND t.status IN ('running', 'success', 'error')
               AND EXISTS (SELECT 1 FROM ew_review_results item WHERE item.review_run_id = r.review_run_id)
               ORDER BY r.rowid DESC LIMIT 1""", (project_id,)
        ).fetchone()
        if not run:
            return None, []
        rows = conn.execute(
            """SELECT r.*, d.bidder_name, d.original_name, rule.category, rule.title, rule.check_rule
            FROM ew_review_results r
            JOIN ew_documents d ON d.document_id = r.document_id
            JOIN ew_rules rule ON rule.rule_id = r.rule_id
            WHERE r.review_run_id = ?
            ORDER BY d.bidder_name,
                CASE WHEN r.status = 'ocr_required' THEN 1 ELSE 0 END,
                CASE r.risk_level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC,
                rule.category, rule.sort_order""", (run["review_run_id"],)
        ).fetchall()
    results = [_public_vision_result(dict(row)) for row in rows]
    # 兼容历史评审结果：OCR 待识别不是风险结论，展示和报告均统一为低风险。
    for result in results:
        if result.get("status") == "ocr_required":
            result["risk_level"] = "low"
    value = dict(run)
    try:
        partial = json.loads(value.pop("task_result_json", "") or "{}")
    except (TypeError, json.JSONDecodeError):
        partial = {}
    if isinstance(partial, dict) and isinstance(partial.get("completed_documents"), list):
        value["completed_document_ids"] = [
            item["document_id"] for item in partial["completed_documents"]
            if isinstance(item, dict) and item.get("document_id")
        ]
    if isinstance(partial, dict) and isinstance(partial.get("highlights"), list):
        value["highlights"] = [
            item for item in partial["highlights"]
            if isinstance(item, dict) and item.get("document_id")
        ]
    return value, results


def reusable_evaluation_document_results(app, project_id: str, rule_set_id: str, profile_id: str,
                                         document_id: str, expected_rule_ids: dict[str, set[str]],
                                         execution_fingerprint: str | None = None,
                                         prompt_version: str | None = None) -> dict[str, list[dict]] | None:
    """查找完全相同执行输入下的单份投标文件结果，用于增量评审。

    execution_fingerprint 由 API 在排队时生成，涵盖招标/投标文件、规则集、
    模型公开配置与全部提示词。未携带它的历史任务不再复用，避免旧提示词或
    旧招标依据悄然混入新一轮结论。
    """
    with connection(app) as conn:
        tasks = conn.execute(
            """SELECT task_id, payload_json, result_json FROM ew_tasks WHERE project_id=? AND task_type='evaluate_all' AND status='success'
               ORDER BY finished_at DESC LIMIT 20""",
            (project_id,),
        ).fetchall()
        for task in tasks:
            if execution_fingerprint:
                try:
                    payload = json.loads(task["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if payload.get("input_fingerprint") != execution_fingerprint:
                    continue
            elif prompt_version:
                # 兼容早期直接创建任务的调用路径；新版 API 一定会走上面的完整指纹。
                try:
                    payload = json.loads(task["payload_json"] or "{}")
                    task_result = json.loads(task["result_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload, task_result = {}, {}
                if payload.get("prompt_version") != prompt_version and task_result.get("prompt_version") != prompt_version:
                    continue
            copied: dict[str, list[dict]] = {}
            valid = True
            for component, rule_ids in expected_rule_ids.items():
                if not rule_ids:
                    continue
                if component == "review":
                    run = conn.execute(
                        "SELECT review_run_id FROM ew_review_runs WHERE task_id=? AND rule_set_id=? AND profile_id=?",
                        (task["task_id"], rule_set_id, profile_id),
                    ).fetchone()
                    if not run:
                        valid = False
                        break
                    rows = conn.execute(
                        """SELECT rule_id, status, evidence, page_hint, reason, conclusion_summary, risk_level, confidence, evidence_quality, coverage_status,
                           automation_status, requires_review, review_reason, vision_status, ocr_status, multimodal_status, vision_pages_json,
                           vision_evidence_pages_json, evidence_layers_json,
                           vision_model, vision_message FROM ew_review_results
                           WHERE review_run_id=? AND document_id=?""",
                        (run["review_run_id"], document_id),
                    ).fetchall()
                else:
                    run = conn.execute(
                        "SELECT score_run_id FROM ew_score_runs WHERE task_id=? AND rule_set_id=? AND profile_id=? AND score_type=?",
                        (task["task_id"], rule_set_id, profile_id, component),
                    ).fetchone()
                    if not run:
                        valid = False
                        break
                    rows = conn.execute(
                        """SELECT rule_id, suggested_score, max_score, evidence, reason, conclusion_summary, confidence, coverage_status,
                           automation_status, requires_review, review_reason, vision_status, ocr_status, multimodal_status, vision_pages_json,
                           vision_evidence_pages_json, evidence_layers_json,
                           vision_model, vision_message FROM ew_score_results
                           WHERE score_run_id=? AND document_id=?""",
                        (run["score_run_id"], document_id),
                    ).fetchall()
                values = [dict(row) for row in rows]
                if {value["rule_id"] for value in values} != rule_ids:
                    valid = False
                    break
                copied[component] = values
            if valid:
                return copied
    return None


def create_score_run(app, project_id: str, task_id: str, score_type: str, profile_id: str | None) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始评分")
    value = {"score_run_id": str(uuid.uuid4()), "project_id": project_id, "rule_set_id": rule_set["rule_set_id"], "task_id": task_id, "score_type": score_type, "profile_id": profile_id, "created_at": now_iso()}
    with connection(app) as conn:
        conn.execute("INSERT INTO ew_score_runs(score_run_id, project_id, rule_set_id, task_id, score_type, profile_id, created_at) VALUES (:score_run_id, :project_id, :rule_set_id, :task_id, :score_type, :profile_id, :created_at)", value)
    return value


def save_score_results(app, score_run_id: str, document_id: str, results: list[dict]) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        for item in results:
            conn.execute(
                """INSERT INTO ew_score_results(score_result_id, score_run_id, document_id, rule_id, suggested_score, max_score,
                   evidence, reason, conclusion_summary, confidence, coverage_status, automation_status, requires_review, review_reason,
                   vision_status, ocr_status, multimodal_status, vision_pages_json, vision_evidence_pages_json, evidence_layers_json, vision_model, vision_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(score_run_id, document_id, rule_id) DO UPDATE SET
                suggested_score=excluded.suggested_score,
                max_score=excluded.max_score, evidence=excluded.evidence, reason=excluded.reason, conclusion_summary=excluded.conclusion_summary, confidence=excluded.confidence, coverage_status=excluded.coverage_status,
                automation_status=excluded.automation_status, requires_review=excluded.requires_review,
                review_reason=excluded.review_reason, vision_status=excluded.vision_status,
                ocr_status=excluded.ocr_status, multimodal_status=excluded.multimodal_status,
                vision_pages_json=excluded.vision_pages_json, vision_evidence_pages_json=excluded.vision_evidence_pages_json,
                evidence_layers_json=excluded.evidence_layers_json, vision_model=excluded.vision_model,
                vision_message=excluded.vision_message, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), score_run_id, document_id, item["rule_id"], item.get("suggested_score"),
                 item.get("max_score"), item.get("evidence", ""), item.get("reason", ""), item.get("conclusion_summary", ""), item.get("confidence"), item.get("coverage_status", "covered"),
                 item.get("automation_status", "needs_review"), 1 if item.get("requires_review", True) else 0,
                 item.get("review_reason", ""), item.get("vision_status", "not_requested"), _ocr_status_value(item), _multimodal_status_value(item),
                 _vision_pages_json(item), _vision_evidence_pages_json(item), _evidence_layers_json(item),
                 item.get("vision_model", ""), item.get("vision_message", ""), timestamp, timestamp),
            )


def score_results_for_run(app, score_run_id: str) -> list[dict]:
    """读取指定评分运行的内部快照，供只读影子分析使用。

    不复用或回写评分结论，也不作为页面 API；调用方必须显式标注其
    ``decision_participation=False``，避免影子机制误入正式评分链。
    """
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT s.*, d.bidder_name, d.original_name, rule.title, rule.check_rule,
                      rule.execution_meta_json, rule.scoring_json
               FROM ew_score_results s JOIN ew_documents d ON d.document_id=s.document_id
               JOIN ew_rules rule ON rule.rule_id=s.rule_id
               WHERE s.score_run_id=? ORDER BY d.bidder_name, rule.sort_order""",
            (score_run_id,),
        ).fetchall()
    values: list[dict] = []
    for row in rows:
        value = _public_vision_result(dict(row))
        for field in ("execution_meta_json", "scoring_json"):
            try:
                value[field[:-5]] = json.loads(value.pop(field) or "{}")
            except (TypeError, json.JSONDecodeError):
                value[field[:-5]] = {}
        values.append(value)
    return values


_PRICE_RULE_MARKERS = re.compile(r"(?:报价|投标价|评标价|评审价|最高限价|预算金额|总价)")
def is_price_rule(value: object) -> bool:
    """统一判断规则是否涉及报价，供工作进程补充本地价格事实。"""
    return bool(_PRICE_RULE_MARKERS.search(str(value or "")))


_NON_FILE_SCORING_PROCESS_PATTERN = re.compile(
    r"异常低价|澄清(?:说明)?|补正|谈判|投诉|算术(?:更正|修正)|评审现场"
)


def disable_non_file_scoring_process_rules(app, rule_set_id: str) -> int:
    """停用旧草稿中误列为评分项的评审过程规则，避免阻塞确认。

    只处理“无有效满分 + 明显属于评审过程”的组合；真正漏填满分的评分项仍由
    complete_missing_rule_scores 补全或明确提示人工补充，绝不静默删除。
    """
    disabled = 0
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT rule_id, title, check_rule, source_text, scoring_json FROM ew_rules
               WHERE rule_set_id = ? AND enabled = 1 AND category IN ('objective', 'subjective')""",
            (rule_set_id,),
        ).fetchall()
        for row in rows:
            try:
                scoring = json.loads(row["scoring_json"] or "{}")
            except json.JSONDecodeError:
                scoring = {}
            text = " ".join(str(row[key] or "") for key in ("title", "check_rule", "source_text"))
            if _valid_max_score(scoring) is None and _NON_FILE_SCORING_PROCESS_PATTERN.search(text):
                conn.execute("UPDATE ew_rules SET enabled = 0, updated_at = ? WHERE rule_id = ?", (now_iso(), row["rule_id"]))
                disabled += 1
    return disabled


def latest_score_results(app, project_id: str, score_type: str) -> tuple[dict | None, list[dict]]:
    current_sources = _current_evaluation_sources(app, project_id, score_type)
    if current_sources:
        with connection(app) as conn:
            field = "objective_score_run_id" if score_type == "objective" else "subjective_score_run_id"
            rows = conn.execute(
                f"""SELECT s.*, d.bidder_name, d.original_name, rule.title, rule.check_rule, rule.check_mode,
                           rule.category AS rule_category, rule.sort_order AS rule_sort_order,
                           rule.execution_meta_json
                    FROM ew_score_results s
                    JOIN ew_evaluation_current_documents current
                      ON current.{field}=s.score_run_id AND current.document_id=s.document_id
                    JOIN ew_documents d ON d.document_id=s.document_id
                    JOIN ew_rules rule ON rule.rule_id=s.rule_id
                    WHERE current.project_id=? AND current.rule_set_id=? AND current.document_sha256=d.sha256
                    ORDER BY d.bidder_name, rule.sort_order""",
                (project_id, current_sources[0]["rule_set_id"]),
            ).fetchall()
        return _current_evaluation_run_value(app, project_id, current_sources), [_public_score_result(dict(row)) for row in rows]
    with connection(app) as conn:
        run = conn.execute(
            """SELECT r.*, t.status AS task_status, t.error AS task_error, t.progress AS task_progress, t.result_json AS task_result_json
               FROM ew_score_runs r JOIN ew_tasks t ON t.task_id = r.task_id
               WHERE r.project_id = ? AND r.score_type = ? AND t.status IN ('running', 'success', 'error')
               AND EXISTS (SELECT 1 FROM ew_score_results item WHERE item.score_run_id = r.score_run_id)
               ORDER BY r.rowid DESC LIMIT 1""",
            (project_id, score_type),
        ).fetchone()
        if not run:
            return None, []
        rows = conn.execute(
            """SELECT s.*, d.bidder_name, d.original_name, rule.title, rule.check_rule, rule.check_mode,
                      rule.category AS rule_category, rule.sort_order AS rule_sort_order,
                      rule.execution_meta_json
            FROM ew_score_results s JOIN ew_documents d ON d.document_id=s.document_id
            JOIN ew_rules rule ON rule.rule_id=s.rule_id
            WHERE s.score_run_id=? ORDER BY d.bidder_name, rule.sort_order""", (run["score_run_id"],)
        ).fetchall()
    value = dict(run)
    try:
        partial = json.loads(value.pop("task_result_json", "") or "{}")
    except (TypeError, json.JSONDecodeError):
        partial = {}
    if isinstance(partial, dict) and isinstance(partial.get("completed_documents"), list):
        value["completed_document_ids"] = [
            item["document_id"] for item in partial["completed_documents"]
            if isinstance(item, dict) and item.get("document_id")
        ]
    return value, [_public_score_result(dict(row)) for row in rows]
