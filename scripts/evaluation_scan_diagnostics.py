#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""综合评审模型成本诊断：只读实际任务账本，不重建或模拟生产评审逻辑。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "evaluation_workspace.db"
EVALUATION_TASK_TYPES = ("evaluate_all", "review_documents", "score_objective", "score_subjective")


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(cursor: sqlite3.Cursor, name: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _json_object(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def select_evaluation_task(
    cursor: sqlite3.Cursor, *, task_id: str | None, project_id: str | None,
) -> sqlite3.Row | None:
    if not _table_exists(cursor, "ew_tasks"):
        return None
    placeholders = ",".join("?" for _ in EVALUATION_TASK_TYPES)
    where = [f"task_type IN ({placeholders})"]
    params: list[str] = list(EVALUATION_TASK_TYPES)
    if task_id:
        where.append("task_id=?")
        params.append(task_id)
    elif project_id:
        where.append("project_id=?")
        params.append(project_id)
    return cursor.execute(
        "SELECT task_id, project_id, task_type, status, payload_json, result_json, "
        "created_at, started_at, finished_at FROM ew_tasks "
        f"WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 1",
        params,
    ).fetchone()


def evaluation_task_summary(row: sqlite3.Row | None) -> tuple[dict | None, dict]:
    if not row:
        return None, {}
    result = _json_object(row["result_json"])
    return {
        "task_id": row["task_id"],
        "project_id": row["project_id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "completion_state": result.get("completion_state"),
        "failed_units": len(result.get("failed_units") or []),
        "prompt_version": result.get("prompt_version"),
        "performance_metrics": result.get("performance_metrics"),
    }, result


def _task_rule_set(cursor: sqlite3.Cursor, project_id: str | None, result: dict) -> str | None:
    """优先返回任务实际运行记录关联的规则集，避免引用其他项目的最新规则。"""
    review_run_id = str(result.get("review_run_id") or "")
    if review_run_id and _table_exists(cursor, "ew_review_runs"):
        row = cursor.execute(
            "SELECT rule_set_id FROM ew_review_runs WHERE review_run_id=?", (review_run_id,)
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    if not project_id or not _table_exists(cursor, "ew_rule_sets"):
        return None
    for status_clause in ("AND status='confirmed'", ""):
        row = cursor.execute(
            "SELECT rule_set_id FROM ew_rule_sets WHERE project_id=? "
            f"{status_clause} ORDER BY updated_at DESC LIMIT 1", (project_id,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    return None


def classify_model_call(phase: str, context_mode: str) -> str:
    """phase 是主事实；context_mode 只兼容早期账本。"""
    phase_value = str(phase or "")
    mode = str(context_mode or "")
    if "full_scan" in phase_value or mode.startswith("full_scan:"):
        return "full_scan"
    if "vision" in phase_value or mode.startswith("vision_") or "vision_locator" in mode:
        return "vision"
    if "_ocr" in phase_value or mode in {"local_ocr", "tencent_ocr", "ocr_batch"}:
        return "ocr"
    if "highlights" in phase_value or mode.startswith("result_highlights"):
        return "summary"
    if "scope_profile" in phase_value or mode == "project_scope_source":
        return "scope"
    if "cross_bid" in phase_value or mode.startswith("cross_bid"):
        return "cross_bid"
    if phase_value.startswith("compare_") or mode == "evidence_batch":
        return "compare"
    if "json_repair" in phase_value or mode == "response_only_json_repair":
        return "repair"
    if phase_value.startswith("extract_rules"):
        return "extraction"
    if phase_value.startswith("evaluate_all_") and any(
        marker in phase_value for marker in ("_review", "_objective", "_subjective")
    ):
        return "judge"
    if ":" in mode:
        return "judge"
    return "other"


def _empty_bucket() -> dict:
    return {
        "calls": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "input_chars": 0,
        "format_errors": 0,
        "local_json_repairs": 0,
    }


def _add_call(bucket: dict, row: sqlite3.Row) -> None:
    bucket["calls"] += 1
    for key in ("total_tokens", "prompt_tokens", "completion_tokens", "cache_hit_tokens", "input_chars"):
        bucket[key] += int(row[key] or 0)
    bucket["format_errors"] += int(str(row["parse_status"] or "") == "invalid_json")
    bucket["local_json_repairs"] += int(row["local_json_repaired"] or 0)


def _finish_bucket(bucket: dict) -> dict:
    prompt_tokens = int(bucket["prompt_tokens"] or 0)
    calls = int(bucket["calls"] or 0)
    return {
        **bucket,
        "cache_hit_rate": round(bucket["cache_hit_tokens"] / prompt_tokens, 4) if prompt_tokens else 0,
        "avg_total_tokens": round(bucket["total_tokens"] / calls, 1) if calls else 0,
        "avg_input_chars": round(bucket["input_chars"] / calls, 1) if calls else 0,
    }


def analyze_ledger(cursor: sqlite3.Cursor, task_id: str | None) -> dict:
    if not task_id or not _table_exists(cursor, "ew_model_calls"):
        return {"available": False, "reason": "未找到目标任务或模型调用账本"}
    try:
        rows = cursor.execute(
            "SELECT phase, context_mode, total_tokens, prompt_tokens, completion_tokens, "
            "cache_hit_tokens, input_chars, parse_status, local_json_repaired "
            "FROM ew_model_calls WHERE task_id=?", (task_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"模型调用账本字段不完整：{exc}"}
    if not rows:
        return {"available": True, "empty": True, "reason": "该任务没有模型调用记录"}

    families: dict[str, dict] = {}
    phases: dict[str, dict] = {}
    total = _empty_bucket()
    for row in rows:
        family = classify_model_call(row["phase"], row["context_mode"])
        family_bucket = families.setdefault(family, _empty_bucket())
        phase_bucket = phases.setdefault(str(row["phase"] or "unknown"), _empty_bucket())
        _add_call(family_bucket, row)
        _add_call(phase_bucket, row)
        _add_call(total, row)
    finished_families = {key: _finish_bucket(value) for key, value in families.items()}
    finished_phases = {key: _finish_bucket(value) for key, value in phases.items()}
    candidates = [
        {
            "family": key,
            "calls": value["calls"],
            "prompt_tokens": value["prompt_tokens"],
            "cache_hit_rate": value["cache_hit_rate"],
            "uncached_prompt_tokens": value["prompt_tokens"] - value["cache_hit_tokens"],
        }
        for key, value in finished_families.items()
        if value["calls"] >= 2 and value["prompt_tokens"] >= 10_000 and value["cache_hit_rate"] < 0.2
    ]
    candidates.sort(key=lambda item: item["uncached_prompt_tokens"], reverse=True)
    return {
        "available": True,
        "empty": False,
        "total": _finish_bucket(total),
        "families": finished_families,
        "phases": dict(sorted(
            finished_phases.items(), key=lambda item: item[1]["prompt_tokens"], reverse=True,
        )),
        "cache_observation_candidates": candidates,
    }


def run(args) -> dict:
    db_path = Path(args.db).expanduser().resolve()
    connection = _connect(db_path)
    try:
        cursor = connection.cursor()
        task_row = select_evaluation_task(
            cursor,
            task_id=str(args.task_id or "") or None,
            project_id=str(args.project_id or "") or None,
        )
        task, result = evaluation_task_summary(task_row)
        task_id = str(task.get("task_id") or "") if task else None
        project_id = str(task.get("project_id") or "") if task else None
        rule_set_id = _task_rule_set(cursor, project_id, result)
        rule_count = 0
        if rule_set_id and _table_exists(cursor, "ew_rules"):
            rule_count = int(cursor.execute(
                "SELECT COUNT(*) FROM ew_rules WHERE rule_set_id=?", (rule_set_id,)
            ).fetchone()[0])
        return {
            "db": str(db_path),
            "task": task,
            "rule_set_id": rule_set_id,
            "rule_count": rule_count,
            "ledger": analyze_ledger(cursor, task_id),
            "boundary": (
                "只读实际任务账本；不读取或保存提示词正文，不模拟规则剪枝，"
                "不改变生产请求、评审结果或缓存行为。"
            ),
        }
    finally:
        connection.close()


def render_text(report: dict) -> str:
    lines = ["综合评审模型成本诊断（只读）", f"数据库：{report['db']}"]
    task = report.get("task") or {}
    if not task:
        return "\n".join([*lines, "未找到目标评审任务。"]) + "\n"
    lines.extend([
        f"任务：{task['task_id']} / {task['task_type']} / {task['status']}",
        f"项目：{task['project_id']}；规则集：{report.get('rule_set_id') or '未定位'}；规则：{report['rule_count']} 条",
        f"提示词版本：{task.get('prompt_version') or '未知'}；失败单元：{task.get('failed_units', 0)}",
    ])
    ledger = report.get("ledger") or {}
    if not ledger.get("available") or ledger.get("empty"):
        lines.append(str(ledger.get("reason") or "没有可用账本"))
        return "\n".join(lines) + "\n"
    total = ledger["total"]
    lines.append(
        f"总调用：{total['calls']}；输入 Token：{total['prompt_tokens']:,}；缓存命中："
        f"{total['cache_hit_tokens']:,}（{total['cache_hit_rate'] * 100:.1f}%）"
    )
    lines.append("\n按阶段族：")
    lines.append(f"{'阶段':<12}{'调用':>7}{'输入Token':>14}{'缓存率':>10}{'格式异常':>10}")
    for family, value in sorted(
        ledger["families"].items(), key=lambda item: item[1]["prompt_tokens"], reverse=True,
    ):
        lines.append(
            f"{family:<12}{value['calls']:>7}{value['prompt_tokens']:>14,}"
            f"{value['cache_hit_rate'] * 100:>9.1f}%{value['format_errors']:>10}"
        )
    candidates = ledger.get("cache_observation_candidates") or []
    if candidates:
        lines.append("\n仅建议继续观察的低命中阶段（不是自动优化指令）：")
        for item in candidates:
            lines.append(
                f"- {item['family']}：{item['calls']} 次，缓存率 {item['cache_hit_rate'] * 100:.1f}%，"
                f"未命中输入约 {item['uncached_prompt_tokens']:,} Token"
            )
    lines.append(f"\n边界：{report['boundary']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="综合评审模型成本诊断（只读）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="evaluation_workspace.db 路径")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--task-id", help="只分析指定评审任务")
    selector.add_argument("--project-id", help="分析指定项目最近一次评审任务")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    try:
        report = run(args)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
