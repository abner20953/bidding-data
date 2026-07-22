"""工作台 SQLite 与文件存储。保持与现有业务模块隔离。"""

from __future__ import annotations

import hashlib
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

_SCORE_TOTAL_PATTERN = re.compile(r"(?:总计|共计|合计|最高(?:得)?|最多(?:得)?|满分(?:为)?)\s*(\d+(?:\.\d+)?)\s*分")
_SCORE_VALUE_PATTERN = re.compile(r"(?:得|扣)\s*(\d+(?:\.\d+)?)\s*分")
_LEGACY_EXTRACT_RULES_USER_SHA256 = "4fe464136f54fb033ac1824271f0d942a3d7f3d13b53c04acdf498ac152ff3d2"
# 该值是 2026-07-22 之前随系统同步到云端、但没有人工编辑过的默认模板。
# 保留它可让本次规则质量升级实际作用于既有部署，同时绝不覆盖用户手动编辑的内容。
_PREVIOUS_DEFAULT_EXTRACT_RULES_USER_SHA256 = "a4bb928f79c5e954c155a344ae817231ade404c684da1afd7f111bdb284ab578"


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
            "extract_rules_finalise_user", "extract_rules_supplement_user",
            "json_repair", "json_repair_user",
        },
        "review_documents": {"review_documents", "review_documents_user", "json_repair", "json_repair_user"},
        "score_objective": {"score_objective", "score_objective_user", "json_repair", "json_repair_user"},
        "score_subjective": {"score_subjective", "score_subjective_user", "json_repair", "json_repair_user"},
        "evaluate_all": {
            "evaluate_all", "evaluate_all_guidance", "evaluate_all_scope_profile", "evaluate_all_scope_profile_user",
            "evaluate_all_full_scan_user", "evaluate_all_review_user", "evaluate_all_objective_user",
            "evaluate_all_subjective_user", "evaluate_all_cross_bid_price_user", "evaluate_all_output_contract",
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
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_model_calls_project ON ew_model_calls(project_id, created_at);
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(score_run_id, document_id, rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ew_score_results_run ON ew_score_results(score_run_id, document_id);
            """
        )
        _ensure_column(conn, "ew_review_results", "final_status", "TEXT")
        _ensure_column(conn, "ew_review_results", "confirmed_at", "TEXT")
        _ensure_column(conn, "ew_model_profiles", "api_key_encrypted", "TEXT")
        _ensure_column(conn, "ew_review_results", "confidence", "TEXT")
        _ensure_column(conn, "ew_review_results", "evidence_quality", "TEXT")
        _ensure_column(conn, "ew_review_results", "automation_status", "TEXT NOT NULL DEFAULT 'needs_review'")
        _ensure_column(conn, "ew_review_results", "requires_review", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "ew_review_results", "review_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_score_results", "effective_score", "REAL")
        _ensure_column(conn, "ew_score_results", "automation_status", "TEXT NOT NULL DEFAULT 'needs_review'")
        _ensure_column(conn, "ew_score_results", "requires_review", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "ew_score_results", "review_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_rules", "source_type", "TEXT")
        _ensure_column(conn, "ew_rules", "source_task_id", "TEXT")
        _ensure_column(conn, "ew_rules", "check_rule", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ew_model_calls", "requested_max_tokens", "INTEGER")
        _ensure_column(conn, "ew_model_calls", "finish_reason", "TEXT")
        _ensure_column(conn, "ew_model_calls", "response_chars", "INTEGER")
        conn.execute("UPDATE ew_rules SET check_rule = title WHERE check_rule IS NULL OR check_rule = ''")
        conn.execute("UPDATE ew_rules SET source_type = CASE WHEN rule_set_id IN (SELECT rule_set_id FROM ew_rule_sets WHERE source_task_id IS NOT NULL) THEN 'ai' ELSE 'manual' END WHERE source_type IS NULL OR source_type = ''")
        _migrate_known_legacy_prompt_override(conn)
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
    legacy = overrides.get("extract_rules_user") if isinstance(overrides, dict) else None
    if not isinstance(legacy, str) or hashlib.sha256(legacy.encode("utf-8")).hexdigest() not in {
        _LEGACY_EXTRACT_RULES_USER_SHA256,
        _PREVIOUS_DEFAULT_EXTRACT_RULES_USER_SHA256,
    }:
        return
    # 历史版本是此前同步过的原文映射模板。新版保留其业务边界，并补齐页码、
    # 评分条款 ID 与叶子评分项，避免复杂评分规则只能由后续补救阶段还原。
    overrides["extract_rules_user"] = default_template("extract_rules_user")
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


def create_project(app, name: str, project_number: str = "", section_name: str = "") -> dict:
    project_id = str(uuid.uuid4())
    timestamp = now_iso()
    with connection(app) as conn:
        conn.execute(
            "INSERT INTO ew_projects(project_id, name, project_number, section_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, name.strip(), project_number.strip(), section_name.strip(), timestamp, timestamp),
        )
        _import_enabled_global_rules(conn, project_id, timestamp)
    project_dir(app, project_id)
    return get_project(app, project_id)


def _import_enabled_global_rules(conn: sqlite3.Connection, project_id: str, timestamp: str) -> None:
    """新项目只在创建时自动复制已启用的通用规则，避免后续配置变更影响历史项目。"""
    templates = conn.execute(
        "SELECT * FROM ew_global_rules WHERE enabled = 1 ORDER BY category, sort_order, created_at"
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
               source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'global', NULL, NULL, 1, ?, ?, ?)""",
            (str(uuid.uuid4()), rule_set_id, template["category"], template["title"], template["check_rule"],
             template["source_text"], template["check_mode"], position, timestamp, timestamp),
        )


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
    return {
        "category": category,
        "title": title,
        "check_rule": check_rule,
        "source_text": str(payload.get("source_text", base.get("source_text", ""))).strip(),
        "check_mode": "ocr" if ocr_required or payload.get("check_mode") == "ocr" else "auto",
        "enabled": 1 if bool(payload.get("enabled", base.get("enabled", True))) else 0,
        "sort_order": int(payload.get("sort_order", base.get("sort_order", 0)) or 0),
    }


def list_global_rules(app) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute(
            "SELECT * FROM ew_global_rules ORDER BY category, sort_order, created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def create_global_rule(app, payload: dict) -> dict:
    rule = _global_rule_payload(payload)
    rule.update({"global_rule_id": str(uuid.uuid4()), "created_at": now_iso(), "updated_at": now_iso()})
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_global_rules(global_rule_id, category, title, check_rule, source_text, check_mode, enabled, sort_order, created_at, updated_at)
               VALUES (:global_rule_id, :category, :title, :check_rule, :source_text, :check_mode, :enabled, :sort_order, :created_at, :updated_at)""",
            rule,
        )
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
               check_mode=:check_mode, enabled=:enabled, sort_order=:sort_order, updated_at=:updated_at WHERE global_rule_id=:global_rule_id""",
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
        "comparison_version": "cross-bid-signals-v2" if task_type == "compare_documents" else None,
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

    prompt_tokens = number("prompt_tokens", "input_tokens")
    completion_tokens = number("completion_tokens", "output_tokens")
    total_tokens = number("total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_model_calls(call_id, task_id, project_id, document_id, phase, profile_id,
               context_mode, input_chars, prompt_tokens, completion_tokens, total_tokens, cache_hit_tokens,
               requested_max_tokens, finish_reason, response_chars, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id, project_id, document_id, phase, profile_id, context_mode,
             max(0, int(input_chars)), prompt_tokens, completion_tokens, total_tokens,
             number("prompt_cache_hit_tokens", "cache_hit_tokens", "cached_tokens"),
             _safe_positive_int(response_metadata.get("requested_max_tokens")),
             str(response_metadata.get("finish_reason") or "")[:64] or None,
             _safe_positive_int(response_metadata.get("response_chars")), now_iso()),
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
    return dict(row)


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
    return value


def list_model_profiles(app) -> list[dict]:
    with connection(app) as conn:
        rows = conn.execute("SELECT * FROM ew_model_profiles ORDER BY created_at").fetchall()
        default_row = conn.execute("SELECT setting_value FROM ew_settings WHERE setting_key = 'default_model_profile_id'").fetchone()
    default_id = default_row["setting_value"] if default_row else None
    profiles = []
    for row in rows:
        profile = _public_model_profile(dict(row))
        profile["is_default"] = profile["profile_id"] == default_id
        profiles.append(profile)
    return profiles


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
        "enabled": 1,
        "created_at": existing["created_at"] if existing else timestamp,
        "updated_at": timestamp,
    }
    return values


def create_model_profile(app, payload: dict) -> dict:
    values = _model_profile_values(app, payload)
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_model_profiles(profile_id, display_name, protocol, base_url, model_name, api_key_env, api_key_encrypted,
            context_limit, timeout_seconds, json_mode, thinking_mode, enabled, created_at, updated_at)
            VALUES (:profile_id, :display_name, :protocol, :base_url, :model_name, :api_key_env, :api_key_encrypted, :context_limit,
            :timeout_seconds, :json_mode, :thinking_mode, :enabled, :created_at, :updated_at)""",
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
        conn.execute(
            """UPDATE ew_model_profiles SET display_name=:display_name, protocol=:protocol, base_url=:base_url,
               model_name=:model_name, api_key_env=:api_key_env, api_key_encrypted=:api_key_encrypted,
               context_limit=:context_limit, timeout_seconds=:timeout_seconds, json_mode=:json_mode,
               thinking_mode=:thinking_mode, enabled=:enabled, updated_at=:updated_at WHERE profile_id=:profile_id""",
            values,
        )
    return _public_model_profile(values)


def delete_model_profile(app, profile_id: str) -> None:
    with connection(app) as conn:
        row = conn.execute("SELECT 1 FROM ew_model_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
        if not row:
            raise ValueError("模型档案不存在")
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


def list_rules(app, project_id: str) -> tuple[dict | None, list[dict]]:
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        return None, []
    with connection(app) as conn:
        rows = conn.execute("SELECT * FROM ew_rules WHERE rule_set_id = ? ORDER BY category, sort_order, created_at", (rule_set["rule_set_id"],)).fetchall()
    return rule_set, [dict(row) for row in rows]


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
        "enabled": 1, "sort_order": int(payload.get("sort_order") or 0), "created_at": timestamp, "updated_at": timestamp,
    }
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode,
            source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
            VALUES (:rule_id, :rule_set_id, :category, :title, :check_rule, :source_text, :source_page, :check_mode,
            :source_type, :source_task_id, :scoring_json, :enabled, :sort_order, :created_at, :updated_at)""", rule,
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
                   source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), draft["rule_set_id"], row["category"], row["title"], row["check_rule"] or row["title"], row["source_text"], row["source_page"],
                 "ocr" if row["check_mode"] == "ocr" else "auto", row["source_type"] or "manual", row["source_task_id"], row["scoring_json"], row["enabled"], row["sort_order"], timestamp, timestamp),
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
        conn.execute("UPDATE ew_rule_sets SET updated_at = ? WHERE rule_set_id = ?", (now_iso(), rule_set["rule_set_id"]))
        updated = conn.execute("SELECT * FROM ew_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    return dict(updated)


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
                   source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], row["category"], row["title"], row["check_rule"] or row["title"],
                 row["source_text"], row["source_page"], "ocr" if row["check_mode"] == "ocr" else "auto",
                 row["source_type"], row["source_task_id"], row["scoring_json"], 0 if row["check_mode"] == "ocr" else 1, index, timestamp, timestamp),
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
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, check_rule, source_text, source_page, check_mode, source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], category, title, check_rule, str(item.get("source_text", "")).strip(),
                 item.get("source_page") if isinstance(item.get("source_page"), int) else None,
                 "ocr" if item.get("ocr_required") or item.get("check_mode") == "ocr" else "auto",
                 task_id, json.dumps(item.get("scoring"), ensure_ascii=False) if item.get("scoring") else None,
                 0 if item.get("ocr_required") or item.get("check_mode") == "ocr" else 1,
                 preserved_rule_count + index, timestamp, timestamp),
            )
        global_rule_count = 0
        global_rules = conn.execute(
            "SELECT * FROM ew_global_rules WHERE enabled = 1 ORDER BY category, sort_order, created_at"
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
                   source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'global', NULL, NULL, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], template["category"], template["title"], template["check_rule"],
                 template["source_text"], template["check_mode"], 0 if template["check_mode"] == "ocr" else 1, position, timestamp, timestamp),
            )
            global_rule_count += 1
        rule_set["global_rule_count"] = global_rule_count
        rule_set["preserved_rule_count"] = preserved_rule_count
    return rule_set


def confirm_rule_set(app, project_id: str) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set:
        raise ValueError("当前没有可确认的规则集")
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


def create_review_run(app, project_id: str, task_id: str, profile_id: str | None) -> dict:
    rule_set = current_rule_set(app, project_id)
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始实质性审查")
    value = {"review_run_id": str(uuid.uuid4()), "project_id": project_id, "rule_set_id": rule_set["rule_set_id"], "task_id": task_id, "profile_id": profile_id, "created_at": now_iso()}
    with connection(app) as conn:
        conn.execute("INSERT INTO ew_review_runs(review_run_id, project_id, rule_set_id, task_id, profile_id, created_at) VALUES (:review_run_id, :project_id, :rule_set_id, :task_id, :profile_id, :created_at)", value)
    return value


def save_review_results(app, review_run_id: str, document_id: str, results: list[dict]) -> None:
    timestamp = now_iso()
    with connection(app) as conn:
        for item in results:
            conn.execute(
                """INSERT INTO ew_review_results(review_result_id, review_run_id, document_id, rule_id, status, evidence, page_hint, reason, risk_level,
                   confidence, evidence_quality, automation_status, requires_review, review_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_run_id, document_id, rule_id) DO UPDATE SET
                status=excluded.status, evidence=excluded.evidence, page_hint=excluded.page_hint, reason=excluded.reason,
                risk_level=excluded.risk_level, confidence=excluded.confidence, evidence_quality=excluded.evidence_quality,
                automation_status=excluded.automation_status, requires_review=excluded.requires_review,
                review_reason=excluded.review_reason, final_status=NULL, confirmed_at=NULL, created_at=excluded.created_at""",
                (str(uuid.uuid4()), review_run_id, document_id, item["rule_id"], item["status"], item.get("evidence", ""),
                 item.get("page_hint"), item.get("reason", ""), item.get("risk_level", "medium"), item.get("confidence", "medium"),
                 item.get("evidence_quality", "limited"), item.get("automation_status", "needs_review"),
                 1 if item.get("requires_review", True) else 0, item.get("review_reason", ""), timestamp),
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
    results = [dict(row) for row in rows]
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
                        """SELECT rule_id, status, evidence, page_hint, reason, risk_level, confidence, evidence_quality,
                           automation_status, requires_review, review_reason FROM ew_review_results
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
                        """SELECT rule_id, suggested_score, effective_score, max_score, evidence, reason, confidence,
                           automation_status, requires_review, review_reason FROM ew_score_results
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
                   evidence, reason, confidence, automation_status, requires_review, review_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(score_run_id, document_id, rule_id) DO UPDATE SET
                suggested_score=excluded.suggested_score, final_score=excluded.final_score, effective_score=excluded.effective_score,
                max_score=excluded.max_score, evidence=excluded.evidence, reason=excluded.reason, confidence=excluded.confidence,
                automation_status=excluded.automation_status, requires_review=excluded.requires_review,
                review_reason=excluded.review_reason, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), score_run_id, document_id, item["rule_id"], item.get("suggested_score"), item.get("final_score"),
                 item.get("effective_score"), item.get("max_score"), item.get("evidence", ""), item.get("reason", ""), item.get("confidence"),
                 item.get("automation_status", "needs_review"), 1 if item.get("requires_review", True) else 0,
                 item.get("review_reason", ""), timestamp, timestamp),
            )


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
    return value, [dict(row) for row in rows]


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
