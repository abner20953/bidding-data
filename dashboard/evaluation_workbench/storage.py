"""工作台 SQLite 与文件存储。保持与现有业务模块隔离。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from dashboard.evaluation_workbench.prompt_templates import (
    PROMPT_TEMPLATE_SETTING, PROMPT_TEMPLATES, default_template, template_presentation,
)


MAX_BID_DOCUMENTS = 10
MAX_QUEUED_TASKS = 3
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


def list_prompt_templates(app) -> list[dict]:
    overrides = _prompt_template_overrides(app)
    values = [
        {"template_id": template_id, "name": meta["name"], "description": meta["description"],
         "content": overrides.get(template_id, meta["content"]), "is_custom": template_id in overrides,
         "placeholders": list(meta.get("placeholders", ())), **template_presentation(template_id)}
        for template_id, meta in PROMPT_TEMPLATES.items()
    ]
    return sorted(values, key=lambda item: (item["sort_order"], item["name"]))


def prompt_template(app, template_id: str) -> str:
    if template_id not in PROMPT_TEMPLATES:
        raise ValueError("不支持的提示词流程")
    return _prompt_template_overrides(app).get(template_id, PROMPT_TEMPLATES[template_id]["content"])


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
            "extract_rules", "extract_rules_guidance", "extract_rules_validation_guidance", "extract_rules_user", "extract_rules_compile_user",
            "extract_rules_continue_user", "extract_rules_coverage_user", "extract_rules_quality_gate_user",
            "extract_rules_finalise_user", "extract_rules_finalise_boundary_focus",
            "extract_rules_finalise_merge_focus", "extract_rules_supplement_user",
            "extract_rules_qualification_supplement_user",
            "extract_rules_scoring_reconcile_user",
            "json_repair", "json_repair_user",
        },
        "review_documents": {"review_documents", "review_documents_user", "json_repair", "json_repair_user"},
        "score_objective": {"score_objective", "score_objective_user", "json_repair", "json_repair_user"},
        "score_subjective": {"score_subjective", "score_subjective_user", "json_repair", "json_repair_user"},
        "evaluate_all": {
            "evaluate_all", "evaluate_all_guidance", "evaluate_all_highlights", "evaluate_all_scope_profile", "evaluate_all_scope_profile_user",
            "evaluate_all_scope_anomaly_guidance", "evaluate_all_full_scan_user", "evaluate_all_review_user", "evaluate_all_objective_user",
            "evaluate_all_subjective_user", "evaluate_all_cross_bid_price_user", "evaluate_all_highlights_user",
            "evaluate_all_visual_user", "evaluate_all_ocr_user", "evaluate_all_visual_contract", "evaluate_all_ocr_contract",
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
def connection(app):
    conn = sqlite3.connect(str(database_path(app)), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_documents_project ON ew_documents(project_id, role, created_at);
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
                final_status TEXT,
                evidence TEXT NOT NULL DEFAULT '',
                page_hint TEXT,
                reason TEXT NOT NULL DEFAULT '',
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
        _ensure_column(conn, "ew_rules", "source_type", "TEXT")
        _ensure_column(conn, "ew_rules", "source_task_id", "TEXT")
        _ensure_column(conn, "ew_rules", "check_rule", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_rules", "execution_meta_json", "TEXT")
        _ensure_column(conn, "ew_global_rules", "execution_meta_json", "TEXT")
        _ensure_column(conn, "ew_evidence_packs", "material_key", "TEXT NOT NULL DEFAULT ''")
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
    """OCR 免费包按中国自然月发放；容器时区已固定为 Asia/Shanghai。"""
    return time.strftime("%Y-%m", time.localtime())


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
    """仅提示是否应人工发起第二阶段验收，绝不自动切换 OCR 优先级。"""
    with connection(app) as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT c.document_id || ':' || c.page_number) AS page_count,
                      COUNT(DISTINCT d.project_id) AS project_count
               FROM ew_ocr_page_cache c JOIN ew_documents d ON d.document_id=c.document_id
               WHERE c.service='rapidocr_local'"""
        ).fetchone()
    pages = int(row["page_count"] or 0) if row else 0
    projects = int(row["project_count"] or 0) if row else 0
    return {
        "sample_pages": pages, "sample_projects": projects,
        # 这只是“提醒人工验收”的最低客观门槛；准确率和内存指标仍须人工确认。
        "ready_for_manual_validation": pages >= 30 and projects >= 3,
    }


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
    with connection(app) as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM ew_tasks WHERE project_id = ? AND task_type = ? AND status IN ('queued', 'running') LIMIT 1",
            (project_id, task_type),
        ).fetchone()
        if duplicate:
            raise ValueError("相同任务已经在排队或运行中，请勿重复提交")
        queued = conn.execute("SELECT COUNT(*) FROM ew_tasks WHERE status = 'queued'").fetchone()[0]
        if queued >= MAX_QUEUED_TASKS:
            raise ValueError(f"当前最多允许 {MAX_QUEUED_TASKS} 个排队任务，请等待已有任务完成")
        task_id = str(uuid.uuid4())
        timestamp = now_iso()
        conn.execute(
            """INSERT INTO ew_tasks(task_id, project_id, task_type, status, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
            (task_id, project_id, task_type, json.dumps(payload or {}, ensure_ascii=False), timestamp, timestamp),
        )
    return get_task(app, task_id)


def task_input_fingerprint(app, project_id: str, task_type: str, profile_id: str | None, prompt_version: str) -> str:
    """仅由文件指纹、规则版本和公开模型配置构成；不包含正文或 API Key。"""
    documents = list_documents(app, project_id)
    rule_set = current_rule_set(app, project_id)
    profile = get_model_profile(app, profile_id, "deepseek-v4-flash")
    vision_profile = resolve_vision_model_profile(app, profile) if task_type == "evaluate_all" else None
    ocr_config = ocr_configuration(app) if task_type == "evaluate_all" else {}
    relevant_roles = {
        "compare_documents": {"tender", "bid"},
        "extract_rules": {"tender", "tender_attachment"},
        "review_documents": {"bid"},
        "score_objective": {"bid"},
        "score_subjective": {"bid"},
        # 综合评审的项目范围画像和全文核验均依赖招标文件及其附件；遗漏这些
        # 输入会使“招标文件已变、投标文件未变”的任务错误复用历史结果。
        "evaluate_all": {"tender", "tender_attachment", "bid"},
    }.get(task_type, {"tender", "tender_attachment", "bid"})
    uses_rules = task_type in {"review_documents", "score_objective", "score_subjective", "evaluate_all"}
    value = {
        "task_type": task_type,
        "prompt_version": prompt_version,
        "documents": sorted((item["document_id"], item["sha256"], item.get("updated_at"), item.get("parse_status")) for item in documents if item["role"] in relevant_roles),
        "rule_set": (rule_set or {}).get("rule_set_id") if uses_rules else None,
        "rule_set_updated_at": (rule_set or {}).get("updated_at") if uses_rules else None,
        "profile": (profile.get("profile_id"), profile.get("model_name"), profile.get("base_url"), profile.get("updated_at"), profile.get("json_mode"), profile.get("thinking_mode")),
        # 共同删改的分段伪删除校验与编辑簇降权改变了查重结论，不能复用旧版本结果。
        "comparison_version": "cross-bid-signals-v4" if task_type == "compare_documents" else None,
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
        rows = conn.execute("SELECT * FROM ew_tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 50", (project_id,)).fetchall()
    return [task_to_dict(row) for row in rows]


def list_task_summaries(app, project_id: str) -> list[dict]:
    """供轮询使用；综合评审仅携带很小的已完成投标人清单。"""
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT task_id, project_id, task_type, status, progress, message, error, created_at, started_at, finished_at, updated_at,
                      CASE WHEN task_type = 'evaluate_all' THEN result_json ELSE NULL END AS result_json
               FROM ew_tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 50""",
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


def previous_scope_anomalies(app, document_id: str, chunk_id: str, chunk_hash: str,
                             *, limit: int = 32) -> list[dict]:
    """读取相同原文页块在旧扫描中发现的范围候选，供新一轮重新判断。

    这里只复用可定位的候选线索，不复用最终结论；因此规则目录或模型变化不会让
    已发现的高价值原文静默消失，当前项目范围仍由最终评审重新判断。
    """
    with connection(app) as conn:
        rows = conn.execute(
            """SELECT findings_json FROM ew_evaluation_scan_cache
               WHERE document_id=? AND chunk_id=? AND chunk_hash=?
               ORDER BY updated_at DESC LIMIT 24""",
            (document_id, chunk_id, chunk_hash),
        ).fetchall()
    values: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["findings_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload.get("scope_anomalies") if isinstance(payload, dict) else None
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("evidence"):
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


def initialize_compare_signal_reviews(app, task_id: str, signals: list[dict]) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO ew_compare_signal_reviews(signal_id, task_id, updated_at) VALUES (?, ?, ?)",
            [(item["signal_id"], task_id, timestamp) for item in signals],
        )


def compare_analysis(app, task_id: str) -> dict | None:
    task = get_task(app, task_id)
    analysis = (task or {}).get("result", {}).get("cross_bid_analysis")
    if not analysis:
        return None
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT signal_id, human_disposition, human_note, reviewed_at FROM ew_compare_signal_reviews WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    reviews = {row["signal_id"]: dict(row) for row in rows}
    for signal in analysis.get("signals", []):
        review = reviews.get(signal.get("signal_id"))
        if review:
            signal.update(review)
    return analysis


def update_compare_signal_review(app, signal_id: str, disposition: str, note: str = "") -> dict:
    allowed = {"pending", "verified", "dismissed", "needs_more_evidence"}
    if disposition not in allowed:
        raise ValueError("不支持的线索核验状态")
    note = str(note or "").strip()[:1000]
    timestamp = now_iso()
    reviewed_at = None if disposition == "pending" else timestamp
    with connection(app) as conn:
        updated = conn.execute(
            """UPDATE ew_compare_signal_reviews SET human_disposition=?, human_note=?, reviewed_at=?, updated_at=?
               WHERE signal_id=?""",
            (disposition, note, reviewed_at, timestamp, signal_id),
        ).rowcount
        if not updated:
            raise ValueError("横向异常线索不存在")
        row = conn.execute("SELECT * FROM ew_compare_signal_reviews WHERE signal_id=?", (signal_id,)).fetchone()
    return dict(row)


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
        "vision_level": "standard" if preset != "off" else "off",
        "baseline_ocr_mode": baseline_ocr_mode,
    }


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
    if not rule_set or rule_set["status"] != "draft":
        raise ValueError("只能修改待确认规则集中的规则")
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
        # 规则内容/评分口径的人工修改属于长期维护内容；启用勾选仅属于当前规则集，
        # 重新提取时必须重新选择，不能因为一次勾选操作把 AI 规则固化到下一版本。
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
        if any(key in payload for key in (
            "execution_strategy", "evidence_requirements", "applicability", "ocr_required", "check_mode",
            "image_mode", "vision_trigger", "vision_level", "acquisition_preset", "evidence_items",
            "baseline_ocr_mode",
        )):
            conn.execute(
                "UPDATE ew_rules SET execution_meta_json = ?, updated_at = ? WHERE rule_id = ?",
                (_execution_meta_json(payload, fallback=rule), now_iso(), rule_id),
            )
        conn.execute("UPDATE ew_rule_sets SET updated_at = ? WHERE rule_set_id = ?", (now_iso(), rule_set["rule_set_id"]))
        updated = conn.execute("SELECT * FROM ew_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    return _rule_public_value(dict(updated))


def replace_rules_from_extraction(app, project_id: str, task_id: str, rules: list[dict]) -> dict:
    with connection(app) as conn:
        # 新一轮 AI 提取刷新普通 AI 规则。人工补充及人工修改过内容/评分口径的
        # AI 规则继续迁移，但上一版本的启用勾选一律不迁移，新草稿统一重新启用。
        # 历史 ai_locked 无法区分“只改勾选”与“改过内容”，不再迁移；今后内容修改
        # 使用 ai_edited 明确记录，彻底切断重新提取与上一次勾选状态的关系。
        current = conn.execute(
            "SELECT * FROM ew_rule_sets WHERE project_id = ? ORDER BY version DESC LIMIT 1", (project_id,)
        ).fetchone()
        preserved = []
        if current:
            preserved = conn.execute(
                "SELECT * FROM ew_rules WHERE rule_set_id = ? AND source_type IN ('manual', 'ai_edited') ORDER BY sort_order, created_at",
                (current["rule_set_id"],),
            ).fetchall()
        prior = conn.execute("SELECT MAX(version) FROM ew_rule_sets WHERE project_id = ?", (project_id,)).fetchone()[0] or 0
        timestamp = now_iso()
        rule_set = {"rule_set_id": str(uuid.uuid4()), "project_id": project_id, "version": prior + 1, "status": "draft", "source_task_id": task_id, "created_at": timestamp, "updated_at": timestamp}
        conn.execute("UPDATE ew_rule_sets SET status = 'superseded', updated_at = ? WHERE project_id = ? AND status != 'superseded'", (timestamp, project_id))
        conn.execute("INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, source_task_id, created_at, updated_at) VALUES (:rule_set_id, :project_id, :version, :status, :source_task_id, :created_at, :updated_at)", rule_set)
        signatures = set()
        preserved_rule_count = 0
        for index, row in enumerate(preserved):
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
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], row["category"], row["title"], row["check_rule"] or row["title"],
                 row["source_text"], row["source_page"], "ocr" if row["check_mode"] == "ocr" else "auto",
                 row["source_type"], row["source_task_id"], row["scoring_json"], row["execution_meta_json"], 0 if row["check_mode"] == "ocr" else 1, index, timestamp, timestamp),
            )
            preserved_rule_count += 1
        for index, item in enumerate(rules):
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
                 preserved_rule_count + index, timestamp, timestamp),
            )
        global_rule_count = 0
        global_rules = conn.execute(
            "SELECT * FROM ew_global_rules ORDER BY category, sort_order, created_at"
        ).fetchall()
        for position, template in enumerate(global_rules, start=preserved_rule_count + len(rules)):
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
                 int(bool(template["enabled"])), position, timestamp, timestamp),
            )
            global_rule_count += 1
        rule_set["global_rule_count"] = global_rule_count
        rule_set["preserved_rule_count"] = preserved_rule_count
    return rule_set


def confirm_rule_set(app, project_id: str) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        raise ValueError("当前没有可确认的规则集")
    # 先清理旧版本/异常模型输出中被误标为评分项的“评审过程规则”，再补齐真正
    # 评分条款的明确满分。这样不会让异常低价解释等事项以 0 分规则阻塞确认。
    disable_non_file_scoring_process_rules(app, rule_set["rule_set_id"])
    complete_missing_rule_scores(app, rule_set["rule_set_id"])
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
    text = re.sub(r"[（(][^()（）]*\d+(?:\.\d+)?\s*分[^()（）]*[)）]\s*$", "", str(value or ""))
    text = re.sub(r"^\s*(?:(?:商务|技术|价格|服务|资格)部分|(?:客观|主观)?评分|评分项?)\s*[-—–:：_]*\s*", "", text)
    return re.sub(r"[\s\W_]+", "", text).casefold()


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


def _tender_declared_total_score(app, project_id: str) -> float | None:
    """从招标文件解析文本读取明示总分（如“（总分100分）”），取出现次数最多的分值。

    只采纳“总分/满分 X 分”的显式表述且要求不小于 50 分，避免把单个评分项的
    分值误当声明总分。找不到招标文件、解析文本或声明总分时返回 None，预检保持静默。
    """
    tender = next((item for item in list_documents(app, project_id) if item.get("role") == "tender"), None)
    parsed_path = str((tender or {}).get("parsed_path") or "")
    if not parsed_path:
        return None
    try:
        text = Path(parsed_path).read_text(encoding="utf-8", errors="ignore")[:500_000]
    except OSError:
        return None
    votes: dict[float, int] = {}
    for pattern in _TENDER_TOTAL_SCORE_PATTERNS:
        for match in pattern.finditer(text):
            value = float(match.group(1))
            if 50 <= value <= 1000:
                votes[value] = votes.get(value, 0) + 1
    if not votes:
        return None
    return max(sorted(votes), key=lambda value: votes[value])


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
        issues.append({"severity": "warning", "code": "duplicate_score_rule",
                       "rule_id": str(group[0].get("rule_id") or ""), "title": str(group[0].get("title") or "评分规则"),
                       "message": f"疑似同一评分事实的重复规则（均 {max_value:g} 分）：{titles}。重复计分会导致总分虚高，请核对后仅保留一条。"})
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
        score_total += max_value
        score_rules.append(rule)
    if declared_total is not None and score_rules and abs(score_total - declared_total) > 0.01:
        titles = "；".join(str(rule.get("title") or "未命名规则") for rule in score_rules[:6])
        if len(score_rules) > 6:
            titles += f" 等{len(score_rules)}条"
        issues.append({"severity": "warning", "code": "score_total_mismatch",
                       "rule_id": str(score_rules[0].get("rule_id") or ""), "title": "评分满分合计",
                       "message": f"招标文件明示总分为 {declared_total:g} 分，但当前启用的评分规则满分合计为 {score_total:g} 分（{titles}）。请核对各评分规则满分是否与招标文件分值构成一致。"})
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
        if source not in {"text", "tencent_ocr", "local_ocr", "vision"}:
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


def save_review_results(app, review_run_id: str, document_id: str, results: list[dict]) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        for item in results:
            conn.execute(
                """INSERT INTO ew_review_results(review_result_id, review_run_id, document_id, rule_id, status, evidence, page_hint, reason, risk_level,
                   confidence, evidence_quality, coverage_status, automation_status, requires_review, review_reason,
                   vision_status, ocr_status, multimodal_status, vision_pages_json, vision_evidence_pages_json, evidence_layers_json, vision_model, vision_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_run_id, document_id, rule_id) DO UPDATE SET
                status=excluded.status, evidence=excluded.evidence, page_hint=excluded.page_hint, reason=excluded.reason,
                risk_level=excluded.risk_level, confidence=excluded.confidence, evidence_quality=excluded.evidence_quality, coverage_status=excluded.coverage_status,
                automation_status=excluded.automation_status, requires_review=excluded.requires_review,
                review_reason=excluded.review_reason, vision_status=excluded.vision_status,
                ocr_status=excluded.ocr_status, multimodal_status=excluded.multimodal_status,
                vision_pages_json=excluded.vision_pages_json, vision_evidence_pages_json=excluded.vision_evidence_pages_json,
                evidence_layers_json=excluded.evidence_layers_json, vision_model=excluded.vision_model,
                vision_message=excluded.vision_message, final_status=NULL, confirmed_at=NULL, created_at=excluded.created_at""",
                (str(uuid.uuid4()), review_run_id, document_id, item["rule_id"], item["status"], item.get("evidence", ""),
                 item.get("page_hint"), item.get("reason", ""), item.get("risk_level", "medium"), item.get("confidence", "medium"),
                 item.get("evidence_quality", "limited"), item.get("coverage_status", "covered"), item.get("automation_status", "needs_review"),
                 1 if item.get("requires_review", True) else 0, item.get("review_reason", ""),
                 item.get("vision_status", "not_requested"), _ocr_status_value(item), _multimodal_status_value(item),
                 _vision_pages_json(item), _vision_evidence_pages_json(item), _evidence_layers_json(item),
                 item.get("vision_model", ""), item.get("vision_message", ""), timestamp),
            )


def latest_review_results(app, project_id: str) -> tuple[dict | None, list[dict]]:
    with connection(app) as conn:
        run = conn.execute(
            """SELECT r.*, t.status AS task_status, t.error AS task_error, t.progress AS task_progress, t.result_json AS task_result_json
               FROM ew_review_runs r JOIN ew_tasks t ON t.task_id = r.task_id
               WHERE r.project_id = ? AND t.status IN ('running', 'success', 'error')
               AND EXISTS (SELECT 1 FROM ew_review_results item WHERE item.review_run_id = r.review_run_id)
               AND r.rule_set_id = (SELECT rule_set_id FROM ew_rule_sets WHERE project_id = ? ORDER BY version DESC LIMIT 1)
               ORDER BY r.rowid DESC LIMIT 1""", (project_id, project_id)
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
                        """SELECT rule_id, status, evidence, page_hint, reason, risk_level, confidence, evidence_quality, coverage_status,
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
                        """SELECT rule_id, suggested_score, effective_score, max_score, evidence, reason, confidence, coverage_status,
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


def update_review_final_status(app, review_result_id: str, final_status: str) -> dict:
    allowed = {"satisfied", "not_satisfied", "partial", "not_found", "manual"}
    if final_status not in allowed:
        raise ValueError("不支持的人工复核结论")
    with connection(app) as conn:
        updated = conn.execute(
            "UPDATE ew_review_results SET final_status = ?, confirmed_at = ? WHERE review_result_id = ?",
            (final_status, now_iso(), review_result_id),
        ).rowcount
        if not updated:
            raise ValueError("审查结果不存在")
        row = conn.execute("SELECT * FROM ew_review_results WHERE review_result_id = ?", (review_result_id,)).fetchone()
    return dict(row)


def confirm_auto_review_results(app, project_id: str) -> int:
    """一次确认所有证据充分的 AI 结论；异常项仍必须逐条复核。"""
    with connection(app) as conn:
        updated = conn.execute(
            """UPDATE ew_review_results SET final_status=status, confirmed_at=?, automation_status='confirmed'
               WHERE review_result_id IN (
                   SELECT r.review_result_id FROM ew_review_results r JOIN ew_review_runs run ON run.review_run_id=r.review_run_id
                   JOIN ew_tasks task ON task.task_id=run.task_id
                   WHERE run.project_id=? AND task.status='success' AND r.requires_review=0
                     AND r.final_status IS NULL
                     AND run.rule_set_id=(SELECT rule_set_id FROM ew_rule_sets WHERE project_id=? ORDER BY version DESC LIMIT 1)
               )""",
            (now_iso(), project_id, project_id),
        ).rowcount
    return updated


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
                """INSERT INTO ew_score_results(score_result_id, score_run_id, document_id, rule_id, suggested_score, final_score, effective_score, max_score,
                   evidence, reason, confidence, coverage_status, automation_status, requires_review, review_reason,
                   vision_status, ocr_status, multimodal_status, vision_pages_json, vision_evidence_pages_json, evidence_layers_json, vision_model, vision_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(score_run_id, document_id, rule_id) DO UPDATE SET
                suggested_score=excluded.suggested_score, final_score=excluded.final_score, effective_score=excluded.effective_score,
                max_score=excluded.max_score, evidence=excluded.evidence, reason=excluded.reason, confidence=excluded.confidence, coverage_status=excluded.coverage_status,
                automation_status=excluded.automation_status, requires_review=excluded.requires_review,
                review_reason=excluded.review_reason, vision_status=excluded.vision_status,
                ocr_status=excluded.ocr_status, multimodal_status=excluded.multimodal_status,
                vision_pages_json=excluded.vision_pages_json, vision_evidence_pages_json=excluded.vision_evidence_pages_json,
                evidence_layers_json=excluded.evidence_layers_json, vision_model=excluded.vision_model,
                vision_message=excluded.vision_message, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), score_run_id, document_id, item["rule_id"], item.get("suggested_score"), item.get("final_score"),
                 item.get("effective_score"), item.get("max_score"), item.get("evidence", ""), item.get("reason", ""), item.get("confidence"), item.get("coverage_status", "covered"),
                 item.get("automation_status", "needs_review"), 1 if item.get("requires_review", True) else 0,
                 item.get("review_reason", ""), item.get("vision_status", "not_requested"), _ocr_status_value(item), _multimodal_status_value(item),
                 _vision_pages_json(item), _vision_evidence_pages_json(item), _evidence_layers_json(item),
                 item.get("vision_model", ""), item.get("vision_message", ""), timestamp, timestamp),
            )


_PRICE_RULE_MARKERS = re.compile(r"(?:报价|投标价|评标价|评审价|最高限价|预算金额|总价)")
_PRICE_ABSENCE_MARKERS = re.compile(r"(?:未见|未提供|未直接呈现|缺失|未核)[^。；;\n]{0,36}(?:报价|总价|金额)|(?:报价|总价|金额)[^。；;\n]{0,36}(?:未见|未提供|未直接呈现|缺失|未核)")
# 真实评分证据的金额写法多样：千分位“¥627,000元”、无分隔“￥：632836 元”、
# “628120.00元”、万元小数“174.6762万元”和中文大写“…元整”都必须命中，
# 否则价格核验已定位的事实会被静默漏掉，陈旧“报价未见”结论无法回收。
_CONCRETE_PRICE_MARKERS = re.compile(
    r"(?:￥|¥|人民币)?\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*元?"
    r"|[￥¥]\s*[:：]?\s*\d{4,}(?:\.\d+)?"
    r"|\d{4,}(?:\.\d+)?\s*元"
    r"|\d+(?:\.\d+)?\s*万元"
    r"|(?:人民币)?[零壹贰叁肆伍陆柒捌玖拾佰仟万亿两]+元整"
)


def is_price_rule(value: object) -> bool:
    """统一判断规则是否涉及报价，供存储回收和工作进程共享。"""
    return bool(_PRICE_RULE_MARKERS.search(str(value or "")))


def reconcile_price_review_results(app, review_run_id: str, objective_run_id: str) -> int:
    """用同批跨投标人报价事实撤销“报价未见”的旧文字结论。

    单文件审查先完成、跨投标人价格核验后完成。若后者已在同一投标文件中定位到
    明确金额，前者不能继续以“未见报价”为高风险依据。这里仅把该类结论降为
    ``partial`` 并保留限价/口径待核验，不自动认定报价合规或改写任何价格分。
    """
    with connection(app) as conn:
        score_rows = conn.execute(
            """SELECT item.document_id, item.evidence, item.reason, rule.title, rule.check_rule
               FROM ew_score_results item JOIN ew_rules rule ON rule.rule_id=item.rule_id
               WHERE item.score_run_id=?""", (objective_run_id,)
        ).fetchall()
        price_documents = {
            str(row["document_id"])
            for row in score_rows
            if is_price_rule(f"{row['title'] or ''} {row['check_rule'] or ''}")
            and _CONCRETE_PRICE_MARKERS.search(f"{row['evidence'] or ''} {row['reason'] or ''}")
        }
        if not price_documents:
            return 0
        rows = conn.execute(
            """SELECT item.review_result_id, item.document_id, item.status, item.evidence, item.reason,
                      rule.title, rule.check_rule
               FROM ew_review_results item JOIN ew_rules rule ON rule.rule_id=item.rule_id
               WHERE item.review_run_id=?""", (review_run_id,)
        ).fetchall()
        updated = 0
        for row in rows:
            if str(row["document_id"]) not in price_documents:
                continue
            rule_text = f"{row['title'] or ''} {row['check_rule'] or ''}"
            current_text = f"{row['evidence'] or ''}\n{row['reason'] or ''}"
            if not is_price_rule(rule_text) or not _PRICE_ABSENCE_MARKERS.search(current_text):
                continue
            pieces = re.split(r"(?<=[。；;])\s*|\n+", str(row["reason"] or ""))
            retained = [piece.strip() for piece in pieces if piece.strip() and not _PRICE_ABSENCE_MARKERS.search(piece)]
            retained.append("同批价格核验已定位报价金额；是否符合限价、报价口径及本规则仍需结合原文复核。")
            conn.execute(
                """UPDATE ew_review_results
                   SET status='partial', reason=?, risk_level='low', confidence='medium',
                       evidence_quality='limited', automation_status='needs_review', requires_review=1,
                       review_reason='同批价格核验已定位报价金额，原“报价未见”表述已撤销，仍需人工核对限价和口径。'
                   WHERE review_result_id=?""",
                (" ".join(dict.fromkeys(retained))[:2000], row["review_result_id"]),
            )
            updated += 1
    return updated


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
    with connection(app) as conn:
        run = conn.execute(
            """SELECT r.*, t.status AS task_status, t.error AS task_error, t.progress AS task_progress, t.result_json AS task_result_json
               FROM ew_score_runs r JOIN ew_tasks t ON t.task_id = r.task_id
               WHERE r.project_id = ? AND r.score_type = ? AND t.status IN ('running', 'success', 'error')
               AND EXISTS (SELECT 1 FROM ew_score_results item WHERE item.score_run_id = r.score_run_id)
               AND r.rule_set_id = (SELECT rule_set_id FROM ew_rule_sets WHERE project_id = ? ORDER BY version DESC LIMIT 1)
               ORDER BY r.rowid DESC LIMIT 1""",
            (project_id, score_type, project_id),
        ).fetchone()
        if not run:
            return None, []
        rows = conn.execute(
            """SELECT s.*, d.bidder_name, d.original_name, rule.title, rule.check_rule, rule.check_mode
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
    return value, [_public_vision_result(dict(row)) for row in rows]


def update_final_score(app, score_result_id: str, final_score: float) -> dict:
    if not math.isfinite(final_score):
        raise ValueError("最终分数必须是有效数字")
    with connection(app) as conn:
        row = conn.execute("SELECT max_score FROM ew_score_results WHERE score_result_id = ?", (score_result_id,)).fetchone()
        if not row:
            raise ValueError("评分结果不存在")
        max_score = row["max_score"]
        if final_score < 0 or (max_score is not None and final_score > max_score):
            raise ValueError("最终分数必须在 0 与该项满分之间")
        conn.execute("UPDATE ew_score_results SET final_score = ?, effective_score = ?, automation_status='confirmed', updated_at = ? WHERE score_result_id = ?", (final_score, final_score, now_iso(), score_result_id))
        updated = conn.execute("SELECT * FROM ew_score_results WHERE score_result_id = ?", (score_result_id,)).fetchone()
    return dict(updated)


def confirm_auto_score_results(app, project_id: str, score_type: str | None = None) -> int:
    params: list[object] = [now_iso(), project_id, project_id]
    type_sql = ""
    if score_type:
        type_sql = " AND run.score_type=?"
        params.append(score_type)
    with connection(app) as conn:
        updated = conn.execute(
            """UPDATE ew_score_results SET final_score=effective_score, automation_status='confirmed', updated_at=?
               WHERE score_result_id IN (
                   SELECT s.score_result_id FROM ew_score_results s JOIN ew_score_runs run ON run.score_run_id=s.score_run_id
                   JOIN ew_tasks task ON task.task_id=run.task_id
                   WHERE run.project_id=? AND task.status='success' AND s.requires_review=0
                     AND s.final_score IS NULL
                     AND run.rule_set_id=(SELECT rule_set_id FROM ew_rule_sets WHERE project_id=? ORDER BY version DESC LIMIT 1)""" + type_sql + ")",
            params,
        ).rowcount
    return updated
