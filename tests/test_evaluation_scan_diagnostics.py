import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "evaluation_scan_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("evaluation_scan_diagnostics", SCRIPT_PATH)
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


class EvaluationScanDiagnosticsTests(unittest.TestCase):
    def test_task_scope_controls_rule_set_documents_and_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "evaluation_workspace.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE ew_projects(project_id TEXT PRIMARY KEY);
                CREATE TABLE ew_rule_sets(
                    rule_set_id TEXT PRIMARY KEY, project_id TEXT, status TEXT,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE ew_rules(
                    rule_id TEXT PRIMARY KEY, rule_set_id TEXT, category TEXT, title TEXT,
                    check_rule TEXT, source_text TEXT, check_mode TEXT, scoring_json TEXT,
                    execution_meta_json TEXT, sort_order INTEGER
                );
                CREATE TABLE ew_review_runs(review_run_id TEXT PRIMARY KEY, rule_set_id TEXT);
                CREATE TABLE ew_documents(
                    document_id TEXT PRIMARY KEY, project_id TEXT, role TEXT, bidder_name TEXT,
                    original_name TEXT, parse_status TEXT, parsed_path TEXT, created_at TEXT
                );
                CREATE TABLE ew_tasks(
                    task_id TEXT PRIMARY KEY, project_id TEXT, task_type TEXT, status TEXT,
                    payload_json TEXT, result_json TEXT, created_at TEXT, started_at TEXT, finished_at TEXT
                );
                CREATE TABLE ew_model_calls(
                    task_id TEXT, project_id TEXT, phase TEXT, context_mode TEXT,
                    total_tokens INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
                    cache_hit_tokens INTEGER, input_chars INTEGER, requested_max_tokens INTEGER,
                    finish_reason TEXT, parse_status TEXT, local_json_repaired INTEGER,
                    document_id TEXT
                );
                """
            )
            conn.executemany("INSERT INTO ew_projects VALUES (?)", [("project-a",), ("project-b",)])
            conn.executemany(
                "INSERT INTO ew_rule_sets VALUES (?,?,?,?,?)",
                [
                    ("rules-a", "project-a", "confirmed", "2026-01-01", "2026-01-01"),
                    ("rules-b", "project-b", "confirmed", "2026-02-01", "2026-02-01"),
                ],
            )
            conn.executemany(
                "INSERT INTO ew_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    ("rule-a", "rules-a", "other", "A规则", "核验A", "A原文", "text", "{}", "{}", 1),
                    ("rule-b", "rules-b", "other", "B规则", "核验B", "B原文", "text", "{}", "{}", 1),
                ],
            )
            conn.execute("INSERT INTO ew_review_runs VALUES (?,?)", ("review-a", "rules-a"))
            conn.executemany(
                "INSERT INTO ew_documents VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("doc-a", "project-a", "bid", "甲", "a.pdf", "success", "missing-a.txt", "2026-01-01"),
                    ("doc-b", "project-b", "bid", "乙", "b.pdf", "success", "missing-b.txt", "2026-02-01"),
                ],
            )
            conn.executemany(
                "INSERT INTO ew_tasks VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("task-a", "project-a", "evaluate_all", "success", "{}", json.dumps({"review_run_id": "review-a"}), "2026-01-01", "2026-01-01", "2026-01-01"),
                    ("task-b", "project-b", "evaluate_all", "success", "{}", "{}", "2026-02-01", "2026-02-01", "2026-02-01"),
                ],
            )
            conn.executemany(
                "INSERT INTO ew_model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("task-a", "project-a", "evaluate_all_full_scan", "full_scan:c1", 120, 100, 20, 50, 400, 1000, "stop", "success", 0, "doc-a"),
                    ("task-b", "project-b", "evaluate_all_review_batch", "乙·review:full_scan", 900, 800, 100, 0, 3000, 2000, "stop", "success", 0, "doc-b"),
                ],
            )
            conn.commit()
            conn.close()

            result = diagnostics.run(SimpleNamespace(
                db=str(db_path), task_id="task-a", project_id=None,
                chunk_chars=14_000, json=False,
            ))

            self.assertEqual(result["task"]["project_id"], "project-a")
            self.assertEqual(result["rule_set_id"], "rules-a")
            self.assertEqual(result["rule_count"], 1)
            self.assertEqual(result["ledger"]["total"]["prompt_tokens"], 100)
            self.assertEqual(result["ledger"]["total"]["cache_hit_tokens"], 50)
            self.assertEqual(result["ledger"]["total"]["cache_hit_rate"], 0.5)
            self.assertEqual(result["ledger"]["families"]["full_scan"]["calls"], 1)

    def test_phase_classification_precedes_ambiguous_context_mode(self):
        self.assertEqual(
            diagnostics.classify_model_call("evaluate_all_subjective_batch", "full_prefix"),
            "judge",
        )
        self.assertEqual(
            diagnostics.classify_model_call("extract_rules_scoring_assembly", "full_prefix"),
            "extraction",
        )


if __name__ == "__main__":
    unittest.main()
