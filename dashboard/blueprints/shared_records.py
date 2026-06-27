# -*- coding: utf-8 -*-
import base64
import datetime
import json
import os
import sqlite3
import time
import uuid

from flask import Blueprint, current_app, jsonify, request, send_file


shared_records_bp = Blueprint(
    "shared_records",
    __name__,
    url_prefix="/dlsgzs/api/shared-records",
)

DB_NAME = "shared_records.db"
RETENTION_MS = 60 * 60 * 1000
RECENT_QUERY_OVERLAP_MS = 1000
CLEANUP_MIN_INTERVAL_MS = 60 * 1000
MAX_ORIGINAL_BYTES = 20 * 1024 * 1024
MAX_FACE_BYTES = 8 * 1024 * 1024
_last_cleanup_ms = 0


def _now_ms():
    return int(time.time() * 1000)


def _base_dir():
    try:
        configured = current_app.config.get("BASE_DIR")
    except RuntimeError:
        configured = None
    return configured or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir():
    path = os.path.join(_base_dir(), "..", "data")
    os.makedirs(path, exist_ok=True)
    return path


def _upload_dir():
    path = os.path.join(_base_dir(), "static", "uploads", "shared_records")
    os.makedirs(path, exist_ok=True)
    return path


def _db_path():
    return os.path.join(_data_dir(), DB_NAME)


def _get_conn():
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def init_shared_records_db():
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_records (
                share_id TEXT PRIMARY KEY,
                client_record_id TEXT,
                device_id TEXT,
                nickname TEXT,
                source TEXT,
                captured_at INTEGER,
                created_at INTEGER,
                expires_at INTEGER,
                metadata_json TEXT,
                original_filename TEXT,
                face_filename TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_shared_records_created_at ON shared_records(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shared_records_expires_at ON shared_records(expires_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shared_records_device_record ON shared_records(device_id, client_record_id)")
        conn.commit()
    finally:
        conn.close()
    cleanup_expired_shared_records()


def cleanup_expired_shared_records(force=False):
    global _last_cleanup_ms
    now = _now_ms()
    if not force and now - _last_cleanup_ms < CLEANUP_MIN_INTERVAL_MS:
        return
    _last_cleanup_ms = now

    expired_rows = []
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT share_id, original_filename, face_filename FROM shared_records WHERE expires_at <= ?",
            (now,),
        )
        expired_rows = c.fetchall()
    finally:
        conn.close()

    removable_ids = []
    for row in expired_rows:
        filenames = [row["original_filename"], row["face_filename"]]
        if _delete_uploaded_files(*filenames):
            removable_ids.append(row["share_id"])

    if removable_ids:
        placeholders = ",".join(["?"] * len(removable_ids))
        conn = _get_conn()
        try:
            conn.execute(
                f"DELETE FROM shared_records WHERE share_id IN ({placeholders})",
                tuple(removable_ids),
            )
            conn.commit()
        finally:
            conn.close()


def _read_metadata():
    if request.is_json:
        data = request.get_json(silent=True) or {}
        metadata = data.get("metadata", data)
        return metadata if isinstance(metadata, dict) else {}

    raw = request.form.get("metadata", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _file_bytes(field_name):
    file_storage = request.files.get(field_name)
    if file_storage and file_storage.filename:
        return file_storage.read()

    if request.is_json:
        data = request.get_json(silent=True) or {}
        raw = data.get(field_name)
        if isinstance(raw, str) and raw.strip():
            if "," in raw:
                raw = raw.split(",", 1)[1]
            try:
                return base64.b64decode(raw)
            except Exception:
                return None
    return None


def _save_image(share_id, suffix, image_bytes, max_bytes):
    if not image_bytes:
        return None, None
    if len(image_bytes) > max_bytes:
        return None, f"{suffix} image is too large"

    filename = f"{share_id}_{suffix}.jpg"
    path = os.path.join(_upload_dir(), filename)
    with open(path, "wb") as f:
        f.write(image_bytes)
    return filename, None


def _delete_uploaded_files(*filenames):
    upload_dir = _upload_dir()
    success = True
    for filename in filenames:
        if not filename:
            continue
        try:
            path = os.path.join(upload_dir, os.path.basename(filename))
            if os.path.exists(path):
                os.remove(path)
        except Exception as exc:
            success = False
            current_app.logger.warning("Failed to remove shared image %s: %s", filename, exc)
    return success


def _record_to_api(row):
    share_id = row["share_id"]
    metadata = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except Exception:
        metadata = {}

    return {
        "share_id": share_id,
        "client_record_id": row["client_record_id"],
        "device_id": row["device_id"],
        "nickname": row["nickname"] or "未命名设备",
        "source": row["source"] or "",
        "captured_at": row["captured_at"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "metadata": metadata,
        "original_url": f"/dlsgzs/api/shared-records/{share_id}/original" if row["original_filename"] else "",
        "face_url": f"/dlsgzs/api/shared-records/{share_id}/face" if row["face_filename"] else "",
    }


@shared_records_bp.route("", methods=["POST"])
@shared_records_bp.route("/", methods=["POST"])
def create_shared_record():
    cleanup_expired_shared_records(force=True)

    metadata = _read_metadata()
    if not metadata:
        return jsonify({"success": False, "error": "metadata is required"}), 400

    client_record_id = str(metadata.get("client_record_id") or "").strip()
    device_id = str(metadata.get("device_id") or "").strip()
    nickname = str(metadata.get("nickname") or "未命名设备").strip() or "未命名设备"
    source = str(metadata.get("source") or "").strip()

    if not client_record_id:
        return jsonify({"success": False, "error": "client_record_id is required"}), 400
    if not device_id:
        return jsonify({"success": False, "error": "device_id is required"}), 400

    try:
        captured_at = int(metadata.get("captured_at") or _now_ms())
    except Exception:
        captured_at = _now_ms()

    share_id = uuid.uuid4().hex
    now = _now_ms()
    expires_at = now + RETENTION_MS

    original_filename, original_error = _save_image(
        share_id,
        "original",
        _file_bytes("original_image"),
        MAX_ORIGINAL_BYTES,
    )
    if original_error:
        return jsonify({"success": False, "error": original_error}), 413

    face_filename, face_error = _save_image(
        share_id,
        "face",
        _file_bytes("face_image"),
        MAX_FACE_BYTES,
    )
    if face_error:
        _delete_uploaded_files(original_filename)
        return jsonify({"success": False, "error": face_error}), 413

    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO shared_records (
                share_id, client_record_id, device_id, nickname, source,
                captured_at, created_at, expires_at, metadata_json,
                original_filename, face_filename
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                share_id,
                client_record_id,
                device_id,
                nickname,
                source,
                captured_at,
                now,
                expires_at,
                json.dumps(metadata, ensure_ascii=False),
                original_filename,
                face_filename,
            ),
        )
        conn.commit()
    except Exception as exc:
        _delete_uploaded_files(original_filename, face_filename)
        current_app.logger.exception("Failed to create shared record")
        return jsonify({"success": False, "error": f"failed to create shared record: {exc}"}), 500
    finally:
        conn.close()

    return jsonify({"success": True, "share_id": share_id, "expires_at": expires_at})


@shared_records_bp.route("/recent", methods=["GET"])
def recent_shared_records():
    cleanup_expired_shared_records()
    now = _now_ms()

    try:
        since = int(request.args.get("since", "0") or "0")
    except Exception:
        since = 0
    since = max(0, since)
    effective_since = max(0, since - RECENT_QUERY_OVERLAP_MS)

    device_id = request.args.get("device_id", "").strip()
    include_self = request.args.get("include_self", "").strip().lower() in ("1", "true", "yes")
    limit = 200

    conditions = ["expires_at > ?"]
    params = [now]
    if since > 0:
        conditions.append("created_at >= ?")
        params.append(effective_since)
    if device_id and not include_self:
        conditions.append("device_id != ?")
        params.append(device_id)

    sql = (
        "SELECT * FROM shared_records WHERE "
        + " AND ".join(conditions)
        + " ORDER BY created_at ASC LIMIT ?"
    )
    params.append(limit)

    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(sql, tuple(params))
        rows = c.fetchall()
        items = [_record_to_api(row) for row in rows]
    finally:
        conn.close()

    max_created_at = max([item["created_at"] for item in items], default=now)
    return jsonify(
        {
            "success": True,
            "items": items,
            "next_cursor": max_created_at,
            "server_time": now,
            "retention_ms": RETENTION_MS,
        }
    )


def _send_shared_image(share_id, column_name):
    cleanup_expired_shared_records()
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            f"SELECT {column_name}, expires_at FROM shared_records WHERE share_id = ?",
            (share_id,),
        )
        row = c.fetchone()
    finally:
        conn.close()

    if not row or row["expires_at"] <= _now_ms():
        return jsonify({"success": False, "error": "shared image expired or not found"}), 404

    filename = row[column_name]
    if not filename:
        return jsonify({"success": False, "error": "shared image not found"}), 404

    path = os.path.join(_upload_dir(), os.path.basename(filename))
    if not os.path.exists(path):
        return jsonify({"success": False, "error": "shared image file missing"}), 404

    return send_file(path, mimetype="image/jpeg", conditional=True)


@shared_records_bp.route("/<share_id>/original", methods=["GET"])
def shared_record_original(share_id):
    return _send_shared_image(share_id, "original_filename")


@shared_records_bp.route("/<share_id>/face", methods=["GET"])
def shared_record_face(share_id):
    return _send_shared_image(share_id, "face_filename")
