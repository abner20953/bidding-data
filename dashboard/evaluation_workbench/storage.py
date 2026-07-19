"""评标工作台 SQLite 与文件存储。保持与现有业务模块隔离。"""

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


MAX_BID_DOCUMENTS = 10
MAX_QUEUED_TASKS = 3
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_SCORE_TOTAL_PATTERN = re.compile(r"(?:总计|共计|合计|最高(?:得)?|最多(?:得)?|满分(?:为)?)\s*(\d+(?:\.\d+)?)\s*分")
_SCORE_VALUE_PATTERN = re.compile(r"(?:得|扣)\s*(\d+(?:\.\d+)?)\s*分")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ew_model_calls_project ON ew_model_calls(project_id, created_at);
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
                source_text TEXT NOT NULL DEFAULT '',
                source_page INTEGER,
                check_mode TEXT NOT NULL DEFAULT 'auto',
                scoring_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
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
        _ensure_column(conn, "ew_rules", "source_type", "TEXT")
        _ensure_column(conn, "ew_rules", "source_task_id", "TEXT")
        conn.execute("UPDATE ew_rules SET source_type = CASE WHEN rule_set_id IN (SELECT rule_set_id FROM ew_rule_sets WHERE source_task_id IS NOT NULL) THEN 'ai' ELSE 'manual' END WHERE source_type IS NULL OR source_type = ''")
        _seed_default_profiles(conn)
    app.extensions["evaluation_workbench_database"] = marker


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
        raise ValueError("评标工作台密钥文件无效，无法读取已保存的模型 API Key") from exc


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
    project_dir(app, project_id)
    return get_project(app, project_id)


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
    original_name = Path(upload.filename or "").name
    extension = Path(original_name).suffix.lower()
    if extension not in {".pdf", ".docx"}:
        raise ValueError("评标工作台目前仅支持 PDF 和 DOCX 文件")
    if role not in {"tender", "tender_attachment", "bid"}:
        raise ValueError("不支持的文件角色")
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
                    raise ValueError(f"单个文件不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
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
        "bidder_name": bidder_name.strip(),
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
    value = {
        "task_type": task_type,
        "prompt_version": prompt_version,
        "documents": sorted((item["document_id"], item["sha256"], item.get("updated_at"), item.get("parse_status")) for item in documents),
        "rule_set": (rule_set or {}).get("rule_set_id"),
        "rule_set_updated_at": (rule_set or {}).get("updated_at"),
        "profile": (profile.get("profile_id"), profile.get("model_name"), profile.get("base_url"), profile.get("updated_at"), profile.get("json_mode"), profile.get("thinking_mode")),
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


def record_model_call(app, task_id: str, project_id: str, phase: str, profile_id: str | None,
                      *, document_id: str | None = None, input_chars: int = 0,
                      context_mode: str = "full", usage: dict | None = None) -> None:
    """保存供应商返回的用量；不保存提示词、正文或密钥。"""
    usage = usage or {}

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
               context_mode, input_chars, prompt_tokens, completion_tokens, total_tokens, cache_hit_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id, project_id, document_id, phase, profile_id, context_mode,
             max(0, int(input_chars)), prompt_tokens, completion_tokens, total_tokens,
             number("prompt_cache_hit_tokens", "cache_hit_tokens", "cached_tokens"), now_iso()),
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
    return [_public_model_profile(dict(row)) for row in rows]


def get_model_profile(app, profile_id: str | None, preferred_model: str = "") -> dict:
    with connection(app) as conn:
        if profile_id:
            row = conn.execute("SELECT * FROM ew_model_profiles WHERE profile_id = ? AND enabled = 1", (profile_id,)).fetchone()
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
        "thinking_mode": payload.get("thinking_mode") if payload.get("thinking_mode") in {"default", "enabled", "disabled"} else "default",
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
    if category not in {"qualification", "compliance", "substantive", "rejection", "objective", "subjective"}:
        raise ValueError("不支持的规则分类")
    timestamp = now_iso()
    rule = {
        "rule_id": str(uuid.uuid4()), "rule_set_id": rule_set["rule_set_id"], "category": category,
        "title": title, "source_text": str(payload.get("source_text", "")).strip(),
        "source_page": int(payload["source_page"]) if str(payload.get("source_page", "")).isdigit() else None,
        "check_mode": "auto" if payload.get("check_mode", "auto") == "auto" else "manual",
        "source_type": "manual", "source_task_id": None,
        "scoring_json": json.dumps(payload.get("scoring"), ensure_ascii=False) if payload.get("scoring") else None,
        "enabled": 1, "sort_order": int(payload.get("sort_order") or 0), "created_at": timestamp, "updated_at": timestamp,
    }
    with connection(app) as conn:
        conn.execute(
            """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, source_text, source_page, check_mode,
            source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
            VALUES (:rule_id, :rule_set_id, :category, :title, :source_text, :source_page, :check_mode,
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
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, source_text, source_page, check_mode,
                   source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), draft["rule_set_id"], row["category"], row["title"], row["source_text"], row["source_page"],
                 row["check_mode"], row["source_type"] or "manual", row["source_task_id"], row["scoring_json"], row["enabled"], row["sort_order"], timestamp, timestamp),
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
        raise ValueError("只能修改待确认规则集中的评分信息")
    with connection(app) as conn:
        row = conn.execute("SELECT * FROM ew_rules WHERE rule_id = ? AND rule_set_id = ?", (rule_id, rule_set["rule_set_id"])).fetchone()
        if not row:
            raise ValueError("规则不存在")
        rule = dict(row)
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
        conn.execute("UPDATE ew_rules SET scoring_json = ?, updated_at = ? WHERE rule_id = ?", (json.dumps(current, ensure_ascii=False), now_iso(), rule_id))
        conn.execute("UPDATE ew_rule_sets SET updated_at = ? WHERE rule_set_id = ?", (now_iso(), rule_set["rule_set_id"]))
        updated = conn.execute("SELECT * FROM ew_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    return dict(updated)


def replace_rules_from_extraction(app, project_id: str, task_id: str, rules: list[dict]) -> dict:
    with connection(app) as conn:
        prior = conn.execute("SELECT MAX(version) FROM ew_rule_sets WHERE project_id = ?", (project_id,)).fetchone()[0] or 0
        timestamp = now_iso()
        rule_set = {"rule_set_id": str(uuid.uuid4()), "project_id": project_id, "version": prior + 1, "status": "draft", "source_task_id": task_id, "created_at": timestamp, "updated_at": timestamp}
        conn.execute("UPDATE ew_rule_sets SET status = 'superseded', updated_at = ? WHERE project_id = ? AND status != 'superseded'", (timestamp, project_id))
        conn.execute("INSERT INTO ew_rule_sets(rule_set_id, project_id, version, status, source_task_id, created_at, updated_at) VALUES (:rule_set_id, :project_id, :version, :status, :source_task_id, :created_at, :updated_at)", rule_set)
        for index, item in enumerate(rules):
            title = str(item.get("title", "")).strip()
            category = str(item.get("category", "")).strip()
            if not title or category not in {"qualification", "compliance", "substantive", "rejection", "objective", "subjective"}:
                continue
            conn.execute(
                """INSERT INTO ew_rules(rule_id, rule_set_id, category, title, source_text, source_page, check_mode, source_type, source_task_id, scoring_json, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?, 1, ?, ?, ?)""",
                (str(uuid.uuid4()), rule_set["rule_set_id"], category, title, str(item.get("source_text", "")).strip(),
                 item.get("source_page") if isinstance(item.get("source_page"), int) else None,
                 "manual" if item.get("check_mode") == "manual" else "auto",
                 task_id, json.dumps(item.get("scoring"), ensure_ascii=False) if item.get("scoring") else None, index, timestamp, timestamp),
            )
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
                """INSERT INTO ew_review_results(review_result_id, review_run_id, document_id, rule_id, status, evidence, page_hint, reason, risk_level, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_run_id, document_id, rule_id) DO UPDATE SET
                status=excluded.status, evidence=excluded.evidence, page_hint=excluded.page_hint, reason=excluded.reason,
                risk_level=excluded.risk_level, final_status=NULL, confirmed_at=NULL, created_at=excluded.created_at""",
                (str(uuid.uuid4()), review_run_id, document_id, item["rule_id"], item["status"], item.get("evidence", ""),
                 item.get("page_hint"), item.get("reason", ""), item.get("risk_level", "medium"), timestamp),
            )


def latest_review_results(app, project_id: str) -> tuple[dict | None, list[dict]]:
    with connection(app) as conn:
        run = conn.execute(
            """SELECT r.* FROM ew_review_runs r JOIN ew_tasks t ON t.task_id = r.task_id
               WHERE r.project_id = ? AND t.status = 'success'
               AND r.rule_set_id = (SELECT rule_set_id FROM ew_rule_sets WHERE project_id = ? ORDER BY version DESC LIMIT 1)
               ORDER BY r.created_at DESC LIMIT 1""", (project_id, project_id)
        ).fetchone()
        if not run:
            return None, []
        rows = conn.execute(
            """SELECT r.*, d.bidder_name, d.original_name, rule.category, rule.title
            FROM ew_review_results r
            JOIN ew_documents d ON d.document_id = r.document_id
            JOIN ew_rules rule ON rule.rule_id = r.rule_id
            WHERE r.review_run_id = ?
            ORDER BY d.bidder_name,
                CASE r.risk_level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC,
                rule.category, rule.sort_order""", (run["review_run_id"],)
        ).fetchall()
    return dict(run), [dict(row) for row in rows]


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
                """INSERT INTO ew_score_results(score_result_id, score_run_id, document_id, rule_id, suggested_score, final_score, max_score, evidence, reason, confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(score_run_id, document_id, rule_id) DO UPDATE SET
                suggested_score=excluded.suggested_score, final_score=excluded.final_score, max_score=excluded.max_score,
                evidence=excluded.evidence, reason=excluded.reason, confidence=excluded.confidence, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), score_run_id, document_id, item["rule_id"], item.get("suggested_score"), item.get("final_score"), item.get("max_score"), item.get("evidence", ""), item.get("reason", ""), item.get("confidence"), timestamp, timestamp),
            )


def latest_score_results(app, project_id: str, score_type: str) -> tuple[dict | None, list[dict]]:
    with connection(app) as conn:
        run = conn.execute(
            """SELECT r.* FROM ew_score_runs r JOIN ew_tasks t ON t.task_id = r.task_id
               WHERE r.project_id = ? AND r.score_type = ? AND t.status = 'success'
               AND r.rule_set_id = (SELECT rule_set_id FROM ew_rule_sets WHERE project_id = ? ORDER BY version DESC LIMIT 1)
               ORDER BY r.created_at DESC LIMIT 1""",
            (project_id, score_type, project_id),
        ).fetchone()
        if not run:
            return None, []
        rows = conn.execute(
            """SELECT s.*, d.bidder_name, d.original_name, rule.title
            FROM ew_score_results s JOIN ew_documents d ON d.document_id=s.document_id
            JOIN ew_rules rule ON rule.rule_id=s.rule_id
            WHERE s.score_run_id=? ORDER BY d.bidder_name, rule.sort_order""", (run["score_run_id"],)
        ).fetchall()
    return dict(run), [dict(row) for row in rows]


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
        conn.execute("UPDATE ew_score_results SET final_score = ?, updated_at = ? WHERE score_result_id = ?", (final_score, now_iso(), score_result_id))
        updated = conn.execute("SELECT * FROM ew_score_results WHERE score_result_id = ?", (score_result_id,)).fetchone()
    return dict(updated)
