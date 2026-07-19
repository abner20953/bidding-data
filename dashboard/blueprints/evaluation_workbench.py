"""评标工作台独立 Blueprint。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request
from werkzeug.security import check_password_hash

from dashboard.evaluation_workbench import storage
from dashboard.evaluation_workbench.ai_gateway import test_connection


evaluation_workbench_bp = Blueprint("evaluation_workbench", __name__)
TASK_PROMPT_VERSION = "token-optimized-v1"


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
    """校验新建项目口令；口令本身只允许通过运行环境提供。"""
    configured_hash = (
        current_app.config.get("EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD_HASH")
        or os.environ.get("EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD_HASH")
    )
    if not configured_hash:
        return False, "新建项目口令尚未在运行环境配置"
    if not isinstance(value, str) or not check_password_hash(configured_hash, value):
        return False, "新建项目口令错误"
    return True, None


def _project_or_404(project_id: str):
    project = storage.get_project(current_app, project_id)
    if not project:
        return None, (jsonify({"error": "评标项目不存在"}), 404)
    return project, None


def _worker_lock_path() -> Path:
    return storage.data_dir(current_app) / "worker.lock"


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
    try:
        subprocess.Popen(
            [sys.executable, "-m", "dashboard.evaluation_workbench.worker"],
            cwd=str(Path(current_app.root_path).parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise


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
        return jsonify({"project": project, "documents": storage.list_documents(current_app, project_id), "tasks": storage.list_tasks(current_app, project_id)})
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


@evaluation_workbench_bp.route("/api/evaluation-workbench/projects/<project_id>/tasks", methods=["POST"])
def tasks_api(project_id):
    _init()
    _, error = _project_or_404(project_id)
    if error:
        return error
    data = _json_body()
    task_type = str(data.get("task_type", ""))
    if task_type not in {"parse_documents", "compare_documents", "extract_rules", "review_documents", "score_objective", "score_subjective", "evaluate_all"}:
        return jsonify({"error": "不支持的评标工作台任务"}), 400
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
        needed = {"objective", "subjective"}
        has_review = bool(categories & {"qualification", "compliance", "substantive", "rejection"})
        if not rule_set or rule_set["status"] != "confirmed" or not has_review or not needed.issubset(categories):
            return jsonify({"error": "请先确认包含审查、客观评分和主观评分项的规则集"}), 400
    try:
        payload = {"profile_id": data.get("profile_id")}
        if task_type in {"extract_rules", "review_documents", "score_objective", "score_subjective", "evaluate_all"}:
            payload["input_fingerprint"] = storage.task_input_fingerprint(
                current_app, project_id, task_type, data.get("profile_id"), TASK_PROMPT_VERSION,
            )
            if data.get("reuse_completed") is True:
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
    return jsonify({"usage": storage.project_token_usage(current_app, project_id)})


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


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-profiles", methods=["GET", "POST"])
def model_profiles_api():
    _init()
    if request.method == "POST":
        try:
            return jsonify({"profile": storage.create_model_profile(current_app, _json_body())}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"profiles": storage.list_model_profiles(current_app)})


@evaluation_workbench_bp.route("/api/evaluation-workbench/model-profiles/<profile_id>", methods=["PATCH", "DELETE"])
def update_model_profile_api(profile_id):
    _init()
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
    try:
        profile = storage.get_model_profile(current_app, profile_id)
        return jsonify({"message": test_connection(profile)})
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
    return render_template(
        "evaluation_workbench/report.html", project=project, rule_set=rule_set, rules=rules,
        documents=storage.list_documents(current_app, project_id), review_run=review_run, reviews=reviews,
        objective_scores=objective_scores, subjective_scores=subjective_scores,
        compare_task=compare_task, compare_pairs=compare_pairs, generated_at=storage.now_iso(),
    )
