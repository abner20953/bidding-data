"""工作台独立 Blueprint。"""

from __future__ import annotations

import os
import hmac
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session
from werkzeug.security import check_password_hash

from dashboard.evaluation_workbench import storage
from dashboard.evaluation_workbench.ai_gateway import model_capabilities, test_connection
from dashboard.evaluation_workbench.prompt_templates import EVALUATION_PROMPT_VERSION


evaluation_workbench_bp = Blueprint("evaluation_workbench", __name__)
TASK_PROMPT_VERSION = EVALUATION_PROMPT_VERSION
MODEL_CONFIGURATION_PASSWORD = "108"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROCESS_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_DEPLOY_COMMIT_CACHE: dict[str, str] = {}


def _read_git_head_commit(base: Path) -> str:
    """不依赖 git 命令，直接读取 .git 的 HEAD 得到短提交号。"""
    git_dir = base / ".git"
    if git_dir.is_file():
        # git worktree 场景：.git 是文本文件，指向真实 git 目录。
        try:
            raw = git_dir.read_text(encoding="utf-8").strip()
            if raw.startswith("gitdir:"):
                target = raw[len("gitdir:"):].strip()
                git_dir = (target if Path(target).is_absolute() else base / target).resolve()
        except OSError:
            return ""
    if not git_dir.is_dir():
        return ""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        ref_path = git_dir / ref
        try:
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip()[:7]
        except OSError:
            pass
        try:
            packed = (git_dir / "packed-refs").read_text(encoding="utf-8", errors="ignore")
            for line in packed.splitlines():
                if line and not line.startswith("#") and line.strip().endswith(ref):
                    return line.split()[0][:7]
        except OSError:
            pass
        return ""
    return head[:7]


def _current_deploy_commit() -> str:
    """返回当前运行代码的 Git 短提交号；优先环境变量，其次部署文件，最后直接读 .git。"""
    if "value" in _DEPLOY_COMMIT_CACHE:
        return _DEPLOY_COMMIT_CACHE["value"]
    value = os.environ.get("DEPLOY_COMMIT", "").strip()[:7]
    if not value:
        for base in [_PROJECT_ROOT, _PROJECT_ROOT / "tools"]:
            try:
                value = (base / ".deploy-commit").read_text(encoding="utf-8").strip()[:7]
            except OSError:
                continue
            if value:
                break
    if not value:
        for base in [_PROJECT_ROOT, _PROJECT_ROOT / "tools"]:
            value = _read_git_head_commit(base)
            if value:
                break
    _DEPLOY_COMMIT_CACHE["value"] = value
    return value


_REPORT_ROLE_LABELS = {"tender": "主招标文件", "tender_attachment": "招标附件", "bid": "投标文件"}
_REPORT_PARSE_STATUS_LABELS = {"pending": "待解析", "queued": "排队中", "running": "解析中", "success": "解析完成", "error": "解析失败"}
_REPORT_RULE_SET_STATUS_LABELS = {"draft": "待确认", "confirmed": "已确认", "superseded": "已替换"}
_REPORT_CATEGORY_LABELS = {
    "qualification": "资格性", "compliance": "符合性", "substantive": "实质性/废标项",
    "rejection": "实质性/废标项", "other": "其他规则", "objective": "客观分", "subjective": "主观分",
}
_REPORT_STATUS_LABELS = {
    "satisfied": "满足", "not_satisfied": "不满足", "partial": "部分满足",
    "not_found": "未找到证据", "manual": "需人工判断", "ocr_required": "需 OCR 后判定",
}
_REPORT_RISK_LABELS = {"high": "高风险", "medium": "中风险", "low": "低风险"}
_REPORT_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
_REPORT_EVIDENCE_LABELS = {"sufficient": "充分", "limited": "有限", "missing": "缺失"}
_REPORT_VISION_STATUS_LABELS = {
    "applied": "图片识别已采纳",
    "applied_partial": "图片识别已补充部分事实",
    "conflict": "图片检查发现疑似字段冲突",
    "uncovered": "图片识别已执行，未覆盖关键材料",
    "failed": "图片识别失败，已保留文字结论",
    "unavailable": "未获得可用的多模态模型",
    "not_located": "未定位到可靠图片页",
    "skipped_text_sufficient": "文字证据充分，未调用图片模型",
    "ocr_applied": "OCR 已核验并采纳",
    "ocr_applied_partial": "OCR 已补充部分文字事实",
    "ocr_uncovered": "OCR 已执行，未覆盖关键材料",
    "ocr_failed": "OCR 未获得可用文字，已保留文字结论",
    "ocr_quota_exhausted": "腾讯 OCR 额度不足，已转图片识别",
    "ocr_not_located": "未定位到可靠 OCR 候选页",
    "ocr_skipped_text_sufficient": "文字证据充分，未调用 OCR",
    "ocr_vision_applied": "OCR 与图片检查均已采纳",
    "ocr_vision_applied_partial": "OCR 与图片检查已补充部分事实",
    "ocr_vision_conflict": "OCR后图片检查发现疑似字段冲突",
}
_REPORT_AI_DECISION_LABELS = {
    "pending_human_review": "待 AI 复核", "no_signal_detected": "未发现线索",
    "confirmed_clue": "AI 确认线索", "suspected_clue": "AI 疑似线索",
    "excluded": "AI 倾向排除", "unassessable": "AI 证据不足",
}


def _report_label(labels: dict, value: object, default: str = "-") -> str:
    value = str(value or "").strip()
    return labels.get(value, value or default)


_REPORT_INTERNAL_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])SI-\d+(?![0-9A-Za-z])")
_REPORT_FIELD_NOTATION_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?:status|risk_level|evidence_quality|confidence|suggested_score|max_score|"
    r"matched_count|needs_ocr|coverage_status|final_score|effective_score|scope|validity|met)"
    r"\s*=\s*[^，。；;：:\s]+"
)


def _clean_report_text(value: object) -> str:
    """与网页端一致的展示清洗：去掉内部编号（SI-1/SI-2）与 JSON 字段名记法
    （status=、suggested_score= 等）、统一页码格式（第P55页 → 第55页）。"""
    text = str(value or "")
    text = _REPORT_INTERNAL_ID_PATTERN.sub("", text)
    text = _REPORT_FIELD_NOTATION_PATTERN.sub("", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    text = text.replace("计分过程：", "")
    text = re.sub(r"第P(\d+)-P?(\d+)页", r"第\1-\2页", text)
    text = re.sub(r"第P(\d+)页", r"第\1页", text)
    text = re.sub(r"(^|[^0-9A-Za-z])P(\d+)-P?(\d+)(?![0-9A-Za-z])", r"\1第\2-\3页", text)
    text = re.sub(r"(^|[^0-9A-Za-z])P(\d+)(?![0-9A-Za-z])", r"\1第\2页", text)
    return text


def _report_compact_text(value: object, limit: int = 240) -> str:
    """压缩打印报告中的长文本，不改变网页端原始结果。"""
    text = re.sub(r"\s+", " ", _clean_report_text(value)).strip()
    return text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"


def _report_brief(parts: list[str], limit: int) -> str:
    """拼接报告摘要且不再做整体清洗，保留各部分已经带上的语义标签（如"计分过程："）。"""
    text = re.sub(r"\s+", " ", "；".join(part for part in parts if part)).strip()
    return text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"


def _report_result_explanation(value: object, rule: dict) -> str:
    """与网页端保持一致：结果区不重复显示已经单列的检查规则。"""
    text = str(value or "").strip()
    candidates = sorted(
        (str(rule.get(key) or "").strip() for key in ("check_rule", "title")),
        key=len,
        reverse=True,
    )
    for candidate in candidates:
        if len(candidate) >= 6:
            text = text.replace(candidate, "")
    text = re.sub(r"(?:本|该)?规则(?:要求|规定|需|是)?[：:，,；;\s]*", "", text)
    return re.sub(r"^[，。；:：\s]+|[，；:：\s]+$", "", text).strip()


def _report_generated_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "-")


def _project_display_name(project: dict) -> str:
    """同名项目在报告标题中优先带上标段/包号，避免浏览器标签混淆。"""
    name = str(project.get("name") or "未命名项目").strip()
    section = str(project.get("section_name") or "").strip()
    return f"{name} · {section}" if section else name


def _report_compact_objective_ocr_text(value: object) -> str:
    """兼容旧结果：打印客观分时只保留 OCR 结论摘要，不展示整页识别原文。"""
    text = str(value or "")
    text = re.sub(
        r"【(腾讯OCR|本地OCR|OCR)原文·([^】]+)】[\s\S]*?(?=【(?:图片识别|腾讯OCR|本地OCR|OCR)|$)",
        r"【\1摘要·\2】已完成候选页文字识别；原始识别明细已省略。",
        text,
    )

    def compact(match: re.Match) -> str:
        body = re.sub(r"\s+", " ", match.group(3) or "").strip()
        return f"【{match.group(1)}摘要·{match.group(2)}】{body[:220]}{'…' if len(body) > 220 else ''}"

    return re.sub(
        r"【(腾讯OCR|本地OCR|OCR)·([^】]+)】([\s\S]*?)(?=【(?:图片识别|腾讯OCR|本地OCR|OCR)|$)",
        compact,
        text,
    ).strip()


def _report_presentation(documents: list[dict], rule_set: dict | None, rules: list[dict],
                         compare_task: dict | None, compare_pairs: list[dict], reviews: list[dict],
                         objective_scores: list[dict], subjective_scores: list[dict]) -> dict:
    """生成仅用于打印的中文摘要视图，所有判断仍直接来自已保存结果。"""
    displayed_documents = [{
        **item,
        "role_label": _report_label(_REPORT_ROLE_LABELS, item.get("role")),
        "parse_status_label": _report_label(_REPORT_PARSE_STATUS_LABELS, item.get("parse_status")),
    } for item in documents]
    displayed_rules = [{
        **item,
        "category_label": _report_label(_REPORT_CATEGORY_LABELS, item.get("category")),
        "source_text_brief": _report_compact_text(item.get("source_text"), 160),
    } for item in rules]

    def present_result(item: dict, *, score: bool = False, objective: bool = False) -> dict:
        value = dict(item)
        value["title_brief"] = _report_compact_text(value.get("title") or value.get("check_rule"), 72)
        evidence = _report_compact_objective_ocr_text(value.get("evidence")) if objective else value.get("evidence")
        reason = _report_compact_objective_ocr_text(value.get("reason")) if objective else value.get("reason")
        layers = value.get("evidence_layers") if isinstance(value.get("evidence_layers"), list) else []
        layer_summaries = [
            _report_compact_text(layer.get("summary"), 140) for layer in layers
            if isinstance(layer, dict) and str(layer.get("summary") or "").strip()
        ]
        # 计分过程是评分留痕的关键一层，必须保留在报告正文；其余补充取最近两层，
        # 避免“计分过程+腾讯OCR+图片识别”三层时把计分过程挤出报告。
        calculation_summaries = [
            f"计分过程：{_report_compact_text(layer.get('summary'), 130)}"
            for layer in layers
            if isinstance(layer, dict) and layer.get("source") == "score_calculation"
            and str(layer.get("summary") or "").strip()
        ]
        other_summaries = [
            summary for layer, summary in zip(layers, layer_summaries)
            if not (isinstance(layer, dict) and layer.get("source") == "score_calculation")
        ]
        stored_summary = _report_compact_text(value.get("conclusion_summary"), 90)
        if stored_summary:
            value["evidence_brief"] = stored_summary
        else:
            evidence_parts = other_summaries[-2:] + calculation_summaries + [_report_result_explanation(evidence, value)]
            value["evidence_brief"] = _report_brief(evidence_parts, 260)
        value["reason_brief"] = _report_compact_text(_report_result_explanation(reason, value), 200)
        value["confidence_label"] = _report_label(_REPORT_CONFIDENCE_LABELS, value.get("confidence"))
        vision_status = str(value.get("vision_status") or "not_requested")
        if vision_status != "not_requested":
            checked_pages = "、".join(f"P{page}" for page in value.get("vision_pages") or [])
            evidence_pages = "、".join(f"P{page}" for page in value.get("vision_evidence_pages") or [])
            value["vision_summary"] = " · ".join(part for part in (
                _report_label(_REPORT_VISION_STATUS_LABELS, vision_status),
                str(value.get("vision_model") or "").strip(),
                f"检查页：{checked_pages}" if checked_pages else "",
                f"证据页：{evidence_pages}" if evidence_pages else "",
            ) if part and part != "-")
        else:
            value["vision_summary"] = ""
        if score:
            value["suggested_score_label"] = value.get("suggested_score")
            if value["suggested_score_label"] is None:
                value["suggested_score_label"] = "待 OCR 后评分" if value.get("coverage_status") == "uncovered" else (
                    "需 OCR 后评分" if value.get("check_mode") == "ocr" else "-"
                )
        else:
            value["status_label"] = _report_label(_REPORT_STATUS_LABELS, value.get("status"))
            value["risk_label"] = _report_label(_REPORT_RISK_LABELS, value.get("risk_level"))
            value["evidence_quality_label"] = _report_label(_REPORT_EVIDENCE_LABELS, value.get("evidence_quality"))
        return value

    cross_analysis = ((compare_task or {}).get("result") or {}).get("cross_bid_analysis")
    compare_summary_rows: list[dict] = []
    compare_signal_rows: list[dict] = []
    if isinstance(cross_analysis, dict):
        for item in cross_analysis.get("pair_summaries") or []:
            assessment = item.get("ai_assessment") if isinstance(item.get("ai_assessment"), dict) else {}
            compare_summary_rows.append({
                "bidder_a": item.get("bidder_a") or "文件 A", "bidder_b": item.get("bidder_b") or "文件 B",
                "dimensions": "、".join(item.get("dimension_labels") or []) or "未发现",
                "signal_count": item.get("signal_count") or 0,
                "priority_label": _report_label({"high": "高", "medium": "中", "normal": "常规", "none": "无"}, item.get("review_priority")),
                "decision_label": _report_label(_REPORT_AI_DECISION_LABELS, assessment.get("decision") or item.get("assessment_result")),
            })
        for item in cross_analysis.get("signals") or []:
            assessment = item.get("ai_assessment") if isinstance(item.get("ai_assessment"), dict) else {}
            compare_signal_rows.append({
                "bidder_a": item.get("bidder_a") or "文件 A", "bidder_b": item.get("bidder_b") or "文件 B",
                "dimension_label": item.get("dimension_label") or "-",
                "basis": _report_compact_text(item.get("basis"), 180),
                "decision_label": _report_label(_REPORT_AI_DECISION_LABELS, assessment.get("decision"), "待 AI 判定"),
                "risk_label": _report_label(_REPORT_RISK_LABELS, assessment.get("risk_level")),
                "confidence_label": _report_label(_REPORT_CONFIDENCE_LABELS, assessment.get("confidence")),
                "reason": _report_compact_text(assessment.get("reason"), 160),
            })
    compare_pair_rows = []
    for item in compare_pairs:
        summary = item.get("result", {}).get("summary", {}) if isinstance(item.get("result"), dict) else {}
        compare_pair_rows.append({
            "bidder_a": item.get("bidder_a") or item.get("filename_a") or "文件 A",
            "bidder_b": item.get("bidder_b") or item.get("filename_b") or "文件 B",
            "exact": summary.get("exact") or 0, "fuzzy": summary.get("fuzzy") or 0,
            "shared_error": summary.get("shared_error") or 0, "entity": summary.get("entity") or 0,
            "ratio_a": summary.get("matched_ratio_a") or 0, "ratio_b": summary.get("matched_ratio_b") or 0,
        })
    return {
        "documents": displayed_documents,
        "rule_set_status_label": _report_label(_REPORT_RULE_SET_STATUS_LABELS, (rule_set or {}).get("status")),
        "rules": displayed_rules,
        "reviews": [present_result(item) for item in reviews],
        "objective_scores": [present_result(item, score=True, objective=True) for item in objective_scores],
        "subjective_scores": [present_result(item, score=True) for item in subjective_scores],
        "cross_analysis": cross_analysis if isinstance(cross_analysis, dict) else None,
        "compare_summary_rows": compare_summary_rows,
        "compare_signal_rows": compare_signal_rows,
        "compare_pair_rows": compare_pair_rows,
    }


def create_worker_app():
    """供独立 worker 读取同一数据目录，避免导入整个 dashboard.app。"""
    from flask import Flask

    app = Flask("evaluation_workbench_worker", template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
    configured = os.environ.get("EVALUATION_WORKBENCH_DATA_DIR")
    if configured:
        app.config["EVALUATION_WORKBENCH_DATA_DIR"] = configured
    return app


def _init() -> None:
    storage.init_database(current_app)


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


def _new_project_password_is_valid(value: object) -> tuple[bool, str | None]:
    """校验新建项目口令；口令只从运行环境读取。"""
    configured_plaintext = (
        current_app.config.get("EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD")
        or os.environ.get("EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD")
    )
    if configured_plaintext:
        if not isinstance(value, str) or not hmac.compare_digest(value, str(configured_plaintext)):
            return False, "新建项目口令错误"
        return True, None

    configured_hash = (
        current_app.config.get("EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD_HASH")
        or os.environ.get("EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD_HASH")
    )
    if not configured_hash:
        return False, "新建项目口令尚未在运行环境配置"
    if not isinstance(value, str) or not check_password_hash(configured_hash, value):
        return False, "新建项目口令错误"
    return True, None


def _model_configuration_is_unlocked() -> bool:
    return bool(session.get("evaluation_workbench_model_configuration_unlocked"))


def _model_configuration_access_error():
    if _model_configuration_is_unlocked():
        return None
    return jsonify({"error": "请先输入配置口令"}), 403


def _global_rule_mutation_access_error(data: dict):
    """通用规则可公开查看，但每次变更都需单独核验口令。"""
    if hmac.compare_digest(str(data.get("password", "")), MODEL_CONFIGURATION_PASSWORD):
        return None
    return jsonify({"error": "新增、修改或删除通用规则前请输入正确口令"}), 403


def _prompt_template_mutation_access_error(data: dict):
    """提示词允许公开查看，但每次保存或恢复默认都需单独校验口令。"""
    if hmac.compare_digest(str(data.get("password", "")), MODEL_CONFIGURATION_PASSWORD):
        return None
    return jsonify({"error": "修改或恢复默认提示词前请输入正确口令"}), 403


def _project_or_404(project_id: str):
    project = storage.get_project(current_app, project_id)
    if not project:
        return None, (jsonify({"error": "评标项目不存在"}), 404)
    return project, None


def _worker_lock_path() -> Path:
    return storage.data_dir(current_app) / "worker.lock"


def _worker_log_stream():
    """保留小体积 worker 诊断日志；只留当前文件和一份备份，避免长期占用磁盘。"""
    path = storage.data_dir(current_app) / "worker.log"
    backup = storage.data_dir(current_app) / "worker.log.1"
    try:
        if path.exists() and path.stat().st_size >= 2 * 1024 * 1024:
            backup.unlink(missing_ok=True)
            path.replace(backup)
        return path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return None


def _start_worker_if_needed() -> None:
    has_queued = storage.has_queued_tasks(current_app)
    has_running = storage.has_running_tasks(current_app)
    if not has_queued and not has_running:
        return
    lock_path = _worker_lock_path()
    if lock_path.exists():
        active = False
        try:
            content = lock_path.read_text(encoding="utf-8").strip()
            if content.isdigit():
                os.kill(int(content), 0)
                active = True
            elif time.time() - lock_path.stat().st_mtime < 30:
                active = True
        except (OSError, ValueError):
            active = False
        if active:
            return
        lock_path.unlink(missing_ok=True)
    if has_running:
        storage.interrupt_stale_running_tasks(current_app)
    if not has_queued:
        return
    try:
        # 原子占位避免两个 HTTP 请求同时启动两个 worker；worker 完成后负责移除。
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write("starting")
    except FileExistsError:
        return
    env = os.environ.copy()
    env["EVALUATION_WORKBENCH_DATA_DIR"] = str(storage.data_dir(current_app))
    log_stream = _worker_log_stream()
    try:
        subprocess.Popen(
            [sys.executable, "-m", "dashboard.evaluation_workbench.worker"],
            cwd=str(Path(current_app.root_path).parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_stream or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_stream else subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise
    finally:
        if log_stream:
            log_stream.close()


@evaluation_workbench_bp.route("/pingbiao")
def evaluation_workbench_view():
    _init()
    return render_template("evaluation_workbench/index.html")


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects", methods=["GET", "POST"])
def projects_api():
    _init()
    if request.method == "GET":
        return jsonify({"projects": storage.list_projects(current_app)})
    data = _json_body()
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "请填写项目名称"}), 400
    password_ok, password_error = _new_project_password_is_valid(data.get("password"))
    if not password_ok:
        return jsonify({"error": password_error}), 403
    project = storage.create_project(current_app, name, str(data.get("project_number", "")), str(data.get("section_name", "")))
    return jsonify({"project": project}), 201


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>", methods=["GET", "PATCH", "DELETE"])
def project_api(project_id):
    _init()
    project, error = _project_or_404(project_id)
    if error:
        return error
    if request.method == "GET":
        _start_worker_if_needed()
        return jsonify({
            "project": project,
            "documents": storage.list_documents(current_app, project_id),
            "tasks": storage.list_task_summaries(current_app, project_id),
            # 新字段：仅供排队提示使用，不改变原 tasks 列表的既有结构。
            "queue_contexts": storage.task_queue_contexts(current_app, project_id),
        })
    if request.method == "DELETE":
        try:
            storage.delete_project(current_app, project_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success"})
    data = _json_body()
    fields = {key: str(data[key]).strip() for key in ("name", "project_number", "section_name") if key in data}
    if "name" in fields and not fields["name"]:
        return jsonify({"error": "项目名称不能为空"}), 400
    if fields:
        fields["updated_at"] = storage.now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with storage.connection(current_app) as conn:
            conn.execute(f"UPDATE ew_projects SET {assignments} WHERE project_id = ?", [*fields.values(), project_id])
    return jsonify({"project": storage.get_project(current_app, project_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/documents", methods=["POST"])
def documents_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "请选择文件"}), 400
    try:
        document = storage.store_upload(
            current_app,
            project_id,
            str(request.form.get("role", "")),
            str(request.form.get("bidder_name", "")),
            upload,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"document": document}), 201


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/documents/<document_id>", methods=["DELETE"])
def delete_document_api(project_id, document_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    try:
        storage.delete_document(current_app, project_id, document_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"status": "success"})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/documents/<document_id>/download", methods=["GET"])
def download_document_api(project_id, document_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    document = next(
        (
            item
            for item in storage.list_documents(current_app, project_id)
            if item["document_id"] == document_id
        ),
        None,
    )
    if document is None:
        return jsonify({"error": "文件不存在"}), 404
    path = storage.document_path(current_app, document)
    if not path.exists():
        return jsonify({"error": "源文件已丢失，无法下载"}), 404
    return send_file(
        path, as_attachment=True, download_name=document["original_name"]
    )


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/tasks", methods=["POST"])
def tasks_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    data = _json_body()
    task_type = str(data.get("task_type", ""))
    if task_type not in {"parse_documents", "compare_documents", "extract_rules", "review_documents", "score_objective", "score_subjective", "evaluate_all"}:
        return jsonify({"error": "不支持的工作台任务"}), 400
    if task_type == "compare_documents":
        documents = storage.list_documents(current_app, project_id)
        if sum(item["role"] == "bid" for item in documents) < 2:
            return jsonify({"error": "请至少上传两份投标文件"}), 400
    if task_type == "review_documents":
        rule_set, rules = storage.list_rules(current_app, project_id)
        if not rule_set or rule_set["status"] != "confirmed" or not rules:
            return jsonify({"error": "请先确认至少一条评审规则"}), 400
    if task_type in {"score_objective", "score_subjective"}:
        score_category = "objective" if task_type == "score_objective" else "subjective"
        rule_set, rules = storage.list_rules(current_app, project_id)
        if not rule_set or rule_set["status"] != "confirmed" or not any(item["category"] == score_category for item in rules):
            return jsonify({"error": "请先确认对应的评分规则"}), 400
    if task_type == "evaluate_all":
        rule_set, rules = storage.list_rules(current_app, project_id)
        categories = {item["category"] for item in rules if item["enabled"]}
        executable = categories & {"qualification", "compliance", "substantive", "rejection", "other", "objective", "subjective"}
        if not rule_set or rule_set["status"] != "confirmed" or not executable:
            return jsonify({"error": "请先确认至少一条可执行的审查或评分规则"}), 400
        # OCR 文字取证与多模态图片取证已解耦。这里不再因某条规则选择了图片
        # 强度就拒绝整个任务：非多模态主模型仍可正常完成 OCR；真正需要外观
        # 判断但没有可用图片模型的规则会在 worker 内逐条标记，不能拖垮整任务。
    try:
        requested_profile_id = data.get("profile_id") or storage.default_model_profile_id(current_app)
        retry_failed_task_id = str(data.get("retry_failed_task_id") or "").strip()
        if retry_failed_task_id:
            if task_type != "evaluate_all":
                return jsonify({"error": "仅综合评审支持仅重跑失败项"}), 400
            prior = storage.get_task(current_app, retry_failed_task_id)
            if not prior or prior.get("project_id") != project_id or prior.get("task_type") != "evaluate_all":
                return jsonify({"error": "待重跑的综合评审任务不存在或不属于当前项目"}), 400
            failed_units = (prior.get("result") or {}).get("failed_units")
            if not isinstance(failed_units, list) or not failed_units:
                return jsonify({"error": "该任务没有可单独重跑的失败项"}), 400
        # force_rerun 必须随任务进入后台。仅在 API 层跳过整任务复用还不够：
        # 综合评审内部还有按投标文件复用的增量缓存。
        payload = {
            "profile_id": requested_profile_id,
            "prompt_version": TASK_PROMPT_VERSION,
            "deploy_commit": _current_deploy_commit(),
            "force_rerun": data.get("force_rerun") is True,
        }
        if retry_failed_task_id:
            payload["retry_failed_task_id"] = retry_failed_task_id
        if task_type in {"compare_documents", "extract_rules", "review_documents", "score_objective", "score_subjective", "evaluate_all"}:
            payload["input_fingerprint"] = storage.task_input_fingerprint(
                current_app, project_id, task_type, requested_profile_id, TASK_PROMPT_VERSION,
            )
            if data.get("force_rerun") is not True and not retry_failed_task_id:
                reusable = storage.find_reusable_task(current_app, project_id, task_type, payload["input_fingerprint"])
                if reusable:
                    return jsonify({"task": reusable, "reused": True})
        task = storage.create_task(current_app, project_id, task_type, payload)
        _start_worker_if_needed()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"task": task}), 202


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/token-usage")
def token_usage_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    return jsonify({
        "usage": storage.project_token_usage(current_app, project_id),
        "latest_run": storage.latest_evaluation_run_usage(current_app, project_id),
    })


@evaluation_workbench_bp.route("/api/evaluation-workbench/build-info")
def build_info_api():
    _init()
    return jsonify({
        "commit": _current_deploy_commit(),
        "deployed_at": _PROCESS_STARTED_AT,
        "prompt_version": TASK_PROMPT_VERSION,
    })


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/tasks", methods=["GET"])
def task_list_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    _start_worker_if_needed()
    return jsonify({"tasks": storage.list_tasks(current_app, project_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/tasks/<task_id>/compare-results")
def compare_results_api(task_id):
    _init()
    task = storage.get_task(current_app, task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"task": task, "pairs": storage.list_compare_pairs(current_app, task_id), "analysis": storage.compare_analysis(current_app, task_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/compare-signals/<signal_id>", methods=["PATCH"])
def update_compare_signal_api(signal_id):
    _init()
    data = _json_body()
    try:
        review = storage.update_compare_signal_review(
            current_app, signal_id, str(data.get("human_disposition", "")), data.get("human_note", "")
        )
        return jsonify({"review": review})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-configuration/unlock", methods=["POST"])
def unlock_model_configuration_api():
    if not hmac.compare_digest(str(_json_body().get("password", "")), MODEL_CONFIGURATION_PASSWORD):
        return jsonify({"error": "配置口令错误"}), 403
    session["evaluation_workbench_model_configuration_unlocked"] = True
    return jsonify({"status": "success"})


@evaluation_workbench_bp.route("/api/evaluation-workbench/vision-configuration", methods=["GET", "PATCH"])
def vision_configuration_api():
    _init()
    if request.method == "PATCH":
        access_error = _model_configuration_access_error()
        if access_error:
            return access_error
        try:
            return jsonify({"configuration": storage.update_vision_configuration(current_app, _json_body())})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"configuration": storage.vision_configuration(current_app)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/ocr-feature-configuration", methods=["GET", "PATCH"])
def ocr_feature_configuration_api():
    """保留兼容的本地 OCR 基线配置 API；腾讯和多模态仍独立配置。"""
    _init()
    if request.method == "PATCH":
        access_error = _model_configuration_access_error()
        if access_error:
            return access_error
        try:
            return jsonify({"configuration": storage.update_ocr_feature_configuration(current_app, _json_body())})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"configuration": storage.ocr_feature_configuration(current_app)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/tencent-ocr-configuration", methods=["GET", "PATCH"])
def tencent_ocr_configuration_api():
    _init()
    if request.method == "PATCH":
        access_error = _model_configuration_access_error()
        if access_error:
            return access_error
        try:
            return jsonify({"configuration": storage.update_ocr_configuration(current_app, _json_body())})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"configuration": storage.ocr_configuration(current_app)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/tencent-ocr-configuration/test", methods=["POST"])
def test_tencent_ocr_configuration_api():
    """不发送业务文件的本地配置检查，避免测试本身消耗免费OCR额度。"""
    _init()
    access_error = _model_configuration_access_error()
    if access_error:
        return access_error
    try:
        import tencentcloud.ocr.v20181119  # noqa: F401
    except ImportError:
        return jsonify({"error": "腾讯 OCR SDK 未安装，请重新部署包含最新依赖的版本"}), 400
    configuration = storage.ocr_configuration(current_app)
    if not configuration["credentials_configured"]:
        return jsonify({"error": "未配置腾讯 OCR SecretId/SecretKey"}), 400
    try:
        credentials = storage.tencent_ocr_credentials(current_app, require_enabled=False)
    except ValueError:
        return jsonify({"error": "已保存的腾讯 OCR 凭据无法解密，请重新配置 SecretId 和 SecretKey"}), 400
    if not credentials:
        return jsonify({"error": "腾讯 OCR 凭据不可用，请重新配置 SecretId 和 SecretKey"}), 400
    return jsonify({"message": "腾讯 OCR SDK、凭据与接口配置已就绪。此检查不发送图片、不消耗免费额度；首个OCR任务会验证云端权限。"})


@evaluation_workbench_bp.route("/api/evaluation-workbench/prompt-templates", methods=["GET"])
def prompt_templates_api():
    _init()
    return jsonify({"templates": storage.list_prompt_templates(current_app)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/prompt-templates/<template_id>", methods=["PATCH", "DELETE"])
def update_prompt_template_api(template_id):
    _init()
    data = _json_body()
    access_error = _prompt_template_mutation_access_error(data)
    if access_error:
        return access_error
    try:
        if request.method == "DELETE":
            return jsonify({"template": storage.reset_prompt_template(current_app, template_id)})
        return jsonify({"template": storage.update_prompt_template(current_app, template_id, data.get("content"))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/global-rules", methods=["GET", "POST"])
def global_rules_api():
    _init()
    try:
        if request.method == "POST":
            data = _json_body()
            access_error = _global_rule_mutation_access_error(data)
            if access_error:
                return access_error
            return jsonify({"rule": storage.create_global_rule(current_app, data)}), 201
        return jsonify({"rules": storage.list_global_rules(current_app)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/global-rules/<global_rule_id>", methods=["PATCH", "DELETE"])
def update_global_rule_api(global_rule_id):
    _init()
    data = _json_body()
    access_error = _global_rule_mutation_access_error(data)
    if access_error:
        return access_error
    try:
        if request.method == "DELETE":
            storage.delete_global_rule(current_app, global_rule_id)
            return jsonify({"status": "success"})
        return jsonify({"rule": storage.update_global_rule(current_app, global_rule_id, data)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-profiles", methods=["GET", "POST"])
def model_profiles_api():
    _init()
    if request.method == "POST":
        access_error = _model_configuration_access_error()
        if access_error:
            return access_error
        try:
            return jsonify({"profile": storage.create_model_profile(current_app, _json_body())}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    profiles = storage.list_model_profiles(current_app)
    return jsonify({"profiles": [{**profile, "capabilities": model_capabilities(profile)} for profile in profiles]})


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-profiles/<profile_id>", methods=["PATCH", "DELETE"])
def update_model_profile_api(profile_id):
    _init()
    access_error = _model_configuration_access_error()
    if access_error:
        return access_error
    if request.method == "DELETE":
        try:
            storage.delete_model_profile(current_app, profile_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success"})
    try:
        return jsonify({"profile": storage.update_model_profile(current_app, profile_id, _json_body())})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-profiles/<profile_id>/test", methods=["POST"])
def test_model_profile_api(profile_id):
    _init()
    access_error = _model_configuration_access_error()
    if access_error:
        return access_error
    try:
        profile = storage.get_model_profile(current_app, profile_id)
        return jsonify({"message": test_connection(profile, storage.prompt_template(current_app, "model_connection_test"))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-profiles/<profile_id>/default", methods=["POST"])
def set_default_model_profile_api(profile_id):
    _init()
    access_error = _model_configuration_access_error()
    if access_error:
        return access_error
    try:
        return jsonify({"profile": storage.set_default_model_profile(current_app, profile_id)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules", methods=["GET", "POST"])
def rules_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    if request.method == "POST":
        try:
            return jsonify({"rule": storage.add_rule(current_app, project_id, _json_body())}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    rule_set, rules = storage.list_rules(current_app, project_id)
    return jsonify({"rule_set": rule_set, "rules": rules})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/<rule_id>", methods=["DELETE", "PATCH"])
def delete_rule_api(project_id, rule_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    try:
        if request.method == "PATCH":
            return jsonify({"rule": storage.update_rule(current_app, project_id, rule_id, _json_body())})
        storage.delete_rule(current_app, project_id, rule_id)
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/confirm", methods=["POST"])
def confirm_rules_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    try:
        return jsonify({"rule_set": storage.confirm_rule_set(current_app, project_id)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/acquisition-validation", methods=["GET"])
def rule_acquisition_validation_api(project_id):
    """确认前的只读图片取证配置预检；不改变既有确认接口的返回语义。"""
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    return jsonify(storage.rule_set_acquisition_validation(current_app, project_id))


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/anchor-scan", methods=["GET"])
def rule_anchor_scan_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    return jsonify({"issues": storage.rule_anchor_scan(current_app, project_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/history-missing", methods=["GET"])
def rule_history_missing_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    return jsonify({"rules": storage.missing_rules_from_history(current_app, project_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/restore", methods=["POST"])
def rule_restore_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    data = _json_body()
    rule_id = str(data.get("rule_id") or "")
    if not rule_id:
        return jsonify({"error": "缺少规则 ID"}), 400
    try:
        rule = storage.restore_rule_from_history(current_app, project_id, rule_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"rule": rule})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/rules/deduplicate", methods=["POST"])
def rule_deduplicate_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    rule_set = storage.current_rule_set(current_app, project_id)
    if not rule_set:
        return jsonify({"error": "当前没有规则集"}), 400
    merged = storage.merge_draft_score_rule_duplicates(current_app, rule_set["rule_set_id"])
    return jsonify({"merged": merged, "rule_set": storage.current_rule_set(current_app, project_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/review-results")
def review_results_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    review_run, results = storage.latest_review_results(current_app, project_id)
    return jsonify({"review_run": review_run, "results": results})


@evaluation_workbench_bp.route("/api/evaluation-workbench/review-results/<review_result_id>", methods=["PATCH"])
def update_review_result_api(review_result_id):
    _init()
    try:
        result = storage.update_review_final_status(current_app, review_result_id, str(_json_body().get("final_status", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"review_result": result})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/review-results/confirm-auto", methods=["POST"])
def confirm_auto_review_results_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    return jsonify({"confirmed_count": storage.confirm_auto_review_results(current_app, project_id)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/score-results/<score_type>")
def score_results_api(project_id, score_type):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    if score_type not in {"objective", "subjective"}:
        return jsonify({"error": "不支持的评分类型"}), 400
    score_run, results = storage.latest_score_results(current_app, project_id, score_type)
    return jsonify({"score_run": score_run, "results": results})


@evaluation_workbench_bp.route("/api/evaluation-workbench/score-results/<score_result_id>", methods=["PATCH"])
def update_score_result_api(score_result_id):
    _init()
    data = _json_body()
    try:
        final_score = float(data.get("final_score"))
        return jsonify({"score_result": storage.update_final_score(current_app, score_result_id, final_score)})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) if str(exc) else "请填写有效的最终分数"}), 400


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/score-results/confirm-auto", methods=["POST"])
def confirm_auto_score_results_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    score_type = str(_json_body().get("score_type", "")).strip() or None
    if score_type not in {None, "objective", "subjective"}:
        return jsonify({"error": "不支持的评分类型"}), 400
    return jsonify({"confirmed_count": storage.confirm_auto_score_results(current_app, project_id, score_type)})


@evaluation_workbench_bp.route("/pingbiao/projects/<project_id>/report")
def evaluation_report_view(project_id):
    """按需生成浏览器可打印的汇总，不写入文件也不启动后台进程。"""
    _init()
    project, error = _project_or_404(project_id)
    if error:
        return error
    rule_set, rules = storage.list_rules(current_app, project_id)
    compare_task, compare_pairs = storage.latest_compare_results(current_app, project_id)
    review_run, reviews = storage.latest_review_results(current_app, project_id)
    _, objective_scores = storage.latest_score_results(current_app, project_id, "objective")
    _, subjective_scores = storage.latest_score_results(current_app, project_id, "subjective")
    presentation = _report_presentation(
        storage.list_documents(current_app, project_id), rule_set, rules, compare_task, compare_pairs,
        reviews, objective_scores, subjective_scores,
    )
    report_project = {**project, "display_name": _project_display_name(project)}
    return render_template(
        "evaluation_workbench/report.html", project=report_project, rule_set=rule_set, review_run=review_run,
        generated_at=_report_generated_time(storage.now_iso()), **presentation,
    )
