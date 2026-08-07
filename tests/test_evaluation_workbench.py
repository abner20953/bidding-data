import io
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import fitz
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash

from dashboard.blueprints import evaluation_workbench as evaluation_workbench_module
from dashboard.blueprints.evaluation_workbench import create_worker_app, evaluation_workbench_bp
from dashboard.evaluation_workbench import local_ocr_gateway, ocr_gateway, storage, worker
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.prompt_context import (
    _anchors, build_rule_context, select_rule_chunk_evidence_map, select_rule_chunk_map, select_rule_chunks,
    split_full_text_chunks,
)
from dashboard.evaluation_workbench.prompt_templates import PROMPT_TEMPLATES
from dashboard.utils.comparator import CollusionDetector


class EvaluationWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="evaluation_workbench_"))
        self.app = create_worker_app()
        self.app.config["SECRET_KEY"] = "evaluation-workbench-test-secret"
        self.app.config["EVALUATION_WORKBENCH_DATA_DIR"] = str(self.temp_dir / "workspace")
        self.app.register_blueprint(evaluation_workbench_bp)
        storage.init_database(self.app)
        self.project = storage.create_project(self.app, "评标测试项目", "TEST-01")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _pdf_upload(filename, text):
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), text)
        content = pdf.tobytes()
        pdf.close()
        return FileStorage(stream=io.BytesIO(content), filename=filename)

    def _add_pdf(self, filename, role, bidder_name, text):
        return storage.store_upload(
            self.app,
            self.project["project_id"],
            role,
            bidder_name,
            self._pdf_upload(filename, text),
        )

    def _run_next_task(self):
        task = storage.next_queued_task(self.app)
        self.assertIsNotNone(task)
        worker.run_task(self.app, task)
        return storage.get_task(self.app, task["task_id"])

    @staticmethod
    def _unlock_model_configuration(client):
        response = client.post("/api/evaluation-workbench/model-configuration/unlock", json={"password": "108"})
        if response.status_code != 200:
            raise AssertionError(response.get_json())

    def test_parse_task_persists_document_metadata(self):
        self._add_pdf("tender.pdf", "tender", "", "采购需求：稳定运行。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")

        finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        documents = storage.list_documents(self.app, self.project["project_id"])
        self.assertTrue(all(item["parse_status"] == "success" for item in documents))
        self.assertTrue(all(item["text_length"] is not None for item in documents))

    def test_bid_upload_requires_bidder_name(self):
        with self.assertRaisesRegex(ValueError, "必须填写投标人名称"):
            self._add_pdf("bid.pdf", "bid", "  ", "技术方案：稳定运行。")

    def test_incomplete_model_envelope_retries_only_current_request(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = {"profile_id": "profile-1", "display_name": "测试模型"}
        task["_evaluation_request_gate"] = worker._EvaluationRequestGate(limit=2, max_limit=2)

        with patch(
            "dashboard.evaluation_workbench.worker.request_json",
            side_effect=[worker.ModelResponseEnvelopeError("模型接口响应不完整"), {"rules": []}],
        ) as request_json, patch("dashboard.evaluation_workbench.worker.time.sleep") as sleep:
            result = worker._request_task_json(
                self.app, task, profile, "test_phase", "system", "user", max_tokens=32,
            )

        self.assertEqual(result, {"rules": []})
        self.assertEqual(request_json.call_count, 2)
        sleep.assert_called_once_with(5)
        refreshed = storage.get_task(self.app, task["task_id"])
        self.assertIn("重试当前分组", refreshed["message"])

    def test_incomplete_model_envelope_allows_second_backoff_retry(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = {"profile_id": "profile-1", "display_name": "测试模型"}
        task["_evaluation_request_gate"] = worker._EvaluationRequestGate(limit=1, max_limit=1)

        with patch(
            "dashboard.evaluation_workbench.worker.request_json",
            side_effect=[
                worker.ModelResponseEnvelopeError("模型接口响应不完整"),
                worker.ModelResponseEnvelopeError("模型接口响应不完整"),
                {"rules": []},
            ],
        ) as request_json, patch("dashboard.evaluation_workbench.worker.time.sleep") as sleep:
            result = worker._request_task_json(
                self.app, task, profile, "test_phase", "system", "user", max_tokens=32,
            )

        self.assertEqual(result, {"rules": []})
        self.assertEqual(request_json.call_count, 3)
        self.assertEqual(sleep.call_args_list[0].args, (5,))
        self.assertEqual(sleep.call_args_list[1].args, (15,))
        self.assertEqual(storage.project_token_usage(self.app, self.project["project_id"])["call_count"], 3)

    def test_transient_connection_failure_retries_only_current_request(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = {"profile_id": "profile-1", "display_name": "测试模型"}

        with patch(
            "dashboard.evaluation_workbench.worker.request_json",
            side_effect=[
                ValueError("模型连接失败：('Connection aborted.', RemoteDisconnected('remote end closed connection'))"),
                ValueError("模型连接失败：Read timed out"),
                {"rules": []},
            ],
        ) as request_json, patch("dashboard.evaluation_workbench.worker.time.sleep") as sleep:
            result = worker._request_task_json(self.app, task, profile, "test_phase", "system", "user", max_tokens=32)

        self.assertEqual(result, {"rules": []})
        self.assertEqual(request_json.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(5,), (15,)])

    def test_client_error_with_timeout_word_does_not_retry(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = {"profile_id": "profile-1", "display_name": "测试模型"}

        with patch(
            "dashboard.evaluation_workbench.worker.request_json",
            side_effect=ValueError("模型请求失败（HTTP 400）：invalid timeout parameter"),
        ) as request_json, patch("dashboard.evaluation_workbench.worker.time.sleep") as sleep:
            with self.assertRaises(ValueError):
                worker._request_task_json(self.app, task, profile, "test_phase", "system", "user", max_tokens=32)

        self.assertEqual(request_json.call_count, 1)
        sleep.assert_not_called()

    def test_score_rule_dedupe_merges_same_clause_with_score_suffix(self):
        rules = worker._dedupe_rule_candidates([
            {
                "category": "objective", "title": "商务部分-企业业绩评分", "check_rule": "每提供一份业绩得3分，最高9分",
                "source_text": "企业业绩每份3分，满分9分", "source_clause_ids": ["SC-12"],
                "scoring": {"max_score": 9, "kind": "manual"},
            },
            {
                "category": "objective", "title": "商务部分-企业业绩评分（满分9分）", "check_rule": "每提供一份有效业绩得3分，最多3份，最高9分",
                "source_text": "企业业绩每份3分，满分9分", "source_clause_ids": ["SC-12"],
                "scoring": {"max_score": 9, "kind": "manual", "items": [{"name": "有效业绩", "max_score": 9}]},
            },
        ])

        self.assertEqual(len(rules), 1)
        self.assertIn("最多3份", rules[0]["check_rule"])
        self.assertEqual(rules[0]["source_clause_ids"], ["SC-12"])

    def test_score_reason_replaces_conflicting_calculation_with_final_score(self):
        reason = worker._reconcile_score_reason(
            "计分过程：3×3=9分，封顶9分。图片确认仅1项。", 3.0,
            adjusted=True, source="图片/OCR",
        )

        self.assertIn("最终建议分：3分", reason)
        self.assertNotIn("3×3=9分", reason)
        self.assertNotIn("封顶9分", reason)

    def test_non_retryable_model_envelope_does_not_repeat_or_reduce_concurrency(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        gate = worker._EvaluationRequestGate(limit=2, max_limit=2)
        task["_evaluation_request_gate"] = gate
        profile = {"profile_id": "profile-1", "display_name": "测试模型"}

        with patch(
            "dashboard.evaluation_workbench.worker.request_json",
            side_effect=worker.ModelResponseEnvelopeError("模型接口业务错误（服务商代码 1004）", retryable=False),
        ) as request_json, patch("dashboard.evaluation_workbench.worker.time.sleep") as sleep:
            with self.assertRaises(worker.ModelResponseEnvelopeError):
                worker._request_task_json(self.app, task, profile, "test_phase", "system", "user", max_tokens=32)

        self.assertEqual(request_json.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(gate.limit, 2)
        self.assertEqual(storage.project_token_usage(self.app, self.project["project_id"])["call_count"], 1)

    def test_full_scan_checkpoint_is_reusable_by_chunk_hash(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        findings = [{"rule_id": "rule-1", "chunk_id": "chunk_1", "evidence": "技术方案", "page_hint": "1"}]

        storage.save_evaluation_scan_checkpoint(
            self.app, self.project["project_id"], document["document_id"], "scan-v2", "chunk_1", "content-hash", findings,
        )

        self.assertEqual(
            storage.get_evaluation_scan_checkpoint(self.app, document["document_id"], "scan-v2", "chunk_1", "content-hash"),
            findings,
        )
        self.assertIsNone(storage.get_evaluation_scan_checkpoint(self.app, document["document_id"], "scan-v2", "chunk_1", "changed"))

    def test_project_scope_profile_checkpoint_is_reusable(self):
        scope = {"project_identity": "测试项目", "technical_topics": ["无人机航测"]}

        storage.save_project_scope_checkpoint(self.app, self.project["project_id"], "scope-v1", scope)

        self.assertEqual(
            storage.get_project_scope_checkpoint(self.app, self.project["project_id"], "scope-v1"), scope,
        )

    def test_evaluation_unit_checkpoint_reuses_only_matching_execution_fingerprint(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "资质", "check_rule": "核验资质"})
        rule_set, _ = storage.list_rules(self.app, self.project["project_id"])
        result = {"rule_id": rule["rule_id"], "status": "satisfied", "evidence": "已提供", "reason": "可核验"}

        storage.save_evaluation_unit_checkpoints(
            self.app, self.project["project_id"], rule_set["rule_set_id"], document["document_id"], "fingerprint-a",
            {"review": {rule["rule_id"]: result}, "objective": {}, "subjective": {}},
        )

        reused = storage.get_evaluation_unit_checkpoints(
            self.app, self.project["project_id"], rule_set["rule_set_id"], document["document_id"], "fingerprint-a",
        )
        self.assertEqual(reused["review"][rule["rule_id"]]["evidence"], "已提供")
        self.assertEqual(
            storage.get_evaluation_unit_checkpoints(
                self.app, self.project["project_id"], rule_set["rule_set_id"], document["document_id"], "changed",
            )["review"], {},
        )
        storage.delete_evaluation_unit_checkpoints(
            self.app, self.project["project_id"], rule_set["rule_set_id"], document["document_id"], "fingerprint-a",
            {"review": {rule["rule_id"]}, "objective": set(), "subjective": set()},
        )
        self.assertEqual(
            storage.get_evaluation_unit_checkpoints(
                self.app, self.project["project_id"], rule_set["rule_set_id"], document["document_id"], "fingerprint-a",
            )["review"], {},
        )

    def test_scope_excerpt_prioritises_business_sections_not_only_fixed_positions(self):
        text = "\n\n".join([
            "[第1页]\n前言说明。" * 80,
            "[第2页]\n一般条款。" * 80,
            "[第3页]\n采购范围：井下工业视频系统扩容，包含摄像机、传输网络和平台适配。" * 20,
            "[第4页]\n其他条款。" * 80,
        ])

        excerpt = worker._scope_excerpt(text, 1_500)

        self.assertIn("井下工业视频系统扩容", excerpt)

    def test_nested_provider_cache_usage_is_aggregated(self):
        task = storage.create_task(self.app, self.project["project_id"], "parse_documents")
        storage.record_model_call(
            self.app, task["task_id"], self.project["project_id"], "test", None,
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                   "prompt_tokens_details": {"cached_tokens": 80}},
        )

        usage = storage.project_token_usage(self.app, self.project["project_id"])
        self.assertEqual(usage["cache_hit_tokens"], 80)
        self.assertEqual(usage["cache_by_phase"][0]["phase"], "test")
        self.assertEqual(usage["cache_by_phase"][0]["cache_hit_tokens"], 80)

    def test_token_usage_breaks_down_vision_and_ocr_families(self):
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_batch", None,
                                  usage={"total_tokens": 100})
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_vision_1", None,
                                  context_mode="vision_standard_1", usage={"total_tokens": 40})
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_ocr", None,
                                  context_mode="tencent_ocr", usage={"total_tokens": 20})
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_ocr", None,
                                  context_mode="local_ocr", usage={"total_tokens": 15})
        document = self._add_pdf("local-ocr.pdf", "bid", "甲公司", "本地 OCR 缓存页")
        storage.save_ocr_page_cache(
            self.app, document["document_id"], 1, "local-hash", local_ocr_gateway.LOCAL_OCR_SERVICE,
            {"service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "本地文字", "parser_version": 1},
        )
        with storage.connection(self.app) as conn:
            conn.execute(
                "INSERT INTO ew_ocr_usage_ledger(usage_id, task_id, project_id, service, month_key, status, billed_units, created_at)"
                " VALUES('u-test-1', ?, ?, 'basic', '2026-07', 'success', 2, ?)",
                (task["task_id"], self.project["project_id"], storage.now_iso()),
            )

        usage = storage.project_token_usage(self.app, self.project["project_id"])

        self.assertEqual(usage["families"]["text"]["call_count"], 1)
        self.assertEqual(usage["families"]["vision"]["call_count"], 1)
        self.assertEqual(usage["families"]["vision"]["total_tokens"], 40)
        self.assertEqual(usage["families"]["tencent_ocr"]["call_count"], 1)
        self.assertEqual(usage["families"]["local_ocr"]["call_count"], 1)
        self.assertEqual(usage["ocr_requests"], 2)
        self.assertEqual(usage["local_ocr_pages"], 1)
        # 汇总口径保持不变，新增字段不影响既有调用方。
        self.assertEqual(usage["call_count"], 4)

    def test_observability_tables_do_not_change_existing_usage_totals(self):
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        document = self._add_pdf("metrics.pdf", "bid", "甲公司", "扫描页")
        storage.record_local_ocr_run(
            self.app, task_id=task["task_id"], project_id=self.project["project_id"],
            document_id=document["document_id"], requested_pages=3, recognized_pages=2,
            empty_pages=1, failed_pages=0, elapsed_ms=4_500, peak_rss_kb=256_000,
            status="success",
        )
        storage.record_output_risk_observation(
            self.app, task["task_id"], self.project["project_id"], "evaluate_all_full_scan",
            document_id=document["document_id"], input_chars=12_000, rule_count=20,
            requested_max_tokens=2_000, predicted_risk_score=80,
            shadow_split_recommended=True, actual_format_error=True,
            actual_finish_reason="length", actual_error_kind="InvalidJsonResponse",
            recovery_action="split_catalog",
        )

        usage = storage.project_token_usage(self.app, self.project["project_id"])

        self.assertEqual(usage["call_count"], 0)
        self.assertEqual(usage["local_ocr_performance"]["run_count"], 1)
        self.assertEqual(usage["local_ocr_performance"]["average_ms_per_page"], 1500)
        self.assertEqual(usage["local_ocr_performance"]["peak_rss_kb"], 256_000)
        self.assertEqual(usage["output_risk_shadow"]["observations"], 1)
        self.assertEqual(usage["output_risk_shadow"]["true_positives"], 1)

    def test_full_scan_output_risk_is_observational_only(self):
        low = worker._full_scan_output_risk(
            [{"id": "r1", "evidence_requirements": []}], {"text": "短页块"}, 3_200,
        )
        high_catalog = [
            {"id": f"r{index}", "score_hint": "复杂计分", "evidence_requirements": ["a", "b", "c"]}
            for index in range(24)
        ]
        high = worker._full_scan_output_risk(high_catalog, {"text": "长" * 11_000}, 2_000)

        self.assertFalse(low["recommend_split"])
        self.assertTrue(high["recommend_split"])
        self.assertGreater(high["score"], low["score"])

    def test_successful_full_scan_records_shadow_risk_without_changing_result(self):
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        document = self._add_pdf("shadow.pdf", "bid", "甲公司", "技术方案完整")
        catalog = [{"id": "rule-1", "rule_id": "rule-1", "q": "核验技术方案", "type": "other"}]
        chunk = {"chunk_id": "chunk_1", "start_page": 1, "end_page": 1, "text": "技术方案完整"}
        with patch("dashboard.evaluation_workbench.worker._request_task_json", return_value={
            "matches": [["rule-1", "1", "技术方案完整", "supports"]],
            "scope_anomalies": [],
        }):
            value, compact_retries, splits, failed = worker._run_full_scan_piece(
                self.app, task, {"context_window": 32_000}, document, catalog, chunk, {}, "system",
            )

        self.assertEqual(value["findings"][0]["rule_id"], "rule-1")
        self.assertEqual((compact_retries, splits, failed), (0, 0, []))
        shadow = storage.project_token_usage(self.app, self.project["project_id"])["output_risk_shadow"]
        self.assertEqual(shadow["observations"], 1)
        self.assertEqual(shadow["format_errors"], 0)

    def test_document_evidence_manifest_is_reusable_without_storing_document_text(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        manifest = [{"chunk_id": "chunk_1", "start_page": 1, "end_page": 1, "chars": 12, "text_hash": "abc"}]

        storage.save_document_evidence_manifest(self.app, document["document_id"], document["sha256"], "v1", manifest)

        self.assertEqual(
            storage.get_document_evidence_manifest(self.app, document["document_id"], document["sha256"], "v1"), manifest,
        )

    def test_task_recovery_summary_separates_json_repair_and_compact_retry(self):
        task = storage.create_task(self.app, self.project["project_id"], "parse_documents")
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_json_repair", None)
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_compact_retry", None)
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_subjective_batch", None,
                                  context_mode="甲公司·subjective 第1组/缺失补评:full_scan_evidence")

        self.assertEqual(storage.task_recovery_summary(self.app, task["task_id"]), {
            "json_repair_count": 1, "compact_retry_count": 1, "missing_rule_retry_count": 1,
        })

    def test_compact_full_scan_matches_are_normalised(self):
        findings = worker._normalise_scan_findings(
            [["rule-1", "7", "类似项目合同", "supports"]], {"rule-1"},
            {"chunk_id": "chunk_1", "start_page": 7, "end_page": 7},
        )

        self.assertEqual(findings[0]["rule_id"], "rule-1")
        self.assertEqual(findings[0]["page_hint"], "7")
        self.assertEqual(findings[0]["tentative_status"], "supports")

    def test_compact_full_scan_match_retains_evidence_origin(self):
        findings = worker._normalise_scan_findings(
            [["rule-1", "7", "技术方案正文", "supports", "high", "bidder_design"]], {"rule-1"},
            {"chunk_id": "chunk_1", "start_page": 7, "end_page": 7},
        )

        self.assertEqual(findings[0]["observation"], "bidder_design")

    def test_model_enum_fields_returned_as_lists_do_not_crash_normalisers(self):
        # MiniMax 偶尔把枚举/ID 字段返回成数组；必须静默回落而不是以
        # "unhashable type: 'list'" 中断整份评审任务。
        chunk = {"chunk_id": "chunk_1", "start_page": 7, "end_page": 7}
        findings = worker._normalise_scan_findings(
            [
                [["rule-1"], "7", "证据", "supports"],  # rule_id 为数组：整条跳过
                ["rule-1", "7", "证据", ["supports"], ["high"], "bidder_text"],
                {"rule_id": "rule-1", "status": ["partial"], "confidence": ["high"],
                 "evidence_priority": {"level": "high"}, "evidence": "证据"},
            ],
            {"rule-1"}, chunk,
        )
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["tentative_status"], "suspected")
        self.assertEqual(findings[0]["confidence"], "medium")
        self.assertEqual(findings[0]["evidence_priority"], "medium")
        self.assertEqual(findings[1]["tentative_status"], "suspected")

        rules = [{"rule_id": "procedure", "check_mode": "auto"}]
        results = worker._normalise_review_results(
            [
                {"rule_id": ["procedure"], "status": "satisfied"},  # rule_id 为数组：跳过
                {"rule_id": "procedure", "status": ["satisfied"], "confidence": ["high"],
                 "risk_level": {"level": "low"}, "evidence_quality": ["sufficient"],
                 "reason": "文本已核验"},
            ],
            rules,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "manual")
        self.assertEqual(results[0]["confidence"], "medium")
        self.assertEqual(results[0]["risk_level"], "medium")

        score = worker._score_result_from_model("rule-1", 1.0, 2.0, {"confidence": ["high"], "evidence": "证据"})
        self.assertEqual(score["confidence"], "medium")

    def test_subjective_full_scan_catalog_keeps_long_scoring_rule(self):
        rules = [{
            "rule_id": "subjective-1", "category": "subjective", "title": "系统功能模块设计",
            "check_rule": "模块要求：" + "甲" * 360 + "第七服务模块。", "source_text": "评分办法",
        }]

        catalog = worker._full_scan_catalog(rules)

        self.assertIn("第七服务模块", catalog[0]["q"])

    def test_score_clause_packets_join_cross_page_score_fragment(self):
        text = """[第34页]
4. 实施进度计划
供应商提供完整的进度控制计划（1.5分）、时间进度安排（时间进度表）
[第35页]
（1.5分）、保障措施（1.5分）、应急预案（1.5分）。
上述方案无缺陷得6分，每处缺陷扣0.5分。
5. 整体实施方案
提供配送、运输供货方案（1.5分）。"""

        packets = worker._score_clause_packets(text)

        progress = next(item for item in packets if "实施进度计划" in item["text"])
        self.assertIn("保障措施（1.5分）", progress["text"])
        self.assertIn("应急预案（1.5分）", progress["text"])
        self.assertEqual(sum("保障措施" in item["text"] for item in packets), 1)

    def test_numeric_boundary_equality_is_not_saved_as_high_risk_deviation(self):
        rule = {"rule_id": "rope", "title": "关键参数响应", "check_rule": "核对参数是否满足招标要求"}
        results = worker._normalise_review_results([{
            "rule_id": "rope", "status": "not_satisfied", "risk_level": "high", "confidence": "high",
            "evidence": "第128页招标要求Φ≥30mm，投标响应Φ30mm，实质差异未披露。",
            "reason": "投标直径30mm未披露偏离。",
        }], [rule])

        self.assertEqual(results[0]["status"], "partial")
        self.assertEqual(results[0]["risk_level"], "low")
        self.assertIn("满足招标≥30mm", results[0]["reason"])
        self.assertNotIn("差异未披露", results[0]["evidence"])

    def test_parse_task_reuses_successful_parse_cache(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.create_task(self.app, self.project["project_id"], "parse_documents")

        finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["parsed_count"], 0)
        self.assertEqual(finished["result"]["skipped_count"], 1)

    def test_project_document_counts_are_not_multiplied_by_tasks(self):
        self._add_pdf("tender.pdf", "tender", "", "采购需求。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        storage.create_task(self.app, self.project["project_id"], "compare_documents")

        project = next(item for item in storage.list_projects(self.app) if item["project_id"] == self.project["project_id"])

        self.assertEqual(project["document_count"], 2)
        self.assertEqual(project["bid_count"], 1)

    def test_idle_project_read_does_not_start_worker(self):
        with patch("dashboard.blueprints.evaluation_workbench.subprocess.Popen") as popen:
            response = self.app.test_client().get(f"/api/evaluation-workbench/projects/{self.project['project_id']}")

        self.assertEqual(response.status_code, 200)
        popen.assert_not_called()

    def test_worker_log_is_bounded_to_current_file_and_one_backup(self):
        log_path = storage.data_dir(self.app) / "worker.log"
        log_path.write_bytes(b"x" * (2 * 1024 * 1024))

        with self.app.app_context():
            stream = evaluation_workbench_module._worker_log_stream()
            self.assertIsNotNone(stream)
            stream.write("new task\n")
            stream.close()

        self.assertEqual(log_path.read_text(encoding="utf-8"), "new task\n")
        self.assertTrue((storage.data_dir(self.app) / "worker.log.1").exists())

    def test_create_project_requires_configured_password(self):
        client = self.app.test_client()
        missing = client.post("/api/evaluation-workbench/projects", json={"name": "新项目", "password": "test-password"})
        self.assertEqual(missing.status_code, 403)

        self.app.config["EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD_HASH"] = generate_password_hash("test-password")
        rejected = client.post("/api/evaluation-workbench/projects", json={"name": "新项目", "password": "incorrect"})
        accepted = client.post("/api/evaluation-workbench/projects", json={"name": "新项目", "password": "test-password"})

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 201)

    def test_create_project_supports_plaintext_runtime_password(self):
        client = self.app.test_client()
        self.app.config["EVALUATION_WORKBENCH_NEW_PROJECT_PASSWORD"] = "plain-runtime-password"

        rejected = client.post("/api/evaluation-workbench/projects", json={"name": "新项目", "password": "incorrect"})
        accepted = client.post("/api/evaluation-workbench/projects", json={"name": "新项目", "password": "plain-runtime-password"})

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 201)

    def test_multi_pdf_compare_creates_one_pair_result(self):
        self._add_pdf("tender.pdf", "tender", "", "采购需求：稳定运行。")
        self._add_pdf("bid-a.pdf", "bid", "甲公司", "技术方案：稳定运行，提供培训。")
        self._add_pdf("bid-b.pdf", "bid", "乙公司", "技术方案：稳定运行，提供培训。")
        task = storage.create_task(self.app, self.project["project_id"], "compare_documents")

        finished = self._run_next_task()
        pairs = storage.list_compare_pairs(self.app, task["task_id"])

        self.assertEqual(finished["status"], "success")
        self.assertEqual(len(pairs), 1)
        self.assertIn("summary", pairs[0]["result"])
        self.assertIn("cross_bid_analysis", finished["result"])
        self.assertEqual(finished["result"]["cross_bid_analysis"]["statutory_collusion_condition"], "not_assessed")

    def test_cross_bid_analysis_separates_dimensions_and_never_auto_determines_collusion(self):
        left = {"document_id": "a", "bidder_name": "甲公司", "original_name": "a.pdf"}
        right = {"document_id": "b", "bidder_name": "乙公司", "original_name": "b.pdf"}
        result = {
            "paragraphs": [
                {"type": "text", "text_a": "独有技术方案完全相同", "text_b": "独有技术方案完全相同", "page_a": 2, "page_b": 3},
                {"type": "shared_error", "text_a": "保证期为为三年", "text_b": "保证期为为三年", "page_a": 4, "page_b": 5, "error_kind": "重复字"},
                {"type": "entity", "text_a": "13800138000", "text_b": "13800138000", "page_a": 6, "page_b": 7},
            ],
            "metadata": {
                "auxiliary": {"matches": [
                    {"field": "author", "label": "作者/创建者", "value": "Same Author", "strength": "reference", "also_in_tender": False},
                    {"field": "creator", "label": "创建软件", "value": "Common Tool", "strength": "weak", "also_in_tender": True},
                ]},
                "text_stats": {
                    "file_a": {"scan_ratio": 0.45},
                    "file_b": {"scan_ratio": 0.60},
                },
            },
        }

        analysis = build_cross_bid_analysis("task-1", [(left, right, result)], tender_loaded=True)

        dimensions = {item["dimension"] for item in analysis["signals"]}
        self.assertEqual(dimensions, {"text_similarity", "text_error", "contact", "metadata"})
        self.assertEqual(analysis["pair_summaries"][0]["review_priority"], "high")
        self.assertEqual(analysis["assessment_scope"], "collusion_signal_only")
        self.assertEqual(analysis["statutory_collusion_condition"], "not_assessed")
        self.assertTrue(analysis["methodology"]["tender_source_excluded"])
        self.assertFalse(analysis["methodology"]["public_template_removed"])
        self.assertTrue(all(item["severity"] == "S3" for item in analysis["signals"]))
        contact = next(item for item in analysis["signals"] if item["dimension"] == "contact")
        self.assertEqual(contact["evidence"][0]["text_a"], "13800138000")
        metadata = next(item for item in analysis["signals"] if item["dimension"] == "metadata")
        self.assertEqual(len(metadata["evidence"]), 2)
        self.assertIn("45.0%", metadata["evidence"][1]["value"])
        self.assertIn("正文查重覆盖有限", metadata["basis"])

    def test_cross_bid_analysis_separates_common_name_email_and_address(self):
        left = {"document_id": "a", "bidder_name": "甲公司", "original_name": "a.pdf"}
        right = {"document_id": "b", "bidder_name": "乙公司", "original_name": "b.pdf"}
        result = {"paragraphs": [
            {"type": "entity", "entity_kind": "person_name", "text_a": "张三", "text_b": "张三", "page_a": 2, "page_b": 4},
            {"type": "entity", "entity_kind": "email", "text_a": "shared@example.com", "text_b": "shared@example.com", "page_a": 3, "page_b": 5},
            {"type": "entity", "entity_kind": "address", "text_a": "北京市朝阳区建国路88号", "text_b": "北京市朝阳区建国路88号", "page_a": 6, "page_b": 7},
        ]}

        analysis = build_cross_bid_analysis("task-1", [(left, right, result)], tender_loaded=True)

        self.assertEqual({item["dimension"] for item in analysis["signals"]}, {"person_name", "email", "address"})
        self.assertNotIn("address", {item["dimension"] for item in analysis["not_executed_dimensions"]})
        self.assertTrue(all(item["signal_type"] == "collusion_signal" for item in analysis["signals"]))

    def test_cross_bid_signal_hides_cover_and_standard_form_noise_but_keeps_technical_text(self):
        left = {"document_id": "a", "bidder_name": "甲公司", "original_name": "a.pdf"}
        right = {"document_id": "b", "bidder_name": "乙公司", "original_name": "b.pdf"}
        result = {"paragraphs": [
            {"type": "text", "text_a": "投标文件正本 项目名称 教学仪器采购 项目编号 JY-01 投标人名称",
             "text_b": "投标文件正本 项目名称 教学仪器采购 项目编号 JY-01 投标人名称", "page_a": 1, "page_b": 1},
            {"type": "text", "text_a": "实验台采用耐腐蚀环氧树脂台面并配独立通风控制模块",
             "text_b": "实验台采用耐腐蚀环氧树脂台面并配独立通风控制模块", "page_a": 88, "page_b": 96},
        ]}

        analysis = build_cross_bid_analysis("task-1", [(left, right, result)], tender_loaded=True)

        signal = next(item for item in analysis["signals"] if item["dimension"] == "text_similarity")
        self.assertEqual(len(signal["evidence"]), 1)
        self.assertIn("环氧树脂", signal["evidence"][0]["text_a"])

    def test_form_field_completion_is_not_a_substantive_tender_edit(self):
        detector = CollusionDetector()
        tender = "采购人：\n项目名称：\n项目编号：\n"
        edit = {"original": "", "modified": "太原市某单位", "source_start": 4, "source_end": 4}

        self.assertTrue(detector._is_form_field_completion(edit, tender))

    def test_voice_only_tender_edit_signal_is_downgraded(self):
        left = {"document_id": "a", "bidder_name": "甲公司", "original_name": "a.pdf"}
        right = {"document_id": "b", "bidder_name": "乙公司", "original_name": "b.pdf"}
        detector = CollusionDetector()
        tender_text = detector.normalize("项目实施完成后,实施方须向用户提交详细资料存档")
        bid_text = detector.normalize("项目实施完成后,我方会向用户提交详细资料存档")
        evidence = detector._shared_tender_edit_evidence(tender_text, bid_text, bid_text)
        self.assertIsNotNone(evidence)
        result = {"paragraphs": [
            {"type": "tender_related", "text_a": "项目实施完成后,我方会向用户提交详细资料存档",
             "text_b": "项目实施完成后,我方会向用户提交详细资料存档", "page_a": 489, "page_b": 308,
             "shared_edits": evidence["changes"], "voice_adaptation_only": evidence["voice_adaptation_only"]},
        ], "metadata": {}}

        analysis = build_cross_bid_analysis("task-1", [(left, right, result)], tender_loaded=True)

        signal = next(item for item in analysis["signals"] if item["dimension"] == "tender_common_edit")
        self.assertEqual(signal["confidence"], "C1")
        self.assertTrue(signal["voice_adaptation_only"])
        self.assertIn("第一人称改写", signal["basis"])
        self.assertEqual(analysis["pair_summaries"][0]["independent_dimension_count"], 0)
        self.assertEqual(analysis["pair_summaries"][0]["review_priority"], "none")
        self.assertEqual(analysis["pair_summaries"][0]["assessment_result"], "pending_human_review")

    def test_mixed_tender_edit_signal_keeps_c2_and_priority(self):
        left = {"document_id": "a", "bidder_name": "甲公司", "original_name": "a.pdf"}
        right = {"document_id": "b", "bidder_name": "乙公司", "original_name": "b.pdf"}
        result = {"paragraphs": [
            {"type": "tender_related", "text_a": "质保3年,我方实施", "text_b": "质保3年,我方实施",
             "page_a": 10, "page_b": 20,
             "shared_edits": [
                 {"original": "实施", "modified": "我", "voice_adaptation": True},
                 {"original": "5年", "modified": "3年"},
             ]},
        ], "metadata": {}}

        analysis = build_cross_bid_analysis("task-1", [(left, right, result)], tender_loaded=True)

        signal = next(item for item in analysis["signals"] if item["dimension"] == "tender_common_edit")
        self.assertEqual(signal["confidence"], "C2")
        self.assertNotIn("voice_adaptation_only", signal)
        self.assertEqual(analysis["pair_summaries"][0]["independent_dimension_count"], 1)
        self.assertEqual(analysis["pair_summaries"][0]["review_priority"], "normal")

    def test_compare_ai_packet_is_limited_to_fixed_rule_evidence(self):
        packet = worker._compare_evidence_packet({
            "signal_id": "signal-1", "bidder_a": "甲公司", "bidder_b": "乙公司", "dimension_label": "正文雷同",
            "basis": "发现 1 处完全雷同", "counter_evidence": ["公共模板可能造成相似"],
            "evidence": [{"page_a": 1, "page_b": 2, "text_a": "A" * 1000, "text_b": "B" * 1000, "ignored": "不得发送"}],
        })

        self.assertEqual(packet["signal_id"], "signal-1")
        self.assertNotIn("ignored", packet["evidence"][0])
        self.assertLessEqual(len(packet["evidence"][0]["text_a"]), 280)
        self.assertNotIn("投标文件全文", str(packet))

    def test_compare_ai_recovers_complete_assessments_then_retries_only_missing_signals(self):
        task = storage.create_task(self.app, self.project["project_id"], "compare_documents")
        signals = [
            {"signal_id": f"signal-{index}", "bidder_a": "甲", "bidder_b": "乙", "dimension_label": "正文雷同", "basis": f"线索{index}", "evidence": []}
            for index in range(1, 4)
        ]
        partial = '{"assessments":[{"signal_id":"signal-1","decision":"excluded","risk_level":"low","confidence":"high","reason":"公共模板","suggested_check":"无需额外核验"},'
        completed = {"assessments": [
            {"signal_id": "signal-2", "decision": "suspected_clue", "risk_level": "medium", "confidence": "medium", "reason": "存在相似表述", "suggested_check": "核验来源"},
            {"signal_id": "signal-3", "decision": "excluded", "risk_level": "low", "confidence": "high", "reason": "通用内容", "suggested_check": "无需额外核验"},
        ]}
        analysis = {"signals": signals, "pair_summaries": []}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            worker.InvalidJsonResponse(partial, "length"), completed,
        ]) as request_json:
            worker._assess_compare_signals_with_ai(self.app, task, analysis)

        self.assertEqual(analysis["ai_assessment"]["status"], "success")
        self.assertEqual(analysis["ai_assessment"]["assessed_count"], 3)
        self.assertEqual(request_json.call_count, 2)
        self.assertIn('"signal-1"', request_json.call_args_list[0].args[2])
        self.assertNotIn('"signal-1"', request_json.call_args_list[1].args[2])
        self.assertTrue(all(item["ai_assessment"]["reason"] != "AI 未返回该线索的可用判定。" for item in signals))

    def test_low_value_ai_dimensions_do_not_escalate_pair_priority(self):
        task = storage.create_task(self.app, self.project["project_id"], "compare_documents")
        signals = [
            {
                "signal_id": "text", "document_a_id": "a", "document_b_id": "b",
                "bidder_a": "甲", "bidder_b": "乙", "dimension": "text_similarity",
                "dimension_label": "正文雷同", "basis": "存在一段独有正文", "evidence": [],
            },
            {
                "signal_id": "error", "document_a_id": "a", "document_b_id": "b",
                "bidder_a": "甲", "bidder_b": "乙", "dimension": "text_error",
                "dimension_label": "共同异常或错误", "basis": "句末格式候选", "evidence": [],
            },
            {
                "signal_id": "edit", "document_a_id": "a", "document_b_id": "b",
                "bidder_a": "甲", "bidder_b": "乙", "dimension": "tender_common_edit",
                "dimension_label": "招标原文共同改动", "basis": "表格结构补全", "evidence": [],
            },
        ]
        analysis = {
            "signals": signals,
            "pair_summaries": [{
                "document_a_id": "a", "document_b_id": "b",
                "bidder_a": "甲", "bidder_b": "乙",
                "independent_dimension_count": 3, "signal_count": 3,
                "dimensions": ["text_similarity", "text_error", "tender_common_edit"],
                "dimension_labels": ["正文雷同", "共同异常或错误", "招标原文共同改动"],
                "review_priority": "high",
            }],
        }
        response = {"assessments": [
            {"signal_id": "text", "decision": "suspected_clue", "risk_level": "high",
             "confidence": "medium", "reason": "存在独有正文", "suggested_check": "核验来源"},
            {"signal_id": "error", "decision": "suspected_clue", "risk_level": "low",
             "confidence": "low", "reason": "格式噪声", "suggested_check": "无需升级"},
            {"signal_id": "edit", "decision": "suspected_clue", "risk_level": "low",
             "confidence": "medium", "reason": "结构补全", "suggested_check": "核对模板"},
        ]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response):
            worker._assess_compare_signals_with_ai(self.app, task, analysis)

        summary = analysis["pair_summaries"][0]
        self.assertEqual(summary["raw_signal_count"], 3)
        self.assertEqual(summary["signal_count"], 1)
        self.assertEqual(summary["independent_dimension_count"], 1)
        self.assertEqual(summary["review_priority"], "normal")

    def test_compare_ai_retries_single_missing_signal_once_then_uses_fallback(self):
        task = storage.create_task(self.app, self.project["project_id"], "compare_documents")
        signal = {"signal_id": "signal-1", "bidder_a": "甲", "bidder_b": "乙", "dimension_label": "正文雷同", "basis": "线索", "evidence": []}
        analysis = {"signals": [signal], "pair_summaries": []}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            worker.InvalidJsonResponse('{"assessments":[', "length"),
            worker.InvalidJsonResponse('{"assessments":[', "length"),
        ]) as request_json:
            worker._assess_compare_signals_with_ai(self.app, task, analysis)

        self.assertEqual(request_json.call_count, 2)
        self.assertEqual(analysis["ai_assessment"]["status"], "partial")
        self.assertEqual(analysis["ai_assessment"]["assessed_count"], 0)
        self.assertEqual(signal["ai_assessment"]["reason"], "AI 未返回该线索的可用判定。")

    def test_compare_signal_disposition_is_persisted_separately(self):
        task = storage.create_task(self.app, self.project["project_id"], "compare_documents")
        storage.initialize_compare_signal_reviews(self.app, task["task_id"], [{"signal_id": "signal-1"}])
        storage.update_task(self.app, task["task_id"], result={"cross_bid_analysis": {
            "signals": [{"signal_id": "signal-1", "human_disposition": "pending", "human_note": ""}]
        }})

        response = self.app.test_client().patch(
            "/api/evaluation-workbench/compare-signals/signal-1",
            json={"human_disposition": "dismissed", "human_note": "公共模板造成"},
        )

        self.assertEqual(response.status_code, 200)
        review = response.get_json()["review"]
        self.assertEqual(review["human_disposition"], "dismissed")
        self.assertEqual(review["human_note"], "公共模板造成")
        self.assertIsNotNone(review["reviewed_at"])
        analysis = storage.compare_analysis(self.app, task["task_id"])
        self.assertEqual(analysis["signals"][0]["human_disposition"], "dismissed")

    def test_rule_extraction_creates_a_draft_rule_set(self):
        self._add_pdf("tender.pdf", "tender", "", "投标人应具备有效资质，技术方案满分十分。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"rules": [
            {"category": "qualification", "title": "具备有效资质", "check_rule": "核验是否提供有效资质材料", "source_text": "投标人应具备有效资质", "check_mode": "auto"},
            {"category": "subjective", "title": "技术方案评分", "source_text": "技术方案满分十分", "ocr_required": True, "scoring": {"max_score": 10}},
        ]}):
            finished = self._run_next_task()

        rule_set, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(rule_set["status"], "draft")
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(item["source_type"] == "ai" for item in rules))
        self.assertEqual(next(item for item in rules if item["title"] == "具备有效资质")["check_rule"], "核验是否提供有效资质材料")
        # 模型误把纯文字方案评分标为 OCR 时，提取归一化应校正为纯文字核验。
        self.assertEqual(next(item for item in rules if item["title"] == "技术方案评分")["check_mode"], "auto")
        self.assertEqual(next(item for item in rules if item["title"] == "技术方案评分")["baseline_ocr_mode"], "auto")

    def test_rule_extraction_treats_objective_rules_with_score_items_as_manual(self):
        self._add_pdf("tender.pdf", "tender", "", "管理体系认证每提供一类得1分，最高3分。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.create_task(self.app, self.project["project_id"], "extract_rules")
        response = {"rules": [
            {
                "category": "objective", "title": "管理体系认证", "check_rule": "每提供一类认证得1分，最高3分",
                "source_text": "管理体系认证每提供一类得1分，最高3分。",
                "scoring": {"max_score": 3, "kind": "boolean", "items": [
                    {"name": "管理体系认证", "max_score": 3, "criterion": "每提供一类得1分"},
                ]},
            },
            {
                "category": "objective", "title": "固定资质得分", "check_rule": "具备该资质得5分",
                "source_text": "具备该资质得5分。", "scoring": {"max_score": 5, "kind": "boolean"},
            },
        ]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response):
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        scoring = json.loads(next(item for item in rules if item["title"] == "管理体系认证")["scoring_json"])
        fixed_scoring = json.loads(next(item for item in rules if item["title"] == "固定资质得分")["scoring_json"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(scoring["kind"], "manual")
        self.assertEqual(fixed_scoring["kind"], "boolean")

    def test_rule_extraction_retries_with_compact_output_after_json_truncation(self):
        self._add_pdf("tender.pdf", "tender", "", "投标人应具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.create_task(self.app, self.project["project_id"], "extract_rules")
        valid = {"rules": [{"category": "qualification", "title": "有效资质", "check_rule": "核验有效资质", "source_text": "应具备有效资质"}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[ValueError("模型未返回有效 JSON（模型输出达到长度上限）"), valid]) as request_json:
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["compact_retry_count"], 1)
        self.assertEqual(request_json.call_count, 2)
        self.assertEqual(request_json.call_args_list[0].kwargs["max_tokens"], 2500)
        self.assertGreaterEqual(request_json.call_args_list[1].kwargs["max_tokens"], request_json.call_args_list[0].kwargs["max_tokens"])
        self.assertEqual(request_json.call_args_list[0].args[0]["thinking_mode"], "disabled")

    def test_rule_extraction_recovers_complete_json_items_before_requesting_only_missing_rules(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        recovered = {
            "category": "qualification", "title": "营业执照", "check_rule": "核验有效营业执照",
            "source_text": "提供有效营业执照",
        }
        missing = {
            "category": "qualification", "title": "法定代表人身份证明", "check_rule": "核验身份证明",
            "source_text": "提供法定代表人身份证明",
        }
        raw = '{"rules":[' + json.dumps(recovered, ensure_ascii=False) + ',{"title":"截断'

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            worker.InvalidJsonResponse(raw, "length"), {"rules": [missing]},
        ]) as request_json:
            rules, compact_retries, split_retries = worker._extract_rule_batch(
                self.app, task, profile, "规则提取系统提示", "投标人应提供营业执照和身份证明。",
                document_id=None, batch_label="rule_batch_1_of_1",
            )

        self.assertEqual({item["title"] for item in rules}, {"营业执照", "法定代表人身份证明"})
        self.assertEqual((compact_retries, split_retries), (0, 0))
        self.assertEqual(request_json.call_count, 2)
        self.assertIn("已回收规则", request_json.call_args_list[1].args[2])
        self.assertIn("营业执照", request_json.call_args_list[1].args[2])

    def test_rule_extraction_mapping_uses_at_most_two_workers_and_preserves_source_order(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        lock = threading.Lock()
        active = 0
        peak = 0

        def fake_extract(_app, _task, _profile, _system, text, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return ([{"category": "qualification", "title": text, "check_rule": f"核验{text}"}], 0, 0)

        with patch("dashboard.evaluation_workbench.worker._extract_rule_batch", side_effect=fake_extract):
            rules, compact_retries, split_retries = worker._extract_rule_batches(
                self.app, task, profile, "规则提取系统提示", ["第一批", "第二批", "第三批"], document_id="tender-1",
            )

        self.assertEqual(peak, 2)
        self.assertEqual([item["title"] for item in rules], ["第一批", "第二批", "第三批"])
        self.assertEqual((compact_retries, split_retries), (0, 0))

    def test_rule_compilation_splits_only_the_overflowing_group_and_keeps_all_rules(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        candidates = [
            {"category": "qualification", "title": f"资格条件{index}", "check_rule": f"核验资格条件{index}",
             "source_text": f"投标人应满足资格条件{index}", "source_page": index}
            for index in range(12)
        ]
        left_rules, right_rules = candidates[:6], candidates[6:]
        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            worker.InvalidJsonResponse('{"rules":[', "length"),
            {"rules": left_rules}, {"missing_rules": []},
            {"rules": right_rules}, {"missing_rules": []},
            {"rules": candidates},
        ]) as request_json:
            compiled, missing, used = worker._compile_rule_candidates(
                self.app, task, profile, "规则编译系统提示", candidates, 40_000,
            )

        self.assertTrue(used)
        self.assertEqual(missing, [])
        self.assertEqual({item["title"] for item in compiled}, {item["title"] for item in candidates})
        self.assertEqual(request_json.call_count, 6)
        self.assertEqual(request_json.call_args_list[0].kwargs["max_tokens"], 6240)

    def test_global_rule_compile_semantically_merges_results_from_different_groups(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        split_results = [
            {"category": "qualification", "title": "营业执照要求", "check_rule": "核验有效营业执照", "source_text": "提供有效营业执照"},
            {"category": "compliance", "title": "营业执照缺失后果", "check_rule": "未提供营业执照则无效", "source_text": "未提供则响应无效"},
        ]
        merged_response = {"rules": [{
            "category": "qualification", "title": "营业执照",
            "check_rule": "核验有效营业执照；未提供则响应无效",
            "source_text": "提供有效营业执照，未提供则响应无效",
        }]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=merged_response) as request_json:
            merged = worker._merge_compiled_rule_groups(
                self.app, task, profile, "规则编译系统提示", split_results, 40_000,
            )

        self.assertEqual(len(merged), 1)
        self.assertIn("未提供则响应无效", merged[0]["check_rule"])
        self.assertEqual(request_json.call_count, 1)

    def test_final_rule_quality_gate_drops_only_explicit_items_and_recovers_score_coverage(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "人工保留资质", "check_rule": "核验人工保留资质",
        })
        storage.create_global_rule(self.app, {
            "category": "substantive", "title": "通用承诺", "check_rule": "核验通用承诺", "enabled": True,
        })
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        rules = [
            {"category": "qualification", "title": "响应有效期", "check_rule": "核验响应有效期"},
            {"category": "compliance", "title": "响应有效期重复", "check_rule": "再次核验响应有效期"},
            {"category": "substantive", "title": "成交后合同签订", "check_rule": "成交后签订合同"},
            {"category": "objective", "title": "类似项目业绩评分", "check_rule": "每项3分，最高9分", "source_text": "业绩每有一个得3分，最高9分", "scoring": {"max_score": 9, "kind": "manual"}},
            {"category": "qualification", "title": "营业执照", "check_rule": "核验营业执照", "ocr_required": True},
            {"category": "subjective", "title": "技术方案", "check_rule": "按完整性评分", "scoring": {"max_score": 10, "kind": "manual"}},
        ]
        response = {"drops": [
            {"rule_id": "R2", "reason": "duplicate", "duplicate_of": "R1"},
            {"rule_id": "R3", "reason": "procedural", "duplicate_of": None},
            {"rule_id": "R4", "reason": "duplicate", "duplicate_of": "R1"},
            {"rule_id": "R5", "reason": "unknown_reason", "duplicate_of": None},
        ]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response) as request_json:
            kept, stats = worker._final_rule_quality_gate(
                self.app, task, profile, "规则提取系统提示", rules, ["类似项目业绩每有一个得3分，最高9分"],
            )

        self.assertEqual({item["title"] for item in kept}, {"响应有效期", "类似项目业绩评分", "营业执照", "技术方案"})
        self.assertTrue(stats["applied"])
        self.assertEqual(stats["dropped_count"], 2)
        self.assertEqual(stats["recovered_score_count"], 1)
        quality_prompt = request_json.call_args.args[2]
        self.assertIn("人工保留资质", quality_prompt)
        self.assertIn("通用承诺", quality_prompt)
        self.assertIn('"rule_id":"R6"', quality_prompt)

    def test_final_rule_quality_gate_failure_keeps_all_compiled_rules(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        rules = [
            {"category": "qualification", "title": f"规则{index}", "check_rule": f"核验规则{index}"}
            for index in range(6)
        ]

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=ValueError("模型接口繁忙")), patch(
            "dashboard.evaluation_workbench.worker.time.sleep"
        ):
            kept, stats = worker._final_rule_quality_gate(
                self.app, task, profile, "规则提取系统提示", rules, [],
            )

        self.assertEqual(kept, rules)
        self.assertFalse(stats["applied"])
        self.assertEqual(stats["failure_count"], 1)
        self.assertEqual(stats["dropped_count"], 0)

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={
            "drops": [{"rule_id": f"R{index}", "reason": "umbrella"} for index in range(1, 7)],
        }):
            kept_after_overreach, overreach_stats = worker._final_rule_quality_gate(
                self.app, task, profile, "规则提取系统提示", rules, [],
            )
        self.assertEqual(kept_after_overreach, rules)
        self.assertEqual(overreach_stats["failure_count"], 1)

    def test_final_rule_operations_rewrite_merge_and_drop_without_touching_scores(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        rules = [
            {"category": "qualification", "title": "代理资格", "check_rule": "核验代理条件", "source_text": "代理商须满足资格条件"},
            {"category": "compliance", "title": "制造商授权书", "check_rule": "核验制造商授权书", "source_text": "代理商须提供制造商授权书", "ocr_required": True},
            {"category": "rejection", "title": "保证金平台状态", "check_rule": "核验平台子账号到账状态", "source_text": "投标保证金金额为五万元"},
            {"category": "compliance", "title": "签章及在线提交", "check_rule": "核验签章并确认在线提交", "source_text": "投标文件应按要求签字盖章并在线提交"},
            {"category": "objective", "title": "业绩评分", "check_rule": "每项3分，最高9分", "source_text": "每项3分，最高9分", "scoring": {"max_score": 9, "kind": "manual"}},
        ]
        rules.extend(
            {"category": "substantive", "title": f"有效规则{index}", "check_rule": f"核验有效规则{index}", "source_text": f"应满足有效规则{index}"}
            for index in range(6, 13)
        )
        boundary_response = {
            "drops": [
                {"rule_id": "R3", "reason": "not_file_verifiable"},
                {"rule_id": "R5", "reason": "duplicate"},
            ],
            "rewrites": [
                {"rule_id": "R4", "reason": "partial_boundary", "title": "电子签章与签字形式", "check_rule": "核验电子签章、扫描签字及涂改确认。", "ocr_required": True},
                {"rule_id": "R5", "reason": "partial_boundary", "title": "错误评分改写", "check_rule": "不得生效", "ocr_required": False},
            ],
            "merges": [],
        }
        merge_response = {
            "drops": [], "rewrites": [],
            "merges": [{
                "rule_ids": ["R1", "R2"], "keep_rule_id": "R1", "reason": "duplicate",
                "title": "生产或代理资格与授权材料", "check_rule": "核验代理资格条件及制造商授权书。", "ocr_required": True,
            }],
        }

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[boundary_response, merge_response]) as request_json:
            kept, stats = worker._finalise_rule_operations(
                self.app, task, profile, "规则提取系统提示", rules,
            )

        self.assertEqual(len(kept), 10)
        self.assertNotIn("保证金平台状态", {item["title"] for item in kept})
        merged = next(item for item in kept if item["title"] == "生产或代理资格与授权材料")
        self.assertIn("代理商须满足资格条件", merged["source_text"])
        self.assertIn("代理商须提供制造商授权书", merged["source_text"])
        self.assertTrue(merged["ocr_required"])
        rewritten = next(item for item in kept if item["title"] == "电子签章与签字形式")
        self.assertNotIn("在线提交", rewritten["check_rule"])
        score = next(item for item in kept if item["category"] == "objective")
        self.assertEqual(score["title"], "业绩评分")
        self.assertEqual(score["scoring"]["max_score"], 9)
        self.assertEqual(stats, {
            "applied": True, "dropped_count": 1, "rewritten_count": 1,
            "merged_count": 1, "failure_count": 0,
        })
        self.assertEqual(request_json.call_count, 2)
        self.assertIn("文件无法直接核验", request_json.call_args_list[0].args[2])
        self.assertIn("重复、条件模板", request_json.call_args_list[1].args[2])

    def test_final_rule_operations_failure_keeps_all_rules(self):
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)
        rules = [
            {"category": "qualification", "title": f"规则{index}", "check_rule": f"核验规则{index}"}
            for index in range(12)
        ]
        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"drops": "invalid"}):
            kept, stats = worker._finalise_rule_operations(
                self.app, task, profile, "规则提取系统提示", rules,
            )
        self.assertEqual(kept, rules)
        self.assertEqual(stats["failure_count"], 2)
        self.assertFalse(stats["applied"])

    def test_explicit_non_ocr_is_overridden_only_by_decisive_visual_evidence(self):
        self.assertTrue(worker._rule_requires_visual_verification({
            "ocr_required": False,
            "check_rule": "核验管理体系证书，并同时提供平台查询截图与证书复印件。",
        }))
        self.assertFalse(worker._rule_requires_visual_verification({
            "ocr_required": False,
            "check_rule": "核验投标文件文字中列明的证书编号、有效期和适用范围。",
        }))

    def test_score_clause_packets_capture_parenthesised_score_table_rows(self):
        text = """[第51页]
投标设备产品整体技术性能指标的响应程度（12 分）
投标设备产品的运营可靠性（8分）
技术支持资料
（7 分） 根据优良情况酌情打分。
普通履约期限为30日。
"""
        packets = worker._score_clause_packets(text)
        self.assertEqual(len(packets), 3)
        self.assertTrue(any("12 分" in packet["score_line"] for packet in packets))
        self.assertTrue(any("8分" in packet["score_line"] for packet in packets))
        self.assertTrue(any("7 分" in packet["score_line"] for packet in packets))

    def test_score_clause_packet_keeps_certificate_context_across_page_break(self):
        text = """管理体系认证证书：每提供一类得1分，最高1分。
（2）取得 CCID 信息系统服务交付能力等级证书、取得 CCRC 信息安全服务资质认证证书。
提供以上证书复印件。
46
[第50页]
每提供一类得1分，最高2分。
"""
        packets = worker._score_clause_packets(text)
        target = next(packet for packet in packets if "最高2分" in packet["score_line"])
        self.assertIn("CCID 信息系统服务交付能力等级证书", target["text"])
        self.assertIn("CCRC 信息安全服务资质认证证书", target["text"])
        unrelated_rule = {
            "title": "类似业绩分类计分",
            "source_text": "每提供一类得1分，最高2分。",
            "check_rule": "核验类似业绩，每提供一类得1分，最高2分。",
        }
        self.assertFalse(worker._score_packet_is_covered(target, [unrelated_rule]))

    def test_package_scope_filters_score_packets_using_nearest_package_heading(self):
        text = """[第35页]
采购包1：
报价得分最高20分。
视觉方案得分最高10分。
[第36页]
采购包2：
报价得分最高20分。
现场服务方案得分最高10分。
[第37页]
采购包3：
报价得分最高20分。
音乐制作方案得分最高15分。
"""
        packets = worker._score_clause_packets(text)
        package_three_packets = worker._filter_score_packets_for_package(packets, 3)

        self.assertEqual(len(packets), 6)
        self.assertEqual(len(package_three_packets), 2)
        self.assertTrue(all(packet["package_numbers"] == [3] for packet in package_three_packets))
        self.assertTrue(any("音乐制作方案" in packet["text"] for packet in package_three_packets))
        self.assertTrue(all("视觉方案" not in packet["text"] for packet in package_three_packets))

    def test_package_scope_filters_only_explicit_other_package_rules(self):
        rules = [
            {"title": "包1报价评分", "check_rule": "采购包1报价最高20分", "source_text": "采购包1：报价得分最高20分。"},
            {"title": "包3音乐评分", "check_rule": "采购包3音乐制作方案最高15分", "source_text": "采购包3：音乐制作方案得分最高15分。"},
            {"title": "通用资格", "check_rule": "包1、包2、包3均须提供营业执照", "source_text": "各采购包均适用。"},
            {"title": "未标注范围的资格", "check_rule": "提供依法设立证明材料", "source_text": "供应商应提供营业执照。"},
        ]

        kept = worker._filter_rules_for_package(rules, 3)

        self.assertEqual([item["title"] for item in kept], ["包3音乐评分", "通用资格", "未标注范围的资格"])
        self.assertEqual(worker._filter_rules_for_package(rules, None), rules)

    def test_project_package_scope_uses_section_name_not_project_number(self):
        package_number, instruction = worker._project_package_scope_instruction(self.app, {
            "name": "开幕式", "project_number": "1", "section_name": "包3",
        })
        no_package_number, no_package_instruction = worker._project_package_scope_instruction(self.app, {
            "name": "普通项目", "project_number": "包3", "section_name": "",
        })

        self.assertEqual(package_number, 3)
        self.assertIn("当前项目仅对应采购包3", instruction)
        self.assertIn("分包/标段：包3", instruction)
        self.assertIsNone(no_package_number)
        self.assertIn("未填写可识别的包号", no_package_instruction)

    def test_qualification_clause_packets_keep_formal_material_requirements(self):
        text = """[第11页]
三、供应商资格要求
依法设立
3.1 的证明材料
供应商应提供营业执照、基本账户信息、近6个月任意一次纳税凭证及社保凭证；依法免税或免缴的提供证明。
财务要求
3.2 证明材料
供应商应提供年度财务审计报告；新成立企业可提供银行资信证明。
[第12页]
业绩要求
3.3 证明材料
供应商应提供近年类似服务业绩，至少一项，并附合同关键页。
[第13页]
四、采购文件获取
获取时间和地点另行通知。
"""
        packets = worker._qualification_clause_packets(text)
        combined = "\n".join(packet["text"] for packet in packets)
        self.assertTrue(packets)
        self.assertIn("纳税凭证及社保凭证", combined)
        self.assertIn("财务审计报告", combined)
        self.assertIn("至少一项", combined)
        self.assertTrue(all(packet["label"].startswith("第") for packet in packets))
        prompt = worker._qualification_rule_supplement_prompt(self.app, packets, [])
        self.assertIn(packets[0]["clause_id"], prompt)
        self.assertIn("资格业绩门槛与同一业绩的加分条款必须同时保留", prompt)
        self.assertIn("相邻非资格内容", prompt)

    def test_qualification_clause_packets_ignore_unrelated_numbered_material_section(self):
        text = """[第31页]
五、施工组织设计
5.1 技术路线
投标人应说明系统架构和实施计划。
5.2 证明材料
投标人可附产品彩页和检测材料。
"""

        self.assertEqual(worker._qualification_clause_packets(text), [])

    def test_qualification_clause_packet_limit_keeps_late_formal_anchor(self):
        text = """[第1页]
一、投标人资格要求
1.1 提供主体资格证明。
[第2页]
第一处相邻说明。
[第3页]
第二处相邻说明。
[第10页]
资格评审标准
核验项目负责人资格。
"""

        packets = worker._qualification_clause_packets(text, limit=2)
        combined = "\n".join(packet["text"] for packet in packets)
        self.assertEqual(len(packets), 2)
        self.assertIn("提供主体资格证明", combined)
        self.assertIn("核验项目负责人资格", combined)

    def test_scoring_reconciliation_preserves_all_clauses_and_corrects_discretionary_category(self):
        packets = worker._score_clause_packets("\n".join([
            "商务部分（9分）", "同类业绩：每提供一个得3分，最高6分。", "管理体系证书：每项1分，最高1分。",
            "技术部分（46分）", "设备选型（5分）：按优劣横向比较、酌情评分。", "技术方案（41分）：按完整性和合理性评分。",
            "投标报价（45分）：按报价公式计算。", "总分（100分）。",
        ]))
        current_rules = [
            {"category": "objective", "title": "同类业绩", "check_rule": "每项3分，最高6分", "source_text": "每提供一个得3分，最高6分", "scoring": {"max_score": 6, "kind": "manual"}},
            {"category": "objective", "title": "设备选型", "check_rule": "按横向比较评分", "source_text": "设备选型（5分）", "scoring": {"max_score": 5, "kind": "manual"}},
            {"category": "subjective", "title": "技术方案", "check_rule": "按完整性评分", "source_text": "技术方案（41分）", "scoring": {"max_score": 41, "kind": "manual"}},
        ]
        clause_ids = [packet["clause_id"] for packet in packets]
        reconciled = {
            "rules": [
                {"category": "objective", "title": "同类业绩", "check_rule": "每提供一个得3分，最高6分。", "source_text": "同类业绩：每提供一个得3分，最高6分。", "source_clause_ids": clause_ids[:2], "scoring": {"max_score": 6, "kind": "manual"}},
                {"category": "objective", "title": "管理体系证书", "check_rule": "每项1分，最高1分。", "source_text": "管理体系证书：每项1分，最高1分。", "source_clause_ids": [clause_ids[2]], "scoring": {"max_score": 1, "kind": "manual"}},
                {"category": "subjective", "title": "设备选型", "check_rule": "按优劣横向比较、酌情评分，满分5分。", "source_text": "设备选型（5分）：按优劣横向比较、酌情评分。", "source_clause_ids": clause_ids[3:5], "scoring": {"max_score": 5, "kind": "manual"}},
                {"category": "subjective", "title": "技术方案", "check_rule": "按完整性和合理性评分，满分41分。", "source_text": "技术方案（41分）：按完整性和合理性评分。", "source_clause_ids": [clause_ids[5]], "scoring": {"max_score": 41, "kind": "manual"}},
                {"category": "objective", "title": "投标报价", "check_rule": "按报价公式计算，满分45分。", "source_text": "投标报价（45分）：按报价公式计算。", "source_clause_ids": clause_ids[6:], "scoring": {"max_score": 45, "kind": "manual"}},
            ],
        }
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=reconciled) as request_json:
            rules, stats = worker._reconcile_scoring_rules(
                self.app, task, profile, "规则提取系统提示", current_rules, packets,
            )

        self.assertTrue(stats["applied"])
        self.assertEqual(stats["failure_count"], 0)
        self.assertEqual(next(item for item in rules if item["title"] == "设备选型")["category"], "subjective")
        self.assertTrue(all(worker._score_packet_is_covered(packet, rules) for packet in packets))
        self.assertIn("完整评分条款", request_json.call_args.args[2])

    def test_scoring_reconciliation_keeps_original_rules_when_model_omits_clause_mapping(self):
        packets = worker._score_clause_packets("业绩：每提供一个得3分，最高6分。\n报价：最高得45分。")
        original = [{"category": "objective", "title": "业绩评分", "check_rule": "每个业绩3分，最高6分", "source_text": "每提供一个得3分，最高6分", "scoring": {"max_score": 6, "kind": "manual"}}]
        task = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        profile = storage.get_model_profile(self.app, None)

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"rules": original}):
            rules, stats = worker._reconcile_scoring_rules(
                self.app, task, profile, "规则提取系统提示", original, packets,
            )

        self.assertEqual(rules, original)
        self.assertFalse(stats["applied"])
        self.assertEqual(stats["failure_count"], 1)

    def test_rule_extraction_splits_long_source_into_bounded_batches(self):
        self._add_pdf("tender.pdf", "tender", "", "用于建立解析文件")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        tender_document = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        long_text = "\n".join(
            f"[第{index}页]\n" + ("投标人应提供有效资质证明材料。\n" * 400)
            for index in range(1, 7)
        )
        Path(tender_document["parsed_path"]).write_text(long_text, encoding="utf-8")
        storage.create_task(self.app, self.project["project_id"], "extract_rules")
        response = {"rules": [{"category": "qualification", "title": "有效资质", "check_rule": "核验有效资质", "source_text": "应提供有效资质证明"}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response) as request_json:
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertGreater(request_json.call_count, 1)
        self.assertTrue(all(call.kwargs["max_tokens"] <= 6000 for call in request_json.call_args_list))
        self.assertTrue(all(len(call.args[2]) < 16_000 for call in request_json.call_args_list))

    def test_rule_extraction_maps_late_source_instead_of_truncating_to_front_excerpt(self):
        self._add_pdf("tender.pdf", "tender", "", "用于建立解析文件")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        tender_document = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        late_clause = "特殊业绩计分：每提供一个同类项目业绩得3分，最高9分。"
        Path(tender_document["parsed_path"]).write_text(("普通说明。\n" * 35_000) + late_clause, encoding="utf-8")
        storage.create_task(self.app, self.project["project_id"], "extract_rules")

        def response(_profile, _system, user_prompt, **_kwargs):
            if late_clause in user_prompt:
                return {"rules": [{
                    "category": "objective", "title": "特殊业绩计分",
                    "check_rule": "每个同类项目业绩得3分，最高9分",
                    "source_text": late_clause, "scoring": {"kind": "manual", "max_score": 9},
                }]}
            return {"rules": []}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=response) as request_json:
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertIn("特殊业绩计分", {item["title"] for item in rules})
        self.assertTrue(any(late_clause in call.args[2] for call in request_json.call_args_list))

    def test_rule_extraction_supplements_missing_score_clause_with_compact_packet(self):
        tender_text = "\n".join([
            "商务评分", "供应商业绩", "业绩每有一个得3分，最高9分。",
            *[f"说明{i}" for i in range(10)], "报价评分", "报价得分最高25分。",
        ])
        self._add_pdf("tender.pdf", "tender", "", tender_text)
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        tender_document = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        Path(tender_document["parsed_path"]).write_text(tender_text, encoding="utf-8")
        storage.create_task(self.app, self.project["project_id"], "extract_rules")
        primary = {"rules": [{"category": "objective", "title": "报价评分", "check_rule": "按报价公式计算", "source_text": "报价得分最高25分", "scoring": {"max_score": 25, "kind": "manual"}}]}
        supplement = {"rules": [{"category": "objective", "title": "类似项目业绩评分", "check_rule": "每个同类型项目业绩计3分，最高9分", "source_text": "业绩每有一个得3分，最高9分", "scoring": {"max_score": 9, "kind": "manual"}}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[primary, supplement, {"rules": []}, {"drops": []}]) as request_json:
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["score_clause_count"], 2)
        self.assertEqual(finished["result"]["scoring_supplement_count"], 1)
        self.assertEqual(request_json.call_count, 4)
        self.assertEqual(next(item for item in rules if item["title"] == "类似项目业绩评分")["scoring_json"], '{"max_score": 9, "kind": "manual"}')

    def test_rule_extraction_checks_each_score_clause_not_only_score_rule_count(self):
        tender_text = "\n".join([
            "商务评分", "供应商业绩", "业绩每有一个得3分，最高9分。",
            *[f"说明{i}" for i in range(10)], "报价评分", "报价得分最高25分。",
        ])
        self._add_pdf("tender.pdf", "tender", "", tender_text)
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        tender_document = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        Path(tender_document["parsed_path"]).write_text(tender_text, encoding="utf-8")
        storage.create_task(self.app, self.project["project_id"], "extract_rules")
        primary = {"rules": [
            {"category": "objective", "title": "报价评分", "check_rule": "按报价公式计算", "source_text": "报价得分最高25分", "scoring": {"max_score": 25, "kind": "manual"}},
            *[{"category": "subjective", "title": f"技术方案{i}评分", "check_rule": "评价技术方案", "source_text": "技术方案评分", "scoring": {"max_score": 5, "kind": "manual"}} for i in range(6)],
        ]}
        supplement = {"rules": [{"category": "objective", "title": "类似项目业绩评分", "check_rule": "每个同类型项目业绩计3分，最高9分", "source_text": "业绩每有一个得3分，最高9分", "scoring": {"max_score": 9, "kind": "manual"}}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[primary, supplement, {"rules": []}, {"drops": []}]) as request_json:
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        supplement_prompt = request_json.call_args_list[1].args[2]
        self.assertEqual(finished["result"]["uncovered_score_clause_count"], 1)
        self.assertEqual(request_json.call_count, 4)
        self.assertIn("业绩每有一个得3分", supplement_prompt)
        self.assertNotIn("报价得分最高25分", supplement_prompt)
        self.assertIn("类似项目业绩评分", {item["title"] for item in rules})

    def test_adjacent_score_rows_remain_independent_coverage_clauses(self):
        packets = worker._score_clause_packets("\n".join([
            "商务评分标准", "业绩评分", "每提供一个同类项目业绩得3分，最高9分。",
            "项目人员评分", "每提供一名持证人员得2分，最高6分。",
        ]))
        performance_rule = [{
            "category": "objective", "title": "业绩评分",
            "check_rule": "每个同类业绩得3分，最高9分",
            "source_text": "每提供一个同类项目业绩得3分，最高9分。",
        }]

        self.assertEqual(len(packets), 2)
        self.assertTrue(worker._score_packet_is_covered(packets[0], performance_rule))
        self.assertFalse(worker._score_packet_is_covered(packets[1], performance_rule))
        self.assertNotEqual(packets[0]["clause_id"], packets[1]["clause_id"])

    def test_draft_rule_can_be_disabled_before_confirmation(self):
        enabled_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "保留审查项"})
        disabled_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "compliance", "title": "取消审查项"})

        response = self.app.test_client().patch(
            f"/api/evaluation-workbench/projects/{self.project['project_id']}/rules/{disabled_rule['rule_id']}",
            json={"enabled": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["rule"]["enabled"], 0)
        storage.confirm_rule_set(self.app, self.project["project_id"])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        status_by_rule = {item["rule_id"]: item["enabled"] for item in rules}
        self.assertEqual(status_by_rule[enabled_rule["rule_id"]], 1)
        self.assertEqual(status_by_rule[disabled_rule["rule_id"]], 0)

    def test_adding_to_confirmed_rules_creates_new_draft_version(self):
        storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "资质"})
        confirmed = storage.confirm_rule_set(self.app, self.project["project_id"])

        storage.add_rule(self.app, self.project["project_id"], {"category": "compliance", "title": "响应"})

        draft, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["version"], 2)
        self.assertEqual({item["title"] for item in rules}, {"资质", "响应"})
        self.assertTrue(all(item["source_type"] == "manual" for item in rules))

    def test_manual_model_key_is_encrypted_and_never_returned_by_api(self):
        profile = storage.create_model_profile(self.app, {
            "display_name": "测试模型", "base_url": "https://example.test/v1", "model_name": "test-model",
            "api_key": "secret-test-key", "json_mode": False,
        })

        self.assertNotIn("api_key", profile)
        self.assertNotIn("api_key_encrypted", profile)
        self.assertTrue(profile["api_key_configured"])
        self.assertEqual(profile["api_key_source"], "manual")
        internal = storage.get_model_profile(self.app, profile["profile_id"])
        self.assertEqual(internal["_api_key"], "secret-test-key")
        response = self.app.test_client().get("/api/evaluation-workbench/model-profiles")
        returned = next(item for item in response.get_json()["profiles"] if item["profile_id"] == profile["profile_id"])
        self.assertNotIn("api_key", returned)
        self.assertNotIn("api_key_encrypted", returned)

    def test_model_profile_rejects_key_with_non_ascii_or_whitespace_characters(self):
        with self.assertRaisesRegex(ValueError, "API Key 含有中文"):
            storage.create_model_profile(self.app, {
                "display_name": "格式错误模型", "base_url": "https://example.test/v1", "model_name": "test-model",
                "api_key": "错误 key",
            })

    def test_model_connection_endpoint_uses_saved_key_without_returning_it(self):
        profile = storage.create_model_profile(self.app, {
            "display_name": "测试模型", "base_url": "https://example.test/v1", "model_name": "test-model", "api_key": "secret-test-key",
        })

        client = self.app.test_client()
        self._unlock_model_configuration(client)
        with patch("dashboard.blueprints.evaluation_workbench.test_connection", return_value="连接成功：模型接口已响应") as test_connection:
            response = client.post(f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}/test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "连接成功：模型接口已响应")
        self.assertEqual(test_connection.call_args.args[0]["_api_key"], "secret-test-key")

    def test_model_profile_can_be_deleted_when_no_task_uses_it(self):
        profile = storage.create_model_profile(self.app, {
            "display_name": "待删除模型", "base_url": "https://example.test/v1", "model_name": "test-model", "api_key": "secret-test-key",
        })

        client = self.app.test_client()
        self._unlock_model_configuration(client)
        response = client.delete(f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(profile["profile_id"], {item["profile_id"] for item in storage.list_model_profiles(self.app)})

    def test_global_default_model_can_be_deleted_and_default_is_reassigned(self):
        profile = storage.create_model_profile(self.app, {
            "display_name": "默认测试模型", "base_url": "https://example.test/v1", "model_name": "default-test", "api_key": "test-key",
        })

        client = self.app.test_client()
        self._unlock_model_configuration(client)
        response = client.post(f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}/default")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.get_model_profile(self.app, None)["profile_id"], profile["profile_id"])
        profiles = storage.list_model_profiles(self.app)
        self.assertTrue(next(item for item in profiles if item["profile_id"] == profile["profile_id"])["is_default"])
        deleted = client.delete(f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertNotEqual(storage.get_model_profile(self.app, None)["profile_id"], profile["profile_id"])

    def test_model_profile_can_be_disabled_and_default_is_reassigned(self):
        profile = storage.create_model_profile(self.app, {
            "display_name": "待禁用默认模型", "base_url": "https://example.test/v1", "model_name": "disable-test", "api_key": "test-key",
        })
        client = self.app.test_client()
        self._unlock_model_configuration(client)
        self.assertEqual(client.post(f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}/default").status_code, 200)

        response = client.patch(
            f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}",
            json={"enabled": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["profile"]["enabled"], 0)
        profiles = storage.list_model_profiles(self.app)
        disabled = next(item for item in profiles if item["profile_id"] == profile["profile_id"])
        self.assertEqual(disabled["enabled"], 0)
        self.assertFalse(disabled["is_default"])
        self.assertNotEqual(storage.get_model_profile(self.app, None)["profile_id"], profile["profile_id"])
        with self.assertRaisesRegex(ValueError, "未找到已启用的模型档案"):
            storage.get_model_profile(self.app, profile["profile_id"])

    def test_vision_configuration_is_opt_in_and_clears_when_default_model_becomes_unusable(self):
        self.assertEqual(storage.vision_configuration(self.app), {"enabled": False, "default_profile_id": None})
        profile = storage.create_model_profile(self.app, {
            "display_name": "图片模型", "base_url": "https://example.test/v1", "model_name": "vision-model",
            "api_key": "test-key", "supports_vision": True,
        })
        with self.assertRaisesRegex(ValueError, "先选择默认图片识别模型"):
            storage.update_vision_configuration(self.app, {"enabled": True, "default_profile_id": None})
        config = storage.update_vision_configuration(self.app, {
            "enabled": True, "default_profile_id": profile["profile_id"],
        })
        self.assertTrue(config["enabled"])
        self.assertEqual(storage.resolve_vision_model_profile(self.app, {"supports_vision": False})["profile_id"], profile["profile_id"])

        storage.update_model_profile(self.app, profile["profile_id"], {"supports_vision": False})
        self.assertEqual(storage.vision_configuration(self.app), {"enabled": False, "default_profile_id": None})

    def test_ocr_feature_configuration_is_independent_from_multimodal_switch(self):
        configuration = storage.ocr_feature_configuration(self.app)
        self.assertTrue(configuration["enabled"])
        self.assertTrue(configuration["fixed"])
        # 兼容旧 PATCH，但 false 不能关闭固定的本地 OCR 基线。
        self.assertTrue(storage.update_ocr_feature_configuration(self.app, {"enabled": False})["enabled"])
        self.assertFalse(storage.vision_configuration(self.app)["enabled"])
        self.assertTrue(storage.ocr_configuration(self.app)["ocr_enabled"])

    def test_ocr_feature_configuration_api_requires_model_configuration_access(self):
        client = self.app.test_client()
        self.assertTrue(client.get("/api/evaluation-workbench/ocr-feature-configuration").get_json()["configuration"]["enabled"])
        self.assertEqual(client.patch(
            "/api/evaluation-workbench/ocr-feature-configuration", json={"enabled": True},
        ).status_code, 403)
        self._unlock_model_configuration(client)
        response = client.patch(
            "/api/evaluation-workbench/ocr-feature-configuration", json={"enabled": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["configuration"]["enabled"])

    def test_rule_image_mode_round_trip_keeps_legacy_vision_fields(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "扫描声明函", "check_rule": "核验声明函文字",
            "ocr_required": True,
        })
        updated = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "image_mode": "ocr_only", "vision_trigger": "text_fallback", "vision_level": "standard",
        })
        self.assertEqual(updated["image_mode"], "ocr_only")
        self.assertEqual(updated["vision_trigger"], "text_fallback")
        self.assertEqual(updated["vision_level"], "standard")
        self.assertEqual(worker._rule_image_mode(updated), "ocr_only")

    def test_baseline_ocr_mode_round_trip_supports_manual_text_and_local_choices(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "证明材料", "check_rule": "核验证明材料",
            "evidence_requirements": ["text", "document", "field"],
        })
        text_only = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "baseline_ocr_mode": "text_only", "acquisition_preset": "off",
        })
        self.assertEqual(text_only["baseline_ocr_mode"], "text_only")
        self.assertFalse(worker._local_ocr_baseline_required(text_only, {"status": "manual"}))
        local_ocr = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "baseline_ocr_mode": "local_ocr", "acquisition_preset": "off",
        })
        self.assertEqual(local_ocr["baseline_ocr_mode"], "local_ocr")
        self.assertTrue(worker._local_ocr_baseline_required(local_ocr, {
            "status": "satisfied", "evidence_quality": "sufficient", "confidence": "high",
        }))

    def test_acquisition_preset_maps_to_legacy_execution_fields_without_breaking_them(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "扫描材料", "check_rule": "核验扫描材料文字",
        })
        updated = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "acquisition_preset": "text", "vision_level": "standard",
        })
        self.assertEqual(updated["acquisition_preset"], "text")
        self.assertEqual(updated["image_mode"], "ocr_only")
        self.assertEqual(updated["vision_trigger"], "text_fallback")
        self.assertEqual(updated["vision_level"], "standard")
        self.assertEqual(updated["acquisition_recommendation"]["acquisition_preset"], "off")
        self.assertEqual(updated["acquisition_recommendation"]["baseline_ocr_mode"], "auto")

    def test_always_acquisition_preset_keeps_auto_channel_but_requires_execution(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "图片材料核验", "check_rule": "核验证明材料本体和关键字段",
        })
        updated = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "acquisition_preset": "always", "vision_level": "standard",
        })
        self.assertEqual(updated["acquisition_preset"], "always")
        self.assertEqual(updated["image_mode"], "auto")
        self.assertEqual(updated["vision_trigger"], "required")
        self.assertEqual(updated["vision_level"], "standard")
        self.assertTrue({"document", "field", "text"}.issubset(set(updated["evidence_requirements"])))

    def test_acquisition_validation_reports_conflicting_budget_without_blocking_confirmation(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "待取证材料", "check_rule": "核验扫描材料", "ocr_required": True,
        })
        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        self.assertTrue(any(item["rule_id"] == rule["rule_id"] and item["code"] == "image_budget_off" for item in validation["issues"]))
        self.assertEqual(self.app.test_client().get(
            f"/api/evaluation-workbench/projects/{self.project['project_id']}/rules/acquisition-validation"
        ).status_code, 200)
        self.assertEqual(storage.confirm_rule_set(self.app, self.project["project_id"])["status"], "confirmed")

    def test_acquisition_validation_warns_about_duplicate_score_rules(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "商务部分-企业业绩评分（满分9分）",
            "check_rule": "按有效业绩数量计分", "scoring": {"max_score": 9, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "企业业绩评分（9分）",
            "check_rule": "按有效业绩数量计分", "scoring": {"max_score": 9, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "价格部分评分（30分）",
            "check_rule": "按基准价公式计分", "scoring": {"max_score": 30, "kind": "manual"},
        })
        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        duplicate = [item for item in validation["issues"] if item["code"] == "duplicate_score_rule"]
        self.assertEqual(len(duplicate), 1)
        self.assertIn("企业业绩", duplicate[0]["message"])
        self.assertEqual(len(duplicate[0]["rule_ids"]), 2)

    def test_confirm_rule_set_merges_duplicate_score_rules(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "企业业绩评分（9分）",
            "source_text": "近三年同类项目业绩，要求提供合同首页、金额页、签字盖章页、供货明细单，每提供一份得3分，最高得9分。",
            "scoring": {"max_score": 9, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "商务部分-企业业绩评分（满分9分）",
            "source_text": "近三年同类项目业绩，要求提供合同首页、金额页、签字盖章页、供货明细单，每提供一份得3分，最高得9分。本款仅指供应商自身业绩。",
            "scoring": {"max_score": 9, "kind": "manual"},
        })

        storage.confirm_rule_set(self.app, self.project["project_id"])

        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        enabled = [item for item in rules if item["enabled"]]
        self.assertEqual(len(enabled), 1)
        self.assertIn("仅指供应商自身业绩", enabled[0]["source_text"])

    def test_confirm_rule_set_keeps_distinct_score_rules(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "售后服务方案评分（6分）",
            "source_text": "售后体系/响应机制、售后内容/人员配备、回访/故障流程、备品备件，各1.5分。",
            "scoring": {"max_score": 6, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "实施进度计划评分（6分）",
            "source_text": "进度控制计划、时间进度安排、保障措施、应急预案，各1.5分。",
            "scoring": {"max_score": 6, "kind": "manual"},
        })

        storage.confirm_rule_set(self.app, self.project["project_id"])

        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        enabled = [item for item in rules if item["enabled"]]
        self.assertEqual(len(enabled), 2)

    def test_score_title_core_keeps_section_named_titles(self):
        self.assertEqual(storage._score_rule_title_core("价格部分评分"), "价格部分评分")
        self.assertEqual(storage._score_rule_title_core("技术部分评分"), "技术部分评分")
        self.assertEqual(storage._score_rule_title_core("商务部分-企业业绩评分（满分9分）"), "企业业绩评分")
        self.assertEqual(storage._score_rule_title_core("企业业绩评分（9分）"), "企业业绩评分")

    def test_merge_uses_source_clause_ids_and_containment(self):
        def add(title, source, scoring, clause_ids=None):
            rule = storage.add_rule(self.app, self.project["project_id"], {
                "category": "subjective", "title": title, "source_text": source,
                "scoring": scoring,
            })
            if clause_ids:
                with storage.connection(self.app) as conn:
                    meta = json.loads(rule.get("execution_meta_json") or "{}")
                    meta["source_clause_ids"] = clause_ids
                    conn.execute("UPDATE ew_rules SET execution_meta_json=? WHERE rule_id=?", (json.dumps(meta, ensure_ascii=False), rule["rule_id"]))
            return rule

        # 同一条款 ID（确定性锚）
        add("整体实施方案评分", "供应商根据项目需求编制配送、运输方案（1.5分）、安装调试（1.5分）。6分", {
            "max_score": 6, "kind": "manual",
            "items": [{"name": "配送方案", "max_score": 1.5, "criterion": "完整得1.5分"}, {"name": "安装调试", "max_score": 1.5, "criterion": "完整得1.5分"}],
        }, clause_ids=["SC-abc"])
        add("整体实施方案评审", "5.整体实施方案 供应商根据项目需求编制配送、运输方案（1.5分）、安装调试（1.5分）。6分", {
            "max_score": 6, "kind": "manual",
            "items": [{"name": "配送、运输供货方案", "max_score": 1.5, "criterion": "完整得1.5分"}, {"name": "安装调试（安装准备）", "max_score": 1.5, "criterion": "完整得1.5分"}],
        }, clause_ids=["SC-abc"])
        # 不同规则（同分值但原文/结构不同）
        add("售后服务方案评分", "售后体系、售后内容、回访流程、备品备件各1.5分。6分", {
            "max_score": 6, "kind": "manual",
            "items": [{"name": "售后体系", "max_score": 1.5, "criterion": "完整得1.5分"}, {"name": "备品备件", "max_score": 1.5, "criterion": "完整得1.5分"}],
        })

        merged = storage.merge_draft_score_rule_duplicates(
            self.app, storage.current_rule_set(self.app, self.project["project_id"])["rule_set_id"],
        )

        self.assertEqual(merged, 1)
        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        enabled = [item for item in rules if item["enabled"]]
        self.assertEqual(len(enabled), 2)
        enabled_titles = {item["title"] for item in enabled}
        self.assertTrue({"整体实施方案评分", "整体实施方案评审"} & enabled_titles)
        self.assertIn("售后服务方案评分", {item["title"] for item in enabled})

    def test_merge_uses_object_core_and_similar_structure(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "产品安全耐用性可升级性兼容性评分",
            "source_text": "投标人提供相关证明材料，能够高于招标文件要求的，得3分；不足以证明得1分；未提供不得分。",
            "scoring": {"max_score": 3, "kind": "manual", "items": [
                {"name": "产品安全耐用性、可升级性、兼容性、易用易维护性", "max_score": 3, "criterion": "高于招标要求得3分"},
            ]},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "产品安全耐用性、可升级性、兼容性、易用易维护性评分",
            "source_text": "2.产品安全耐用性 评标委员会根据产品安全耐用性评定，投标人提供相关证明材料，能够高于招标文件要求的，得3分；不足以证明得1分；未提供不得分。",
            "scoring": {"max_score": 3, "kind": "manual", "items": [
                {"name": "高于招标文件要求", "max_score": 3, "criterion": "高于招标要求得3分"},
                {"name": "不足以证明高于要求", "max_score": 1, "criterion": "不足以证明得1分"},
                {"name": "未提供", "max_score": 0, "criterion": "未提供不得分"},
            ]},
        })

        merged = storage.merge_draft_score_rule_duplicates(
            self.app, storage.current_rule_set(self.app, self.project["project_id"])["rule_set_id"],
        )

        self.assertEqual(merged, 1)
        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        enabled = [item for item in rules if item["enabled"]]
        self.assertEqual(len(enabled), 1)

    def test_score_parent_mismatch_detects_missing_score_rules(self):
        tender_text = (
            "[第1页]\n第一部分价格部分（30分）\n"
            "[第2页]\n第二部分商务部分（11分）\n"
            "[第3页]\n第三部分服务部分（29分）\n"
            "[第4页]\n第四部分技术部分（30分）\n"
        )
        rules = [
            {"enabled": True, "category": "objective", "source_page": 1, "title": "价格", "scoring_json": json.dumps({"max_score": 30})},
            {"enabled": True, "category": "subjective", "source_page": 3, "title": "售后", "scoring_json": json.dumps({"max_score": 6})},
            {"enabled": True, "category": "subjective", "source_page": 3, "title": "培训", "scoring_json": json.dumps({"max_score": 5})},
        ]
        issues = storage._score_parent_mismatches(tender_text, rules)
        by_parent = {item["title"]: item for item in issues}
        self.assertIn("第三部分服务部分（29分）", by_parent)
        self.assertIn("差额 +18", by_parent["第三部分服务部分（29分）"]["message"])

    def test_extract_score_from_conclusion_cases(self):
        self.assertEqual(worker._extract_score_from_conclusion("两份业绩要件齐全，建议各计3分共6分，签章需图片复核。", 9), 6)
        self.assertEqual(worker._extract_score_from_conclusion("暂计9分封顶，先按两项完整计6分。", 9), 6)
        self.assertEqual(worker._extract_score_from_conclusion("方案完整无缺陷得6分；存在缺陷每处扣0.5分。", 6), 6)
        self.assertEqual(worker._extract_score_from_conclusion("未提供任何证书，建议0分。", 2), 0)
        self.assertIsNone(worker._extract_score_from_conclusion("报告待核，暂不给分。", 9))
        self.assertIsNone(worker._extract_score_from_conclusion("无法确定是否满足，需人工复核。", 9))
        self.assertIsNone(worker._extract_score_from_conclusion("满分9分，评审时按证明认定。", 9))

    def test_rule_execution_strategy_keeps_cross_bid(self):
        self.assertEqual(worker._rule_execution_strategy({"execution_strategy": "cross_bid"}), "cross_bid")
        self.assertEqual(worker._rule_execution_strategy({"execution_strategy": "visual"}), "point")

    def test_confirmed_rule_set_allows_enabled_toggle_only(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "业绩评分（9分）",
            "source_text": "近三年同类项目业绩，每提供一份得3分，最高得9分。",
            "scoring": {"max_score": 9, "kind": "manual"},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        rule_id = rules[0]["rule_id"]
        before = storage.current_rule_set(self.app, self.project["project_id"])["updated_at"]
        time.sleep(1)

        storage.update_rule(self.app, self.project["project_id"], rule_id, {"enabled": False})

        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        self.assertFalse(rules[0]["enabled"])
        after = storage.current_rule_set(self.app, self.project["project_id"])["updated_at"]
        self.assertNotEqual(before, after)
        with self.assertRaises(ValueError):
            storage.update_rule(self.app, self.project["project_id"], rule_id, {"check_rule": "修改规则内容"})

    def test_cross_bid_review_result_is_normalised(self):
        rule = {"rule_id": "r1", "execution_strategy": "cross_bid"}
        normal = worker._normalise_cross_bid_review_result("review", rule, {
            "rule_id": "r1", "status": "partial",
            "reason": "串通投标需跨投标人交叉比对账户、编制单位、联系人、报价规律等→单标段扫描范围不足以独立判断，需人工跨标段核验",
            "conclusion_summary": "单包不足以判定，需结合其他投标人材料统一核验。",
        })
        self.assertIn("本卷书面承诺已核验", normal["conclusion_summary"])
        self.assertNotIn("单标段扫描范围", normal["reason"])
        self.assertIn("本卷书面承诺已核验", normal["reason"])

        kept = worker._normalise_cross_bid_review_result("review", rule, {
            "rule_id": "r1", "status": "not_satisfied",
            "reason": "未提供书面承诺，串通情形无法核验", "conclusion_summary": "未提供承诺书",
        })
        self.assertEqual(kept["conclusion_summary"], "未提供承诺书")

        missing = worker._normalise_cross_bid_review_result("review", rule, {
            "rule_id": "r1", "status": "partial",
            "reason": "未提供承诺书，需人工核验", "conclusion_summary": "承诺缺失",
        })
        self.assertIn("未提供承诺书", missing["reason"])

        non_review = worker._normalise_cross_bid_review_result("objective", rule, {
            "rule_id": "r1", "status": "partial", "reason": "原文保留", "conclusion_summary": "摘要保留",
        })
        self.assertEqual(non_review["reason"], "原文保留")

    def test_replace_rules_keeps_manual_rules_but_replaces_ai_rules(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "compliance", "title": "投标函出具与实质性承诺",
            "check_rule": "核验投标函是否出具并包含实质性承诺。",
            "source_text": "投标函……",
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "企业业绩评分（9分）",
            "source_text": "近三年同类项目业绩，每提供一份得3分，最高得9分。",
            "scoring": {"max_score": 9, "kind": "manual"},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        with storage.connection(self.app) as conn:
            conn.execute("UPDATE ew_rules SET source_type='ai' WHERE title='企业业绩评分（9分）'")

        storage.replace_rules_from_extraction(
            self.app, self.project["project_id"], "task-inherit",
            [{"category": "compliance", "title": "投标报价不得超过最高限价", "check_rule": "核验报价未超限价。",
              "source_text": "最高限价……"}],
        )

        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        titles = {item["title"]: item for item in rules}
        # 人工/编辑规则保留；纯 AI 规则由新提取整批替换（提取管线内部保证完整性）。
        self.assertIn("投标函出具与实质性承诺", titles)
        self.assertNotIn("企业业绩评分（9分）", titles)
        self.assertIn("投标报价不得超过最高限价", titles)
        self.assertTrue(titles["投标函出具与实质性承诺"]["enabled"])

    def test_replace_rules_does_not_inherit_disabled_rules(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "rejection", "title": "串通投标情形作无效",
            "check_rule": "核验串通情形。", "source_text": "串通投标……",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        rule_id = storage.list_rules(self.app, self.project["project_id"])[1][0]["rule_id"]
        storage.update_rule(self.app, self.project["project_id"], rule_id, {"enabled": False})
        with storage.connection(self.app) as conn:
            conn.execute("UPDATE ew_rules SET source_type='ai' WHERE rule_id=?", (rule_id,))

        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-inherit", [])

        rules = storage.list_rules(self.app, self.project["project_id"])[1]
        self.assertNotIn("串通投标情形作无效", {item["title"] for item in rules})

    def test_score_leaf_total_below_warns_and_counting_exempt(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "实施进度计划评分（6分）",
            "source_text": "进度控制计划1.5、时间进度安排1.5、保障措施1.5、应急预案1.5。",
            "scoring": {"max_score": 6, "kind": "manual", "items": [
                {"name": "保障措施", "max_score": 1.5, "criterion": "完整得1.5分"},
                {"name": "应急预案", "max_score": 1.5, "criterion": "完整得1.5分"},
                {"name": "应急保障措施", "max_score": 1.5, "criterion": "完整得1.5分"},
            ]},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "企业业绩评分（9分）",
            "source_text": "近三年同类项目业绩，每提供一份得3分，最高得9分。",
            "scoring": {"max_score": 9, "kind": "manual", "items": [
                # 叶子只写单份分值（3 分）而满分 9 分：只有计数型豁免生效时才不误报。
                {"name": "企业业绩", "max_score": 3, "criterion": "每提供一份合格合同得3分，最高得9分"},
            ]},
        })

        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        below = [item for item in validation["issues"] if item["code"] == "score_leaf_total_below"]
        self.assertEqual(len(below), 1)
        self.assertIn("实施进度计划", below[0]["title"])

    def test_hard_tender_anchor_scan_only_for_first_extraction(self):
        self._add_pdf("tender.pdf", "tender", "", "用于建立解析文件")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "other", "title": "技术方案内部一致性核验",
            "check_rule": "核验技术方案内部一致性。",
        })
        tender = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        Path(tender["parsed_path"]).write_text(
            "投标有效期不少于90日历天；不接受联合体投标；串通投标的作无效处理；信用中国查询。\n" * 2,
            encoding="utf-8",
        )

        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        missing = [item for item in validation["issues"] if item["code"].startswith("tender_requirement_")]
        self.assertTrue(missing)

        # 已有历史确认规则集后不再跑关键词扫描（继承机制兜底）。
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "compliance", "title": "投标有效期", "check_rule": "核验投标有效期不少于90日历天。",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-again", [])
        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        missing = [item for item in validation["issues"] if item["code"].startswith("tender_requirement_")]
        self.assertEqual(missing, [])

    def test_score_clause_packet_includes_item_heading_and_continuation(self):
        text = (
            "[第34页]\n"
            "1.组织实施保障 6 分 供应商提供本项目保障计划、重点难点分析和解决方案（1.5分）。\n"
            "2.售后服务方案 6 分 供应商提供完善的售后服务体系（1.5分）。\n"
            "3.培训方案 5 分 提供培训目标（1分）。\n"
            "4.实施进度计划 6 分 供应商提供完善的进度控制计划（1.5分）、时间进度安排（时间进度表）\n"
            "[第35页]\n"
            "32\n"
            "（1.5分）、保障措施（1.5分）、应急预案（1.5分）。上述方案无缺陷得6分。\n"
        )
        packets = worker._score_clause_packets(text)
        progress = next(
            (item for item in packets if "保障措施" in str(item.get("text") or "")),
            None,
        )
        self.assertIsNotNone(progress)
        packet_text = str(progress.get("text") or "")
        self.assertIn("实施进度计划", packet_text)
        self.assertIn("进度控制计划", packet_text)
        self.assertIn("时间进度安排", packet_text)
        self.assertIn("保障措施", packet_text)
        self.assertIn("应急预案", packet_text)
        self.assertIn("6 分", packet_text)

    def test_score_clause_packet_uses_stable_page_based_id(self):
        text = (
            "[第33页]\n"
            "第一部分价格部分（30分）满足招标文件要求且投标价格最低的投标报价为评标基准价。30分 客观\n"
            "第二部分商务部分（11分）\n"
            "[第34页]\n"
            "1.企业业绩 每提供一份得3分，最高得9分。9分 客观\n"
            "2.认证证书 每有一项得1分。2分 客观\n"
        )
        packets = worker._score_clause_packets(text)
        ids = [item.get("clause_id") for item in packets]
        self.assertEqual(ids, ["SC-33-1", "SC-33-2", "SC-34-1", "SC-34-2"])

    def test_hard_tender_anchor_gaps_detects_uncovered(self):
        text = "投标有效期不少于90日历天；不接受联合体投标；串通投标的作无效处理。"
        gaps = worker._hard_tender_anchor_gaps([], text)
        labels = {item["label"] for item in gaps}
        self.assertIn("投标有效期", labels)
        self.assertIn("联合体", labels)
        self.assertIn("串通投标", labels)
        covered = [
            {"category": "compliance", "title": "投标有效期", "check_rule": "核验投标有效期不少于90日历天。", "source_text": "投标有效期……"},
            {"category": "rejection", "title": "串通投标情形作无效", "check_rule": "核验串通情形。", "source_text": "串通投标……"},
        ]
        gaps = worker._hard_tender_anchor_gaps(covered, text)
        labels = {item["label"] for item in gaps}
        self.assertNotIn("投标有效期", labels)
        self.assertNotIn("串通投标", labels)
        self.assertIn("联合体", labels)

    def test_acquisition_validation_warns_when_score_total_differs_from_tender_declared(self):
        self._add_pdf("tender.pdf", "tender", "", "用于建立解析文件")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        tender = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        Path(tender["parsed_path"]).write_text(
            "评审办法\n3.2.1 分值构成（总分100分）：商务部分：10 分；技术部分：55 分；报价：35 分。\n" * 2,
            encoding="utf-8",
        )
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "商务业绩评分15分",
            "check_rule": "按有效业绩数量计分", "scoring": {"max_score": 15, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "技术实施方案评分30分",
            "check_rule": "按方案分档计分", "scoring": {"max_score": 30, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "报价得分计算35分",
            "check_rule": "按基准价公式计分", "scoring": {"max_score": 35, "kind": "manual"},
        })

        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        mismatch = [item for item in validation["issues"] if item["code"] == "score_total_mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertIn("100", mismatch[0]["message"])
        self.assertIn("80", mismatch[0]["message"])
        # 确认不被该预检阻断
        self.assertEqual(storage.confirm_rule_set(self.app, self.project["project_id"])["status"], "confirmed")

    def test_acquisition_validation_silent_when_score_total_matches_tender_declared(self):
        self._add_pdf("tender.pdf", "tender", "", "用于建立解析文件")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        tender = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "tender")
        Path(tender["parsed_path"]).write_text("分值构成（总分100分）详见评分标准。", encoding="utf-8")
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "报价得分计算35分",
            "check_rule": "按基准价公式计分", "scoring": {"max_score": 35, "kind": "manual"},
        })
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "技术实施方案评分65分",
            "check_rule": "按方案分档计分", "scoring": {"max_score": 65, "kind": "manual"},
        })

        validation = storage.rule_set_acquisition_validation(self.app, self.project["project_id"])
        self.assertFalse(any(item["code"] == "score_total_mismatch" for item in validation["issues"]))

    def test_score_rule_dedupe_merges_truncated_duplicate_clause(self):
        full = {"category": "objective", "title": "商务部分-企业业绩评分（满分9分）",
                "check_rule": "按有效业绩数量计分",
                "scoring": {"max_score": 9},
                "source_text": "投标供应商近三年完成的同类项目案例，每提供一份得3分，最高得9分。本款仅指供应商自身业绩。"}
        truncated = {"category": "objective", "title": "企业业绩评分（9分）",
                     "check_rule": "按有效业绩合同要件计分",
                     "scoring": {"max_score": 9},
                     "source_text": "投标供应商近三年完成的同类项目案例，每提供一份得3分，最高得9分。"}
        merged = worker._dedupe_rule_candidates([full, truncated])
        self.assertEqual(len(merged), 1)
        # 同名同分值但原文真正不同的规则不合并，留给确认前预检提示人工核对。
        detailed = {"category": "subjective", "title": "售后服务方案评分（6分）",
                    "check_rule": "核验售后服务体系", "scoring": {"max_score": 6},
                    "source_text": "供应商提供完善的售后服务体系、售后服务响应机制（1.5分）。"}
        compressed = {"category": "subjective", "title": "售后服务方案评分（6分，含保障措施）",
                      "check_rule": "核验售后保障与应急预案", "scoring": {"max_score": 6},
                      "source_text": "保障措施（1.5分）、应急预案（1.5分）。上述方案无缺陷得6分。"}
        self.assertEqual(len(worker._dedupe_rule_candidates([detailed, compressed])), 2)

    def test_ocr_cached_pages_backfill_unique_total_quote(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "只有文字层目录，没有报价。")
        value, _ = worker._local_total_quote_with_ocr(self.app, document)
        self.assertIsNone(value)
        storage.save_ocr_page_cache(self.app, document["document_id"], 50, "hash-50", "local_rapidocr", {
            "text": "开标一览表" + chr(10) + "总报价（小写）：628120.00元" + chr(10) + "总报价（大写）：陆拾贰万捌仟壹佰贰拾元整",
        })
        value, excerpt = worker._local_total_quote_with_ocr(self.app, document)
        self.assertEqual(str(value), "628120.00")
        self.assertIn("OCR", excerpt)
        # OCR 页文字出现两个不同报价时同样宁可留空。
        storage.save_ocr_page_cache(self.app, document["document_id"], 51, "hash-51", "local_rapidocr", {
            "text": "另一页" + chr(10) + "总报价：599000.00元",
        })
        value, _ = worker._local_total_quote_with_ocr(self.app, document)
        self.assertIsNone(value)

    def test_clean_model_text_strips_ocr_replacement_chars(self):
        noisy = "第56页载明：单位负责�为同���存在直接控股关系"
        cleaned = worker._clean_model_text(noisy)
        self.assertNotIn("�", cleaned)
        self.assertIn("…", cleaned)
        self.assertEqual(worker._clean_model_text("正常文字"), "正常文字")

    def test_clean_model_text_strips_internal_ids_and_field_notation(self):
        text = ("计分过程：SI-1供货方案（第P55页）：建议有效；status=not_found、risk=high、"
                "evidence_quality=missing；suggested_score=0。结论scope=partial；validity=partial；met=false。")
        cleaned = worker._clean_model_text(text)
        self.assertNotIn("SI-1", cleaned)
        self.assertNotIn("status=", cleaned)
        self.assertNotIn("risk=", cleaned)
        self.assertNotIn("suggested_score=", cleaned)
        self.assertNotIn("scope=", cleaned)
        self.assertNotIn("validity=", cleaned)
        self.assertNotIn("met=", cleaned)
        self.assertIn("供货方案", cleaned)
        self.assertIn("第P55页", cleaned)

    def test_score_evidence_validity_labels_use_chinese_and_unknown_fallback(self):
        def build(validity):
            return worker._score_evidence_text({
                "evidence_items": [{"name": "项目一", "page_hint": "1", "validity": validity, "reason": "同类型"}],
            })
        self.assertIn("有效；同类型", build("valid"))
        self.assertIn("需人工核验；同类型", build("uncertain"))
        self.assertIn("无效；同类型", build("invalid"))
        self.assertIn("部分有效；同类型", build("partial"))
        self.assertIn("需人工核验；同类型", build("whatever"))
        self.assertNotIn("；同类型", build(""))
        self.assertNotIn("partial", build("partial"))

    def test_truncate_field_adds_omission_marker(self):
        self.assertEqual(worker._truncate_field("短文本", 2000), "短文本")
        long_text = "长" * 2005
        truncated = worker._truncate_field(long_text, 2000)
        self.assertLessEqual(len(truncated), 2000)
        self.assertIn("内容过长已省略", truncated)

    def test_score_reason_text_keeps_calculation_out_of_reason(self):
        result = worker._score_result_from_model(
            "rule-1", 3.0, 9.0,
            {"suggested_score": 3, "confidence": "high",
             "calculation": "1项×3分=3分", "reason": "建议得3分"},
        )
        self.assertEqual(result["reason"], "建议得3分")
        layers = [layer for layer in result.get("evidence_layers", []) if layer.get("source") == "score_calculation"]
        self.assertEqual(len(layers), 1)
        self.assertIn("1项×3分=3分", layers[0]["summary"])
        self.assertNotIn("计分过程", result["reason"])

    def test_score_evidence_text_normalizes_page_format(self):
        def build(page_hint):
            return worker._score_evidence_text({
                "matched_count": 1,
                "evidence_items": [{"name": "项目一", "page_hint": page_hint, "validity": "valid", "reason": "同类型"}],
            })
        self.assertIn("第55页", build("P55"))
        self.assertNotIn("第P55页", build("P55"))
        # 散页必须用“、”连接，不能被误写成连续区间
        scatter = build("P55、P57")
        self.assertIn("第55、57页", scatter)
        self.assertNotIn("第55-57页", scatter)
        # 原文带范围分隔符时才保留区间语义
        span = build("第P55-P58页")
        self.assertIn("第55-58页", span)

    def test_evaluation_highlights_caps_six_per_bidder_and_shortens_headline(self):
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {("bid-a", f"r{i}"): {"_critical_eligible": False} for i in range(1, 9)}
        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a",
            "headline": "这是一个非常长的总览句子，需要验证是否会被压缩到四十个字以内并且加上省略号表示内容未完整展示。",
            "highlights": [
                {"rule_id": f"r{i}", "level": "high", "keyword": f"事项{i}", "conclusion": f"结论{i}", "basis": "依据"} for i in range(1, 9)
            ],
        }]}, candidates, allowed)
        self.assertEqual(len(values), 1)
        self.assertLessEqual(len(values[0]["highlights"]), 6)
        self.assertGreater(len(values[0]["highlights"]), 3)
        self.assertLessEqual(len(values[0]["headline"]), 41)

    def test_highlight_display_candidate_translates_status_labels(self):
        display = worker._highlight_display_candidate({
            "type": "review", "rule_id": "rule-9", "category": "compliance", "title": "承诺函",
            "status": "not_found", "risk_level": "high", "confidence": "high",
            "evidence_quality": "missing", "evidence": "全文未发现", "reason": "需人工核验",
        })
        self.assertEqual(display["rule_id"], "rule-9")
        self.assertEqual(display["status_label"], "未找到证据")
        self.assertEqual(display["risk_label"], "高")
        self.assertEqual(display["evidence_quality_label"], "缺失")
        self.assertNotIn("status", display)
        self.assertNotIn("risk_level", display)

    def test_evaluation_can_queue_ocr_only_rule_without_multimodal_profile(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "扫描声明函")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "声明函", "check_rule": "核验扫描声明函文字",
            "ocr_required": True,
        })
        storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "image_mode": "ocr_only", "vision_trigger": "required", "vision_level": "standard",
        })
        storage.update_ocr_feature_configuration(self.app, {"enabled": True})
        storage.confirm_rule_set(self.app, self.project["project_id"])

        with patch("dashboard.blueprints.evaluation_workbench._start_worker_if_needed"):
            response = self.app.test_client().post(
                f"/api/evaluation-workbench/projects/{self.project['project_id']}/tasks",
                json={"task_type": "evaluate_all"},
            )

        self.assertEqual(response.status_code, 202)
        with patch("dashboard.evaluation_workbench.worker._evaluation_ocr_enabled", return_value=True), patch("dashboard.evaluation_workbench.worker._ocr_page_texts", return_value=(
            [{"page": 1, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "声明函内容"}], "",
        )) as ocr_pages, patch("dashboard.evaluation_workbench.worker._run_visual_supplement") as visual, patch(
            "dashboard.evaluation_workbench.worker.request_json",
            return_value={"results": [{"rule_id": rule["rule_id"], "status": "ocr_required"}]},
        ):
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        ocr_pages.assert_called()
        visual.assert_not_called()

    def test_tencent_ocr_configuration_uses_hidden_credentials_and_conservative_monthly_limit(self):
        with patch.dict(os.environ, {"TENCENTCLOUD_SECRET_ID": "", "TENCENTCLOUD_SECRET_KEY": ""}, clear=False):
            configured = storage.update_ocr_configuration(self.app, {
                "enabled": True, "secret_id": "AKID-example", "secret_key": "secret-example", "region": "ap-guangzhou",
                "services": {"basic": {"enabled": True, "monthly_limit": 1}},
            })
        self.assertTrue(configured["enabled"])
        self.assertTrue(configured["credentials_configured"])
        self.assertEqual(configured["credentials_source"], "manual")
        self.assertNotIn("secret-example", json.dumps(configured, ensure_ascii=False))
        task = storage.create_task(self.app, self.project["project_id"], "parse_documents")
        usage = storage.reserve_ocr_request(self.app, task, "basic")
        self.assertTrue(usage)
        self.assertIsNone(storage.reserve_ocr_request(self.app, task, "basic"))
        storage.complete_ocr_request(self.app, usage, status="error", detail="模拟失败仍保守计入额度")
        services = {item["service"]: item for item in configured["services"]}
        self.assertIn("efficient", services)
        self.assertFalse(services["fast"]["enabled"])
        self.assertFalse(services["efficient"]["enabled"])

    def test_disabled_tencent_ocr_keeps_credentials_available_for_configuration_test_only(self):
        storage.update_ocr_configuration(self.app, {
            "tencent_enabled": True, "secret_id": "AKID-example", "secret_key": "secret-example",
        })
        storage.update_ocr_configuration(self.app, {"tencent_enabled": False})

        self.assertIsNone(storage.tencent_ocr_credentials(self.app))
        self.assertEqual(storage.tencent_ocr_credentials(self.app, require_enabled=False)[2], "ap-guangzhou")

    def test_tencent_ocr_sdk_top_level_response_is_parsed(self):
        class FakeResponse:
            @staticmethod
            def to_json_string():
                return json.dumps({
                    "TextDetections": [
                        {"DetectedText": "证书编号 A123", "Confidence": 98.5},
                        {"DetectedText": "有效期 2028-12-31", "Confidence": 97},
                    ],
                    "RequestId": "request-1",
                }, ensure_ascii=False)

        result = ocr_gateway._result_from_response("accurate", FakeResponse())

        self.assertEqual(result["line_count"], 2)
        self.assertIn("证书编号 A123", result["text"])
        self.assertEqual(result["request_id"], "request-1")
        self.assertEqual(result["parser_version"], ocr_gateway.OCR_PARSER_VERSION)

    def test_tencent_ocr_api_explorer_nested_response_remains_compatible(self):
        class FakeResponse:
            @staticmethod
            def to_json_string():
                return json.dumps({"Response": {
                    "TextDetections": [{"DetectedText": "投标人名称", "Confidence": 96}],
                    "RequestId": "request-2",
                }}, ensure_ascii=False)

        result = ocr_gateway._result_from_response("basic", FakeResponse())

        self.assertEqual(result["text"], "投标人名称")
        self.assertEqual(result["request_id"], "request-2")

    def test_tencent_ocr_structured_services_keep_field_and_table_semantics(self):
        class BizResponse:
            @staticmethod
            def to_json_string():
                return json.dumps({
                    "Name": "甲公司", "RegNum": "91110000A123", "Person": "张三",
                    "Address": "北京市", "RequestId": "biz-1",
                }, ensure_ascii=False)

        class TableResponse:
            @staticmethod
            def to_json_string():
                return json.dumps({"TableDetections": [{"Cells": [
                    {"RowTl": 1, "ColTl": 0, "Text": "ISO证书"},
                    {"RowTl": 0, "ColTl": 1, "Text": "证书名称"},
                    {"RowTl": 0, "ColTl": 0, "Text": "序号"},
                ]}], "RequestId": "table-1"}, ensure_ascii=False)

        business = ocr_gateway._result_from_response("biz_license", BizResponse())
        table = ocr_gateway._result_from_response("table", TableResponse())

        self.assertIn("企业名称：甲公司", business["text"])
        self.assertIn("统一社会信用代码：91110000A123", business["text"])
        self.assertIn("法定代表人：张三", business["text"])
        self.assertIn("表1·第1行第1列：序号", table["text"])
        self.assertIn("表1·第1行第2列：证书名称", table["text"])
        self.assertIn("表1·第2行第1列：ISO证书", table["text"])

    def test_tencent_ocr_page_cache_is_page_and_service_scoped(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        storage.save_ocr_page_cache(self.app, document["document_id"], 1, "page-hash", "accurate", {"text": "证书编号A", "confidence": 98})
        cached = storage.get_ocr_page_cache(self.app, document["document_id"], 1, "page-hash", "accurate")
        self.assertEqual(cached["text"], "证书编号A")
        self.assertIsNone(storage.get_ocr_page_cache(self.app, document["document_id"], 1, "page-hash", "basic"))

    def test_local_ocr_is_always_available_as_direct_or_fallback_path(self):
        initial = storage.ocr_configuration(self.app)
        self.assertTrue(initial["local"]["enabled"])
        self.assertFalse(initial["local"]["readiness"]["ready_for_manual_validation"])
        # 兼容旧请求中的 local.enabled=false，但不能让关闭腾讯云后完全失去 OCR。
        updated = storage.update_ocr_configuration(self.app, {"tencent_enabled": False, "local": {"enabled": False}})
        self.assertFalse(updated["tencent_enabled"])
        self.assertTrue(updated["local"]["enabled"])
        self.assertEqual(updated["local"]["mode"], "fallback_or_primary")

    def test_local_ocr_is_used_when_tencent_ocr_is_not_enabled(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        rule = {"title": "证书编号", "check_rule": "核验证书编号", "vision_level": "standard"}
        with patch("dashboard.evaluation_workbench.worker._local_ocr_page_texts", return_value=([
            {"page": 1, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "证书编号 A123"},
        ], "")) as local_pages:
            values, failure = worker._ocr_page_texts(self.app, {"task_id": "task"}, document, rule, {}, "standard", pages=[1])

        self.assertEqual(failure, "")
        self.assertEqual(values[0]["service"], local_ocr_gateway.LOCAL_OCR_SERVICE)
        self.assertEqual(local_pages.call_args.kwargs["rule"], rule)
        self.assertEqual(local_pages.call_args.kwargs["level"], "standard")

    def test_ocr_runtime_uses_local_path_when_tencent_is_disabled(self):
        self.assertTrue(worker._ocr_runtime_enabled({
            "tencent_enabled": False, "local": {"enabled": True, "runtime_available": True},
        }))
        self.assertTrue(worker._ocr_runtime_enabled({
            "tencent_enabled": True, "credentials_configured": True,
            "local": {"enabled": True, "runtime_available": False},
        }))
        self.assertFalse(worker._ocr_runtime_enabled({
            "tencent_enabled": True, "credentials_configured": False,
            "local": {"enabled": True, "runtime_available": False},
        }))
        self.assertFalse(worker._ocr_runtime_enabled({
            "tencent_enabled": False, "local": {"enabled": True, "runtime_available": False},
        }))

    def test_evaluation_local_ocr_does_not_require_a_vision_model_profile(self):
        configuration = {"tencent_enabled": False, "local": {"enabled": True, "runtime_available": True}}

        self.assertTrue(worker._evaluation_ocr_enabled(True, configuration))
        self.assertTrue(worker._evaluation_ocr_enabled(False, configuration))

    def test_legacy_ocr_rule_uses_hybrid_chain_instead_of_skipping_local_ocr(self):
        legacy_rule = {
            "title": "证书扫描件", "check_rule": "核验证书扫描件文字内容",
            "check_mode": "ocr", "ocr_required": True, "evidence_requirements": ["visual"],
        }

        self.assertEqual(worker._rule_image_strategy(legacy_rule), "hybrid")

    def test_material_rule_keeps_local_baseline_when_enhancement_is_off(self):
        rule = {
            "title": "认证证书", "check_rule": "核验证书编号和有效期",
            "image_mode": "off", "vision_trigger": "off", "vision_level": "off",
            "evidence_requirements": ["document", "field"],
        }
        self.assertTrue(worker._local_ocr_baseline_required(rule, {"status": "manual"}))
        # 普通文字结论已充分时，材料/字段元数据本身不能导致重复 OCR。
        self.assertFalse(worker._local_ocr_baseline_required(rule, {
            "status": "satisfied", "evidence_quality": "sufficient", "confidence": "high",
        }))

    def test_tencent_remains_fallback_if_local_runtime_returns_no_text(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        storage.update_ocr_configuration(self.app, {
            "enabled": True, "secret_id": "AKID-example", "secret_key": "secret-example", "region": "ap-guangzhou",
            "services": {"accurate": {"enabled": True, "monthly_limit": 10}},
        })
        rule = {"title": "证书编号", "check_rule": "核验证书编号", "vision_level": "standard"}
        response = {"service": "accurate", "text": "证书编号 A123", "parser_version": ocr_gateway.OCR_PARSER_VERSION}
        with patch("dashboard.evaluation_workbench.worker._local_ocr_page_texts", return_value=([], "本地 RapidOCR 运行环境不可用")), \
             patch("dashboard.evaluation_workbench.worker.request_tencent_ocr", return_value=(response, "")):
            values, _ = worker._ocr_page_texts(
                self.app, {"task_id": "task", "project_id": self.project["project_id"]}, document, rule, {}, "standard", pages=[1],
                allow_tencent=False,
            )
        self.assertEqual(values[0]["service"], "accurate")

    def test_tencent_ocr_is_a_second_pass_after_local_ocr(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        storage.update_ocr_configuration(self.app, {
            "enabled": True, "secret_id": "AKID-example", "secret_key": "secret-example", "region": "ap-guangzhou",
            "local": {"enabled": True}, "services": {"accurate": {"enabled": True, "monthly_limit": 10}},
        })
        rule = {"title": "证书编号", "check_rule": "核验证书编号", "vision_level": "standard"}
        response = {"service": "accurate", "text": "证书编号 A123", "parser_version": ocr_gateway.OCR_PARSER_VERSION}
        # 本地文本缺少规则所涉“编号”字段信号时按字段缺口升级腾讯复核。
        local_response = [{"page": 1, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "本地扫描件模糊", "confidence": 0.95}]
        with patch("dashboard.evaluation_workbench.worker.request_tencent_ocr", return_value=(response, "")), \
             patch("dashboard.evaluation_workbench.worker._local_ocr_page_texts", return_value=(local_response, "")) as local_pages:
            values, failure = worker._ocr_page_texts(self.app, {"task_id": "task", "project_id": self.project["project_id"]}, document, rule, {}, "standard", pages=[1])

        self.assertEqual(failure, "")
        self.assertEqual(values[0]["text"], "证书编号 A123")
        local_pages.assert_called_once()

    def test_local_ocr_page_cache_avoids_starting_a_subprocess_again(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        rendered = worker._render_ocr_page(self.app, document, 1, "fast")
        self.assertIsNotNone(rendered)
        _, image_hash = rendered
        storage.save_ocr_page_cache(self.app, document["document_id"], 1, image_hash, local_ocr_gateway.LOCAL_OCR_SERVICE, {
            "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "本地缓存文字", "parser_version": local_ocr_gateway.LOCAL_OCR_PARSER_VERSION,
        })
        with patch("dashboard.evaluation_workbench.worker.request_local_ocr") as local_process:
            values, failure = worker._local_ocr_page_texts(self.app, document, [1])

        self.assertEqual(failure, "")
        self.assertEqual(values[0]["text"], "本地缓存文字")
        local_process.assert_not_called()

    def test_local_ocr_gateway_converts_subprocess_result_without_importing_rapidocr(self):
        with tempfile.TemporaryDirectory(prefix="local_ocr_test_") as folder:
            image_path = Path(folder) / "page.jpg"
            image_path.write_bytes(b"jpeg")
            completed = type("Completed", (), {
                "returncode": 0,
                "stdout": "RapidOCR initializing\n" + json.dumps({
                    "ok": True,
                    "pages": [{"page": 3, "text": "本地识别文字", "line_count": 1, "confidence": 96.5}],
                    "metrics": {"elapsed_ms": 1234, "peak_rss_kb": 321000,
                                "model": "PP-OCRv5-mobile-onnx", "limit_side_len": 960},
                }, ensure_ascii=False),
                "stderr": "",
            })()
            metrics = {}
            with patch.dict(os.environ, {"RAPIDOCR_WORKER_MODE": "oneshot"}), \
                 patch("dashboard.evaluation_workbench.local_ocr_gateway.subprocess.run", return_value=completed) as run:
                values, error = local_ocr_gateway.request_local_ocr(
                    [{"page": 3, "path": str(image_path)}], metrics=metrics,
                )

        self.assertIsNone(error)
        self.assertEqual(values[0]["service"], local_ocr_gateway.LOCAL_OCR_SERVICE)
        self.assertEqual(values[0]["parser_version"], local_ocr_gateway.LOCAL_OCR_PARSER_VERSION)
        self.assertEqual(values[0]["state"], "recognized")
        self.assertEqual(metrics["recognized_pages"], 1)
        self.assertEqual(metrics["peak_rss_kb"], 321000)
        self.assertEqual(metrics["limit_side_len"], 960)
        run.assert_called_once()

    def test_local_ocr_gateway_retains_empty_and_failed_page_states(self):
        with tempfile.TemporaryDirectory(prefix="local_ocr_test_") as folder:
            first = Path(folder) / "first.jpg"
            second = Path(folder) / "second.jpg"
            first.write_bytes(b"jpeg")
            second.write_bytes(b"jpeg")
            completed = type("Completed", (), {
                "returncode": 0,
                "stdout": json.dumps({"ok": True, "pages": [
                    {"page": 1, "text": ""}, {"page": 2, "error": "model page failure"},
                ]}, ensure_ascii=False),
                "stderr": "",
            })()
            with patch.dict(os.environ, {"RAPIDOCR_WORKER_MODE": "oneshot"}), \
                 patch("dashboard.evaluation_workbench.local_ocr_gateway.subprocess.run", return_value=completed):
                values, error = local_ocr_gateway.request_local_ocr([
                    {"page": 1, "path": str(first)}, {"page": 2, "path": str(second)},
                ])

        self.assertIsNone(error)
        self.assertEqual([item["state"] for item in values], ["empty", "failed"])

    def test_local_ocr_serve_pool_reuses_persistent_worker_across_batches(self):
        import sys
        script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    pages = [{'page': int(item['page']), 'text': '常驻文字', 'line_count': 1, 'confidence': 99.0} for item in req.get('pages') or []]\n"
            "    print(json.dumps({'ok': True, 'pages': pages, 'metrics': {'elapsed_ms': 5, 'peak_rss_kb': 1000, 'model': 'fake', 'limit_side_len': 960}}), flush=True)\n"
        )
        pool = local_ocr_gateway._ServePool(command_factory=lambda: [sys.executable, "-c", script])
        try:
            first_worker = pool.acquire()
            first = first_worker.request([{"page": 1, "path": __file__}], 30)
            pool.release(first_worker)
            second_worker = pool.acquire()
            second = second_worker.request([{"page": 2, "path": __file__}], 30)
            pool.release(second_worker)
        finally:
            pool.shutdown()

        self.assertIs(first_worker, second_worker)
        self.assertEqual(second_worker.served_batches, 2)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(second["pages"][0]["text"], "常驻文字")

    def test_local_ocr_serve_pool_reaps_idle_workers(self):
        import sys
        script = "import sys\nfor line in sys.stdin:\n    print('{\"ok\": true, \"pages\": []}', flush=True)\n"
        pool = local_ocr_gateway._ServePool(command_factory=lambda: [sys.executable, "-c", script])
        try:
            serve_worker = pool.acquire()
            serve_worker.request([{"page": 1, "path": __file__}], 30)
            pool.release(serve_worker)
            self.assertTrue(serve_worker.is_alive())
            serve_worker.last_used -= local_ocr_gateway._SERVE_IDLE_SECONDS + 1
            pool.reap_idle()
            self.assertFalse(serve_worker.is_alive())
            self.assertEqual(len(pool._workers), 0)
        finally:
            pool.shutdown()

    def test_local_ocr_falls_back_to_oneshot_when_serve_worker_broken(self):
        import sys
        with tempfile.TemporaryDirectory(prefix="local_ocr_test_") as folder:
            image_path = Path(folder) / "page.jpg"
            image_path.write_bytes(b"jpeg")
            completed = type("Completed", (), {
                "returncode": 0,
                "stdout": json.dumps({"ok": True, "pages": [{"page": 1, "text": "回退文字", "line_count": 1}]}, ensure_ascii=False),
                "stderr": "",
            })()
            broken_pool = local_ocr_gateway._ServePool(command_factory=lambda: [sys.executable, "-c", "import sys; sys.exit(0)"])
            try:
                with patch("dashboard.evaluation_workbench.local_ocr_gateway._pool", return_value=broken_pool), \
                     patch("dashboard.evaluation_workbench.local_ocr_gateway.subprocess.run", return_value=completed) as run:
                    values, error = local_ocr_gateway.request_local_ocr([{"page": 1, "path": str(image_path)}])
            finally:
                broken_pool.shutdown()

        self.assertIsNone(error)
        self.assertEqual(values[0]["text"], "回退文字")
        run.assert_called_once()

    def test_local_ocr_max_workers_clamped_by_env(self):
        with patch.dict(os.environ, {"LOCAL_OCR_MAX_WORKERS": "2"}):
            self.assertEqual(local_ocr_gateway.local_ocr_max_workers(), 2)
        with patch.dict(os.environ, {"LOCAL_OCR_MAX_WORKERS": "99"}):
            self.assertEqual(local_ocr_gateway.local_ocr_max_workers(), 4)
        with patch.dict(os.environ, {"LOCAL_OCR_MAX_WORKERS": "abc"}):
            self.assertEqual(local_ocr_gateway.local_ocr_max_workers(), 1)

    def test_ocr_numeric_thread_count_env_override(self):
        with patch.dict(os.environ, {"RAPIDOCR_OMP_THREADS": "2"}):
            self.assertEqual(local_ocr_gateway._ocr_numeric_thread_count(), "2")
            self.assertEqual(local_ocr_gateway._runtime_env()["OMP_NUM_THREADS"], "2")
        with patch.dict(os.environ, {"RAPIDOCR_OMP_THREADS": "9"}):
            self.assertEqual(local_ocr_gateway._ocr_numeric_thread_count(), "4")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RAPIDOCR_OMP_THREADS", None)
            self.assertEqual(local_ocr_gateway._ocr_numeric_thread_count(), "1")

    def test_render_ocr_page_reuses_task_level_render_cache(self):
        from unittest.mock import MagicMock
        pixmap = MagicMock()
        pixmap.tobytes.return_value = b"jpeg-ocr-bytes"
        page = MagicMock()
        page.rect.width = 100
        page.rect.height = 100
        page.get_pixmap.return_value = pixmap
        pdf = MagicMock()
        pdf.page_count = 5
        pdf.__getitem__.return_value = page
        pdf.__enter__.return_value = pdf
        pdf.__exit__.return_value = False
        document = {"extension": ".pdf", "document_id": "doc-render-cache"}
        task = {}
        with patch("dashboard.evaluation_workbench.worker.storage.document_path", return_value="x.pdf"), \
             patch("dashboard.evaluation_workbench.worker.fitz.open", return_value=pdf) as open_pdf:
            first = worker._render_ocr_page(self.app, document, 2, "fast", task=task)
            second = worker._render_ocr_page(self.app, document, 2, "fast", task=task)

        self.assertEqual(first[0], b"jpeg-ocr-bytes")
        self.assertEqual(first, second)
        open_pdf.assert_called_once()

    def test_local_ocr_caches_empty_page_and_reports_failed_page(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        with patch("dashboard.evaluation_workbench.worker._render_ocr_page", return_value=(b"jpeg", "local-empty")), \
             patch("dashboard.evaluation_workbench.worker.request_local_ocr", return_value=([
                 {"page": 1, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "state": "empty"},
             ], None)):
            values, failure = worker._local_ocr_page_texts(self.app, document, [1])

        cached = storage.get_ocr_page_cache(
            self.app, document["document_id"], 1, "local-empty", local_ocr_gateway.LOCAL_OCR_SERVICE,
        )
        self.assertEqual(values, [])
        self.assertIn("未在候选页识别到文字", failure)
        self.assertTrue(cached["empty"])

    def test_local_ocr_serializes_parallel_subprocess_requests(self):
        documents = [
            self._add_pdf("bid-a.pdf", "bid", "甲公司", "扫描件候选页"),
            self._add_pdf("bid-b.pdf", "bid", "乙公司", "扫描件候选页"),
        ]
        active = maximum = 0
        lock = threading.Lock()

        def fake_local_ocr(pages, *, metrics=None):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            if isinstance(metrics, dict):
                metrics.update({"status": "success", "recognized_pages": 1, "elapsed_ms": 40})
            return ([{"page": pages[0]["page"], "service": local_ocr_gateway.LOCAL_OCR_SERVICE,
                      "state": "recognized", "text": "本地识别文字"}], None)

        with patch("dashboard.evaluation_workbench.worker.request_local_ocr", side_effect=fake_local_ocr):
            threads = [threading.Thread(target=worker._local_ocr_page_texts, args=(self.app, document, [1])) for document in documents]
            for item in threads:
                item.start()
            for item in threads:
                item.join()

        self.assertEqual(maximum, 1)

    def test_local_ocr_uses_high_quality_rendering_for_high_strength_certificate_rule(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        rule = {"title": "认证证书", "check_rule": "核验证书编号与有效期"}
        with patch("dashboard.evaluation_workbench.worker._render_ocr_page", return_value=(b"jpeg", "quality-hash")) as render, \
             patch("dashboard.evaluation_workbench.worker.request_local_ocr", return_value=([
                 {"page": 1, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "state": "recognized", "text": "证书编号A123"},
             ], None)):
            worker._local_ocr_page_texts(self.app, document, [1], rule=rule, level="high")

        self.assertEqual(render.call_args.args[3], "accurate")

    def test_local_ocr_runtime_environment_scopes_model_home_and_threads(self):
        with patch.dict(os.environ, {"RAPIDOCR_MODEL_HOME": "/tmp/rapidocr-model"}, clear=False):
            value = local_ocr_gateway._runtime_env()

        self.assertEqual(value["HOME"], "/tmp/rapidocr-model")
        self.assertEqual(value["OMP_NUM_THREADS"], "1")
        self.assertEqual(value["OPENBLAS_NUM_THREADS"], "1")

    def test_local_ocr_detector_side_length_is_conservative_and_bounded(self):
        from dashboard.evaluation_workbench import rapidocr_worker

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rapidocr_worker._detector_limit_side_len(), 960)
        with patch.dict(os.environ, {"RAPIDOCR_LIMIT_SIDE_LEN": "4096"}, clear=False):
            self.assertEqual(rapidocr_worker._detector_limit_side_len(), 1280)
        with patch.dict(os.environ, {"RAPIDOCR_LIMIT_SIDE_LEN": "invalid"}, clear=False):
            self.assertEqual(rapidocr_worker._detector_limit_side_len(), 960)

    def test_objective_ocr_fallback_keeps_summary_without_raw_page_text(self):
        original = {"evidence": "文字层已识别5项候选证书", "suggested_score": 3}
        raw_text = "身份证号码 410000000000000000\n合同金额 9001781元"

        objective = worker._with_ocr_fallback_evidence(
            original, "objective", "高精度版", [118, 200], raw_text,
        )
        review = worker._with_ocr_fallback_evidence(
            original, "review", "高精度版", [118, 200], raw_text,
        )

        self.assertIn("已完成2页候选材料文字识别", objective["evidence"])
        self.assertNotIn("身份证号码", objective["evidence"])
        self.assertEqual(objective["suggested_score"], 3)
        self.assertIn("身份证号码", review["evidence"])

    def test_ocr_raw_evidence_compacts_fragmented_table_header_but_keeps_short_values(self):
        raw_text = "综合\n单\n总价\n单\n价\n（含\n序号\n分项名称\nKJ11\n2\n套"

        result = worker._with_ocr_raw_evidence({"evidence": "文字层已定位材料。"}, "【OCR原文·P7】", raw_text)

        self.assertIn("表格字段：KJ11 2 套", result["evidence"])
        self.assertNotIn("综合\n单\n总价", result["evidence"])

    def test_reason_merge_dedupes_same_calculation_but_keeps_different_calculations(self):
        duplicate = worker._merge_reason_text(
            "计分过程：确认1项，建议2分。",
            "【图片识别·standard·P8】计分过程：确认1项，建议2分。",
        )
        different = worker._merge_reason_text(
            "计分过程：确认1项，建议2分。",
            "【图片识别·standard·P8】计分过程：确认2项，建议4分。",
        )

        self.assertEqual(duplicate.count("计分过程"), 1)
        self.assertEqual(different.count("计分过程"), 2)
        self.assertIn("建议4分", different)

    def test_subjective_prompt_requires_specific_non_full_score_basis(self):
        template = PROMPT_TEMPLATES["evaluate_all_subjective_user"]["content"]

        self.assertIn("非满分时", template)
        self.assertIn("具体缺项", template)

    def test_report_compacts_legacy_objective_ocr_raw_text(self):
        value = (
            "文字层建议1分。"
            "【腾讯OCR原文·高精度版·P12】身份证号码410000000000000000\n合同金额9001781元"
        )

        compact = evaluation_workbench_module._report_compact_objective_ocr_text(value)

        self.assertIn("腾讯OCR摘要", compact)
        self.assertNotIn("身份证号码", compact)
        self.assertIn("文字层建议1分", compact)

    def test_report_keeps_score_calculation_layer_in_evidence_brief(self):
        objective_scores = [{
            "rule_id": "r1", "title": "业绩评分", "check_rule": "每个同类型项目得3分，最高9分。",
            "evidence": "文字层证据。", "reason": "文字层理由。",
            "max_score": 9, "suggested_score": 6,
            "evidence_layers": [
                {"source": "score_calculation", "summary": "计分过程：3项×3分=9分，封顶9分。"},
                {"source": "tencent_ocr", "summary": "腾讯 OCR 摘要。"},
                {"source": "vision", "summary": "图片识别摘要。"},
            ],
        }]
        presentation = evaluation_workbench_module._report_presentation(
            [], None, [], None, [], [], objective_scores, [],
        )
        brief = presentation["objective_scores"][0]["evidence_brief"]
        self.assertIn("计分过程：3项×3分=9分", brief)
        self.assertIn("腾讯 OCR 摘要", brief)
        self.assertIn("图片识别摘要", brief)

    def test_supplement_text_keeps_latest_ocr_or_visual_fact_at_length_limit(self):
        merged = worker._merge_supplement_text("旧结论" * 900, "【图片识别】证书编号A123，有效期至2029年。")

        self.assertLessEqual(len(merged), 2000)
        self.assertIn("【图片识别】证书编号A123", merged)
        self.assertIn("旧结论", merged)

    def test_ocr_prompt_packing_keeps_every_paid_page_in_bounded_context(self):
        values = [
            {"page": page, "service": "accurate", "text": f"第{page}页开头\n" + ("识别文字" * 3000) + f"\n第{page}页结尾"}
            for page in range(1, 11)
        ]

        packed = worker._pack_ocr_page_texts(values, max_chars=20_000)

        self.assertLessEqual(len(packed), 20_000)
        for page in range(1, 11):
            self.assertIn(f"[第{page}页", packed)
            self.assertIn(f"第{page}页开头", packed)
            self.assertIn(f"第{page}页结尾", packed)

    def test_ocr_prompt_packing_keeps_rule_matched_middle_rows(self):
        values = [{
            "page": 1, "service": "table",
            "text": "表头\n" + ("普通说明\n" * 800) + "ISO14001环境管理体系认证证书 有效期2029年\n" + ("普通说明\n" * 800) + "表尾",
        }]
        rule = {"title": "ISO14001认证评分", "check_rule": "核验ISO14001环境管理体系认证证书有效期"}

        packed = worker._pack_ocr_page_texts(values, max_chars=1200, rule=rule)

        self.assertLessEqual(len(packed), 1200)
        self.assertIn("ISO14001环境管理体系认证证书", packed)

    def test_tencent_ocr_error_classification_does_not_treat_transient_failure_as_service_outage(self):
        class TransientError(Exception):
            code = "RequestLimitExceeded"

        class PageError(Exception):
            code = "InvalidParameterValue.ImageSizeTooLarge"

        self.assertEqual(ocr_gateway._classify_ocr_exception(TransientError("temporary overload"))["kind"], "transient")
        self.assertTrue(ocr_gateway._classify_ocr_exception(TransientError("temporary overload"))["retryable"])
        self.assertEqual(ocr_gateway._classify_ocr_exception(PageError("image too large"))["kind"], "page")

    def test_ocr_supplement_updates_score_and_routes_confirmed_page_to_multimodal(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 50,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "证书评分", "check_rule": "证书有效得2分",
                "vision_trigger": "required", "vision_level": "standard",
                "scoring": {"max_score": 2}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 2,
                    "evidence": "文字层仅见目录", "reason": "扫描件待核验",
                    "confidence": "low", "visual_page_candidates": [30]}
        parsed = {
            "coverage": "covered", "conclusion_scope": "full", "evidence_pages": [12],
            "suggested_score": 2, "confidence": "high",
            "evidence": "P12识别到证书名称、编号与有效期",
            "reason": "文字字段满足计分条件",
        }
        with patch("dashboard.evaluation_workbench.worker._ocr_page_texts", return_value=([
            {"page": 12, "service": "accurate", "text": "证书编号A123，有效期2029-12-31"},
            {"page": 20, "service": "accurate", "text": "附件目录"},
        ], "")), patch("dashboard.evaluation_workbench.worker._request_task_json", return_value=parsed):
            result = worker._run_ocr_supplement(
                self.app, {"task_id": "task"}, document, "objective", rule, original,
                {"profile_id": "text", "display_name": "文本模型"},
            )

        self.assertEqual(result["suggested_score"], 2)
        self.assertEqual(result["vision_status"], "ocr_applied")
        self.assertEqual(result["ocr_candidate_pages"], [12, 20])
        self.assertEqual(result["ocr_evidence_pages"], [12])
        self.assertEqual(worker._vision_page_candidates(document, rule, result)[:2], [12, 30])

    def test_partial_local_ocr_failure_cannot_claim_full_rule_coverage(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 50,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "证书评分", "check_rule": "证书有效得2分",
                "vision_trigger": "required", "vision_level": "standard", "scoring": {"max_score": 2}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 2,
                    "evidence": "文字层未找到证书", "reason": "待 OCR", "confidence": "low"}
        parsed = {
            "coverage": "covered", "conclusion_scope": "full", "suggested_score": 2,
            "evidence": "P12证书编号和有效期可见", "reason": "满足计分条件",
            "confidence": "high", "evidence_pages": [12],
        }
        with patch("dashboard.evaluation_workbench.worker._ocr_page_texts", return_value=([
            {"page": 12, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "证书编号A123"},
        ], "本地 RapidOCR 未完成 P13")), patch(
            "dashboard.evaluation_workbench.worker._request_task_json", return_value=parsed,
        ):
            result = worker._run_ocr_supplement(
                self.app, {"task_id": "task"}, document, "objective", rule, original,
                {"profile_id": "text", "display_name": "文本模型"},
            )

        self.assertEqual(result["vision_status"], "ocr_applied_partial")
        self.assertEqual(result["suggested_score"], 0)
        self.assertIn("未完成 P13", result["vision_message"])

    def test_high_ocr_reuses_single_visual_locator_result_for_later_multimodal_fallback(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 50,
                    "original_name": "扫描投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "证书扫描件", "check_rule": "核验证书编号及扫描件",
                "vision_trigger": "required", "vision_level": "high"}
        original = {"rule_id": "cert", "status": "ocr_required", "evidence": "",
                    "reason": "纯扫描件未定位", "confidence": "low"}

        with patch("dashboard.evaluation_workbench.worker._vision_page_candidates", return_value=[]), \
             patch("dashboard.evaluation_workbench.worker._locate_visual_pages", return_value=[12]) as locate, \
             patch("dashboard.evaluation_workbench.worker._ocr_page_texts",
                   return_value=([], "腾讯 OCR 未启用")) as ocr_pages:
            result = worker._run_ocr_supplement(
                self.app, {"task_id": "task"}, document, "review", rule, original,
                {"profile_id": "text"}, locator_profile={"profile_id": "vision"},
            )

        locate.assert_called_once()
        self.assertEqual(ocr_pages.call_args.args[4]["visual_page_candidates"], [12])
        self.assertEqual(result["visual_page_candidates"], [12])
        self.assertEqual(result["vision_status"], "ocr_failed")

    def test_rule_vision_policy_round_trip_keeps_existing_ocr_rule_disabled_until_strength_selected(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "扫描证明材料", "check_rule": "核验扫描件盖章", "ocr_required": True,
        })
        _, listed_rules = storage.list_rules(self.app, self.project["project_id"])
        listed = next(item for item in listed_rules if item["rule_id"] == rule["rule_id"])
        # 旧 OCR 规则默认只保留“需要图片”的语义；不因升级而自动产生图片模型调用。
        self.assertEqual(listed["vision_trigger"], "required")
        self.assertEqual(listed["vision_level"], "off")
        updated = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "vision_trigger": "text_fallback", "vision_level": "standard",
        })
        self.assertEqual(updated["vision_trigger"], "text_fallback")
        self.assertEqual(updated["vision_level"], "standard")

    def test_model_configuration_management_requires_password(self):
        client = self.app.test_client()
        locked = client.post("/api/evaluation-workbench/model-profiles", json={
            "display_name": "锁定测试", "base_url": "https://example.test/v1", "model_name": "test-model", "api_key": "test-key",
        })
        wrong = client.post("/api/evaluation-workbench/model-configuration/unlock", json={"password": "wrong"})
        self._unlock_model_configuration(client)
        allowed = client.post("/api/evaluation-workbench/model-profiles", json={
            "display_name": "解锁测试", "base_url": "https://example.test/v1", "model_name": "test-model", "api_key": "test-key",
        })

        self.assertEqual(locked.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(allowed.status_code, 201)

    def test_prompt_templates_are_publicly_readable_but_mutations_require_password(self):
        client = self.app.test_client()
        listed = client.get("/api/evaluation-workbench/prompt-templates")
        self.assertEqual(listed.status_code, 200)
        templates = listed.get_json()["templates"]
        self.assertEqual({item["configuration_group"] for item in templates}, {"business", "workflow", "system"})
        self.assertEqual(
            [item["template_id"] for item in templates if item["configuration_group"] == "business"],
            ["compare_ai_assessment", "extract_rules_guidance", "extract_rules_package_scope",
             "extract_rules_validation_guidance", "evaluate_all_guidance", "evaluate_all_scope_anomaly_guidance"],
        )
        self.assertTrue(all(item["section"] and item["change_level"] for item in templates))
        extraction_template = next(item for item in templates if item["template_id"] == "extract_rules_user")
        self.assertIn("不得逐条复述招标原文", extraction_template["content"])
        internal_template = next(item for item in templates if item["template_id"] == "extract_rules_compile_user")
        self.assertEqual(internal_template["configuration_group"], "system")
        self.assertEqual(internal_template["section"], "评审规则内部处理")
        compare_guidance = next(item for item in templates if item["template_id"] == "compare_ai_assessment")
        self.assertEqual(compare_guidance["name"], "文件查重 · 通用业务指令")
        original = next(item for item in templates if item["template_id"] == "evaluate_all")
        before_fingerprint = storage.prompt_template_fingerprint(self.app)
        locked = client.patch("/api/evaluation-workbench/prompt-templates/evaluate_all", json={"content": "请严格逐项核验，并用简洁中文说明证据和理由。"})
        updated = client.patch("/api/evaluation-workbench/prompt-templates/evaluate_all", json={"content": "请严格逐项核验，并用简洁中文说明证据和理由。", "password": "108"})
        self.assertEqual(locked.status_code, 403)
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["template"]["is_custom"])
        self.assertEqual(storage.prompt_template(self.app, "evaluate_all"), "请严格逐项核验，并用简洁中文说明证据和理由。")
        self.assertNotEqual(storage.prompt_template_fingerprint(self.app), before_fingerprint)
        restored = client.delete("/api/evaluation-workbench/prompt-templates/evaluate_all", json={"password": "108"})
        self.assertEqual(restored.status_code, 200)
        self.assertFalse(restored.get_json()["template"]["is_custom"])
        self.assertEqual(storage.prompt_template(self.app, "evaluate_all"), original["content"])

    def test_default_prompt_templates_keep_runtime_contracts(self):
        for template_id, meta in PROMPT_TEMPLATES.items():
            content = meta["content"]
            declared = set(meta.get("placeholders") or ())
            actual = set(re.findall(r"\{\{([a-z_]+)\}\}", content))
            self.assertEqual(actual, declared, template_id)
            self.assertGreaterEqual(len(content), 20, template_id)
            self.assertLessEqual(len(content), 12_000, template_id)
            rendered = storage.render_prompt_template(
                self.app, template_id, **{name: f"<{name}>" for name in declared},
            )
            self.assertIsNone(re.search(r"\{\{[a-z_]+\}\}", rendered), template_id)

        extraction = PROMPT_TEMPLATES["extract_rules_user"]["content"]
        self.assertIn('"source_page":数字或null', extraction)
        self.assertIn('"source_clause_ids"', extraction)
        self.assertIn('"items"', extraction)
        self.assertIn("机器可读文字、表格、元数据或后续 OCR", extraction)
        extraction_system = PROMPT_TEMPLATES["extract_rules"]["content"]
        self.assertIn("随后附加的通用业务指令", extraction_system)
        extraction_guidance = PROMPT_TEMPLATES["extract_rules_guidance"]["content"]
        extraction_user = PROMPT_TEMPLATES["extract_rules_user"]["content"]
        extraction_continue = PROMPT_TEMPLATES["extract_rules_continue_user"]["content"]
        self.assertIn("正式评审依据门槛", extraction_guidance)
        self.assertIn("不得把普通技术描述", extraction_guidance)
        self.assertIn("资格业绩的最低数量", extraction_guidance)
        self.assertIn("技术/服务要求逐项响应覆盖", extraction_guidance)
        self.assertIn("通常每个采购包控制为1至3条规则", extraction_guidance)
        self.assertIn("技术参数★条款的受控保留规则", extraction_guidance)
        # 去重后该约束只在系统侧 guidance 一份（通过 _system_prompt 进入每次提取调用），
        # 用户任务模板不再重复携带，避免同一次调用重复发送与后续维护漂移。
        self.assertIn("技术参数★条款的受控保留规则", worker._system_prompt(self.app, "extract_rules"))
        self.assertNotIn("技术参数★条款的受控保留规则", extraction_user)
        self.assertIn("subjective|other", extraction_user)
        self.assertIn("category=other", extraction_user)
        self.assertIn("subjective|other", extraction_continue)
        extraction_composed = worker._system_prompt(self.app, "extract_rules")
        self.assertIn("正式评审依据门槛", extraction_composed)
        self.assertIn("未来或外部事项", extraction_composed)
        evaluation_composed = worker._system_prompt(self.app, "evaluate_all")
        self.assertIn("招标文件复述只能证明", evaluation_composed)
        self.assertIn("概括性方案、目录、偏离表“无偏离”", evaluation_composed)
        self.assertIn("模板提示文字", evaluation_composed)
        for template_id in (
            "compare_ai_assessment_user", "review_documents_user", "score_objective_user",
            "score_subjective_user", "evaluate_all_review_user", "evaluate_all_objective_user",
            "evaluate_all_subjective_user",
        ):
            self.assertIn("恰好返回一次", PROMPT_TEMPLATES[template_id]["content"], template_id)
        review_template = PROMPT_TEMPLATES["evaluate_all_review_user"]["content"]
        self.assertIn("不得自行给分", review_template)
        self.assertIn('"page_hint":"页码或null"', review_template)
        self.assertIn("正向义务", review_template)
        self.assertIn("未找到直接证据时应返回 not_found", review_template)
        qualification_template = PROMPT_TEMPLATES["extract_rules_qualification_supplement_user"]["content"]
        self.assertIn("正式资格候选区段", qualification_template)

        self.assertIn("相邻非资格内容", qualification_template)
        extraction_validation = PROMPT_TEMPLATES["extract_rules_validation_guidance"]["content"]
        compile_template = PROMPT_TEMPLATES["extract_rules_compile_user"]["content"]
        coverage_template = PROMPT_TEMPLATES["extract_rules_coverage_user"]["content"]
        quality_gate_template = PROMPT_TEMPLATES["extract_rules_quality_gate_user"]["content"]
        finalise_template = PROMPT_TEMPLATES["extract_rules_finalise_user"]["content"]
        for value in (extraction_guidance, compile_template, coverage_template):
            self.assertIn("履约", value)
            self.assertIn("电子投标文件", value)
            self.assertIn("串通、行贿、弄虚作假", value)
        self.assertIn("同一响应字段的期限、地点、标准、金额", compile_template)
        self.assertIn("source_clause_ids", compile_template)
        self.assertIn("scoring.items", compile_template)
        self.assertIn("逐项响应覆盖候选", compile_template)
        for value in (extraction_user, extraction_continue, compile_template, coverage_template):
            self.assertIn('"evidence_requirements"', value)
            self.assertIn("每条输出都必须保留该字段", value)
        self.assertIn("不得因材料名称或可能存在扫描页而把整条规则强制 OCR", compile_template)
        self.assertIn("category=other 的逐项响应覆盖规则", coverage_template)
        self.assertIn('"drops"', quality_gate_template)
        self.assertIn("受保护规则", quality_gate_template)
        self.assertIn("勾选或取消勾选状态不是本轮提取依据", quality_gate_template)
        self.assertIn("具有独立人工复核价值", quality_gate_template)
        self.assertIn('"rewrites"', finalise_template)
        self.assertIn('"merges"', finalise_template)
        self.assertIn("objective/subjective", finalise_template)
        self.assertIn("不接受联合体", extraction_validation)
        self.assertIn("平台子账号", extraction_validation)
        self.assertIn("必须为 manual", extraction_validation)
        self.assertIn("没有实际标记时不", extraction_guidance)
        self.assertIn("中小企业声明函", extraction_guidance)
        self.assertIn("总项目名称", evaluation_composed)

        compact_scan = worker._full_scan_prompt(
            self.app, {"original_name": "投标文件.pdf", "bidder_name": "投标人"},
            [], {"chunk_id": "chunk-1", "text": "正文"}, {}, compact=True,
        )
        self.assertIn("每段摘录最多 60 字", compact_scan)
        self.assertNotIn("每段摘录最多 90 字", compact_scan)
        self.assertNotIn("matches 每个数组恰好六项、最多36条", compact_scan)
        self.assertNotIn("scope_anomalies 每个数组恰好五项、最多8条", compact_scan)

    def test_combined_review_prompt_includes_shared_price_facts_without_name_error(self):
        document = {
            "original_name": "投标文件.pdf", "bidder_name": "甲公司",
            "_shared_price_facts": "投标总报价为 628120 元，最高限价为 638913.36 元。",
        }
        payload = [{
            "rule_id": "price-rule", "title": "投标报价符合性", "source_text": "报价不得超过最高限价",
            "check_rule": "核验投标报价是否超过最高限价", "ocr_required": False,
        }]
        prompt = worker._combined_batch_prompt(self.app, "review", document, payload, "报价表原文", compact=False)
        self.assertIn("【已核验价格事实】", prompt)
        self.assertIn("628120", prompt)

    def test_shared_price_facts_reuse_precomputed_project_upper_limit(self):
        document = self._add_pdf("price.pdf", "bid", "甲公司", "投标总报价：628120 元")
        with patch.object(worker, "_local_project_upper_limit") as project_limit:
            facts = worker._document_shared_price_facts(
                self.app, self.project["project_id"], document,
                upper_limit=(Decimal("638913.36"), "最高限价：638913.36 元"),
            )
        project_limit.assert_not_called()
        self.assertIn("638913.36", facts)

    def test_task_prompt_fingerprint_ignores_unrelated_template_changes(self):
        client = self.app.test_client()
        before_evaluation = storage.task_prompt_template_fingerprint(self.app, "evaluate_all")
        before_extraction = storage.task_prompt_template_fingerprint(self.app, "extract_rules")

        response = client.patch(
            "/api/evaluation-workbench/prompt-templates/extract_rules_guidance",
            json={"content": "完整提取可由投标文件核验的评审规则，并保留评分条件、证明材料和分值上限。", "password": "108"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.task_prompt_template_fingerprint(self.app, "evaluate_all"), before_evaluation)
        self.assertNotEqual(storage.task_prompt_template_fingerprint(self.app, "extract_rules"), before_extraction)

    def test_global_rules_require_password_and_are_all_imported_with_default_selection(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/api/evaluation-workbench/global-rules").status_code, 200)
        self.assertEqual(client.post("/api/evaluation-workbench/global-rules", json={"title": "无口令", "check_rule": "不应保存"}).status_code, 403)
        created = client.post("/api/evaluation-workbench/global-rules", json={
            "category": "substantive", "title": "营业执照有效性",
            "check_rule": "核验是否提供有效营业执照", "source_text": "投标人应提供有效营业执照。",
            "ocr_required": True, "enabled": True, "password": "108",
        })
        self.assertEqual(created.status_code, 201)
        disabled = client.post("/api/evaluation-workbench/global-rules", json={
            "category": "other", "title": "默认不选项", "check_rule": "应导入但默认不选", "enabled": False, "password": "108",
        })
        self.assertEqual(disabled.status_code, 201)

        new_project = storage.create_project(self.app, "自动导入项目")
        rule_set, rules = storage.list_rules(self.app, new_project["project_id"])

        self.assertEqual(rule_set["status"], "draft")
        self.assertEqual(len(rules), 2)
        imported = {rule["title"]: rule for rule in rules}
        self.assertTrue(all(rule["source_type"] == "global" for rule in rules))
        self.assertEqual(imported["营业执照有效性"]["check_rule"], "核验是否提供有效营业执照")
        self.assertEqual(imported["营业执照有效性"]["check_mode"], "ocr")
        self.assertEqual(imported["营业执照有效性"]["acquisition_preset"], "smart")
        self.assertTrue(imported["营业执照有效性"]["enabled"])
        self.assertFalse(imported["默认不选项"]["enabled"])

        self.assertEqual(client.patch(f"/api/evaluation-workbench/global-rules/{created.get_json()['rule']['global_rule_id']}", json={"title": "更新名称"}).status_code, 403)
        client.patch(f"/api/evaluation-workbench/global-rules/{created.get_json()['rule']['global_rule_id']}", json={"title": "更新名称", "password": "108"})
        unchanged = {rule["title"] for rule in storage.list_rules(self.app, new_project["project_id"])[1]}
        self.assertIn("营业执照有效性", unchanged)
        self.assertNotIn("更新名称", unchanged)

    def test_new_global_rule_immediately_syncs_only_current_draft_rule_sets(self):
        # self.project 有人工规则，代表用户正在编辑的待确认项目。
        existing = storage.add_rule(self.app, self.project["project_id"], {
            "category": "substantive", "title": "人工已有规则", "check_rule": "核验人工已有材料",
        })
        confirmed_project = storage.create_project(self.app, "已确认项目")
        storage.add_rule(self.app, confirmed_project["project_id"], {
            "category": "substantive", "title": "已确认规则", "check_rule": "核验已确认材料",
        })
        storage.confirm_rule_set(self.app, confirmed_project["project_id"])
        # 该项目没有任何规则集，也应得到可立即查看的新待确认规则集。
        empty_project = storage.create_project(self.app, "尚未提取项目")

        created = storage.create_global_rule(self.app, {
            "category": "compliance", "title": "即时同步规则", "check_rule": "核验新增通用要求", "enabled": False,
        })

        self.assertEqual(created["synced_draft_rule_sets"], 2)
        _, draft_rules = storage.list_rules(self.app, self.project["project_id"])
        synced = next(item for item in draft_rules if item["title"] == "即时同步规则")
        self.assertEqual(synced["source_type"], "global")
        self.assertFalse(synced["enabled"])
        self.assertEqual(next(item for item in draft_rules if item["rule_id"] == existing["rule_id"])["source_type"], "manual")
        empty_set, empty_rules = storage.list_rules(self.app, empty_project["project_id"])
        self.assertEqual(empty_set["status"], "draft")
        self.assertEqual([item["title"] for item in empty_rules], ["即时同步规则"])
        _, confirmed_rules = storage.list_rules(self.app, confirmed_project["project_id"])
        self.assertNotIn("即时同步规则", {item["title"] for item in confirmed_rules})

        duplicate = storage.create_global_rule(self.app, {
            "category": "substantive", "title": "人工已有规则", "check_rule": "核验人工已有材料",
        })
        _, after_duplicate = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(sum(item["title"] == "人工已有规则" for item in after_duplicate), 1)
        self.assertEqual(next(item for item in after_duplicate if item["title"] == "人工已有规则")["source_type"], "manual")
        self.assertEqual(duplicate["synced_draft_rule_sets"], 1)

    def test_rule_extraction_merges_all_global_rules_with_default_selection_without_exact_duplicates(self):
        storage.create_global_rule(self.app, {
            "category": "qualification", "title": "通用营业执照", "check_rule": "核验是否提供有效营业执照", "source_text": "通用基线",
        })
        storage.create_global_rule(self.app, {
            "category": "compliance", "title": "完全重复规则", "check_rule": "核验响应文件是否完整", "source_text": "通用基线",
        })
        storage.create_global_rule(self.app, {
            "category": "other", "title": "默认不选通用项", "check_rule": "核验是否提供其他材料", "source_text": "通用基线", "enabled": False,
        })

        rule_set = storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-1", [
            {"category": "compliance", "title": "完全重复规则", "check_rule": "核验响应文件是否完整", "source_text": "招标原文"},
            {"category": "substantive", "title": "报价限制", "check_rule": "核验报价未超过最高限价", "source_text": "最高限价"},
        ])
        _, rules = storage.list_rules(self.app, self.project["project_id"])

        self.assertEqual(rule_set["global_rule_count"], 2)
        self.assertEqual(len(rules), 4)
        self.assertEqual({item["source_type"] for item in rules}, {"ai", "global"})
        self.assertEqual(sum(item["title"] == "完全重复规则" for item in rules), 1)
        self.assertFalse(next(item for item in rules if item["title"] == "默认不选通用项")["enabled"])

    def test_manual_check_rule_is_preserved_and_can_be_updated(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "营业执照", "check_rule": "核验是否提供有效营业执照", "source_text": "投标人应提供营业执照。",
        })
        self.assertEqual(rule["check_rule"], "核验是否提供有效营业执照")
        updated = storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {"check_rule": "核验营业执照是否在有效期内"})
        self.assertEqual(updated["check_rule"], "核验营业执照是否在有效期内")

    def test_other_manual_rule_is_included_in_combined_review(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "已提供承诺函。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "other", "title": "其他承诺", "check_rule": "核验是否提供承诺函", "source_text": "应提供承诺函。",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={
            "results": [{"rule_id": rule["rule_id"], "status": "satisfied", "evidence": "承诺函", "reason": "已提供", "risk_level": "low"}],
        }):
            finished = self._run_next_task()
        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(reviews[0]["category"], "other")

    def test_auto_results_can_be_confirmed_in_batch_while_exceptions_remain(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案。")
        review_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "资质"})
        score_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "objective", "title": "资质评分", "scoring": {"kind": "boolean", "max_score": 5}})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        storage.update_task(self.app, task["task_id"], status="success")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], "profile-1")
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", "profile-1")
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [
            {"rule_id": review_rule["rule_id"], "status": "satisfied", "confidence": "high", "evidence_quality": "sufficient", "risk_level": "low", "requires_review": False, "automation_status": "ready_for_batch_confirmation",
             "vision_status": "applied", "vision_pages": [3, 5], "vision_evidence_pages": [5],
             "evidence_layers": [{"source": "vision", "summary": "P5证书内容清晰", "checked_pages": [3, 5], "evidence_pages": [5], "model": "图片模型"}],
             "vision_model": "图片模型", "vision_message": "图片证据已写入。"},
        ])
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [
            {"rule_id": score_rule["rule_id"], "suggested_score": 5, "effective_score": 5, "max_score": 5, "confidence": "high", "evidence": "资质证书", "requires_review": False, "automation_status": "ready_for_batch_confirmation",
             "vision_status": "applied_partial", "vision_pages": [8], "vision_model": "图片模型", "vision_message": "已补充部分图片事实。"},
        ])

        reviews = self.app.test_client().post(f"/api/evaluation-workbench/projects/{self.project['project_id']}/review-results/confirm-auto")
        scores = self.app.test_client().post(f"/api/evaluation-workbench/projects/{self.project['project_id']}/score-results/confirm-auto", json={"score_type": "objective"})
        _, review_rows = storage.latest_review_results(self.app, self.project["project_id"])
        _, score_rows = storage.latest_score_results(self.app, self.project["project_id"], "objective")

        self.assertEqual(reviews.get_json()["confirmed_count"], 1)
        self.assertEqual(scores.get_json()["confirmed_count"], 1)
        self.assertEqual(review_rows[0]["final_status"], "satisfied")
        self.assertEqual(review_rows[0]["vision_pages"], [3, 5])
        self.assertEqual(review_rows[0]["vision_evidence_pages"], [5])
        self.assertEqual(review_rows[0]["evidence_layers"][0]["evidence_pages"], [5])
        self.assertEqual(review_rows[0]["vision_status"], "applied")
        self.assertEqual(score_rows[0]["final_score"], 5.0)
        self.assertEqual(score_rows[0]["vision_pages"], [8])
        self.assertEqual(score_rows[0]["vision_status"], "applied_partial")

    def test_normalise_conclusion_summary_cleans_and_truncates(self):
        self.assertEqual(worker._normalise_conclusion_summary("结论：投标有效期承诺缺失，需复核。"), "投标有效期承诺缺失，需复核。")
        self.assertEqual(worker._normalise_conclusion_summary("综上：逐项响应无偏离，建议满分。"), "逐项响应无偏离，建议满分。")
        self.assertEqual(worker._normalise_conclusion_summary(""), "")
        self.assertEqual(worker._normalise_conclusion_summary(None), "")
        long_text = "，".join(f"事项{i}" for i in range(1, 40))
        out = worker._normalise_conclusion_summary(long_text)
        self.assertLessEqual(len(out), 61)
        self.assertTrue(out.endswith("…"))

    def test_result_models_carry_conclusion_summary(self):
        review = worker._review_result_from_model(
            {"status": "not_found", "summary": "结论：投标有效期三项承诺未发现，需复核。", "evidence": "x", "reason": "y"},
            "r1", "not_found",
        )
        self.assertEqual(review["conclusion_summary"], "投标有效期三项承诺未发现，需复核。")
        score = worker._score_result_from_model(
            "r2", 3.0, 6.0, {"suggested_score": 3, "confidence": "high", "summary": "综上：逐项响应无偏离，建议满分。"},
        )
        self.assertEqual(score["conclusion_summary"], "逐项响应无偏离，建议满分。")
        self.assertEqual(worker._score_result_from_model("r3", None, 6.0, {"confidence": "low"})["conclusion_summary"], "")

    def test_reconcile_summary_score_strips_conflicting_definite_score(self):
        self.assertEqual(
            worker._reconcile_summary_score("建议价格分29.75分，报价632836元为次低有效报价。", 29.72),
            "建议价格分，报价632836元为次低有效报价。",
        )
        self.assertEqual(worker._reconcile_summary_score("建议分9分，待核验签字盖章。", 9.0), "建议分9分，待核验签字盖章。")
        self.assertEqual(worker._reconcile_summary_score("建议暂计0.5-1分，需复核。", 0.75), "建议暂计0.5-1分，需复核。")
        self.assertEqual(worker._reconcile_summary_score("满分9分，建议得3分。", 3.0), "满分9分，建议得3分。")
        self.assertEqual(worker._reconcile_summary_score("", 3.0), "")
        self.assertEqual(worker._reconcile_summary_score("建议满分6分，逐项无偏离。", 3.0), "建议满分，逐项无偏离。")

    def test_evidence_traceability_guidance_present_in_result_prompts(self):
        from dashboard.evaluation_workbench.prompt_templates import EVALUATION_PROMPT_VERSION, PROMPT_TEMPLATES
        marker = "文本层无法定位具体金额时"
        for template_id in ("evaluate_all_review_user", "evaluate_all_objective_user",
                            "evaluate_all_cross_bid_price_user"):
            self.assertIn(marker, PROMPT_TEMPLATES[template_id]["content"])
        # v33 曾把该约束同时追加到系统叠加层 evaluate_all_guidance，导致 review/
        # objective/cross_bid 收到两遍、full_scan/subjective/OCR/vision 白扛一遍；
        # 修复后只保留在真正产生报价/声明函结论的三个用户模板中。
        self.assertNotIn(marker, PROMPT_TEMPLATES["evaluate_all_guidance"]["content"])
        self.assertNotIn(marker, PROMPT_TEMPLATES["evaluate_all_subjective_user"]["content"])
        self.assertNotIn(marker, PROMPT_TEMPLATES["evaluate_all_full_scan_user"]["content"])
        self.assertIn("统一为 partial 或需图片核验", PROMPT_TEMPLATES["evaluate_all_review_user"]["content"])
        self.assertEqual(EVALUATION_PROMPT_VERSION, "vision-evidence-contract-v45")

    def test_scope_chapter_and_text_error_guidance_present(self):
        from dashboard.evaluation_workbench.prompt_templates import EVALUATION_PROMPT_VERSION, PROMPT_TEMPLATES
        scope_marker = "整章模板混用"
        text_marker = "疑为复制粘贴或 OCR 断字"
        # 范围章节指南只挂载在 scope_anomaly_guidance：全文扫描和范围规则复核分别
        # 通过 worker 注入，避免同一指南在模板与注入层出现两次白烧 token。
        self.assertIn(scope_marker, PROMPT_TEMPLATES["evaluate_all_scope_anomaly_guidance"]["content"])
        for template_id in ("evaluate_all_full_scan_user", "evaluate_all_review_user",
                            "evaluate_all_subjective_user"):
            self.assertNotIn(scope_marker, PROMPT_TEMPLATES[template_id]["content"])
        # 文字错误线索仅审查组需要，单点挂载不重复。
        self.assertIn(text_marker, PROMPT_TEMPLATES["evaluate_all_review_user"]["content"])
        self.assertNotIn(text_marker, PROMPT_TEMPLATES["evaluate_all_full_scan_user"]["content"])
        self.assertNotIn(text_marker, PROMPT_TEMPLATES["evaluate_all_scope_anomaly_guidance"]["content"])
        self.assertIn("必须点名最具辨识度的偏离对象", PROMPT_TEMPLATES["evaluate_all_scope_anomaly_guidance"]["content"])
        self.assertIn("risk_level 应为 high", PROMPT_TEMPLATES["evaluate_all_scope_anomaly_guidance"]["content"])
        self.assertEqual(EVALUATION_PROMPT_VERSION, "vision-evidence-contract-v45")

    def test_ocr_visual_contracts_deduplicated_but_keep_hard_constraints(self):
        from dashboard.evaluation_workbench.prompt_templates import PROMPT_TEMPLATES
        ocr_contract = PROMPT_TEMPLATES["evaluate_all_ocr_contract"]["content"]
        visual_contract = PROMPT_TEMPLATES["evaluate_all_visual_contract"]["content"]
        ocr_user = PROMPT_TEMPLATES["evaluate_all_ocr_user"]["content"]
        visual_user = PROMPT_TEMPLATES["evaluate_all_visual_user"]["content"]
        # 契约只保留协议必需字段与独有约束，删除与用户模板语义重复的解释句。
        self.assertNotIn("coverage 只表示本次 OCR 文字是否覆盖规则相关材料", ocr_contract)
        self.assertNotIn("证据和理由不复述规则", ocr_contract)
        self.assertNotIn("coverage 只描述本次传入图片是否包含规则相关事实", visual_contract)
        self.assertNotIn("未覆盖、模糊或未出现的字段不是冲突", visual_contract)
        # 硬约束仍在：字段清单、证据页边界、material 冲突逐字值要求。
        self.assertIn("coverage、conclusion_scope、evidence_pages", ocr_contract)
        self.assertIn("不得机械列出全部处理页", ocr_contract)
        self.assertIn("material 冲突必须提供双方非空的逐字值", visual_contract)
        # 被去重的约束在用户模板里仍然存在，去重不丢信息。
        self.assertIn("coverage=covered 仅表示OCR文字覆盖到规则相关材料", ocr_user)
        self.assertIn("证据与理由不得复述规则", ocr_user)
        self.assertIn("都不是冲突，conflict_level 应保持 none", visual_user)

    def test_compact_text_result_drops_internal_objects(self):
        result = {
            "rule_id": "r1", "status": "partial", "evidence": "证据", "reason": "理由",
            "conclusion_summary": "摘要", "coverage_status": "partial", "vision_status": "ocr_applied_partial",
            "evidence_layers": [{"source": "local_ocr", "summary": "大段内容"}],
            "visual_page_candidates": [1, 2], "vision_message": "长消息",
            "evidence_items": [{"name": "证书", "requirement": "核验编号"}],
            "score_items": [{"item_id": "a", "status": "unresolved"}],
            "field_checks": [],
            "summary": "前序图片结论", "coverage": "covered", "conclusion_scope": "partial",
            "needs_more_image": True, "conflict_level": "possible",
        }
        compact = worker._compact_text_result(result)
        self.assertNotIn("evidence_layers", compact)
        self.assertNotIn("visual_page_candidates", compact)
        self.assertNotIn("vision_message", compact)
        self.assertEqual(compact["status"], "partial")
        self.assertEqual(compact["conclusion_summary"], "摘要")
        self.assertEqual(len(compact["evidence_items"]), 1)
        self.assertEqual(compact["score_items"][0]["item_id"], "a")
        # 图片/OCR 解析协议别名键必须保留，供跨批上下文使用。
        self.assertEqual(compact["summary"], "前序图片结论")
        self.assertEqual(compact["coverage"], "covered")
        self.assertEqual(compact["conclusion_scope"], "partial")
        self.assertTrue(compact["needs_more_image"])
        self.assertEqual(compact["conflict_level"], "possible")

    def test_scope_template_mixing_enforces_high_risk_and_object_summary(self):
        raw = {
            "status": "partial",
            "evidence": "第10-11页出现施工总平面图等土建模板；第775页出现高层建筑留洞口、灌浇青铅",
            "reason": "范围画像不含土建总承包、强电配电柜/电力电缆施工、青铅灌浇等工艺，被作为本项目方案写入→属整章模板混用",
            "summary": "部分施工工艺段为通用模板，需复核",
            "risk_level": "medium", "confidence": "medium", "evidence_quality": "sufficient",
        }
        result = worker._review_result_from_model(raw, "r1", "partial", scope_rule=True)
        self.assertEqual(result["risk_level"], "high")
        self.assertIn("第10-11、775页", result["conclusion_summary"])
        self.assertIn("非本项目场景模板", result["conclusion_summary"])
        plain = worker._review_result_from_model(raw, "r2", "partial")
        self.assertEqual(plain["risk_level"], "medium")
        self.assertEqual(plain["conclusion_summary"], "部分施工工艺段为通用模板，需复核")

    def test_scope_summary_enrichment_uses_model_summary_objects_and_pages(self):
        evidence = "第740-768页配电柜安装；第775-780页高层建筑老虎口与青铅灌浇"
        reason = "项目范围画像仅含机房改造→正文将建筑工程施工总承包模板作为本项目方案写入→属画像外工艺模板混用"
        summary = "强电、桥架、家用新风等建筑机电通用模板写入本项目方案，建议人工核验"
        enriched = worker._enrich_scope_summary(evidence, reason, summary)
        self.assertIn("第740-768、775-780页", enriched)
        self.assertIn("强电、桥架等非本项目场景模板", enriched)
        self.assertLessEqual(len(enriched), 60)

    def test_flaw_scoring_rule_detection(self):
        self.assertTrue(worker._flaw_scoring_rule({
            "title": "安全文明施工保障措施方案主观评分", "check_rule": "无瑕疵得6分；存在瑕疵按分档扣分",
            "source_text": "", "scoring_json": json.dumps({"max_score": 6, "items": [
                {"name": "施工保障措施方案瑕疵档得分", "max_score": 6, "criterion": "按瑕疵档打分"}]}),
        }))
        self.assertFalse(worker._flaw_scoring_rule({
            "title": "技术参数响应情况主观评分", "check_rule": "按指标应答覆盖与证明材料齐全情况评分",
            "source_text": "", "scoring_json": json.dumps({"max_score": 35}),
        }))

    def test_flaw_scoring_rule_matches_quality_wording_across_industries(self):
        # 环保/软件等行业的方案评分不一定写“瑕疵/套用”，完整性、科学性、可行性等
        # 质量措辞同样需要范围候选页支撑判断。
        self.assertTrue(worker._flaw_scoring_rule({
            "title": "污水处理处置方案评分", "check_rule": "按处置工艺的科学性与可行性分档评分",
            "source_text": "", "scoring_json": json.dumps({"max_score": 10}),
        }))
        self.assertTrue(worker._flaw_scoring_rule({
            "title": "软件开发实施方案评分", "check_rule": "按需求理解完整性与实施计划合理性评分",
            "source_text": "", "scoring_json": json.dumps({"max_score": 12}),
        }))

    def test_scope_rule_detection_without_range_keyword(self):
        # 不同行业规则不一定带“范围/无关”字样，组合信号也要能识别为范围类规则。
        self.assertTrue(worker._is_scope_consistency_rule({
            "title": "服务内容与采购需求一致性核验", "check_rule": "核对服务内容是否与采购需求相符",
        }))
        self.assertTrue(worker._is_scope_consistency_rule({
            "title": "技术交付内容相符性核验", "check_rule": "核对技术交付内容是否与本项目一致",
        }))
        self.assertFalse(worker._is_scope_consistency_rule({
            "title": "投标报价得分", "check_rule": "按公式计算报价得分",
        }))

    def test_scope_mixing_detection_works_across_industries(self):
        # 纯语义信号应跨行业命中：环保项目混入矿山/农田内容，软件项目混入土建/硬件内容。
        for raw in (
            {"status": "not_satisfied", "risk_level": "medium", "confidence": "high",
             "evidence": "第60-80页出现矿山爆破、农田灌溉等章节",
             "reason": "处置方案混入矿山爆破与农田灌溉内容，超出本项目污水处理厂改造范围，"
                       "且被作为处置方案写入→属非本项目内容",
             "summary": "处置方案混入非本项目工艺内容，需人工复核",
             "evidence_quality": "sufficient"},
            {"status": "not_satisfied", "risk_level": "medium", "confidence": "high",
             "evidence": "第20-35页出现土建施工、硬件生产线组装等章节",
             "reason": "软件开发方案混入土建施工与硬件生产内容，不属于本项目软件系统开发范围，"
                       "且被作为实施方案写入→属整章模板混用",
             "summary": "实施方案混入非本项目内容，需人工复核",
             "evidence_quality": "sufficient"},
        ):
            result = worker._review_result_from_model(raw, "r1", "not_satisfied", scope_rule=True)
            self.assertEqual(result["risk_level"], "high")
            self.assertIn("第", result["conclusion_summary"])

    def test_review_satisfied_downgraded_when_still_needs_review(self):
        # “满足”与“需人工复核”不能并存：非低风险/高置信/证据充分时降为 partial。
        review = worker._review_result_from_model(
            {"status": "satisfied", "evidence": "具备资质", "reason": "已提供",
             "risk_level": "medium", "confidence": "high", "evidence_quality": "sufficient"},
            "r1", "satisfied",
        )
        self.assertEqual(review["status"], "partial")
        self.assertIn("需人工复核", review["review_reason"])
        auto = worker._review_result_from_model(
            {"status": "satisfied", "evidence": "具备资质", "reason": "已提供",
             "risk_level": "low", "confidence": "high", "evidence_quality": "sufficient"},
            "r2", "satisfied",
        )
        self.assertEqual(auto["status"], "satisfied")
        self.assertEqual(auto["review_reason"], "")

    def test_review_result_marks_truncated_model_text(self):
        review = worker._review_result_from_model(
            {"status": "satisfied", "evidence": "第57页：", "reason": "结论依据→已填写，但",
             "summary": "", "risk_level": "medium", "confidence": "medium", "evidence_quality": "limited"},
            "r1", "satisfied",
        )
        self.assertEqual(review["status"], "partial")
        self.assertIn("结论输出不完整，需人工复核", review["reason"])
        self.assertIn("结论输出不完整，需人工复核", review["evidence"])
        self.assertEqual(review["conclusion_summary"], "")

    def test_truncated_text_detector(self):
        self.assertTrue(worker._looks_truncated_text("已填写，但"))
        self.assertTrue(worker._looks_truncated_text("第57页："))
        self.assertTrue(worker._looks_truncated_text("计分过程："))
        self.assertFalse(worker._looks_truncated_text("已填写，但需人工核验原件。"))
        self.assertTrue(worker._raw_result_text_incomplete("review", {"evidence": "第57页：", "reason": "完整结论。"}))
        self.assertTrue(worker._raw_result_text_incomplete("objective", {"reason": "完整结论。", "calculation": "合计："}))
        self.assertFalse(worker._raw_result_text_incomplete("review", {"evidence": "第57页内容", "reason": "完整结论。"}))

    def test_score_result_model_reconciles_summary_score(self):
        score = worker._score_result_from_model(
            "r4", 29.72, 30.0,
            {"suggested_score": 29.72, "confidence": "high",
             "summary": "建议价格分29.75分，报价632836元为次低有效报价。"},
        )
        self.assertNotIn("29.75", score["conclusion_summary"])
        self.assertIn("报价632836元", score["conclusion_summary"])

    def test_apply_ocr_summary_overrides_or_clears_conclusion_summary(self):
        rule = {"rule_id": "r1", "title": "证书核验", "scoring_json": json.dumps({"max_score": 6})}
        working = {"rule_id": "r1", "status": "not_found", "evidence": "文字层", "reason": "文字层理由",
                   "risk_level": "medium", "confidence": "medium", "evidence_quality": "limited",
                   "conclusion_summary": "文字层旧结论"}
        payload = {"pages": [1, 2], "service_labels": "本地 RapidOCR", "local_only": True, "failure": "", "incomplete_pages": False}
        parsed = {"status": "satisfied", "summary": "结论：已核验证书并采纳", "evidence": "OCR事实", "reason": "OCR理由",
                  "content_coverage": "covered", "coverage": "covered", "conclusion_scope": "full",
                  "evidence_pages": [1], "risk_level": "low", "confidence": "high"}
        merged = worker._apply_ocr_summary("review", rule, working, parsed, payload)
        self.assertEqual(merged["conclusion_summary"], "已核验证书并采纳")
        merged2 = worker._apply_ocr_summary("review", rule, working, {**parsed, "summary": None}, payload)
        self.assertEqual(merged2["conclusion_summary"], "")

    def test_storage_round_trip_conclusion_summary(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案。")
        review_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "资质"})
        score_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "objective", "title": "资质评分", "scoring": {"kind": "boolean", "max_score": 5}})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        rule_set = storage.current_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all", {"prompt_version": "vision-evidence-contract-v31"})
        storage.update_task(self.app, task["task_id"], status="success")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], "profile-1")
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", "profile-1")
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [{
            "rule_id": review_rule["rule_id"], "status": "not_found", "reason": "未发现",
            "conclusion_summary": "投标有效期承诺缺失，需复核。",
        }])
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [{
            "rule_id": score_rule["rule_id"], "suggested_score": 5, "max_score": 5, "confidence": "high",
            "conclusion_summary": "资质满足，建议满分。",
        }])
        _, review_rows = storage.latest_review_results(self.app, self.project["project_id"])
        _, score_rows = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        self.assertEqual(review_rows[0]["conclusion_summary"], "投标有效期承诺缺失，需复核。")
        self.assertEqual(score_rows[0]["conclusion_summary"], "资质满足，建议满分。")
        reused = storage.reusable_evaluation_document_results(
            self.app, self.project["project_id"], rule_set["rule_set_id"], "profile-1", document["document_id"],
            {"review": {review_rule["rule_id"]}, "objective": {score_rule["rule_id"]}},
            prompt_version="vision-evidence-contract-v31",
        )
        self.assertIsNotNone(reused)
        self.assertEqual(reused["review"][0]["conclusion_summary"], "投标有效期承诺缺失，需复核。")
        self.assertEqual(reused["objective"][0]["conclusion_summary"], "资质满足，建议满分。")

    def test_evidence_pack_reuses_only_confirmed_candidate_pages_and_records_provenance(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "认证证书见第12页。")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "认证评分", "check_rule": "核验认证证书", "scoring": {"max_score": 2},
        })
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        result = {
            "rule_id": rule["rule_id"], "suggested_score": 2, "max_score": 2, "confidence": "high",
            "visual_page_candidates": [12], "ocr_candidate_pages": [12], "ocr_evidence_pages": [12],
            "vision_pages": [12, 13], "vision_evidence_pages": [12], "vision_status": "ocr_vision_applied",
            "evidence": "证书名称可见", "reason": "建议得2分",
            "evidence_layers": [{"source": "tencent_ocr", "summary": "P12识别到证书名称", "checked_pages": [12], "evidence_pages": [12], "service": "高精度版"}],
        }
        scan = {
            "chunks": [{"chunk_id": "chunk-1", "start_page": 10, "end_page": 14}],
            "findings": [{"rule_id": rule["rule_id"], "chunk_id": "chunk-1", "evidence": "第12页列有认证证书", "tentative_status": "supports", "evidence_priority": "high", "confidence": "high"}],
            "evidence_ledger": {rule["rule_id"]: {"candidates": [{"chunk_id": "chunk-1", "source": "scan", "evidence": "第12页列有认证证书"}]}}
        }
        pack = worker._build_shadow_evidence_pack(task, document, "objective", rule, result, scan)
        storage.save_evidence_packs(self.app, self.project["project_id"], task["task_id"], document["document_id"], document["sha256"], [pack])
        saved = storage.list_evidence_packs(self.app, task["task_id"], document["document_id"])

        self.assertEqual(len(saved), 1)
        payload = saved[0]["payload"]
        self.assertTrue(payload["decision_participation"] is False)
        self.assertEqual(payload["mode"], "candidate_pages_only")
        self.assertFalse(payload["acquisition_plan"]["decision_participation"])
        self.assertEqual(payload["acquisition_plan"]["actual_execution"]["ocr_evidence_pages"], [12])
        self.assertIn({"source": "ocr_evidence", "page": 12}, payload["page_provenance"])
        self.assertEqual(payload["ocr_findings"][0]["evidence_pages"], [12])
        self.assertEqual(
            storage.evidence_pack_pages(self.app, document["document_id"], document["sha256"], pack["material_key"]),
            [12],
        )
        # 证据包页码仅作为候选，不携带上一次的分数、状态或理由。
        reused = worker._with_evidence_pack_candidates(self.app, document, rule, {"status": "manual"})
        self.assertEqual(reused["evidence_pack_candidate_pages"], [12])
        self.assertEqual(reused["status"], "manual")

    def test_shadow_acquisition_plan_is_generic_and_does_not_change_result(self):
        rule = {
            "rule_id": "certificate", "title": "材料证明", "check_rule": "核验材料名称、型号、编号和有效期",
            "check_mode": "ocr", "ocr_required": True, "vision_level": "standard",
        }
        result = {"status": "manual", "ocr_candidate_pages": [8], "ocr_evidence_pages": [8]}
        before = dict(result)
        plan = worker._build_shadow_acquisition_plan({"extension": ".pdf", "page_count": 20}, rule, result)
        self.assertEqual(plan["channels"], ["ocr"])
        self.assertEqual(plan["coverage_level"], "standard")
        self.assertIn("关键字段", plan["stop_condition"])
        self.assertEqual(result, before)

    def test_explicit_material_field_policy_only_early_stops_after_multiple_field_signals(self):
        explicit = {
            "rule_id": "generic-certificate", "title": "认证证书核验",
            "check_rule": "核验认证证书名称、编号、日期、型号",
            "execution_meta_json": json.dumps({
                "acquisition_preset": "text", "image_mode": "ocr_only",
                "vision_trigger": "text_fallback", "vision_level": "standard",
                "evidence_requirements": ["document", "field", "text"],
            }),
        }
        material_only = [{"text": "认证证书"}]
        complete = [{"text": "认证证书 编号：ABC-2026-001 有效期至：2028年12月 型号：X100"}]
        self.assertFalse(worker._ocr_discovery_is_sufficient(explicit, material_only))
        self.assertTrue(worker._ocr_discovery_is_sufficient(explicit, complete))

    def test_deleting_project_removes_files_and_related_records(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        source = storage.document_path(self.app, document)
        project_path = storage.project_dir(self.app, self.project["project_id"])

        response = self.app.test_client().delete(f"/api/evaluation-workbench/projects/{self.project['project_id']}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(source.exists())
        self.assertFalse(project_path.exists())
        self.assertIsNone(storage.get_project(self.app, self.project["project_id"]))

    def test_review_uses_confirmed_rules_and_persists_manual_fallback(self):
        self._add_pdf("tender.pdf", "tender", "", "投标人应具备有效资质。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "本公司具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "有效资质", "source_text": "投标人应具备有效资质"})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "review_documents")

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"results": []}):
            finished = self._run_next_task()

        review_run, results = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertIsNotNone(review_run)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["rule_id"], rule["rule_id"])
        self.assertEqual(results[0]["status"], "manual")
        confirmed = storage.update_review_final_status(self.app, results[0]["review_result_id"], "satisfied")
        self.assertEqual(confirmed["final_status"], "satisfied")

    def test_page_context_retrieval_falls_back_when_rule_has_no_local_clue(self):
        parsed = self.temp_dir / "parsed.txt"
        parsed.write_text("[第1页]\n营业执照复印件。\n\n[第2页]\n技术方案和实施计划。\n", encoding="utf-8")

        retrieved = build_rule_context(parsed, [{"title": "营业执照", "source_text": "提供营业执照"}], 1000)
        fallback = build_rule_context(parsed, [{"title": "串通投标", "source_text": "不同投标人由同一单位编制"}], 1000)

        self.assertEqual(retrieved["mode"], "retrieved_pages")
        self.assertIn("营业执照", retrieved["text"])
        self.assertEqual(fallback["mode"], "full_prefix")

    def test_partial_page_context_keeps_matched_rules_without_full_document_fallback(self):
        parsed = self.temp_dir / "parsed-partial.txt"
        parsed.write_text(
            "[第1页]\n营业执照复印件。\n\n[第2页]\n技术方案和实施计划。\n\n[第3页]\n报价明细。\n",
            encoding="utf-8",
        )
        rules = [
            {"rule_id": "matched", "title": "营业执照", "source_text": "提供营业执照"},
            {"rule_id": "unmatched", "title": "串通投标", "source_text": "不同投标人由同一单位编制"},
        ]

        context = build_rule_context(parsed, rules, 1000, allow_partial=True)

        self.assertEqual(context["mode"], "retrieved_pages_partial")
        self.assertEqual(context["unmatched_rule_ids"], ["unmatched"])
        self.assertIn("营业执照", context["text"])
        self.assertNotIn("[第3页]", context["text"])

    def test_performance_rule_keeps_short_section_anchors_and_selects_performance_pages(self):
        parsed = self.temp_dir / "performance-pages.txt"
        parsed.write_text(
            "[第1页]\n目录：近年的类似项目情况表见第10页。\n\n"
            "[第2页]\n响应函。\n\n"
            "[第10页]\n近年的类似项目情况表\n项目名称：无人机航测项目\n发包人：甲单位\n合同价格：59000元。\n",
            encoding="utf-8",
        )
        rule = {
            "rule_id": "performance",
            "title": "商务业绩按数量计分",
            "check_rule": "统计投标截止日期三个年度内同类型项目业绩数量，每个得3分，最高9分。",
            "source_text": "同类型项目业绩每有一个得3分，最高9分",
        }

        chunks = split_full_text_chunks(parsed, target_chars=90, overlap_pages=0)

        self.assertIn("业绩", _anchors(rule))
        self.assertIn("类似项目", _anchors(rule))
        selected = select_rule_chunks(chunks, [rule])
        self.assertTrue(selected)
        self.assertTrue(any("类似项目情况表" in item["text"] for item in chunks if item["chunk_id"] in selected))

    def test_rule_chunk_map_does_not_assign_another_rules_fallback_page(self):
        chunks = [
            {"chunk_id": "chunk-a", "text": "类似项目业绩情况表和合同业绩。"},
            {"chunk_id": "chunk-b", "text": "项目人员持证人员证书情况。"},
        ]
        rules = [
            {"rule_id": "a", "title": "业绩评分", "check_rule": "核验类似项目合同业绩"},
            {"rule_id": "b", "title": "人员评分", "check_rule": "核验持证项目人员"},
        ]

        mapping = select_rule_chunk_map(chunks, rules, per_rule=1)

        self.assertEqual(mapping["a"], ["chunk-a"])
        self.assertEqual(mapping["b"], ["chunk-b"])

    def test_rule_chunk_evidence_map_keeps_anchor_offset_for_long_chunk(self):
        chunks = [{"chunk_id": "chunk-a", "text": "无关前言" * 600 + "\n关键证据：类似项目合同业绩。"}]
        rule = {"rule_id": "a", "title": "业绩评分", "check_rule": "核验类似项目合同业绩"}

        mapping = select_rule_chunk_evidence_map(chunks, [rule], per_rule=1)

        self.assertEqual(mapping["a"][0]["chunk_id"], "chunk-a")
        self.assertGreater(mapping["a"][0]["offset"], 1000)
        self.assertIn("类似项目", mapping["a"][0]["anchor"])

    def test_rule_execution_metadata_is_persisted_and_overrides_legacy_keyword_routing(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "综合材料", "check_rule": "核验材料完整性",
            "execution_strategy": "section", "evidence_requirements": ["text", "visual"],
            "applicability": {"scope": "package", "package_ids": [2]},
        })
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        saved = next(item for item in rules if item["rule_id"] == rule["rule_id"])

        self.assertEqual(saved["execution_strategy"], "section")
        self.assertEqual(saved["evidence_requirements"], ["text", "visual"])
        self.assertEqual(saved["applicability"]["package_ids"], [2])
        self.assertEqual(worker._rule_execution_strategy(saved), "section")
        self.assertFalse(worker._rule_requires_visual_verification(saved))

    def test_material_and_field_evidence_requirements_preserve_text_route(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "检测报告评分", "check_rule": "核验检测报告编号和日期",
            "evidence_requirements": ["document", "field"], "scoring": {"max_score": 2},
        })
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        saved = next(item for item in rules if item["rule_id"] == rule["rule_id"])

        self.assertEqual(saved["evidence_requirements"], ["document", "field", "text"])
        self.assertEqual(worker._rule_image_strategy(saved), "ocr")

    def test_compound_rule_evidence_items_are_persisted_without_creating_child_rules(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "substantive", "title": "关键参数及证明材料",
            "check_rule": "逐项核验两个独立参数及对应证明材料",
            "evidence_items": [
                {"item_id": "a", "name": "参数A", "requirement": "核验参数A的检测报告", "source_page": 12, "evidence_requirements": ["text", "document", "field", "visual"]},
                {"item_id": "b", "name": "参数B", "requirement": "核验参数B的检测报告", "source_page": "13", "evidence_requirements": ["text", "visual"]},
            ],
        })
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        saved = next(item for item in rules if item["rule_id"] == rule["rule_id"])

        self.assertEqual(len(saved["evidence_items"]), 2)
        self.assertEqual(saved["evidence_items"][0]["item_id"], "a")
        self.assertEqual(saved["evidence_items"][1]["source_page"], 13)
        self.assertEqual(len(rules), 1)

    def test_compound_rule_acquisition_rotates_pages_across_evidence_items(self):
        rule = {
            "title": "复合技术参数", "check_rule": "核验各项参数和证明材料",
            "execution_meta_json": json.dumps({"evidence_items": [
                {"item_id": "a", "name": "参数A", "requirement": "参数A 检测报告"},
                {"item_id": "b", "name": "参数B", "requirement": "参数B 检测报告"},
                {"item_id": "c", "name": "参数C", "requirement": "参数C 检测报告"},
            ]}, ensure_ascii=False),
        }
        candidates = {"复合技术参数": [9, 10], "参数A": [3, 4], "参数B": [6, 7], "参数C": [8, 9]}
        with patch.object(worker, "_vision_page_candidates", side_effect=lambda _doc, view, _result: candidates.get(view["title"], [])), \
             patch.object(worker, "_prioritise_material_pages", side_effect=lambda _doc, _rule, pages, **_kwargs: pages):
            plan = worker._compound_acquisition_plan({"extension": ".pdf", "page_count": 12}, rule, {})

        self.assertEqual(plan["candidate_pages"][:3], [3, 6, 8])
        self.assertEqual(worker._compound_acquisition_page_limit(rule, "standard", "ocr", 6), 6)
        six_items = [
            {"item_id": f"item_{index}", "name": f"参数{index}", "requirement": f"核验参数{index}"}
            for index in range(1, 7)
        ]
        expanded_rule = {**rule, "execution_meta_json": json.dumps({"evidence_items": six_items}, ensure_ascii=False)}
        self.assertEqual(worker._compound_acquisition_page_limit(expanded_rule, "standard", "ocr", 6), 8)

    def test_ocr_discovery_does_not_early_stop_counting_or_form_rules(self):
        values = [{"page": 3, "text": "检测报告 编号 ABC-2026"}]
        normal = {"title": "检测报告核验", "check_rule": "核验检测报告编号", "category": "qualification"}
        counting = {**normal, "category": "objective", "scoring_json": json.dumps({"kind": "manual", "max_score": 3, "items": [{"name": "报告", "max_score": 3}]})}

        self.assertTrue(worker._ocr_discovery_is_sufficient(normal, values))
        self.assertFalse(worker._ocr_discovery_is_sufficient(counting, values))
        self.assertEqual(worker._ocr_discovery_page_count(normal, "standard", [1, 2, 3, 4]), 3)
        self.assertEqual(worker._ocr_discovery_page_count(counting, "standard", [1, 2, 3, 4]), 4)

    def test_structured_score_items_participate_in_page_retrieval(self):
        chunks = [
            {"chunk_id": "deployment", "text": "部署实施方案包括安装调试和上线计划。"},
            {"chunk_id": "maintenance", "text": "运维保障方案包括巡检、响应和故障恢复。"},
        ]
        rule = {
            "rule_id": "solution", "category": "subjective", "title": "技术方案评分",
            "check_rule": "按各模块分别评分", "scoring_json": json.dumps({
                "max_score": 6, "items": [
                    {"name": "部署实施方案", "max_score": 3, "criterion": "安装调试和上线计划"},
                    {"name": "运维保障方案", "max_score": 3, "criterion": "巡检和故障恢复"},
                ],
            }, ensure_ascii=False),
        }

        mapping = select_rule_chunk_map(chunks, [rule], per_rule=2)

        self.assertEqual(set(mapping["solution"]), {"deployment", "maintenance"})

    def test_combined_evaluation_persists_original_three_result_types(self):
        self._add_pdf("tender.pdf", "tender", "", "投标人具备资质得5分，技术方案满分10分。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "本公司具备资质，技术方案完整。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        review_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "有效资质", "source_text": "具备资质"})
        objective_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "objective", "title": "资质得分", "source_text": "具备资质得5分", "scoring": {"kind": "boolean", "max_score": 5}})
        subjective_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "subjective", "title": "技术方案", "source_text": "技术方案满分10分", "scoring": {"max_score": 10}})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            {"project_identity": "测试项目", "scope_summary": "资质与技术方案", "service_targets": [], "core_tasks": [],
             "technical_topics": ["技术方案"], "equipment_or_materials": [], "deliverables": [], "standards_or_rules": [], "regions": [], "keywords": ["资质"]},
            {"results": [{"rule_id": review_rule["rule_id"], "status": "satisfied", "evidence": "具备资质", "reason": "已提供",
                          "risk_level": "low", "confidence": "high", "evidence_quality": "sufficient"}]},
            {"results": [{"rule_id": objective_rule["rule_id"], "met": True, "evidence": "具备资质", "reason": "已提供"}]},
            {"results": [{"rule_id": subjective_rule["rule_id"], "suggested_score": 8, "evidence": "技术方案完整", "reason": "较完整"}]},
        ]) as request_json:
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        _, objectives = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        _, subjectives = storage.latest_score_results(self.app, self.project["project_id"], "subjective")
        usage = storage.project_token_usage(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(reviews[0]["status"], "satisfied")
        self.assertEqual(objectives[0]["suggested_score"], 5.0)
        self.assertEqual(subjectives[0]["suggested_score"], 8.0)
        self.assertEqual(usage["call_count"], 4)
        self.assertEqual(usage["input_chars"] > 0, True)
        self.assertEqual(request_json.call_args_list[0].args[0]["thinking_mode"], "disabled")
        self.assertEqual(request_json.call_args_list[1].args[0]["thinking_mode"], "adaptive")
        self.assertEqual(request_json.call_args_list[2].args[0]["thinking_mode"], "disabled")
        self.assertEqual(request_json.call_args_list[3].args[0]["thinking_mode"], "adaptive")

    def test_evaluation_highlights_downgrade_unqualified_critical_and_deduplicate(self):
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {
            ("bid-a", "qualified"): {"_critical_eligible": True},
            ("bid-a", "unqualified"): {"_critical_eligible": False},
        }

        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a", "headline": "存在应优先复核事项",
            "highlights": [
                {"rule_id": "qualified", "level": "critical", "keyword": "**明确否决**", "conclusion": "缺少有效资质", "basis": "证据充分"},
                {"rule_id": "unqualified", "level": "critical", "keyword": "待核验", "conclusion": "材料存在疑点", "basis": "仍需人工确认"},
                {"rule_id": "qualified", "level": "high", "keyword": "重复", "conclusion": "不应重复显示", "basis": "-"},
            ],
        }]}, candidates, allowed)

        self.assertEqual(len(values), 1)
        self.assertEqual(len(values[0]["highlights"]), 2)
        self.assertEqual(values[0]["highlights"][0]["level"], "critical")
        self.assertEqual(values[0]["highlights"][0]["keyword"], "明确否决")
        self.assertEqual(values[0]["highlights"][1]["level"], "high")

    def test_copying_response_table_highlight_is_attention_not_high_risk(self):
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {("bid-a", "copy"): {"_critical_eligible": False, "title": "照抄照搬"}}

        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a", "headline": "存在复核事项",
            "highlights": [{"rule_id": "copy", "level": "high", "keyword": "参数复述", "conclusion": "技术响应表复述参数", "basis": "逐项响应表"}],
        }]}, candidates, allowed)

        self.assertEqual(values[0]["highlights"][0]["level"], "attention")

    def test_scope_template_mixing_highlight_injected_when_model_omits(self):
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {
            ("bid-a", "quote"): {"_critical_eligible": False, "type": "review", "rule_id": "quote",
                                 "status": "not_satisfied", "risk_level": "high",
                                 "title": "报价一致性", "evidence": "第22页金额空白",
                                 "reason": "报价缺失", "conclusion_summary": "报价无法核验"},
            ("bid-a", "scope"): {"_critical_eligible": False, "type": "review", "rule_id": "scope",
                                 "status": "not_satisfied", "risk_level": "high",
                                 "title": "项目范围无关内容核验",
                                 "evidence": "第775-780页高层建筑老虎口与青铅灌浇",
                                 "reason": "正文将建筑工程施工总承包模板作为本项目方案写入→属整章模板混用",
                                 "conclusion_summary": "施工章套用建筑工程总承包模板，工艺对象超出本项目范围"},
        }
        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a", "headline": "报价异常与范围偏离存疑",
            "highlights": [{"rule_id": "quote", "level": "high", "keyword": "报价缺失",
                            "conclusion": "报价无法核验", "basis": "第22页金额空白"}],
        }]}, candidates, allowed)
        self.assertEqual(len(values[0]["highlights"]), 2)
        injected = next(item for item in values[0]["highlights"] if item["rule_id"] == "scope")
        self.assertEqual(injected["level"], "high")
        self.assertIn("施工章套用建筑工程总承包模板", injected["conclusion"])
        self.assertIn("第775-780页", injected["basis"])

    def test_scope_highlight_fallback_skips_low_risk_or_absent_marker(self):
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {
            ("bid-a", "weak"): {"_critical_eligible": False, "type": "review",
                                "status": "partial", "risk_level": "medium",
                                "title": "项目范围无关内容核验",
                                "evidence": "第775页出现通用施工规范", "reason": "与项目语境存在差异",
                                "conclusion_summary": "部分内容需复核"},
        }
        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a", "headline": "无",
            "highlights": [{"rule_id": "weak", "level": "attention", "keyword": "需复核",
                            "conclusion": "部分内容需复核", "basis": "第775页"}],
        }]}, candidates, allowed)
        self.assertEqual(len(values[0]["highlights"]), 1)

    def test_scope_highlight_fallback_matches_out_of_scope_phrasing(self):
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {
            ("bid-a", "scope"): {"_critical_eligible": False, "type": "review", "rule_id": "scope",
                                 "status": "not_satisfied", "risk_level": "high",
                                 "title": "项目范围无关内容核验",
                                 "evidence": "第740-794页出现老虎口/青铅灌浇、暴雨应急预案等章节",
                                 "reason": "上述施工对象/工艺均超出本项目网络与机房模块化改造范围，"
                                           "且被作为本项目施工方案写入→范围偏离候选集中体现",
                                 "conclusion_summary": "施工章套用建筑工程总承包模板，工艺对象超出本项目范围"},
        }
        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a", "headline": "范围偏离存疑", "highlights": [],
        }]}, candidates, allowed)
        self.assertEqual(len(values[0]["highlights"]), 1)
        self.assertEqual(values[0]["highlights"][0]["level"], "high")
        self.assertIn("第740-794页", values[0]["highlights"][0]["basis"])

    def test_highlights_fallback_covers_bidder_missed_by_model(self):
        candidates = [
            {"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []},
            {"document_id": "bid-b", "bidder_name": "乙公司", "candidates": []},
        ]
        allowed = {
            ("bid-a", "scope"): {"_critical_eligible": False, "type": "review", "rule_id": "scope",
                                 "status": "not_satisfied", "risk_level": "high", "title": "项目范围无关内容核验",
                                 "evidence": "第561-569页出现华福证券内容", "reason": "与本项目范围不符→属模板混用",
                                 "conclusion_summary": "投标文件混入其他项目范围内容"},
            ("bid-b", "consistency"): {"_critical_eligible": False, "type": "review", "rule_id": "consistency",
                                       "status": "not_satisfied", "risk_level": "high", "title": "关键要素内部一致性",
                                       "evidence": "第370页变更阈值5%", "reason": "变更阈值5%与招标20%不一致",
                                       "conclusion_summary": "变更管理阈值与招标要求不一致"},
            ("bid-b", "qual"): {"_critical_eligible": False, "type": "review", "rule_id": "qual",
                                "status": "partial", "risk_level": "medium", "title": "投标人资格要求",
                                "evidence": "第16页目录", "reason": "部分资格材料待核验",
                                "conclusion_summary": "资格材料部分缺失待核验"},
            ("bid-b", "score"): {"_critical_eligible": False, "type": "score", "rule_id": "score",
                                 "title": "管理体系认证评分", "suggested_score": 1.5, "max_score": 3.0,
                                 "evidence": "第15页", "reason": "仅一项证书可确认",
                                 "conclusion_summary": "认证证书证据不足"},
        }
        values = worker._normalise_evaluation_highlights({"summaries": [{
            "document_id": "bid-a", "headline": "范围混用需复核",
            "highlights": [{"rule_id": "scope", "level": "high", "keyword": "范围混用",
                            "conclusion": "投标文件混入其他项目范围内容", "basis": "第561-569页"}],
        }]}, candidates, allowed)
        by_bidder = {item["bidder_name"]: item for item in values}
        self.assertEqual(set(by_bidder.keys()), {"甲公司", "乙公司"})
        # 模型已返回的家保持原样，不重复生成。
        self.assertEqual(len(by_bidder["甲公司"]["highlights"]), 1)
        # 被漏掉的家自动兜底，最多 3 条且按重要程度排序。
        fallback = by_bidder["乙公司"]
        self.assertLessEqual(len(fallback["highlights"]), 3)
        self.assertEqual(fallback["highlights"][0]["level"], "high")
        self.assertIn("第370页", fallback["highlights"][0]["basis"])
        self.assertEqual(fallback["overall_level"], "high")

    def test_highlights_fallback_skips_bidder_without_qualified_candidates(self):
        candidates = [{"document_id": "bid-c", "bidder_name": "丙公司", "candidates": []}]
        allowed = {
            ("bid-c", "scan"): {"_critical_eligible": False, "type": "review", "rule_id": "scan",
                                "status": "ocr_required", "risk_level": "low", "title": "资格核验",
                                "evidence": "", "reason": "扫描件待识别", "conclusion_summary": ""},
            ("bid-c", "score"): {"_critical_eligible": False, "type": "score", "rule_id": "score",
                                 "title": "业绩评分", "suggested_score": 8, "max_score": 10,
                                 "evidence": "", "reason": "正常", "conclusion_summary": ""},
        }
        values = worker._normalise_evaluation_highlights({"summaries": []}, candidates, allowed)
        self.assertEqual(values, [])

    def test_highlights_fallback_level_mapping(self):
        self.assertEqual(worker._fallback_highlight_level(
            {"type": "review", "status": "not_satisfied", "risk_level": "high"}), "high")
        self.assertEqual(worker._fallback_highlight_level(
            {"type": "review", "status": "not_found", "risk_level": "medium"}), "high")
        self.assertEqual(worker._fallback_highlight_level(
            {"type": "review", "status": "partial", "risk_level": "medium"}), "attention")
        self.assertEqual(worker._fallback_highlight_level(
            {"type": "score", "suggested_score": 1, "max_score": 10}), "attention")
        # 部分满足+中风险应能进入兜底（条数上限内）。
        candidates = [{"document_id": "bid-a", "bidder_name": "甲公司", "candidates": []}]
        allowed = {
            ("bid-a", "r1"): {"_critical_eligible": False, "type": "review", "rule_id": "r1",
                              "status": "not_satisfied", "risk_level": "high", "title": "资格要求",
                              "reason": "未提供证明", "conclusion_summary": "资格证明缺失"},
            ("bid-a", "r3"): {"_critical_eligible": False, "type": "review", "rule_id": "r3",
                              "status": "partial", "risk_level": "medium", "title": "技术方案",
                              "reason": "部分覆盖", "conclusion_summary": "方案部分覆盖"},
        }
        values = worker._normalise_evaluation_highlights({"summaries": []}, candidates, allowed)
        levels = {item["rule_id"]: item["level"] for item in values[0]["highlights"]}
        self.assertEqual(levels["r1"], "high")
        self.assertEqual(levels["r3"], "attention")

    def test_copying_response_rule_is_low_risk_in_review_results(self):
        rules = [{"rule_id": "copy", "title": "技术响应照抄照搬核验", "check_rule": "检查是否照抄招标参数"}]
        values = worker._normalise_review_results([{
            "rule_id": "copy", "status": "not_satisfied", "risk_level": "high", "confidence": "high",
            "evidence": "技术响应表逐项复述采购参数", "reason": "参数存在照抄。",
        }], rules)

        self.assertEqual(values[0]["status"], "partial")
        self.assertEqual(values[0]["risk_level"], "low")
        self.assertIn("正常对照", values[0]["reason"])
        self.assertIn("参数存在照抄", values[0]["reason"])

    def test_technical_source_baseline_downgrades_inherited_parameter_noise(self):
        rules = [{
            "rule_id": "parameter", "title": "技术参数前后一致性", "check_rule": "检查技术参数是否矛盾或不一致",
        }]
        values = worker._normalise_review_results([{
            "rule_id": "parameter", "status": "not_satisfied", "risk_level": "high", "confidence": "high",
            "evidence": "频率响应 Frequency Response：40-20Hz", "reason": "参数表述异常。",
        }], rules, "采购需求：频率响应[Frequency Response]：40-20Hz。")

        self.assertEqual(values[0]["status"], "partial")
        self.assertEqual(values[0]["risk_level"], "low")
        self.assertIn("招标原文", values[0]["reason"])

    def test_latest_review_results_exposes_saved_important_highlights(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "资质材料。")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "有效资质", "check_rule": "缺失资质将导致投标无效",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], None)
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "status": "not_satisfied", "risk_level": "high",
            "confidence": "high", "evidence_quality": "sufficient", "evidence": "未见有效资质",
        }])
        storage.update_task(self.app, task["task_id"], status="success", result={"highlights": [{
            "document_id": document["document_id"], "bidder_name": "甲公司", "overall_level": "critical",
            "highlights": [{"rule_id": rule["rule_id"], "level": "critical", "keyword": "明确否决", "conclusion": "未见有效资质", "basis": "证据充分"}],
        }]})

        review_run, rows = storage.latest_review_results(self.app, self.project["project_id"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(review_run["highlights"][0]["bidder_name"], "甲公司")
        self.assertEqual(review_run["highlights"][0]["highlights"][0]["keyword"], "明确否决")

    def test_highlight_failure_does_not_fail_completed_evaluation(self):
        self._add_pdf("tender.pdf", "tender", "", "投标人未提供有效资质的，作无效投标处理。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "未附有效资质材料。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "有效资质", "check_rule": "未提供有效资质的，作无效投标处理。",
            "source_text": "投标人须提供有效资质。",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            {"project_identity": "测试项目", "scope_summary": "资质审查", "service_targets": [], "core_tasks": [],
             "technical_topics": [], "equipment_or_materials": [], "deliverables": [], "standards_or_rules": [], "regions": [], "keywords": ["资质"]},
            {"results": [{"rule_id": rule["rule_id"], "status": "not_satisfied", "evidence": "未附有效资质材料",
                          "reason": "未见证明文件", "risk_level": "high", "confidence": "high", "evidence_quality": "sufficient"}]},
            RuntimeError("important-summary unavailable"),
        ]):
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["highlight_failure_count"], 1)
        self.assertEqual(len(reviews), 1)

    def test_combined_evaluation_runs_two_bidders_with_bounded_parallelism(self):
        self._add_pdf("bid-a.pdf", "bid", "甲公司", "甲公司具备有效资质。")
        self._add_pdf("bid-b.pdf", "bid", "乙公司", "乙公司具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "有效资质", "source_text": "具备有效资质",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        active = peak = 0
        lock = threading.Lock()

        def response(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"results": [{"rule_id": rule["rule_id"], "status": "satisfied", "evidence": "有效资质", "reason": "已提供"}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=response):
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(peak, 2)
        self.assertEqual(len(reviews), 2)

    def test_running_evaluation_exposes_only_completed_document_ids(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "甲公司具备有效资质。")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "有效资质", "source_text": "具备有效资质",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        profile = storage.get_model_profile(self.app, None)
        run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], profile["profile_id"])
        storage.save_review_results(self.app, run["review_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "status": "satisfied", "evidence": "有效资质", "reason": "已提供",
        }])
        storage.update_task(self.app, task["task_id"], status="running", result={
            "partial": True, "completed_documents": [{"document_id": document["document_id"], "bidder_name": "甲公司"}],
        })

        review_run, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        summary = next(item for item in storage.list_task_summaries(self.app, self.project["project_id"]) if item["task_id"] == task["task_id"])

        self.assertEqual(review_run["task_status"], "running")
        self.assertEqual(review_run["completed_document_ids"], [document["document_id"]])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(summary["completed_documents"][0]["document_id"], document["document_id"])

    def test_queued_task_exposes_running_project_progress_without_result_content(self):
        running = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        self.assertEqual(storage.next_queued_task(self.app)["task_id"], running["task_id"])
        storage.update_task(self.app, running["task_id"], progress=42, message="正在提取资格规则")
        second_project = storage.create_project(self.app, "另一项目", "TEST-02", "包2")
        waiting = storage.create_task(self.app, second_project["project_id"], "evaluate_all")

        contexts = storage.task_queue_contexts(self.app, second_project["project_id"])

        self.assertEqual(contexts[waiting["task_id"]]["waiting_count"], 1)
        active = contexts[waiting["task_id"]]["active_task"]
        self.assertEqual(active["project_name"], "评标测试项目")
        self.assertEqual(active["progress"], 42)
        self.assertNotIn("result_json", active)

    def test_project_status_keeps_tasks_compatible_and_adds_queue_context(self):
        running = storage.create_task(self.app, self.project["project_id"], "extract_rules")
        storage.next_queued_task(self.app)
        storage.update_task(self.app, running["task_id"], progress=55, message="正在处理规则")
        second_project = storage.create_project(self.app, "排队项目", section_name="包1")
        waiting = storage.create_task(self.app, second_project["project_id"], "evaluate_all")

        with patch.object(evaluation_workbench_module, "_start_worker_if_needed"):
            response = self.app.test_client().get(f"/api/evaluation-workbench/projects/{second_project['project_id']}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("tasks", payload)
        context = payload["queue_contexts"][waiting["task_id"]]
        self.assertEqual(context["waiting_count"], 1)
        self.assertEqual(context["active_task"]["project_name"], "评标测试项目")

    def test_long_document_is_fully_scanned_before_rule_group_synthesis(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "近年的类似项目情况表：项目一。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "商务业绩按数量计分",
            "check_rule": "每个同类型项目得3分，最高9分。",
            "source_text": "每个同类型项目得3分，最高9分。",
            "scoring": {"kind": "manual", "max_score": 9},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        with patch.object(worker, "FULL_SCAN_THRESHOLD_CHARS", 1), patch.object(worker, "FULL_SCAN_CHUNK_CHARS", 100_000), patch(
            "dashboard.evaluation_workbench.worker.request_json",
            side_effect=[
                {"findings": [{"rule_id": rule["rule_id"], "evidence": "项目一", "page_hint": "1", "observation": "发现一项业绩", "matched_count": 1}]},
                {"results": [{"rule_id": rule["rule_id"], "suggested_score": 3, "matched_count": 1,
                              "evidence_items": [{"name": "项目一", "page_hint": "1", "validity": "valid", "reason": "同类型"}],
                              "calculation": "1项×3分=3分", "reason": "建议得3分", "confidence": "high"}]},
                {"summaries": []},
            ],
        ) as request_json:
            finished = self._run_next_task()

        _, results = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["full_scan_document_count"], 1)
        self.assertEqual(finished["result"]["full_scan_batch_count"], 1)
        self.assertEqual(request_json.call_count, 3)
        self.assertIn("全文证据扫描", request_json.call_args_list[0].args[2])
        self.assertEqual(results[0]["suggested_score"], 3.0)
        self.assertIn("AI共识别1项", results[0]["evidence"])
        self.assertIn("项目一", results[0]["evidence"])
        calculation_layers = [
            layer.get("summary") for layer in (results[0].get("evidence_layers") or [])
            if layer.get("source") == "score_calculation"
        ]
        self.assertIn("1项×3分=3分", calculation_layers)
        self.assertNotIn("计分过程", results[0]["reason"])

    def test_cross_bid_price_rule_is_recalculated_with_all_bidders(self):
        bid_a = self._add_pdf("a.pdf", "bid", "甲公司", "投标报价：100万元。")
        bid_b = self._add_pdf("b.pdf", "bid", "乙公司", "投标报价：120万元。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "最低价报价得分",
            "check_rule": "投标报价得分=（评标基准价／投标报价）×10，保留两位小数。",
            "source_text": "最低评审价得10分", "scoring": {"kind": "manual", "max_score": 10},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        cross = {"results": [
            {"document_id": bid_a["document_id"], "rule_id": rule["rule_id"], "quoted_price": 100,
             "suggested_score": 10, "evidence": "投标报价100万元", "calculation": "100/100×10=10", "confidence": "high"},
            {"document_id": bid_b["document_id"], "rule_id": rule["rule_id"], "quoted_price": 120,
             "suggested_score": 8.32, "evidence": "投标报价120万元", "calculation": "100/120×10=8.32", "confidence": "high"},
        ]}
        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=cross) as request_json:
            finished = self._run_next_task()

        _, results = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        scores = {item["bidder_name"]: item["suggested_score"] for item in results}
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["cross_bid_price"]["result_count"], 2)
        self.assertEqual(scores["甲公司"], 10.0)
        self.assertEqual(scores["乙公司"], 8.33)
        self.assertEqual(request_json.call_count, 1)
        self.assertIn(bid_a["document_id"], request_json.call_args.args[2])
        self.assertIn(bid_b["document_id"], request_json.call_args.args[2])

    def test_cross_bid_price_uses_unique_local_total_quote_when_model_omits_one_price(self):
        bid_a = self._add_pdf("a.pdf", "bid", "甲公司", "总报价：￥100000元。")
        bid_b = self._add_pdf("b.pdf", "bid", "乙公司", "投标总报价（大写）壹拾贰万元整 ￥：120000元。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        # PDF 测试夹具的默认字体不保证中文可提取；以解析完成后的真实文本缓存模拟
        # 线上解析器输出，专门覆盖本地“唯一总报价”回填分支。
        local_a = self.temp_dir / "quote-a.txt"
        local_b = self.temp_dir / "quote-b.txt"
        local_a.write_text("[第1页]\n投标总报价：￥100000元。\n", encoding="utf-8")
        local_b.write_text("[第1页]\n投标总报价（大写）壹拾贰万元整 ￥：120000元。\n", encoding="utf-8")
        with storage.connection(self.app) as conn:
            conn.execute("UPDATE ew_documents SET parsed_path=? WHERE document_id=?", (str(local_a), bid_a["document_id"]))
            conn.execute("UPDATE ew_documents SET parsed_path=? WHERE document_id=?", (str(local_b), bid_b["document_id"]))
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "最低价报价得分",
            "check_rule": "投标报价得分=（评标基准价／投标报价）×10，保留两位小数。",
            "source_text": "最低评审价得10分", "scoring": {"kind": "manual", "max_score": 10},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        cross = {"results": [
            {"document_id": bid_a["document_id"], "rule_id": rule["rule_id"], "quoted_price": 100000,
             "suggested_score": 10, "evidence": "总报价100000元", "confidence": "high"},
            {"document_id": bid_b["document_id"], "rule_id": rule["rule_id"], "quoted_price": None,
             "suggested_score": None, "evidence": "已核对开标一览表", "confidence": "medium"},
        ]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=cross):
            finished = self._run_next_task()

        _, results = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        scores = {item["bidder_name"]: item["suggested_score"] for item in results}
        self.assertEqual(finished["status"], "success")
        self.assertEqual(scores["甲公司"], 10.0)
        self.assertEqual(scores["乙公司"], 8.33)

    def test_form_candidates_include_adjacent_continuation_pages(self):
        parsed = self.temp_dir / "statement.txt"
        parsed.write_text(
            "[第314页]\n中小企业声明函\n1. 货物甲，所属行业工业。\n"
            "[第315页]\n2. 电子绘画板（教师）、电子绘画板（学生），企业类型中型企业。\n"
            "[第316页]\n投标人（盖章） 日期。\n"
            "[第317页]\n第九章 合同文本\n",
            encoding="utf-8",
        )
        document = {"document_id": "statement", "parsed_path": str(parsed), "extension": ".pdf", "page_count": 317}
        rule = {"title": "中小企业声明函填写完整性", "check_rule": "核对中小企业声明函填写内容"}
        result = {"evidence": "第314页中小企业声明函", "reason": "需要核对后续填写项"}

        pages = worker._vision_page_candidates(document, rule, result)

        self.assertIn(314, pages)
        self.assertIn(315, pages)
        self.assertIn(316, pages)
        self.assertNotIn(317, pages)
        self.assertEqual(worker._form_bundle_page_limit(rule, "low", pages, 2), 3)

    def test_manual_objective_score_can_be_calculated_from_matched_count(self):
        payload = [{
            "rule_id": "performance", "title": "业绩评分", "check_rule": "每个业绩得3分，最高9分",
            "source_text": "每个业绩得3分，最高9分", "scoring": {"kind": "manual", "max_score": 9},
        }]

        results = worker._normalise_score_results(
            [{"rule_id": "performance", "matched_count": 2, "evidence": "项目甲、项目乙", "reason": "均为同类项目"}],
            payload, "objective",
        )

        self.assertEqual(results[0]["suggested_score"], 6.0)
        self.assertIn("AI共识别2项", results[0]["evidence"])

    def test_score_result_inherits_ocr_requirement_from_rule_payload(self):
        payload = [{
            "rule_id": "license", "title": "许可证评分", "check_rule": "核验许可证图片",
            "source_text": "提供许可证得5分", "ocr_required": True,
            "scoring": {"kind": "boolean", "max_score": 5},
        }]

        results = worker._normalise_score_results(
            [{"rule_id": "license", "met": True, "evidence": "目录列有许可证", "confidence": "high", "needs_ocr": False}],
            payload, "objective",
        )

        self.assertEqual(results[0]["suggested_score"], 5.0)
        self.assertTrue(results[0]["requires_review"])
        self.assertIsNone(results[0]["effective_score"])
        self.assertNotIn("OCR", results[0]["reason"])
        self.assertIn("复核", results[0]["review_reason"])

    def test_cross_bid_price_failure_never_leaves_local_provisional_score(self):
        bid_a = self._add_pdf("price-a.pdf", "bid", "甲公司", "投标报价：100万元。")
        bid_b = self._add_pdf("price-b.pdf", "bid", "乙公司", "投标报价：120万元。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "最低价报价得分",
            "check_rule": "最低评审价得10分，其他报价按最低价比例得分。",
            "source_text": "最低评审价得10分", "scoring": {"kind": "manual", "max_score": 10},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            worker.InvalidJsonResponse("{\"results\":[", "length"),
            worker.InvalidJsonResponse("{\"results\":[", "length"),
        ]) as request_json:
            finished = self._run_next_task()

        _, results = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(request_json.call_count, 2)
        self.assertEqual({item["document_id"] for item in results}, {bid_a["document_id"], bid_b["document_id"]})
        self.assertTrue(all(item["suggested_score"] is None for item in results))
        self.assertTrue(all("暂无法计算" in item["reason"] for item in results))

    def test_scope_anomaly_keeps_any_late_off_topic_content_for_final_review(self):
        chunks = [
            {"chunk_id": "chunk_1", "start_page": 1, "end_page": 20,
             "text": "[第1页]\n无人机航测服务项目。"},
            {"chunk_id": "chunk_12", "start_page": 121, "end_page": 130,
             "text": "[第127页]\n锅炉燃烧控制设备安装与蒸汽管网调试方案。"},
        ]
        scan = {
            "chunks": chunks, "findings": [], "failed_chunks": [], "chunk_count": 2,
            "project_scope": {"scope_summary": "无人机航测服务", "technical_topics": ["无人机航测"]},
            "scope_anomalies": [{"chunk_id": "chunk_12", "dimension": "无关技术与设备", "candidate_priority": "high",
                                  "evidence": "锅炉燃烧控制设备安装", "relation": "与无人机航测无关", "observation": "需核验"}],
        }
        rules = [{
            "rule_id": "unrelated", "category": "other", "title": "投标文件出现无关内容",
            "check_rule": "全文核对无关项目名称及技术方案矛盾", "source_text": "",
        }]
        context = worker._full_scan_review_context(scan, rules, 20_000)
        self.assertIn("锅炉燃烧控制设备安装", context["text"])
        self.assertIn("chunk_12", context["pages"])

    def test_scope_anomaly_context_is_not_injected_into_unrelated_review_group(self):
        scan = {
            "chunks": [{"chunk_id": "chunk_9", "start_page": 81, "end_page": 90,
                        "text": "锅炉燃烧控制设备安装与蒸汽管网调试方案。"}],
            "findings": [], "failed_chunks": [], "chunk_count": 1,
            "project_scope": {"scope_summary": "无人机航测服务"},
            "scope_anomalies": [{"chunk_id": "chunk_9", "dimension": "技术对象偏离",
                                  "candidate_priority": "high", "evidence": "锅炉燃烧控制设备安装",
                                  "relation": "项目范围未包含该设备"}],
        }
        rules = [{"rule_id": "license", "category": "qualification", "title": "营业执照",
                  "check_rule": "检查营业执照", "source_text": ""}]

        context = worker._full_scan_review_context(scan, rules, 8_000)

        self.assertNotIn("项目范围偏离候选", context["text"])
        self.assertNotIn("锅炉燃烧控制设备安装", context["text"])

    def test_scope_anomaly_pages_injected_into_flaw_scoring_subjective_group(self):
        chunks = [{
            "chunk_id": "chunk_37", "start_page": 775, "end_page": 785,
            "text": "[第775页]\n针对高层建筑工程的特点，对于留洞口、电梯井、管道井等“老虎口”必须设置栏杆。\n"
                    "[第776页]\n上层灌浇青铅，下层不得有人作业。",
        }]
        scan = {
            "chunks": chunks, "findings": [], "failed_chunks": [], "chunk_count": 1,
            "project_scope": {"scope_summary": "办公区机房与网络改造"},
            "scope_anomalies": [{
                "chunk_id": "chunk_37", "page_hint": "775", "dimension": "高层建筑洞口与青铅灌浇",
                "candidate_priority": "high", "evidence": "针对高层建筑工程…“老虎口”…灌浇青铅",
                "relation": "与本项目机房场景不符",
            }],
        }
        flaw_rule = {
            "rule_id": "safety", "category": "subjective", "title": "安全文明施工保障措施方案主观评分",
            "check_rule": "无瑕疵得6分；存在瑕疵按分档扣分，瑕疵定义同服务要求条款", "source_text": "",
            "scoring_json": json.dumps({"max_score": 6, "kind": "manual", "items": [
                {"name": "施工保障措施方案瑕疵档得分", "max_score": 6, "criterion": "按瑕疵档打分"}]}),
        }
        context = worker._full_scan_review_context(scan, [flaw_rule], 30_000)
        self.assertIn("青铅灌浇", context["text"])
        self.assertIn("chunk_37", context["pages"])
        self.assertIn("范围偏离候选提示", context["text"])

        plain_rule = {
            "rule_id": "params", "category": "subjective", "title": "技术参数响应情况主观评分",
            "check_rule": "按指标逐项应答与证明材料齐全情况评分", "source_text": "",
            "scoring_json": json.dumps({"max_score": 35}),
        }
        context2 = worker._full_scan_review_context(scan, [plain_rule], 30_000)
        self.assertNotIn("青铅灌浇", context2["text"])
        self.assertNotIn("范围偏离候选提示", context2["text"])

    def test_flaw_subjective_group_limits_scope_anomaly_chunks_to_two(self):
        chunks = [
            {"chunk_id": "chunk_a", "start_page": 10, "end_page": 20, "text": "页面甲内容。"},
            {"chunk_id": "chunk_b", "start_page": 40, "end_page": 50, "text": "页面乙内容。"},
            {"chunk_id": "chunk_c", "start_page": 80, "end_page": 90, "text": "页面丙内容。"},
        ]
        scan = {
            "chunks": chunks, "findings": [], "failed_chunks": [], "chunk_count": 3,
            "project_scope": {"scope_summary": "机房与网络改造"},
            "scope_anomalies": [
                {"chunk_id": "chunk_a", "dimension": "偏离A", "candidate_priority": "high",
                 "evidence": "偏离A原文", "relation": "与本项目不符"},
                {"chunk_id": "chunk_b", "dimension": "偏离B", "candidate_priority": "medium",
                 "evidence": "偏离B原文", "relation": "与本项目不符"},
                {"chunk_id": "chunk_c", "dimension": "偏离C", "candidate_priority": "medium",
                 "evidence": "偏离C原文", "relation": "与本项目不符"},
            ],
        }
        flaw_rule = {
            "rule_id": "safety", "category": "subjective", "title": "安全文明施工保障措施方案主观评分",
            "check_rule": "无瑕疵得6分；存在瑕疵按分档扣分", "source_text": "",
            "scoring_json": json.dumps({"max_score": 6, "kind": "manual", "items": [
                {"name": "瑕疵档得分", "max_score": 6, "criterion": "按瑕疵档打分"}]}),
        }
        context = worker._full_scan_review_context(scan, [flaw_rule], 30_000)
        included = [page for page in context["pages"] if page in {"chunk_a", "chunk_b", "chunk_c"}]
        self.assertLessEqual(len(included), 2)
        self.assertIn("chunk_a", included)

    def test_consistency_extraction_and_evidence_guidance_present(self):
        from dashboard.evaluation_workbench.prompt_templates import PROMPT_TEMPLATES
        # A：提取环节保护表格/表单完整性事实不被合并删除。
        self.assertIn("表格/表单完整性事实", PROMPT_TEMPLATES["extract_rules_validation_guidance"]["content"])
        self.assertIn("偏差表日期/金额/税率等字段", PROMPT_TEMPLATES["extract_rules_validation_guidance"]["content"])
        # C：一致性类规则证据未覆盖全部位置时不得判 satisfied。
        self.assertIn("未覆盖同一事实的全部出现位置", PROMPT_TEMPLATES["evaluate_all_review_user"]["content"])

    def test_consistency_signal_chunks_recalls_value_pages(self):
        chunks = [
            {"chunk_id": "chunk_a", "text": "普通技术方案正文，无特殊数值。"},
            {"chunk_id": "chunk_b", "text": "对于整体需求变化在总工作量5%以内的调整，不再额外收取费用。"},
            {"chunk_id": "chunk_c", "text": "投标总报价699690.00元，投标有效期90天，日期2026年7月27日。"},
            {"chunk_id": "chunk_d", "text": "证书编号ABC123，有效期至2027-06-30。"},
        ]
        rule = {"rule_id": "r1", "title": "投标文件关键要素内部一致性",
                "check_rule": "核对关键要素与主要响应口径前后是否一致", "execution_strategy": "consistency"}
        extra = worker._consistency_signal_chunks(chunks, [rule], per_rule_limit=2)
        self.assertLessEqual(len(extra), 2)
        self.assertEqual(extra[0], "chunk_b")
        self.assertIn("chunk_c", extra)
        # 非 consistency 策略不触发信号页召回。
        plain = {"rule_id": "r2", "title": "技术方案", "check_rule": "按方案完整性评分",
                 "execution_strategy": "section"}
        self.assertEqual(worker._consistency_signal_chunks(chunks, [plain], per_rule_limit=2), [])

    def test_full_scan_context_adds_consistency_signal_chunks_only_for_normal_run(self):
        scan = {
            "chunks": [
                {"chunk_id": "chunk_a", "start_page": 1, "end_page": 10, "text": "普通技术方案正文。"},
                {"chunk_id": "chunk_b", "start_page": 360, "end_page": 370,
                 "text": "变更管理：工作量变动在整体工作量5%以内不再额外收取费用。"},
                {"chunk_id": "chunk_c", "start_page": 460, "end_page": 469,
                 "text": "投标总报价699690.00元，有效期90天，日期2026年。"},
            ],
            "findings": [], "failed_chunks": [], "chunk_count": 3,
            "scope_anomalies": [], "project_scope": {},
        }
        rule = {"rule_id": "r1", "category": "other", "title": "投标文件关键要素内部一致性",
                "check_rule": "核对关键要素与主要响应口径前后是否一致", "execution_strategy": "consistency"}
        context = worker._full_scan_review_context(scan, [rule], 30_000)
        self.assertIn("chunk_b", context["pages"])
        self.assertIn("chunk_c", context["pages"])
        # 补评保持原行为：不额外补信号页。
        targeted = worker._full_scan_review_context(scan, [rule], 30_000, targeted=True)
        self.assertNotIn("chunk_b", targeted["pages"])
        self.assertNotIn("chunk_c", targeted["pages"])

    def test_full_scan_context_reserves_raw_evidence_for_each_rule(self):
        late_a = "资质证书编号A-2026，满足资格条件。"
        late_b = "项目业绩B-2025，累计业绩证明齐全。"
        scan = {
            "chunks": [
                {"chunk_id": "chunk_1", "start_page": 1, "end_page": 10, "text": "甲" * 8_000 + late_a + "甲" * 2_000},
                {"chunk_id": "chunk_2", "start_page": 11, "end_page": 20, "text": "乙" * 8_000 + late_b + "乙" * 2_000},
            ],
            "findings": [
                {"rule_id": "rule-a", "chunk_id": "chunk_1", "page_hint": "8", "evidence": late_a,
                 "tentative_status": "supports", "evidence_priority": "high", "confidence": "high"},
                {"rule_id": "rule-b", "chunk_id": "chunk_2", "page_hint": "18", "evidence": late_b,
                 "tentative_status": "supports", "evidence_priority": "high", "confidence": "high"},
            ],
            "failed_chunks": [], "chunk_count": 2, "scope_anomalies": [], "project_scope": {},
        }
        rules = [
            {"rule_id": "rule-a", "category": "qualification", "title": "有效资质", "check_rule": "核验资质证书"},
            {"rule_id": "rule-b", "category": "objective", "title": "类似业绩", "check_rule": "核验业绩数量"},
        ]

        context = worker._full_scan_review_context(scan, rules, 12_000)

        self.assertIn(late_a, context["text"])
        self.assertIn(late_b, context["text"])
        self.assertEqual(context["pages"][:2], ["chunk_1", "chunk_2"])
        self.assertLessEqual(len(context["text"]), 12_000)

    def test_targeted_full_scan_context_keeps_direct_evidence_with_small_budget(self):
        evidence = "第88页：营业执照统一社会信用代码为91310000TEST。"
        scan = {
            "chunks": [
                {"chunk_id": "chunk_1", "start_page": 1, "end_page": 40, "text": "甲" * 45_000},
                {"chunk_id": "chunk_2", "start_page": 41, "end_page": 100,
                 "text": "乙" * 20_000 + evidence + "乙" * 20_000},
            ],
            "findings": [{"rule_id": "license", "chunk_id": "chunk_2", "page_hint": "88", "evidence": evidence,
                          "tentative_status": "supports", "evidence_priority": "high", "confidence": "high"}],
            "failed_chunks": [], "chunk_count": 2, "scope_anomalies": [], "project_scope": {},
        }
        rules = [{"rule_id": "license", "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照"}]

        context = worker._full_scan_review_context(scan, rules, 14_000, targeted=True)

        self.assertIn(evidence, context["text"])
        self.assertEqual(context["pages"], ["chunk_2"])
        self.assertLessEqual(len(context["text"]), 14_000)

    def test_review_normalisation_marks_only_explicit_ocr_gap_as_ocr_required(self):
        rules = [
            {"rule_id": "image", "check_mode": "ocr"},
            {"rule_id": "procedure", "check_mode": "auto"},
        ]
        output = [
            {"rule_id": "image", "status": "manual", "reason": "营业执照扫描件需 OCR 识别"},
            {"rule_id": "procedure", "status": "manual", "reason": "需要人工确认是否已签字盖章"},
        ]

        results = worker._normalise_review_results(output, rules)

        self.assertEqual(results[0]["status"], "ocr_required")
        self.assertEqual(results[0]["risk_level"], "low")
        self.assertEqual(results[1]["status"], "manual")

        missing = worker._normalise_review_results([], [rules[0]])
        self.assertEqual(missing[0]["status"], "ocr_required")

    def test_review_normalisation_keeps_text_partial_when_only_one_subfact_needs_ocr(self):
        rule = {"rule_id": "coverage", "check_mode": "auto", "title": "技术服务逐项响应覆盖"}
        output = [{
            "rule_id": "coverage", "status": "partial", "risk_level": "medium",
            "confidence": "high", "evidence_quality": "sufficient",
            "evidence": "电力电缆规格与清单不匹配；认证证书复印件需 OCR 确认。",
            "reason": "文字层已发现电缆规格串号；证书图像仍待 OCR 核验。",
        }]

        result = worker._normalise_review_results(output, [rule])[0]

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["risk_level"], "medium")
        self.assertEqual(result["confidence"], "high")

    def test_scope_rule_batch_prompt_keeps_multiple_distinct_representative_evidence(self):
        prompt = worker._combined_batch_prompt(
            self.app, "review", {"original_name": "投标.pdf", "bidder_name": "甲公司"},
            [{"rule_id": "scope", "category": "other", "title": "项目范围无关内容核验",
              "check_rule": "全文检查与本项目范围无关内容", "source_text": ""}],
            "【项目范围偏离候选】不同施工对象。", compact=False,
        )
        normal_prompt = worker._combined_batch_prompt(
            self.app, "review", {"original_name": "投标.pdf", "bidder_name": "甲公司"},
            [{"rule_id": "license", "category": "qualification", "title": "营业执照",
              "check_rule": "核验营业执照", "source_text": ""}],
            "营业执照内容。", compact=False,
        )

        self.assertIn("最多四项不同类型的代表性原文", prompt)
        self.assertIn("每类保留一条最有辨识度的原文短语", prompt)
        self.assertNotIn("最多四项不同类型的代表性原文", normal_prompt)

    def test_review_normalisation_downgrades_unread_ocr_rule_instead_of_high_risk_failure(self):
        rules = [{"rule_id": "license", "check_mode": "ocr"}]
        output = [{
            "rule_id": "license", "status": "not_satisfied", "risk_level": "high",
            "confidence": "high", "evidence_quality": "missing",
            "reason": "文本未检索到许可证复印件",
        }]

        result = worker._normalise_review_results(output, rules)[0]

        self.assertEqual(result["status"], "ocr_required")
        self.assertEqual(result["risk_level"], "low")
        self.assertEqual(result["confidence"], "low")
        self.assertIn("当前未执行 OCR", result["reason"])

        legacy = worker._normalise_review_results([{
            "rule_id": "signature", "status": "satisfied", "risk_level": "low",
            "evidence": "响应函列有法定代表人签字栏",
        }], [{
            "rule_id": "signature", "check_mode": "auto", "title": "响应函签章",
            "check_rule": "核验法定代表人签字并加盖单位章",
        }])[0]
        self.assertEqual(legacy["status"], "ocr_required")
        self.assertEqual(legacy["risk_level"], "low")
        self.assertIn("当前未执行 OCR", legacy["reason"])

    def test_visual_evidence_rules_receive_ocr_fallback_without_project_keywords(self):
        self.assertTrue(worker._rule_requires_visual_verification({
            "title": "人员资格", "check_rule": "核验操控员执照复印件的有效性",
        }))
        self.assertTrue(worker._rule_requires_visual_verification({
            "title": "报价文件", "check_rule": "核验法定代表人签字及单位盖章",
        }))
        self.assertFalse(worker._rule_requires_visual_verification({
            "title": "服务期限", "check_rule": "核验承诺服务期限为30日",
        }))

    def test_explicit_non_ocr_rule_is_not_overridden_by_visual_keyword_fallback(self):
        self.assertFalse(worker._rule_requires_visual_verification({
            "title": "证照要求", "check_rule": "核验许可证名称及有效期", "ocr_required": False,
        }))

    def test_inapplicable_template_rules_require_concrete_trigger(self):
        rules = [
            {"title": "★号条款响应", "check_rule": "核验★号条款", "source_text": "★号条款响应"},
            {"title": "进口产品限制", "check_rule": "招标文件不接受进口产品投标的内容时核验", "source_text": "进口产品（如有）"},
            {"title": "报价修正确认", "check_rule": "不涉及报价修正，或报价不一致时确认", "source_text": "报价修正（如有）"},
        ]
        tender = "项目属性：■服务。符合性审查包含★号条款响应的交叉引用。"
        self.assertEqual(worker._filter_inapplicable_template_rules(rules, tender), [])

        active_tender = "■本项目不接受进口产品。第五章：★ 音频文件必须为 WAV。"
        titles = [item["title"] for item in worker._filter_inapplicable_template_rules(rules[:2], active_tender)]
        self.assertEqual(titles, ["★号条款响应", "进口产品限制"])

    def test_technical_star_material_rule_is_recovered_only_with_complete_formal_chain(self):
        tender = """[第99页]
指标按重要性分为“★”、“#”和“△”。★代表实质性指标，不满足该指标项将导致投标被拒绝。
按要求提供证明材料（证明材料包括产品技术资料、技术参数、功能截图、检测报告、产品资质证书或说明书）。
[第101页]
3.2.1 产品指标表
面板无线 AP  ★  硬件规格  内置智能天线系统，接口不少于2个千兆电口。
"""
        seed = worker._technical_star_requirement_seed(tender)
        self.assertIsNotNone(seed)
        self.assertEqual(seed["category"], "substantive")
        self.assertEqual(seed["source_page"], 99)
        self.assertIn("全部叶子指标", seed["check_rule"])
        self.assertEqual(seed["evidence_requirements"], ["text", "visual"])

        rules = worker._ensure_technical_star_requirement_rule([], tender)
        self.assertEqual([item["title"] for item in rules], ["★技术参数及证明材料响应"])
        normalised = worker._normalise_visual_rule_policies(rules)
        # 提取默认均为“仅基础识别”；★规则的 visual 证据需求仍保留，本地 OCR 基线与系统建议据此工作，
        # 是否升级增强通道由人工确认规则时选择。
        self.assertEqual(normalised[0]["vision_trigger"], "off")
        self.assertEqual(normalised[0]["acquisition_preset"], "off")
        self.assertEqual(normalised[0]["evidence_requirements"], ["text", "visual"])
        self.assertEqual(storage.rule_acquisition_recommendation(normalised[0])["acquisition_preset"], "visual")

        # 只有★或普通技术参数、但没有证明材料和明确后果时不得自行补成否决规则。
        incomplete = "[第20页]\n技术参数表：★硬件规格，端口不少于2个。"
        self.assertIsNone(worker._technical_star_requirement_seed(incomplete))
        # 多包文件无法由本地兜底可靠判定所属包时，不得把某包的★技术要求自动带入另一包。
        multi_package = tender + "\n采购包1：网络设备。\n采购包2：机房设备。"
        self.assertIsNone(worker._technical_star_requirement_seed(multi_package, package_number=1))

    def test_visual_rule_policy_is_generic_and_uses_ai_recommendation(self):
        rules = worker._normalise_visual_rule_policies([{
            "rule_id": "any-visual-rule", "category": "qualification", "title": "任意扫描材料",
            "check_rule": "核验扫描件上的盖章状态", "source_text": "应加盖单位章",
            "check_mode": "ocr", "evidence_requirements": ["visual"],
        }])
        self.assertEqual(len(rules), 1)
        # 默认“仅基础识别”：增强通道全部关闭，但 OCR 需求与证据维度保留，
        # 本地 OCR 基线仍会运行；AI 升级建议保持原有判断，供人工选择。
        self.assertEqual(rules[0]["vision_trigger"], "off")
        self.assertEqual(rules[0]["vision_level"], "off")
        self.assertEqual(rules[0]["acquisition_preset"], "off")
        self.assertTrue(rules[0]["ocr_required"])
        # check_mode=ocr 被推荐逻辑视为材料/字段类，混合视觉事实时建议“智能升级”而非纯视觉通道。
        self.assertEqual(storage.rule_acquisition_recommendation(rules[0])["acquisition_preset"], "smart")

    def test_acquisition_recommendation_stays_consistent_with_ocr_marking(self):
        # 模型判定决定性证据在 OCR 层但不含外观词（证书编号核验）：建议不低于 smart。
        rec = storage.rule_acquisition_recommendation({
            "title": "认证证书编号核验", "check_rule": "核验证书编号与有效期", "source_text": "提供认证证书复印件",
            "check_mode": "ocr", "ocr_required": True,
            "execution_meta_json": {"evidence_requirements": ["document", "field"]},
        })
        self.assertEqual(rec["acquisition_preset"], "smart")
        # 执行级外观词（骑缝章/手写等）在统一建议词汇表内，建议不能落回 off。
        for term in ("骑缝章", "手写"):
            rec = storage.rule_acquisition_recommendation({
                "title": "签章核验", "check_rule": f"核验{term}是否完整", "source_text": "",
                "check_mode": "ocr", "ocr_required": True,
            })
            self.assertNotEqual(rec["acquisition_preset"], "off")
        # 纯文字规则建议保持 off，不误升级。
        rec = storage.rule_acquisition_recommendation({
            "title": "服务期限", "check_rule": "核验服务期限是否为30日", "source_text": "服务期限30日",
            "check_mode": "auto",
            "execution_meta_json": {"evidence_requirements": ["text"]},
        })
        self.assertEqual(rec["acquisition_preset"], "off")

    def test_rule_policy_keeps_text_verifiable_material_out_of_forced_ocr(self):
        material, plain = worker._normalise_visual_rule_policies([{
            "rule_id": "certificate", "category": "objective", "title": "认证证书评分",
            "check_rule": "核验证书名称、编号、有效期并按有效证书数量计分",
            "ocr_required": True, "evidence_requirements": ["text", "document", "field", "visual"],
        }, {
            "rule_id": "duration", "category": "compliance", "title": "服务期限",
            "check_rule": "核验服务期限是否为30日", "ocr_required": False,
            "evidence_requirements": ["text"],
        }])
        self.assertFalse(material["ocr_required"])
        self.assertEqual(material["check_mode"], "auto")
        self.assertEqual(material["vision_trigger"], "off")
        self.assertEqual(material["baseline_ocr_mode"], "auto")
        self.assertFalse(plain["ocr_required"])
        self.assertEqual(plain["baseline_ocr_mode"], "auto")
        self.assertEqual(plain["vision_trigger"], "off")
        self.assertFalse(worker._local_ocr_baseline_required(plain, {
            "status": "satisfied", "evidence_quality": "sufficient", "confidence": "high",
        }))
        self.assertTrue(worker._local_ocr_baseline_required(plain, {
            "status": "manual", "evidence_quality": "missing", "confidence": "low",
        }))

    def test_enabled_visual_rule_reports_unavailable_model_instead_of_looking_like_visual_result(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "已提供证书明细，扫描件见附件。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "证书评分", "check_rule": "核验证书扫描件",
            "check_mode": "ocr", "scoring": {"kind": "boolean", "max_score": 2},
        })
        storage.update_rule(self.app, self.project["project_id"], rule["rule_id"], {
            "vision_trigger": "required", "vision_level": "standard",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={
            "results": [{"rule_id": rule["rule_id"], "suggested_score": 0, "needs_ocr": True,
                         "evidence": "扫描件见P1", "reason": "需图片核验", "confidence": "low"}],
        }):
            finished = self._run_next_task()
        _, rows = storage.latest_score_results(self.app, self.project["project_id"], "objective")

        self.assertEqual(finished["status"], "success")
        self.assertEqual(rows[0]["vision_status"], "unavailable")
        self.assertEqual(rows[0]["vision_pages"], [])

    def test_tencent_upgrade_pages_field_gap_driven(self):
        rule = {"title": "体系认证证书核验", "check_rule": "核验证书编号、有效期与认证范围", "source_text": ""}
        local_values = [
            {"page": 1, "text": "证书编号 ABC123，有效期 2026-12-31，认证范围：软件开发。", "confidence": 0.95},
            {"page": 2, "text": "证书编号 DEF456，有效期 2027-06-30，认证范围：环保服务。", "confidence": 0.91},
            {"page": 3, "text": "扫描件内容模糊，仅见部分字样。", "confidence": 0.9},
            {"page": 4, "text": "证书编号 GHI789，有效期 2028-03-15，认证范围：信息化服务。", "confidence": 0.5},
        ]
        # 页1/2/4 含编号+日期信号，只有低置信的页4升级；页3缺字段信号也会升级。
        upgraded = worker._tencent_upgrade_pages(rule, local_values, "", "standard")
        self.assertEqual(upgraded, [3, 4])
        # 不再因“证书类”就把全部页送腾讯。
        self.assertNotIn(1, upgraded)
        self.assertNotIn(2, upgraded)
        # 本地失败时仍全页容错升级。
        self.assertEqual(worker._tencent_upgrade_pages(rule, local_values, "本地 RapidOCR 未完成", "standard"), [1, 2, 3, 4])

    def test_should_run_vision_for_rule_gap_driven(self):
        rule = {"title": "证书核验", "check_rule": "核验证书编号与有效期"}
        # 本地 OCR 归纳明确无需外观核验 → 混合/文字策略都不再看图。
        self.assertFalse(worker._should_run_vision_for_rule(
            "auto", "hybrid", "text_fallback", rule, {"_ocr_visual_review_required": False},
        ))
        self.assertFalse(worker._should_run_vision_for_rule(
            "auto", "ocr", "text_fallback", rule, {"_ocr_visual_review_required": False},
        ))
        # 本地 OCR 归纳确认有外观待核验 → 混合/文字策略升级看图。
        self.assertTrue(worker._should_run_vision_for_rule(
            "auto", "hybrid", "text_fallback", rule, {"_ocr_visual_review_required": True},
        ))
        self.assertTrue(worker._should_run_vision_for_rule(
            "auto", "ocr", "text_fallback", rule, {"_ocr_visual_review_required": True},
        ))
        # 纯视觉策略或人工强制始终看图。
        self.assertTrue(worker._should_run_vision_for_rule(
            "auto", "vision", "off", rule, {"_ocr_visual_review_required": False},
        ))
        self.assertTrue(worker._should_run_vision_for_rule(
            "auto", "hybrid", "required", rule, {"_ocr_visual_review_required": False},
        ))
        # combined 通道始终看图；模型未给出判定时回退旧启发式（OCR 未完整覆盖即看）。
        self.assertTrue(worker._should_run_vision_for_rule(
            "combined", "hybrid", "text_fallback", rule, {},
        ))
        self.assertTrue(worker._should_run_vision_for_rule(
            "auto", "hybrid", "text_fallback", rule, {"vision_status": "ocr_applied_partial"},
        ))
        self.assertFalse(worker._should_run_vision_for_rule(
            "auto", "hybrid", "text_fallback", rule, {"vision_status": "ocr_applied"},
        ))

    def test_ocr_visual_gap_flag(self):
        self.assertTrue(worker._ocr_visual_gap_flag({"visual_review_required": True}, True))
        self.assertFalse(worker._ocr_visual_gap_flag({"visual_review_required": False}, True))
        self.assertIsNone(worker._ocr_visual_gap_flag({}, True))
        self.assertTrue(worker._ocr_visual_gap_flag({}, False))

    def test_visual_page_candidates_only_accept_explicit_page_references(self):
        document = {"extension": ".pdf", "page_count": 300}
        result = {
            "visual_page_candidates": [224, 227],
            "evidence": "AI共识别2项；1. 节能证书（第P224页）；2. 环境标志证书（P227页）",
            "reason": "建议得2分，有效期至2029-12-17。",
        }
        self.assertEqual(worker._vision_page_candidates(document, {}, result), [224, 227])

        # 旧展示文本没有结构化页码时，也只能识别 P224 / P227，不能把“2项”“1.”误作页码。
        legacy = {"evidence": result["evidence"], "reason": result["reason"]}
        self.assertEqual(worker._vision_page_candidates(document, {}, legacy), [224, 227])

    def test_visual_page_candidates_complete_dense_repeated_attachment_sequence(self):
        document = {"extension": ".pdf", "page_count": 300}
        result = {"visual_page_candidates": [150, 152, 144, 146]}

        candidates = worker._vision_page_candidates(document, {}, result)

        self.assertEqual(candidates[:4], [150, 152, 144, 146])
        self.assertIn(148, candidates[:6])
        self.assertEqual(worker._complete_repeated_page_sequence([10, 40, 42], 300), [])

    def test_visual_page_candidates_prioritise_gap_context_over_bare_structured_pages(self):
        document = {"extension": ".pdf", "page_count": 300}
        result = {
            "visual_page_candidates": [224, 227],
            "evidence": (
                "节能证书明细（P224）；环境标志证书明细（P227）；"
                "证书扫描件齐备性（第195-197、200-208页）待 OCR 核验"
            ),
            "reason": "文字层尚未确认扫描件外观。",
        }
        candidates = worker._vision_page_candidates(document, {}, result)
        # “扫描件齐备性待核验”直接描述图片缺口，其页段优先于裸结构化页码；
        # 文字明细页仍保留在候选中，供后续批次使用。
        self.assertEqual(candidates[:2], [195, 200])
        self.assertIn(224, candidates)
        self.assertIn(227, candidates)

    def test_visual_page_candidates_ignore_page_hint_after_ocr_merge(self):
        # OCR 合并会把 page_hint 改写为“OCR 已处理页清单”；材料实际所在页
        # 只应从证据语句（前缀已剥离）中取得，已处理页清单不能回流为候选。
        document = {"extension": ".pdf", "page_count": 600}
        result = {
            "vision_status": "ocr_applied_partial",
            "page_hint": "P118、P200、P483",
            "evidence": (
                "【腾讯OCR·通用文字识别（高精度版）·P118、P200、P483、P144】"
                "质量管理体系证书编号与有效期已在P144文字层明确。"
            ),
            "reason": "其余证书待核验。",
        }
        self.assertEqual(worker._vision_page_candidates(document, {}, result), [144])

    def test_visual_page_candidates_prioritise_ocr_confirmed_evidence_pages(self):
        document = {"extension": ".pdf", "page_count": 600}
        result = {
            "vision_status": "ocr_applied_partial",
            "ocr_candidate_pages": [118, 200, 483],
            "ocr_evidence_pages": [200],
            "visual_page_candidates": [144],
            "page_hint": "P118、P200、P483",
            "evidence": "OCR确认P200含目标证书，P144为文字层目录线索。",
        }

        self.assertEqual(worker._vision_page_candidates(document, {}, result)[:2], [200, 144])

    def test_scan_visual_candidates_use_page_range_only_when_no_precise_page_exists(self):
        precise = {"findings": [
            {"rule_id": "r", "page_hint": "P126", "page_range": "第121-130页"},
            {"rule_id": "r", "page_hint": "", "page_range": "第141-150页"},
        ]}
        coarse_only = {"findings": [
            {"rule_id": "r", "page_hint": "", "page_range": "第121-130页"},
        ]}

        self.assertEqual(worker._scan_visual_page_candidates(precise, "r", 200), [126])
        fallback = worker._scan_visual_page_candidates(coarse_only, "r", 200)
        self.assertTrue(fallback)
        self.assertTrue(all(121 <= page <= 130 for page in fallback))

    def test_visual_page_candidates_ignore_ocr_prefix_page_lists(self):
        # 【腾讯OCR·…·P…】前缀里的页码是“已处理页清单”，不是材料所在页；
        # 只有正文叙述中的命中页（如“证书在P144明确”）才应进入候选。
        document = {"extension": ".pdf", "page_count": 600}
        result = {
            "evidence": (
                "【腾讯OCR·通用文字识别（高精度版）·P118、P200、P201、P360、P483、P144】"
                "质量管理体系证书编号、有效期、主体、颁证机构已在P144文字层明确；"
                "CCID/CCRC在OCR覆盖页未见。"
            ),
            "reason": "证书真实性与平台截图待核验。",
        }
        self.assertEqual(worker._vision_page_candidates(document, {}, result), [144])

    def test_score_results_preserve_structured_evidence_pages_for_visual_routing(self):
        payload = [{"rule_id": "cert", "scoring": {"max_score": 2}, "ocr_required": True}]
        output = [{"rule_id": "cert", "suggested_score": 2, "needs_ocr": True, "evidence_items": [
            {"name": "节能证书", "page_hint": "P224"},
            {"name": "环境标志证书", "page_hint": "第227页"},
        ]}]
        result = worker._normalise_score_results(output, payload, "objective")[0]
        self.assertEqual(result["visual_page_candidates"], [224, 227])

    def test_visual_followup_prefers_adjacent_pages_after_uncovered_result(self):
        document = {"page_count": 300}
        followup = worker._visual_followup_pages(
            document, [224, 227], [224, 227], {"coverage": "not_covered", "requested_pages": []}, "standard",
        )
        self.assertEqual(followup, [225, 228, 223, 226])

    def test_visual_followup_honours_model_requested_pages_before_static_candidates(self):
        document = {"page_count": 300}
        followup = worker._visual_followup_pages(
            document, [224, 227], [224, 227],
            {"coverage": "not_covered", "requested_pages": [225, 226]}, "standard",
        )
        # 模型看过首轮图片后点名的相邻页优先级最高，其次才是首轮页的其他相邻页。
        self.assertEqual(followup, [225, 226, 228, 223])

    def test_standard_visual_strength_sends_three_parallel_material_pages_together(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 30,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "三项认证证书", "check_rule": "逐项核验三项认证证书",
                "vision_trigger": "required", "vision_level": "standard", "scoring": {"max_score": 3}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 3, "evidence": "文字层待核验",
                    "reason": "三项证书扫描件待核验", "confidence": "low",
                    "visual_page_candidates": [10, 11, 12]}
        sent_pages = []

        def render_images(app, document, pages, level, **_kwargs):
            sent_pages.extend(pages)
            return [{"page": page, "type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "default"}} for page in pages]

        with patch("dashboard.evaluation_workbench.worker._render_vision_images", side_effect=render_images), \
             patch("dashboard.evaluation_workbench.worker._request_task_json", return_value={
                 "coverage": "covered", "conclusion_scope": "full", "needs_more_image": False,
                 "evidence": "P10、P11、P12分别可见三项证书", "reason": "三项材料均已覆盖",
                 "suggested_score": 3, "confidence": "high",
             }):
            worker._run_visual_supplement(
                self.app, {"task_id": "task"}, document, "objective", rule, original,
                {"profile_id": "vision", "display_name": "图片模型"},
            )

        self.assertEqual(sent_pages, [10, 11, 12])

    def test_standard_visual_strength_proactively_covers_remaining_candidate_branch(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 30,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "多项认证证书", "check_rule": "逐项核验多项认证证书",
                "vision_trigger": "required", "vision_level": "standard", "scoring": {"max_score": 5}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 5, "evidence": "文字层待核验",
                    "reason": "多项证书扫描件待核验", "confidence": "low",
                    "visual_page_candidates": [10, 11, 12, 13, 14]}
        sent_batches = []

        def render_images(app, document, pages, level, **_kwargs):
            sent_batches.append(list(pages))
            return [{"page": page, "type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "default"}} for page in pages]

        responses = [
            # 首轮结论不完整（partial）：系统仍应主动覆盖剩余候选分支。
            {"coverage": "covered", "conclusion_scope": "partial", "needs_more_image": False,
             "evidence_pages": [10, 11, 12, 13], "evidence": "P10-P13可见四项材料", "reason": "首批材料清晰", "suggested_score": 4, "confidence": "high"},
            {"coverage": "covered", "conclusion_scope": "partial", "needs_more_image": False,
             "evidence_pages": [14], "evidence": "P14可见第五项材料", "reason": "补充分支", "suggested_score": 1, "confidence": "high"},
        ]
        with patch("dashboard.evaluation_workbench.worker._render_vision_images", side_effect=render_images), \
             patch("dashboard.evaluation_workbench.worker._request_task_json", side_effect=responses):
            result = worker._run_visual_supplement(
                self.app, {"task_id": "task"}, document, "objective", rule, original,
                {"profile_id": "vision", "display_name": "图片模型"},
            )

        self.assertEqual(sent_batches, [[10, 11, 12, 13], [14]])
        self.assertEqual(result["vision_pages"], [10, 11, 12, 13, 14])
        self.assertEqual(result["vision_evidence_pages"], [10, 11, 12, 13, 14])
        self.assertIn("P14可见第五项材料", result["evidence"])

    def test_second_visual_batch_receives_first_batch_and_can_reconcile_final_score(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 30,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "五项认证证书", "check_rule": "逐项核验五项认证证书",
                "vision_trigger": "required", "vision_level": "standard", "scoring": {"max_score": 5}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 5, "evidence": "文字层待核验",
                    "reason": "五项证书扫描件待核验", "confidence": "low",
                    "visual_page_candidates": [10, 11, 12, 13, 14]}
        prompts = []
        responses = [
            {"coverage": "covered", "conclusion_scope": "partial", "needs_more_image": False,
             "evidence_pages": [10, 11, 12, 13], "evidence": "前四项证书可见",
             "reason": "仍缺第五项", "suggested_score": 4, "confidence": "high"},
            {"coverage": "covered", "conclusion_scope": "full", "needs_more_image": False,
             "evidence_pages": [14], "evidence": "第五项证书可见，结合前批共五项",
             "reason": "两批合并后五项齐全", "suggested_score": 5, "confidence": "high"},
        ]

        def request_json(*args, **_kwargs):
            prompts.append(args[5][0]["text"])
            return responses[len(prompts) - 1]

        with patch("dashboard.evaluation_workbench.worker._render_vision_images", side_effect=lambda _a, _d, pages, _l, **_k: [
            {"page": page, "type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "default"}}
            for page in pages
        ]), patch("dashboard.evaluation_workbench.worker._request_task_json", side_effect=request_json):
            result = worker._run_visual_supplement(
                self.app, {"task_id": "task"}, document, "objective", rule, original,
                {"profile_id": "vision", "display_name": "图片模型"},
            )

        self.assertEqual(len(prompts), 2)
        self.assertIn("prior_image_batches", prompts[1])
        self.assertIn("前四项证书可见", prompts[1])
        self.assertEqual(result["vision_status"], "applied")
        self.assertEqual(result["suggested_score"], 5)
        self.assertIn("第五项证书可见", result["evidence"])

    def test_visual_supplement_stops_early_when_first_batch_is_conclusive(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 30,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "多项认证证书", "check_rule": "逐项核验多项认证证书",
                "vision_trigger": "required", "vision_level": "standard", "scoring": {"max_score": 5}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 5, "evidence": "文字层待核验",
                    "reason": "多项证书扫描件待核验", "confidence": "low",
                    "visual_page_candidates": [10, 11, 12, 13, 14]}
        sent_batches = []

        def render_images(app, document, pages, level, **_kwargs):
            sent_batches.append(list(pages))
            return [{"page": page, "type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "default"}} for page in pages]

        with patch("dashboard.evaluation_workbench.worker._render_vision_images", side_effect=render_images), \
             patch("dashboard.evaluation_workbench.worker._request_task_json", return_value={
                 "coverage": "covered", "conclusion_scope": "full", "needs_more_image": False,
                 "conflict_level": "none",
                 "evidence_pages": [10, 11, 12, 13],
                 "evidence": "P10-P13可见全部五项材料", "reason": "材料齐备", "suggested_score": 5, "confidence": "high",
             }):
            result = worker._run_visual_supplement(
                self.app, {"task_id": "task"}, document, "objective", rule, original,
                {"profile_id": "vision", "display_name": "图片模型"},
            )

        # 首轮已完整覆盖且无冲突：不为剩余候选页消耗第二批多模态调用。
        self.assertEqual(sent_batches, [[10, 11, 12, 13]])
        self.assertEqual(result["vision_pages"], [10, 11, 12, 13])
        self.assertEqual(result["vision_status"], "applied")
        self.assertEqual(result["suggested_score"], 5)

    def test_printed_page_offset_requires_consistent_footer_evidence(self):
        page_texts = {page: f"这是第{page}页的正文内容。\n{page - 2}\n" for page in range(3, 31)}
        self.assertEqual(worker._estimate_printed_page_offset(page_texts, 30), 2)
        # 样本不足时不纠偏。
        self.assertEqual(worker._estimate_printed_page_offset({3: "正文\n1\n", 4: "正文\n2\n"}, 30), 0)
        # 报价表等数字密集页的零散数字不能形成多数一致的偏移。
        scattered = {page: f"正文\n{page - (page % 5)}\n" for page in range(3, 31)}
        self.assertEqual(worker._estimate_printed_page_offset(scattered, 30), 0)

    def test_visual_candidates_correct_toc_printed_pages_to_pdf_order(self):
        parsed = self.temp_dir / "parsed_toc.txt"
        parts = []
        for page in range(1, 31):
            if page in {12, 13}:
                parts.append(f"[第{page}页]\n\n")  # 纯扫描页：解析文本为空
            else:
                parts.append(f"[第{page}页]\n这是第{page}页的正文内容。\n{page - 2}\n")
        parsed.write_text("".join(parts), encoding="utf-8")
        document = {"document_id": "doc-toc", "extension": ".pdf", "page_count": 30,
                    "parsed_path": str(parsed)}
        result = {"evidence": "目录标注的证书扫描件（P10、P11）待 OCR 核验", "reason": ""}

        candidates = worker._vision_page_candidates(document, {}, result)

        # 目录语境的印刷页码 P10、P11 按估计偏移 +2 纠偏为 PDF 第 12、13 页。
        self.assertEqual(candidates[:2], [12, 13])

    def test_directory_material_entry_prioritises_actual_attachment_pages(self):
        parsed = self.temp_dir / "parsed_directory_material.txt"
        parts = ["[第1页]\n目录\n精密空调（氟泵）节能产品认证证书复印件……10/11\n"]
        for page in range(2, 31):
            text = "" if page in {12, 13} else f"这是第{page}页的正文内容。\n{page - 2}\n"
            parts.append(f"[第{page}页]\n{text}")
        parsed.write_text("".join(parts), encoding="utf-8")
        document = {"document_id": "doc-directory-material", "extension": ".pdf", "page_count": 30,
                    "parsed_path": str(parsed)}
        rule = {"title": "强制节能产品认证证书", "check_rule": "核验精密空调（氟泵）节能产品认证证书复印件"}
        result = {"evidence": "目录和承诺页待核验（P1、P8）", "visual_page_candidates": [1, 8]}

        candidates = worker._vision_page_candidates(document, rule, result)

        # 目录明示的印刷页10/11先纠偏为PDF 12/13，优先于目录/承诺等普通候选。
        self.assertEqual(candidates[:2], [12, 13])
        self.assertIn(1, candidates)

    def test_long_directory_continuation_prioritises_material_page_for_vision_and_ocr(self):
        parsed = self.temp_dir / "parsed_long_directory.txt"
        parts = ["[第1页]\n投标文件首页\n", "[第2页]\n目录\n商务部分……3\n技术部分……4\n附件目录……5\n"]
        for page in range(3, 8):
            line = "普通目录条目……%d\n" % (page + 10)
            if page == 7:
                line = "精密空调（氟泵）节能产品认证证书复印件……564\n" + line
            parts.append(f"[第{page}页]\n{line}其他附件目录……{page + 20}\n技术资料目录……{page + 30}\n")
        for page in range(8, 565):
            text = "" if page == 564 else f"这是第{page}页的正文内容。\n"
            parts.append(f"[第{page}页]\n{text}")
        parsed.write_text("".join(parts), encoding="utf-8")
        document = {"document_id": "doc-long-directory", "extension": ".pdf", "page_count": 564,
                    "parsed_path": str(parsed)}
        rule = {"title": "强制节能产品认证证书", "check_rule": "核验精密空调（氟泵）节能产品认证证书复印件"}
        result = {"evidence": "仅见承诺，证书待核验", "reason": "需检查证书本体", "visual_page_candidates": [2, 200]}

        self.assertEqual(worker._page_material_role("精密空调认证证书复印件……564\n其他附件……565\n技术资料……566"), "directory")
        visual_pages = worker._vision_page_candidates(document, rule, result)
        ocr_pages = worker._ocr_candidate_pages(document, rule, result, "standard")

        self.assertEqual(visual_pages[0], 564)
        self.assertEqual(ocr_pages[0], 564)

    def test_directory_word_in_body_is_not_misclassified_as_directory(self):
        body = "本产品符合强制性产品认证目录要求，并提供认证证书复印件。\n参数说明\n有效期说明"
        self.assertEqual(worker._page_material_role(body), "certificate")

    def test_page_specific_business_license_route_falls_back_without_disabling_later_license_page(self):
        rule = {"title": "营业执照", "check_rule": "核验营业执照和统一社会信用代码"}

        wrong_page = worker._ocr_service_candidates_for_page(rule, "standard", "法定代表人身份证明及授权委托书")
        actual_license = worker._ocr_service_candidates_for_page(rule, "standard", "营业执照\n统一社会信用代码：91110108")

        self.assertNotIn("biz_license", wrong_page)
        self.assertEqual(actual_license[0], "biz_license")

    def test_partial_visual_score_uses_confirmed_leaf_items_only(self):
        rule = {"scoring": {"max_score": 3, "items": [
            {"item_id": "SI-1", "max_score": 1}, {"item_id": "SI-2", "max_score": 2},
        ]}}
        parsed = {"score_items": [
            {"item_id": "SI-1", "status": "confirmed", "suggested_score": 1, "evidence_pages": [396, 397]},
            {"item_id": "SI-2", "status": "unresolved", "suggested_score": 2, "evidence_pages": []},
        ]}

        self.assertEqual(worker._confirmed_partial_score(rule, parsed, 0, 3), 1)
        self.assertEqual(worker._confirmed_partial_score(rule, {"suggested_score": 3}, 0, 3), 0)
        self.assertEqual(worker._confirmed_partial_score(rule, parsed, 0, 3, checked_pages=[10]), 0)

    def test_partial_visual_score_accepts_confirmed_single_cap_materials_with_model_item_ids(self):
        rule = {"scoring": {"max_score": 3, "items": [{"item_id": "SI-1", "max_score": 3}]}}
        parsed = {"score_items": [
            {"item_id": "合同-淹底乡", "status": "confirmed", "suggested_score": 3, "evidence_pages": [188, 189]},
            {"item_id": "合同-待补", "status": "unresolved", "suggested_score": 3, "evidence_pages": []},
        ]}

        self.assertEqual(worker._confirmed_partial_score(rule, parsed, 0, 3, checked_pages=[189]), 3)
        self.assertEqual(worker._confirmed_partial_score(rule, parsed, 0, 3, checked_pages=[20]), 0)

    def test_evidence_gated_visual_score_can_correct_downward(self):
        rule = {"category": "objective", "title": "同类业绩评分", "check_rule": "每提供1份有效业绩得3分，最高9分",
                "scoring": {"max_score": 9, "items": [{"item_id": "SI-1", "max_score": 9}]}}
        parsed = {"score_items": [{"item_id": "业绩1", "status": "confirmed", "suggested_score": 3, "evidence_pages": [12]}]}

        self.assertTrue(worker._requires_discrete_document_evidence(rule))
        self.assertEqual(worker._confirmed_partial_score(
            rule, parsed, 6, 9, checked_pages=[12], evidence_gated=True,
        ), 3)

    def test_cross_bid_price_fact_retracts_stale_price_absence_review(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "报价文件")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "compliance", "title": "投标报价核验", "check_rule": "核验投标报价是否符合最高限价",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], None)
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", None)
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "status": "not_found", "evidence": "未见总报价金额", "reason": "报价总价未直接呈现。",
            "risk_level": "high", "confidence": "high", "evidence_quality": "sufficient",
        }])
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "suggested_score": 0, "max_score": 10,
            "evidence": "投标总报价（大写）：人民币陆拾贰万柒仟元整；¥627,000元。", "reason": "已定位报价。",
        }])

        self.assertEqual(storage.reconcile_price_review_results(self.app, review_run["review_run_id"], score_run["score_run_id"]), 1)
        with storage.connection(self.app) as conn:
            row = conn.execute("SELECT status, risk_level, reason FROM ew_review_results WHERE review_run_id=?", (review_run["review_run_id"],)).fetchone()
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["risk_level"], "low")
        self.assertIn("已定位报价金额", row["reason"])

    def test_price_fact_retraction_matches_amount_without_thousands_separator(self):
        document = self._add_pdf("bid.pdf", "bid", "乙公司", "报价文件")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "compliance", "title": "投标报价核验", "check_rule": "核验投标报价是否符合最高限价",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], None)
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", None)
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "status": "not_found", "evidence": "未见总报价金额", "reason": "报价总价未直接呈现。",
            "risk_level": "high", "confidence": "high", "evidence_quality": "sufficient",
        }])
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "suggested_score": 0, "max_score": 10,
            "evidence": "投标总报价￥：632836 元", "reason": "已定位报价，另核实174.6762万元口径。",
        }])

        self.assertEqual(storage.reconcile_price_review_results(self.app, review_run["review_run_id"], score_run["score_run_id"]), 1)
        with storage.connection(self.app) as conn:
            row = conn.execute("SELECT status FROM ew_review_results WHERE review_run_id=?", (review_run["review_run_id"],)).fetchone()
        self.assertEqual(row["status"], "partial")

    def test_non_price_rule_amount_does_not_count_as_located_bid_price(self):
        document = self._add_pdf("bid.pdf", "bid", "丙公司", "投标文件")
        price_rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "compliance", "title": "投标报价核验", "check_rule": "核验投标报价是否符合最高限价",
        })
        perf_rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "近三年同类业绩", "check_rule": "核验同类项目业绩合同",
            "scoring": {"max_score": 6},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], None)
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", None)
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [{
            "rule_id": price_rule["rule_id"], "status": "not_found", "evidence": "未见总报价金额", "reason": "报价总价未直接呈现。",
            "risk_level": "high", "confidence": "high", "evidence_quality": "sufficient",
        }])
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [{
            "rule_id": perf_rule["rule_id"], "suggested_score": 2, "max_score": 6,
            "evidence": "合同金额2163180元", "reason": "已定位同类业绩合同。",
        }])

        self.assertEqual(storage.reconcile_price_review_results(self.app, review_run["review_run_id"], score_run["score_run_id"]), 0)
        with storage.connection(self.app) as conn:
            row = conn.execute("SELECT status, risk_level FROM ew_review_results WHERE review_run_id=?", (review_run["review_run_id"],)).fetchone()
        self.assertEqual(row["status"], "not_found")
        self.assertEqual(row["risk_level"], "high")

    def test_irrelevant_visual_pages_cannot_become_evidence_pages(self):
        merged, _, checked, evidence = worker._merge_usable_visual_responses([(
            [118, 144], {
                "coverage": "covered", "conclusion_scope": "partial", "evidence": "P144证书可见",
                "evidence_pages": [118, 144], "irrelevant_pages": [118],
            },
        )])

        self.assertEqual(checked, [118, 144])
        self.assertEqual(evidence, [144])
        self.assertIsNotNone(merged)

    def test_multi_page_visual_result_without_evidence_pages_cannot_complete_or_change_score(self):
        merged, scope, checked, evidence = worker._merge_usable_visual_responses([(
            [10, 11], {
                "coverage": "covered", "conclusion_scope": "full", "evidence": "两页均已查看", "suggested_score": 2,
            },
        )])
        _, single_scope, single_checked, single_evidence = worker._merge_usable_visual_responses([(
            [12], {"coverage": "covered", "conclusion_scope": "full", "evidence": "单页证书可见"},
        )])

        self.assertEqual(scope, "partial")
        self.assertEqual(checked, [10, 11])
        self.assertEqual(evidence, [])
        self.assertIsNotNone(merged)
        self.assertEqual(single_scope, "full")
        self.assertEqual(single_checked, [12])
        self.assertEqual(single_evidence, [12])

    def test_vision_render_scale_has_preflight_pixel_ceiling(self):
        class Page:
            rect = type("Rect", (), {"width": 10_000, "height": 10_000})()

        scale = worker._safe_vision_render_scale(Page(), 2.0)

        self.assertLess(scale, 1.0)
        self.assertLessEqual(
            10_000 * 10_000 * scale * scale,
            worker._VISION_MAX_PIXELS_PER_PAGE * 1.0001,
        )

    def test_confirmed_visual_fact_removes_matching_stale_pending_clause(self):
        rule = {"title": "认证证书", "check_rule": "核验证书复印件和有效期"}
        text = worker._reconcile_stale_pending_text(
            "未见证书复印件，需OCR核验。仍需核验投标产品型号。",
            "P144证书复印件清晰可读且在有效期内。", rule,
        )

        self.assertNotIn("未见证书", text)
        self.assertIn("投标产品型号", text)

    def test_visual_candidates_append_scan_neighbor_after_text_anchor(self):
        parsed = self.temp_dir / "parsed_anchor.txt"
        parts = []
        for page in range(1, 21):
            text = "" if page == 11 else f"这是第{page}页的正文内容。" * 12
            parts.append(f"[第{page}页]\n{text}\n")
        parsed.write_text("".join(parts), encoding="utf-8")
        document = {"document_id": "doc-anchor", "extension": ".pdf", "page_count": 20,
                    "parsed_path": str(parsed)}
        result = {"evidence": "证书明细（P10）", "reason": ""}

        candidates = worker._vision_page_candidates(document, {}, result)

        # 文字锚点 P10 之后紧跟其相邻纯扫描页 P11（证书附件本体）。
        self.assertEqual(candidates[:2], [10, 11])

    def test_multimodal_is_skipped_when_hybrid_rule_is_fully_covered_by_ocr(self):
        rule = {"rule_id": "social", "title": "社保缴纳证明", "check_rule": "核验社保缴纳证明编号与所属单位",
                "vision_trigger": "text_fallback", "vision_level": "standard"}
        # 混合规则无纯视觉核验目标且 OCR 完整覆盖：跳过图片模型并说明原因。
        note = worker._multimodal_skip_note("hybrid", {"vision_status": "ocr_applied"}, rule, "text_fallback")
        self.assertIn("未再调用图片模型", note)
        # 含签章等纯视觉核验目标的规则不能跳过。
        visual_rule = {**rule, "check_rule": "核验社保缴纳证明是否加盖公章"}
        self.assertEqual(worker._multimodal_skip_note("hybrid", {"vision_status": "ocr_applied"}, visual_rule, "text_fallback"), "")
        # required 触发尊重人工显式要求，不跳过。
        self.assertEqual(worker._multimodal_skip_note("hybrid", {"vision_status": "ocr_applied"}, rule, "required"), "")
        # OCR 未完整覆盖时不跳过。
        self.assertEqual(worker._multimodal_skip_note("hybrid", {"vision_status": "ocr_applied_partial"}, rule, "text_fallback"), "")
        # 纯视觉规则不适用跳过逻辑。
        self.assertEqual(worker._multimodal_skip_note("vision", {"vision_status": "ocr_applied"}, rule, "text_fallback"), "")

    def test_pure_visual_rule_gets_legible_render_floor_on_low_level(self):
        seal_rule = {"title": "签字盖章", "check_rule": "核验响应函是否签字并加盖公章"}
        setting = worker._vision_render_setting(seal_rule, "low")
        self.assertEqual(setting["scale"], 1.8)
        self.assertEqual(setting["detail"], "standard")
        # 页数预算仍由人工选择的档位决定。
        self.assertEqual(setting["max_pages"], 2)
        # 混合规则不提高清晰度。
        cert_rule = {"title": "认证证书", "check_rule": "核验证书编号及扫描件完整性"}
        self.assertEqual(worker._vision_render_setting(cert_rule, "low")["scale"], 1.15)
        # 已有高清档位不被改动。
        self.assertEqual(worker._vision_render_setting(seal_rule, "high")["scale"], 2.0)

    def test_rule_image_strategy_and_ocr_service_routing_are_generic(self):
        self.assertEqual(worker._rule_image_strategy({
            "title": "证书复印件", "check_rule": "核验证书编号及扫描件完整性",
        }), "hybrid")
        self.assertEqual(worker._rule_image_strategy({
            "title": "签字盖章", "check_rule": "核验响应函是否签字并加盖公章",
        }), "vision")
        self.assertEqual(worker._rule_image_strategy({
            "title": "声明函", "check_rule": "核对声明函填写内容与所属行业",
        }), "ocr")
        self.assertEqual(worker._ocr_service_candidates({"title": "资格证书"}, "standard")[0], "accurate")
        self.assertEqual(worker._ocr_service_candidates({"title": "普通印刷文字"}, "standard")[:3],
                         ["fast", "basic", "efficient"])
        self.assertEqual(worker._ocr_service_candidates({"title": "普通印刷文字"}, "low")[:3],
                         ["efficient", "fast", "basic"])

    def test_visual_prompt_allows_full_cross_layer_score_reconciliation(self):
        visual = PROMPT_TEMPLATES["evaluate_all_visual_user"]["content"]
        ocr = PROMPT_TEMPLATES["evaluate_all_ocr_user"]["content"]

        self.assertIn("文字证据与图片证据的合并覆盖", visual)
        self.assertIn("重新给出整条规则的 suggested_score", visual)
        self.assertIn("prior_image_batches", visual)
        self.assertIn("evidence_pages", visual)
        self.assertIn("irrelevant_pages", visual)
        self.assertIn("score_items", visual)
        self.assertIn("evidence_pages", ocr)
        self.assertIn("不得逐行抄录OCR全文", ocr)

    def test_high_vision_locator_groups_cover_scanned_document_without_persistent_cache(self):
        groups = worker._vision_locator_groups(25)
        self.assertEqual(groups[0], list(range(1, 13)))
        self.assertEqual(groups[1], list(range(13, 25)))
        self.assertEqual(groups[2], [25])

    def test_visual_locator_renders_labeled_contact_sheet_from_pdf(self):
        document = self._add_pdf("scan.pdf", "bid", "甲", "扫描件示例")
        document["page_count"] = 1
        sheets = worker._render_vision_locator_sheets(self.app, document, [[1]])
        self.assertEqual(sheets[0]["pages"], [1])
        self.assertEqual(sheets[0]["mime_type"], "image/jpeg")
        self.assertIsInstance(sheets[0]["image_bytes"], bytes)

    def test_uncovered_visual_response_does_not_pollute_text_result(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 300, "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "认证证书", "check_rule": "核验证书", "vision_trigger": "required", "vision_level": "low"}
        original = {"rule_id": "cert", "suggested_score": 2, "max_score": 2, "evidence": "P224 证书明细", "reason": "需 OCR 核验证书", "confidence": "high", "visual_page_candidates": [224]}
        task = {"task_id": "task"}
        profile = {"profile_id": "vision", "display_name": "图片模型"}
        with patch("dashboard.evaluation_workbench.worker._render_vision_images", return_value=[{
            "page": 224, "type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "low"},
        }]), patch("dashboard.evaluation_workbench.worker._request_task_json", return_value={
            "coverage": "not_covered", "needs_more_image": True, "requested_pages": [],
        }):
            result = worker._run_visual_supplement(self.app, task, document, "objective", rule, original, profile)
        self.assertEqual(result["evidence"], original["evidence"])
        self.assertEqual(result["reason"], original["reason"])
        self.assertEqual(result["vision_status"], "uncovered")
        self.assertEqual(result["vision_pages"], [224])
        self.assertNotIn("图片未触及", result["evidence"])

    def test_partial_visual_evidence_is_kept_without_overriding_text_score(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 300, "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "认证证书", "check_rule": "核验证书", "vision_trigger": "required", "vision_level": "standard",
                "scoring": {"max_score": 2}}
        original = {"rule_id": "cert", "suggested_score": 0, "max_score": 2, "evidence": "文字层待核验",
                    "reason": "需 OCR 核验证书", "confidence": "low", "visual_page_candidates": [195, 200, 197, 208]}
        task = {"task_id": "task"}
        profile = {"profile_id": "vision", "display_name": "图片模型"}

        def render_images(app, document, pages, level, **_kwargs):
            return [{"page": page, "type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "low"}} for page in pages]

        responses = [
            {"coverage": "covered", "conclusion_scope": "partial", "needs_more_image": True,
             "requested_pages": [], "evidence": "P195 可见证书编号", "reason": "仅覆盖一份证书",
             "suggested_score": 2, "confidence": "high"},
            {"coverage": "not_covered", "conclusion_scope": "none", "needs_more_image": True,
             "requested_pages": [], "evidence": "", "reason": "未见另一份证书"},
        ]
        with patch("dashboard.evaluation_workbench.worker._render_vision_images", side_effect=render_images), \
             patch("dashboard.evaluation_workbench.worker._request_task_json", side_effect=responses):
            result = worker._run_visual_supplement(self.app, task, document, "objective", rule, original, profile)

        self.assertEqual(result["vision_status"], "applied_partial")
        self.assertEqual(result["suggested_score"], 0)
        self.assertEqual(result["confidence"], "low")
        self.assertIn("P195 可见证书编号", result["evidence"])
        self.assertEqual(result["vision_pages"], [195, 200, 197, 208, 196, 201, 198, 209])

    def test_visual_field_conflict_is_highlighted_without_overriding_score(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 300, "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "认证证书", "check_rule": "核验证书", "vision_trigger": "required",
                "vision_level": "low", "scoring": {"max_score": 2}}
        original = {"rule_id": "cert", "suggested_score": 2, "max_score": 2, "evidence": "文字层证书号A123",
                    "reason": "文字层建议2分", "confidence": "high", "visual_page_candidates": [10]}
        task = {"task_id": "task"}
        profile = {"profile_id": "vision", "display_name": "图片模型"}
        response = {
            "coverage": "covered", "conclusion_scope": "full", "needs_more_image": False,
            "conflict_level": "material", "field_checks": [{
                "field": "证书号", "text_value": "A123", "image_value": "A128", "match": "conflict",
            }],
            "suggested_score": 0, "confidence": "high", "evidence": "图片可见证书号A128", "reason": "编号不一致",
        }
        with patch("dashboard.evaluation_workbench.worker._render_vision_images", return_value=[{
            "page": 10, "type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "low"},
        }]), patch("dashboard.evaluation_workbench.worker._request_task_json", return_value=response):
            result = worker._run_visual_supplement(self.app, task, document, "objective", rule, original, profile)

        self.assertEqual(result["vision_status"], "conflict")
        self.assertEqual(result["suggested_score"], 2)
        self.assertEqual(result["confidence"], "low")
        self.assertIn("文字层“A123”/ 图片“A128”", result["reason"])
        self.assertIn("重点复核", result["vision_message"])
        self.assertEqual(worker._visual_response_conflict_level({
            "conflict_level": "none",
            "field_checks": [{"match": "conflict", "text_value": "A123", "image_value": "A128"}],
        }), "possible")
        # 字段未在图片出现（无图片值）属于覆盖不足，不升级为冲突。
        self.assertEqual(worker._visual_response_conflict_level({
            "conflict_level": "none",
            "field_checks": [{"match": "no", "text_value": "A123"}, {"match": "mismatch", "text_value": "A123"}],
        }), "none")
        self.assertEqual(worker._visual_response_conflict_level({
            "conflict_level": "material", "field_checks": [],
        }), "possible")

    def test_possible_visual_suspicion_keeps_applied_status_instead_of_conflict(self):
        # 模型自评 possible（一般疑似）时保留图片补充成果，只在提示中告知人工留意。
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 300, "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "认证证书", "check_rule": "核验证书", "vision_trigger": "required",
                "vision_level": "low", "scoring": {"max_score": 2}}
        original = {"rule_id": "cert", "suggested_score": 1, "max_score": 2, "evidence": "文字层待核验",
                    "reason": "文字层建议1分", "confidence": "low", "visual_page_candidates": [10]}
        task = {"task_id": "task"}
        profile = {"profile_id": "vision", "display_name": "图片模型"}
        response = {
            "coverage": "covered", "conclusion_scope": "full", "needs_more_image": False,
            "conflict_level": "possible", "field_checks": [],
            "suggested_score": 2, "confidence": "high", "evidence": "图片可见证书齐全", "reason": "编号拼写疑似笔误",
        }
        with patch("dashboard.evaluation_workbench.worker._render_vision_images", return_value=[{
            "page": 10, "type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "low"},
        }]), patch("dashboard.evaluation_workbench.worker._request_task_json", return_value=response):
            result = worker._run_visual_supplement(self.app, task, document, "objective", rule, original, profile)

        self.assertEqual(result["vision_status"], "applied")
        self.assertEqual(result["suggested_score"], 2)
        self.assertEqual(result["confidence"], "high")
        self.assertIn("一般疑似字段差异", result["vision_message"])

    def test_review_normalisation_reconciles_positive_reason_and_negative_status(self):
        result = worker._normalise_review_results([{
            "rule_id": "joint", "status": "not_satisfied", "risk_level": "medium",
            "confidence": "high", "evidence": "未见联合体协议",
            "reason": "全文未发现联合体协议，投标人为单一主体，符合不接受联合体要求。",
        }], [{"rule_id": "joint", "check_mode": "auto"}])[0]
        self.assertEqual(result["status"], "satisfied")
        self.assertEqual(result["risk_level"], "low")

    def test_lowest_price_formula_is_recalculated_with_decimal_rounding(self):
        rule = {
            "title": "投标报价得分", "check_rule": "投标报价得分=（评标基准价／投标报价）×20，保留两位小数。",
            "source_text": "以最低评标价为评标基准价。",
        }
        score, calculation = worker._deterministic_price_score(rule, 835680, [833800, 835680, 838500], 20)
        self.assertEqual(score, 19.96)
        self.assertIn("19.96", calculation)

    def test_average_factor_price_formula_is_recalculated_only_when_all_terms_are_explicit(self):
        rule = {
            "title": "价格评分", "check_rule": "评标基准价为有效报价算术平均值的97%。投标报价高于基准价的，每高于1%扣1分；低于基准价的，每低于1%扣0.5分。",
            "source_text": "价格分满分45分。",
        }
        score, calculation = worker._deterministic_price_score(rule, 1_720_000, [1_720_000, 1_835_000, 1_925_000, 1_909_660], 45)
        self.assertEqual(score, 42.99)
        self.assertIn("评标基准价", calculation)
        ambiguous = {"title": "价格评分", "check_rule": "按评标基准价计算价格得分", "source_text": "满分45分"}
        self.assertIsNone(worker._deterministic_price_score(ambiguous, 100, [100, 120], 45)[0])

    def test_suggested_score_recovered_from_reason_text(self):
        rule = {"rule_id": "r1", "scoring": {"max_score": 14, "kind": "quantity"}}
        raw = {"reason": "逐项复制招标条款并标符合要求，未发现负偏离；按规则每项不扣分，结果=14分，封顶14分。建议14分。需人工核验证明材料。"}
        self.assertEqual(worker._suggested_score(rule, raw, "objective", 14.0), 14.0)

    def test_suggested_score_recovery_rejects_conflict_and_overflow(self):
        rule = {"rule_id": "r1", "scoring": {"max_score": 14, "kind": "quantity"}}
        conflict = {"reason": "建议14分。最终建议分：12分。"}
        self.assertIsNone(worker._suggested_score(rule, conflict, "objective", 14.0))
        overflow = {"reason": "建议20分。"}
        self.assertIsNone(worker._suggested_score(rule, overflow, "objective", 14.0))
        subjective = {"reason": "方案完整、针对性强，建议得7.5分。"}
        self.assertEqual(worker._suggested_score(rule, subjective, "subjective", 14.0), 7.5)

    def test_lowest_price_formula_matches_common_chinese_phrasing(self):
        rule = {
            "title": "投标报价评分（10分）", "check_rule": "",
            "source_text": "投标价格最低的投标报价为评标基准价，其价格分为满分。其他投标人的价格分统一按照下列公式计算：投标报价得分=（评标基准价/投标报价）×10。（1）最低报价不作为中标的唯一保证。",
        }
        self.assertEqual(worker._price_formula_kind(rule), "lowest_ratio")
        score, calculation = worker._deterministic_price_score(rule, 550000, [500000, 550000, 580000], 10)
        self.assertEqual(score, 9.09)
        self.assertIn("评标基准价", calculation)

    def test_visual_advance_estimated_covers_baseline_ocr_rules(self):
        enhancement = {"title": "证书图片核验", "vision_trigger": "text_fallback", "vision_level": "standard"}
        self.assertTrue(worker._visual_advance_estimated(enhancement))
        baseline = {"title": "报价字段核验", "execution_meta_json": json.dumps({"baseline_ocr_mode": "local_ocr"})}
        self.assertTrue(worker._visual_advance_estimated(baseline))
        required = {"title": "营业执照", "ocr_required": True}
        self.assertTrue(worker._visual_advance_estimated(required))
        text_only = {"title": "售后服务方案评分", "execution_meta_json": json.dumps({"baseline_ocr_mode": "text_only"})}
        self.assertFalse(worker._visual_advance_estimated(text_only))

    def test_directory_candidates_join_split_material_name_and_page_reference(self):
        pages = {
            2: "目录\n精密空调节能产品认证\n证书复印件……564\n",
            564: "节能产品认证证书扫描件",
        }
        rule = {"title": "精密空调节能产品认证证书", "check_rule": "核验证书复印件"}
        self.assertEqual(worker._directory_material_candidates(pages, rule, 600, 0), [564])

    def test_scope_candidate_known_to_tender_is_not_routed_as_off_topic(self):
        candidate = {"evidence": "响应清单列有移动储物柜及配件", "observation": "疑似与项目无关"}
        tender = "采购清单：移动储物柜，规格详见技术需求。"
        self.assertTrue(worker._scope_candidate_matches_tender_material(candidate, tender))
        self.assertFalse(worker._scope_candidate_matches_tender_material(
            {"evidence": "井道作业安全方案", "observation": "疑似无关"}, tender,
        ))

    def test_score_dedupe_ignores_section_navigation_prefix(self):
        left = {"category": "objective", "title": "商务部分-企业业绩评分（满分9分）", "check_rule": "业绩评分", "source_clause_ids": ["SC-1"], "scoring": {"max_score": 9}}
        right = {"category": "objective", "title": "企业业绩评分", "check_rule": "业绩评分", "source_clause_ids": ["SC-1"], "scoring": {"max_score": 9}}
        self.assertEqual(len(worker._dedupe_rule_candidates([left, right])), 1)

    def test_score_aggregate_prunes_only_its_provable_child_scores(self):
        aggregate = {
            "category": "subjective", "title": "服务方案29分评分", "source_type": "ai",
            "scoring": {"max_score": 29, "items": [
                {"name": "培训方案", "max_score": 5}, {"name": "售后服务方案", "max_score": 6},
                {"name": "组织实施保障方案", "max_score": 6}, {"name": "整体实施方案", "max_score": 6},
                {"name": "保障措施", "max_score": 1.5}, {"name": "应急预案", "max_score": 1.5},
                {"name": "项目化保障补充", "max_score": 3},
            ]},
        }
        children = [
            {"category": "subjective", "title": "培训方案5分评分", "source_type": "ai", "scoring": {"max_score": 5}},
            {"category": "subjective", "title": "售后服务方案6分评分", "source_type": "ai", "scoring": {"max_score": 6}},
        ]
        manual = {"category": "subjective", "title": "培训方案5分评分", "source_type": "ai_edited", "scoring": {"max_score": 5}}
        result = worker._prune_overlapping_score_aggregates([aggregate, *children, manual])
        self.assertEqual([item["title"] for item in result], ["服务方案29分评分", "培训方案5分评分"])

    def test_coverage_rule_catalog_keeps_leaf_requirements(self):
        rule = {
            "rule_id": "coverage", "category": "other", "title": "采购需求逐项响应覆盖",
            "check_rule": "；".join(f"第{index}项具体要求" for index in range(1, 100)),
        }
        catalog = worker._full_scan_catalog([rule])
        self.assertEqual(catalog[0]["coverage"], 1)
        self.assertGreater(len(catalog[0]["q"]), worker.FULL_SCAN_CATALOG_RULE_CHARS)

    def test_scope_anomaly_normalises_open_dimension_without_fixed_keywords(self):
        candidates = worker._normalise_scope_anomalies(
            [["127", "无关设备与工艺", "high", "锅炉燃烧控制设备", "不属于航测服务", "建议核验来源"]],
            {"chunk_id": "chunk_12", "start_page": 121, "end_page": 130},
        )

        self.assertEqual(candidates[0]["dimension"], "无关设备与工艺")
        self.assertEqual(candidates[0]["candidate_priority"], "high")
        self.assertEqual(candidates[0]["page_range"], "第121-130页")

    def test_scope_anomaly_filters_explicit_normal_items_but_keeps_outside_scope(self):
        candidates = worker._normalise_scope_anomalies([
            ["12", "设备", "low", "网络交换机安装", "与本项目技术要求一致，无异常"],
            ["13", "施工工艺", "high", "蒸汽管网吹扫", "属于本项目范围之外，需结合原页核验"],
            ["14", "历史业绩", "low", "外地项目名称", "合理出现，不构成范围偏离"],
        ], {"chunk_id": "chunk_2", "start_page": 11, "end_page": 20})

        self.assertEqual([item["evidence"] for item in candidates], ["蒸汽管网吹扫"])

    def test_scope_anomaly_merge_recovers_only_actionable_medium_high_history(self):
        current = [{"chunk_id": "chunk_2", "candidate_priority": "low", "evidence": "当前低风险线索",
                    "relation": "具体工作对象缺少范围依据"}]
        previous = [
            {"chunk_id": "chunk_2", "candidate_priority": "high", "evidence": "历史高价值线索",
             "relation": "具体工艺与采购范围不一致"},
            {"chunk_id": "chunk_2", "candidate_priority": "low", "evidence": "历史低价值线索",
             "relation": "题材存在差异"},
            {"chunk_id": "chunk_2", "candidate_priority": "high", "evidence": "明确正常内容",
             "relation": "与本项目范围一致，无异常"},
        ]

        merged = worker._merge_scope_anomalies(current, previous)

        self.assertEqual([item["evidence"] for item in merged], ["历史高价值线索", "当前低风险线索"])
        self.assertEqual(merged[0]["candidate_source"], "prior_scan")

    def test_previous_scope_anomalies_survive_scan_key_change_as_candidates(self):
        document = self._add_pdf("scope-history.pdf", "bid", "甲公司", "投标方案正文")
        old_candidate = {"chunk_id": "chunk_1", "page_range": "第1-10页", "page_hint": "8",
                         "candidate_priority": "high",
                         "evidence": "与采购对象不同的实施工艺", "relation": "缺少项目范围依据"}
        storage.save_evaluation_scan_checkpoint(
            self.app, self.project["project_id"], document["document_id"], "old-rule-catalog",
            "chunk_1", "same-content-hash", {"findings": [], "scope_anomalies": [old_candidate]},
        )

        recovered = storage.previous_scope_anomalies(
            self.app, document["document_id"], 1, 10,
        )

        self.assertEqual(recovered[0]["evidence"], old_candidate["evidence"])
        # 页码不重叠的历史候选不应返回；低优先级候选也不返回
        self.assertEqual(storage.previous_scope_anomalies(self.app, document["document_id"], 20, 30), [])
        low = {"chunk_id": "chunk_1", "page_range": "第1-10页", "candidate_priority": "low",
               "evidence": "低优先级线索"}
        storage.save_evaluation_scan_checkpoint(
            self.app, self.project["project_id"], document["document_id"], "old-rule-catalog",
            "chunk_2", "hash-b", {"findings": [], "scope_anomalies": [low]},
        )
        self.assertNotIn("低优先级线索", [item["evidence"] for item in
                                          storage.previous_scope_anomalies(self.app, document["document_id"], 1, 10)])

    def test_scope_candidate_evidence_visible_requires_evidence_in_chunk(self):
        candidate = {"evidence": "旧轮已定位的具体工艺", "candidate_priority": "high"}
        self.assertTrue(worker._scope_candidate_evidence_visible(
            candidate, re.sub(r"\s+", "", "安全章节包含 旧轮已定位的具体工艺 描述")))
        self.assertFalse(worker._scope_candidate_evidence_visible(candidate, "其他内容"))
        self.assertFalse(worker._scope_candidate_evidence_visible({"evidence": "短"}, "短"))

    def test_scope_profile_review_guidance_present(self):
        from dashboard.evaluation_workbench.prompt_templates import PROMPT_TEMPLATES, EVALUATION_PROMPT_VERSION
        guidance = PROMPT_TEMPLATES["evaluate_all_scope_anomaly_guidance"]["content"]
        self.assertIn("项目范围画像", guidance)
        self.assertIn("可解释性检验", guidance)
        self.assertIn("不限定行业或采购类型", guidance)
        scan = PROMPT_TEMPLATES["evaluate_all_full_scan_user"]["content"]
        self.assertIn("每 10 页最多 2 条，整块最多 12 条", scan)
        self.assertEqual(EVALUATION_PROMPT_VERSION, "vision-evidence-contract-v45")

    def test_full_scan_reruns_rule_evidence_but_rechecks_previous_scope_candidate(self):
        document = self._add_pdf("scope-rerun.pdf", "bid", "甲公司", "投标方案正文")
        document.update({"text_length": 30_000, "parsed_path": str(self.temp_dir / "unused.txt")})
        chunk = {"chunk_id": "chunk_1", "start_page": 1, "end_page": 10,
                 "text": "本轮模型未重新报告范围候选。旧轮已定位的具体工艺"}
        chunk_hash = hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        old_candidate = {"chunk_id": "chunk_1", "page_range": "第1-10页", "page_hint": "8",
                         "dimension": "工作对象偏离", "candidate_priority": "high",
                         "evidence": "旧轮已定位的具体工艺", "relation": "缺少项目范围依据"}
        storage.save_evaluation_scan_checkpoint(
            self.app, self.project["project_id"], document["document_id"], "old-rule-catalog",
            "chunk_1", chunk_hash, {"findings": [], "scope_anomalies": [old_candidate]},
        )
        task = {"task_id": "fresh-task", "project_id": self.project["project_id"],
                "payload": {"force_rerun": True}}
        profile = {"profile_id": "model-a", "model_name": "generic-model"}
        rules = [{"rule_id": "scope", "category": "other", "title": "项目范围无关内容核验",
                  "check_rule": "全文检查与本项目无关的内容", "source_text": ""}]

        with patch.object(worker, "_document_evidence_chunks", return_value=[chunk]), patch.object(
            worker, "_run_full_scan_piece",
            return_value=({"findings": [], "scope_anomalies": []}, 0, 0, []),
        ) as scan_piece:
            result = worker._scan_document_fulltext(
                self.app, task, profile, document, rules, {"scope_summary": "信息系统建设"}, "system",
            )

        scan_piece.assert_called_once()
        self.assertEqual(result["scope_anomalies"][0]["evidence"], old_candidate["evidence"])
        self.assertEqual(result["scope_anomalies"][0]["candidate_source"], "prior_scan")

    def test_full_scan_prompt_requires_topic_and_concrete_object_scope_checks(self):
        prompt = worker._full_scan_prompt(
            self.app, {"original_name": "投标.pdf", "bidder_name": "甲公司"}, [],
            {"chunk_id": "chunk_1", "start_page": 1, "end_page": 10, "text": "安全施工方案"},
            {"scope_summary": "信息系统建设"}, compact=False,
        )

        self.assertIn("先判断章节上位主题是否相关", prompt)
        self.assertIn("具体对象或工艺", prompt)
        self.assertNotIn("青铅", prompt)

    def test_combined_evaluation_splits_review_rules_into_small_groups(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "投标文件包含全部承诺。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        for index in range(9):
            storage.add_rule(self.app, self.project["project_id"], {
                "category": "qualification", "title": f"承诺事项{index}", "source_text": "承诺事项",
            })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"results": []}) as request_json:
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["batch_count"], 2)
        self.assertEqual(request_json.call_count, 2)

    def test_evaluation_batches_separate_evidence_strategies_and_respect_complexity(self):
        rules = [
            {"rule_id": "point", "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照"},
            {"rule_id": "count", "category": "objective", "title": "业绩数量", "check_rule": "每个业绩得3分"},
            {"rule_id": "section", "category": "subjective", "title": "技术方案", "check_rule": "核验实施方案各模块",
             "scoring_json": json.dumps({"max_score": 12, "items": [
                 {"name": f"模块{index}", "max_score": 2, "criterion": "按完整性评分"} for index in range(6)
             ]}, ensure_ascii=False)},
        ]

        groups = worker._evaluation_rule_batches("subjective", rules)

        self.assertEqual({rule["rule_id"] for group in groups for rule in group}, {"point", "count", "section"})
        self.assertTrue(all(len({worker._rule_execution_strategy(rule) for rule in group}) == 1 for group in groups))
        self.assertEqual(next(group for group in groups if group[0]["rule_id"] == "section"), [rules[2]])

    def test_evaluation_request_gate_promotes_then_degrades_one_level_at_a_time(self):
        gate = worker._EvaluationRequestGate(2, max_limit=3)

        for _ in range(6):
            gate.record_success()
        self.assertEqual(gate.limit, 3)
        self.assertTrue(gate.reduce_after_rate_limit())
        self.assertEqual(gate.limit, 2)
        self.assertTrue(gate.reduce_after_rate_limit())
        self.assertEqual(gate.limit, 1)

    def test_minimax_combined_evaluation_uses_recoverable_read_timeout_without_changing_profile(self):
        profile = {"base_url": "https://api.minimaxi.com/v1", "timeout_seconds": 600, "thinking_mode": "adaptive"}

        effective = worker._task_request_profile(profile, "evaluate_all_review_batch", "disabled")

        self.assertEqual(effective["timeout_seconds"], 240)
        self.assertEqual(effective["thinking_mode"], "disabled")
        self.assertEqual(profile["timeout_seconds"], 600)

    def test_single_compound_score_rule_splits_only_explicit_additive_items_after_truncation(self):
        self._add_pdf("compound.pdf", "bid", "甲公司", "部署方案完整。运维方案完整。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        document = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "bid")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "复合方案评分", "check_rule": "部署和运维各3分",
            "source_text": "部署方案3分，运维方案3分", "scoring": {"max_score": 6, "kind": "manual", "items": [
                {"name": "部署方案", "max_score": 3, "criterion": "完整合理"},
                {"name": "运维方案", "max_score": 3, "criterion": "完整合理"},
            ]},
        })
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        profile = storage.get_model_profile(self.app, None)
        responses = [
            worker.InvalidJsonResponse('{"results":[', "length"),
            {"results": [{"rule_id": rule["rule_id"], "suggested_score": 3, "evidence": "部署方案完整", "reason": "部署项得3分", "confidence": "high"}]},
            {"results": [{"rule_id": rule["rule_id"], "suggested_score": 3, "evidence": "运维方案完整", "reason": "运维项得3分", "confidence": "high"}]},
        ]

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=responses) as request_json:
            results, _, split_count, _, mode = worker._run_combined_batch(
                self.app, task, profile, document, "subjective", [rule], "综合评审系统提示", 60_000, "复合规则",
            )

        self.assertEqual(request_json.call_count, 3)
        self.assertEqual(split_count, 1)
        self.assertEqual(mode, "split_score_items")
        self.assertEqual(results[0]["suggested_score"], 6.0)
        self.assertIn("子项组1", results[0]["evidence"])
        self.assertIn("子项组2", results[0]["evidence"])

    def test_combined_evaluation_retries_only_invalid_json_document_with_compact_prompt(self):
        self._add_pdf("tender.pdf", "tender", "", "投标人具备资质得5分，技术方案满分10分。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "本公司具备资质，技术方案完整。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        review_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "有效资质", "source_text": "具备资质"})
        objective_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "objective", "title": "资质得分", "source_text": "具备资质得5分", "scoring": {"kind": "boolean", "max_score": 5}})
        subjective_rule = storage.add_rule(self.app, self.project["project_id"], {"category": "subjective", "title": "技术方案", "source_text": "技术方案满分10分", "scoring": {"max_score": 10}})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            {"project_identity": "测试项目", "scope_summary": "资质与技术方案", "service_targets": [], "core_tasks": [],
             "technical_topics": [], "equipment_or_materials": [], "deliverables": [], "standards_or_rules": [], "regions": [], "keywords": []},
            ValueError("模型未返回有效 JSON"),
            {"results": [{"rule_id": review_rule["rule_id"], "status": "satisfied", "evidence": "具备资质", "reason": "已提供", "risk_level": "low"}]},
            {"results": [{"rule_id": objective_rule["rule_id"], "met": True, "evidence": "具备资质", "reason": "已提供"}]},
            {"results": [{"rule_id": subjective_rule["rule_id"], "suggested_score": 8, "evidence": "技术方案完整", "reason": "较完整"}]},
        ]) as request_json:
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["compact_retry_count"], 1)
        self.assertEqual(request_json.call_count, 5)
        self.assertEqual(request_json.call_args_list[0].args[0]["thinking_mode"], "disabled")
        self.assertEqual(request_json.call_args_list[1].args[0]["thinking_mode"], "adaptive")
        self.assertEqual(request_json.call_args_list[2].args[0]["thinking_mode"], "disabled")

    def test_combined_evaluation_retries_only_truncated_rule_with_compact_prompt(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "本公司已提供中小企业声明函及全部字段。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        document = next(item for item in storage.list_documents(self.app, self.project["project_id"]) if item["role"] == "bid")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "中小企业声明函完整性", "source_text": "声明函字段完整",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        profile = storage.get_model_profile(self.app, None)
        responses = [
            {"results": [{"rule_id": rule["rule_id"], "status": "partial", "evidence": "第57页：",
                          "reason": "结论依据→已填写，但", "summary": "", "risk_level": "medium",
                          "confidence": "medium", "evidence_quality": "limited"}]},
            {"results": [{"rule_id": rule["rule_id"], "status": "partial", "evidence": "第57页字段完整",
                          "reason": "结论依据→已填写，需人工核验原件。", "summary": "字段完整，需人工核验",
                          "risk_level": "medium", "confidence": "medium", "evidence_quality": "limited"}]},
        ]
        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=responses) as request_json:
            results, retry_count, _, _, mode = worker._run_combined_batch(
                self.app, task, profile, document, "review", [rule], "综合评审系统提示", 60_000, "声明函规则",
            )
        self.assertEqual(request_json.call_count, 2)
        self.assertEqual(mode, "full_document+incomplete_retry")
        self.assertNotIn("结论输出不完整", results[0]["reason"])
        self.assertEqual(results[0]["reason"], "结论依据→已填写，需人工核验原件。")
        self.assertIn("字符串内不得出现未转义的英文双引号", request_json.call_args_list[1].args[2])

    def test_combined_evaluation_repairs_only_raw_response_before_resending_document(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "本公司具备有效资质，正文不应在修复调用中重发。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "有效资质", "source_text": "具备有效资质",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        raw = '{"results":[{"rule_id":"%s","status":"satisfied",}]}' % rule["rule_id"]
        repaired = {"results": [{"rule_id": rule["rule_id"], "status": "satisfied", "evidence": "有效资质",
                                 "reason": "已提供", "risk_level": "low", "confidence": "high",
                                 "evidence_quality": "sufficient"}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            worker.InvalidJsonResponse(raw, "stop"), repaired,
        ]) as request_json:
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertEqual(request_json.call_count, 2)
        self.assertIn(raw, request_json.call_args_list[1].args[2])
        self.assertNotIn("正文不应在修复调用中重发", request_json.call_args_list[1].args[2])
        self.assertEqual(request_json.call_args_list[1].args[0]["thinking_mode"], "disabled")

    def test_combined_evaluation_keeps_completed_groups_visible_after_later_connection_error(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "投标文件包含全部承诺。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        for index in range(9):
            storage.add_rule(self.app, self.project["project_id"], {
                "category": "qualification", "title": f"承诺事项{index}", "source_text": "承诺事项",
            })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            {"results": []},
            *[ValueError("模型连接失败：timeout") for _ in range(4)],
        ]), patch("dashboard.evaluation_workbench.worker.time.sleep"):
            finished = self._run_next_task()

        review_run, results = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["completion_state"], "partial_success")
        self.assertEqual(finished["progress"], 100)
        self.assertEqual(review_run["task_status"], "success")
        self.assertEqual(len(results), 9)
        self.assertEqual(len(finished["result"]["failed_units"]), 1)

    def test_combined_evaluation_keeps_running_when_single_rule_returns_invalid_json_twice(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "投标文件包含承诺事项。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "承诺事项", "source_text": "承诺事项",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            ValueError("模型未返回有效 JSON"),
            ValueError("模型未返回有效 JSON"),
        ]):
            finished = self._run_next_task()

        review_run, results = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["manual_fallback_rule_count"], 1)
        self.assertEqual(review_run["task_status"], "success")
        self.assertEqual(results[0]["rule_id"], rule["rule_id"])
        self.assertEqual(results[0]["status"], "manual")
        self.assertIn("格式异常", results[0]["reason"])

    def test_combined_evaluation_strictly_retries_group_before_splitting_it(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "投标文件包含资质和承诺。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rules = [
            storage.add_rule(self.app, self.project["project_id"], {
                "category": "qualification", "title": title, "source_text": title,
            })
            for title in ("资质", "承诺")
        ]
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        valid = lambda rule: {"results": [{"rule_id": rule["rule_id"], "status": "satisfied", "evidence": rule["title"], "reason": "已提供", "risk_level": "low"}]}

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[
            ValueError("模型未返回有效 JSON"),
            ValueError("模型未返回有效 JSON"),
            valid(rules[0]),
            valid(rules[1]),
        ]) as request_json:
            finished = self._run_next_task()

        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["compact_retry_count"], 1)
        self.assertEqual(finished["result"]["split_retry_count"], 1)
        self.assertEqual(request_json.call_count, 4)
        self.assertEqual(request_json.call_args_list[1].args[0]["thinking_mode"], "disabled")

    def test_token_usage_endpoint_returns_only_aggregated_metadata(self):
        task = storage.create_task(self.app, self.project["project_id"], "parse_documents")
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "test", None,
                                  input_chars=123, usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})

        response = self.app.test_client().get(f"/api/evaluation-workbench/projects/{self.project['project_id']}/token-usage")

        self.assertEqual(response.status_code, 200)
        usage = response.get_json()["usage"]
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(usage["input_chars"], 123)

    def test_build_info_endpoint_returns_version_metadata(self):
        response = self.app.test_client().get("/api/evaluation-workbench/build-info")

        self.assertEqual(response.status_code, 200)
        info = response.get_json()
        self.assertIn("commit", info)
        self.assertIn("deployed_at", info)
        self.assertIn("prompt_version", info)

    def test_token_usage_endpoint_includes_latest_evaluation_run(self):
        task = storage.create_task(
            self.app, self.project["project_id"], "evaluate_all",
            {"profile_id": None, "prompt_version": "v-test", "deploy_commit": "abc123"},
        )
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "review", None,
                                  input_chars=456, usage={"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240})
        storage.update_task(self.app, task["task_id"], status="success", result={"ok": True})

        response = self.app.test_client().get(f"/api/evaluation-workbench/projects/{self.project['project_id']}/token-usage")

        self.assertEqual(response.status_code, 200)
        latest = response.get_json()["latest_run"]
        self.assertIsNotNone(latest)
        self.assertEqual(latest["total_tokens"], 240)
        self.assertEqual(latest["call_count"], 1)
        self.assertEqual(latest["deploy_commit"], "abc123")
        self.assertEqual(latest["prompt_version"], "v-test")
        self.assertTrue(latest["finished_at"])

    def test_latest_run_prefers_most_recent_consuming_task(self):
        evaluate = storage.create_task(self.app, self.project["project_id"], "evaluate_all", {"prompt_version": "v1", "deploy_commit": "aaa1111"})
        storage.record_model_call(self.app, evaluate["task_id"], self.project["project_id"], "review", None,
                                  input_chars=100, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        storage.update_task(self.app, evaluate["task_id"], status="success", result={"ok": True})
        time.sleep(1)
        compare = storage.create_task(self.app, self.project["project_id"], "compare_documents", {"prompt_version": "v2", "deploy_commit": "bbb2222"})
        storage.record_model_call(self.app, compare["task_id"], self.project["project_id"], "compare_ai", None,
                                  input_chars=200, usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})
        storage.update_task(self.app, compare["task_id"], status="success", result={"ok": True})
        time.sleep(1)
        parse = storage.create_task(self.app, self.project["project_id"], "parse_documents")
        storage.update_task(self.app, parse["task_id"], status="success", result={"ok": True})

        response = self.app.test_client().get(f"/api/evaluation-workbench/projects/{self.project['project_id']}/token-usage")

        latest = response.get_json()["latest_run"]
        self.assertEqual(latest["task_type"], "compare_documents")
        self.assertEqual(latest["total_tokens"], 30)
        self.assertEqual(latest["deploy_commit"], "bbb2222")

    def test_combined_task_can_reuse_matching_completed_input(self):
        self._add_pdf("tender.pdf", "tender", "", "资质得5分，技术方案满分10分。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "具备资质，技术方案完整。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "资质", "source_text": "具备资质"})
        storage.add_rule(self.app, self.project["project_id"], {"category": "objective", "title": "资质得分", "source_text": "资质得5分", "scoring": {"kind": "boolean", "max_score": 5}})
        storage.add_rule(self.app, self.project["project_id"], {"category": "subjective", "title": "技术方案", "source_text": "技术方案满分10分", "scoring": {"max_score": 10}})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        fingerprint = storage.task_input_fingerprint(self.app, self.project["project_id"], "evaluate_all", None, worker.PROMPT_VERSION)
        prior = storage.create_task(self.app, self.project["project_id"], "evaluate_all", {"profile_id": None, "input_fingerprint": fingerprint})
        storage.update_task(self.app, prior["task_id"], status="success", result={"cached": True})

        with patch("dashboard.blueprints.evaluation_workbench._start_worker_if_needed"):
            response = self.app.test_client().post(
                f"/api/evaluation-workbench/projects/{self.project['project_id']}/tasks",
                json={"task_type": "evaluate_all"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["reused"])
        self.assertEqual(response.get_json()["task"]["task_id"], prior["task_id"])

    def test_combined_evaluation_allows_only_review_rules_and_marks_ocr_requirement(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "营业执照信息。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "证照图像", "source_text": "提供清晰证照图片", "ocr_required": True,
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        with patch("dashboard.blueprints.evaluation_workbench._start_worker_if_needed"):
            response = self.app.test_client().post(
                f"/api/evaluation-workbench/projects/{self.project['project_id']}/tasks",
                json={"task_type": "evaluate_all"},
            )
        self.assertEqual(response.status_code, 202)

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={
            "results": [{"rule_id": rule["rule_id"], "status": "ocr_required"}],
        }) as request_json:
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(request_json.call_count, 1)
        self.assertEqual(reviews[0]["status"], "ocr_required")
        self.assertEqual(reviews[0]["risk_level"], "low")
        self.assertIn("OCR", reviews[0]["reason"])

    def test_combined_evaluation_reuses_unchanged_bid_documents_after_adding_a_bid(self):
        self._add_pdf("bid-a.pdf", "bid", "甲公司", "甲公司具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "有效资质", "source_text": "具备有效资质"})
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        response = {"results": [{"rule_id": rule["rule_id"], "status": "satisfied", "evidence": "有效资质", "reason": "已提供", "risk_level": "low"}]}
        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response):
            self._run_next_task()

        self._add_pdf("bid-b.pdf", "bid", "乙公司", "乙公司具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response) as request_json:
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["result"]["reused_document_count"], 1)
        self.assertEqual(finished["result"]["model_document_count"], 1)
        self.assertEqual(request_json.call_count, 1)
        self.assertEqual(len(reviews), 2)

    def test_combined_evaluation_does_not_reuse_results_from_old_prompt_version(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "甲公司具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "有效资质", "source_text": "具备有效资质",
        })
        rule_set = storage.confirm_rule_set(self.app, self.project["project_id"])
        profile = storage.get_model_profile(self.app, None)
        old_task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        old_run = storage.create_review_run(self.app, self.project["project_id"], old_task["task_id"], profile["profile_id"])
        storage.save_review_results(self.app, old_run["review_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "status": "manual", "reason": "旧版未定位到上下文",
        }])
        storage.update_task(self.app, old_task["task_id"], status="success", result={
            "review_run_id": old_run["review_run_id"], "prompt_version": "token-optimized-v3",
            "rule_set_id": rule_set["rule_set_id"],
        })
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        response = {"results": [{
            "rule_id": rule["rule_id"], "status": "satisfied", "evidence": "有效资质",
            "reason": "全文确认已提供", "risk_level": "low", "confidence": "high",
            "evidence_quality": "sufficient",
        }]}

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response) as request_json:
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["result"]["reused_document_count"], 0)
        self.assertEqual(finished["result"]["model_document_count"], 1)
        self.assertEqual(request_json.call_count, 1)
        self.assertEqual(reviews[0]["status"], "satisfied")

    def test_rule_extraction_does_not_hard_filter_model_returned_rules(self):
        self._add_pdf("tender.pdf", "tender", "", "资格审查和评分标准。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.create_task(self.app, self.project["project_id"], "extract_rules")

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"rules": [
            {"category": "qualification", "title": "具备资质", "source_text": "提供有效资质"},
            {"category": "compliance", "title": "响应文件份数", "source_text": "响应文件正本一份、副本两份"},
        ]}):
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["result"]["excluded_rule_count"], 0)
        self.assertEqual({item["title"] for item in rules}, {"具备资质", "响应文件份数"})

    def test_rule_extraction_keeps_performance_score_with_bid_deadline_range(self):
        self._add_pdf("tender.pdf", "tender", "", "业绩每有一个得3分，最高9分。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.create_task(self.app, self.project["project_id"], "extract_rules")

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"rules": [
            {"category": "objective", "title": "同类项目业绩", "check_rule": "核验投标截止日前三年内同类项目业绩，每个得3分，最高9分。", "source_text": "供应商提供投标截止日期三个年度内同类型项目业绩，每有一个得3分，最高9分。", "scoring": {"max_score": 9, "kind": "manual"}},
            {"category": "compliance", "title": "响应文件份数", "check_rule": "核验是否按要求提交正副本份数", "source_text": "响应文件正本一份、副本两份"},
        ]}):
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["excluded_rule_count"], 0)
        performance_rule = next(item for item in rules if item["title"] == "同类项目业绩")
        self.assertEqual(performance_rule["category"], "objective")
        self.assertEqual(performance_rule["scoring_json"], '{"max_score": 9, "kind": "manual"}')

    def test_objective_score_calculates_confirmed_boolean_rule(self):
        self._add_pdf("tender.pdf", "tender", "", "具备有效资质得5分。")
        self._add_pdf("bid.pdf", "bid", "甲公司", "本公司具备有效资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "有效资质", "source_text": "具备有效资质得5分。",
            "scoring": {"kind": "boolean", "max_score": 5},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "score_objective")

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={"results": [
            {"rule_id": rule["rule_id"], "met": True, "evidence": "具备有效资质", "reason": "已提供"},
        ]}):
            finished = self._run_next_task()

        score_run, results = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        self.assertEqual(finished["status"], "success")
        self.assertIsNotNone(score_run)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["suggested_score"], 5.0)
        self.assertIsNone(results[0]["final_score"])
        updated = storage.update_final_score(self.app, results[0]["score_result_id"], 4.5)
        self.assertEqual(updated["final_score"], 4.5)

    def test_confirming_rules_infers_explicit_max_score_from_source_text(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "业绩证明评分",
            "source_text": "每提供一个得1分，最多得2分。",
        })

        storage.confirm_rule_set(self.app, self.project["project_id"])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        stored = next(item for item in rules if item["rule_id"] == rule["rule_id"])
        scoring = __import__("json").loads(stored["scoring_json"])
        self.assertEqual(scoring["max_score"], 2.0)
        self.assertEqual(scoring["kind"], "manual")
        self.assertEqual(scoring["source"], "source_text_inferred")

    def test_confirming_rules_disables_non_file_scoring_process_with_zero_score(self):
        storage.add_rule(self.app, self.project["project_id"], {
            "category": "compliance", "title": "有效响应函", "check_rule": "核验响应函是否已提供。",
        })
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "异常低价投标审查启动与处理",
            "check_rule": "评审委员会启动异常低价审查后，要求供应商在评审现场说明报价合理性。",
            "source_text": "供应商不能在合理时间说明报价合理性的，作无效投标处理。",
            "scoring": {"max_score": 0, "kind": "manual"},
        })

        storage.confirm_rule_set(self.app, self.project["project_id"])

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        stored = next(item for item in rules if item["rule_id"] == rule["rule_id"])
        self.assertFalse(stored["enabled"])

    def test_extraction_filter_removes_non_file_scoring_process(self):
        rules = [{
            "category": "objective", "title": "异常低价投标审查启动与处理",
            "check_rule": "在评审现场要求供应商说明报价合理性。",
            "source_text": "异常低价投标审查后给予说明时间。",
            "scoring": {"max_score": 0, "kind": "manual"},
        }]

        self.assertTrue(worker._is_non_file_scoring_process(rules[0]))

    def test_manual_objective_and_subjective_rules_preserve_explicit_scoring(self):
        objective = storage.add_rule(self.app, self.project["project_id"], {
            "category": "objective", "title": "人工补充业绩评分", "check_rule": "核验业绩数量并按规则计分。",
            "scoring": {"max_score": 9, "kind": "manual"},
        })
        subjective = storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "人工补充方案评分", "check_rule": "评价方案完整性和可行性。",
            "scoring": {"max_score": 15, "kind": "manual"},
        })

        storage.confirm_rule_set(self.app, self.project["project_id"])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        scoring_by_rule = {item["rule_id"]: __import__("json").loads(item["scoring_json"]) for item in rules}

        self.assertEqual(scoring_by_rule[objective["rule_id"]], {"max_score": 9, "kind": "manual"})
        self.assertEqual(scoring_by_rule[subjective["rule_id"]], {"max_score": 15, "kind": "manual"})

    def test_draft_scoring_rule_can_be_corrected_without_recreation(self):
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "subjective", "title": "方案评分", "source_text": "方案评分。",
        })

        response = self.app.test_client().patch(
            f"/api/evaluation-workbench/projects/{self.project['project_id']}/rules/{rule['rule_id']}",
            json={"scoring": {"max_score": 10}},
        )

        self.assertEqual(response.status_code, 200)
        scoring = __import__("json").loads(response.get_json()["rule"]["scoring_json"])
        self.assertEqual(scoring["max_score"], 10.0)
        self.assertEqual(scoring["source"], "manual")

    def test_reextract_preserves_edited_content_and_selection_states(self):
        first = storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-1", [{
            "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照", "source_text": "应提供营业执照。",
        }])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        ai_rule = next(item for item in rules if item["source_type"] == "ai")
        edited_rule = storage.update_rule(self.app, self.project["project_id"], ai_rule["rule_id"], {
            "check_rule": "核验营业执照及其有效状态",
        })
        storage.update_rule(self.app, self.project["project_id"], edited_rule["rule_id"], {"enabled": False})
        manual_rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "other", "title": "人工补充承诺", "check_rule": "核验承诺函", "source_text": "人工维护规则。",
        })
        storage.update_rule(self.app, self.project["project_id"], manual_rule["rule_id"], {"enabled": False})

        second = storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-2", [{
            "category": "qualification", "title": "投标人资质", "check_rule": "核验投标人资质", "source_text": "应具备资质。",
        }])
        _, refreshed = storage.list_rules(self.app, self.project["project_id"])

        self.assertEqual(first["version"] + 1, second["version"])
        self.assertEqual(second["preserved_rule_count"], 2)
        edited = next(item for item in refreshed if item["title"] == "营业执照")
        manual = next(item for item in refreshed if item["title"] == "人工补充承诺")
        self.assertEqual(edited["source_type"], "ai_edited")
        self.assertEqual(manual["source_type"], "manual")
        # 新设计：人工/编辑规则无条件继承并沿用启用状态，不再重置为启用，
        # 避免“用户停用的规则在重新提取后复活”。
        self.assertEqual(edited["enabled"], 0)
        self.assertEqual(manual["enabled"], 0)

    def test_reextract_does_not_preserve_ai_rule_selection_only_changes(self):
        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-1", [{
            "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照", "source_text": "应提供营业执照。",
        }])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        original = next(item for item in rules if item["title"] == "营业执照")
        unchecked = storage.update_rule(
            self.app, self.project["project_id"], original["rule_id"], {"enabled": False},
        )
        self.assertEqual(unchecked["source_type"], "ai")

        second = storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-2", [{
            "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照", "source_text": "应提供营业执照。",
        }])
        _, refreshed = storage.list_rules(self.app, self.project["project_id"])
        extracted_again = next(item for item in refreshed if item["title"] == "营业执照")

        self.assertEqual(second["preserved_rule_count"], 0)
        self.assertNotEqual(extracted_again["rule_id"], original["rule_id"])
        self.assertEqual(extracted_again["source_type"], "ai")
        self.assertEqual(extracted_again["enabled"], 1)

    def test_reextract_skips_ai_duplicate_of_edited_score_rule(self):
        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-1", [{
            "category": "objective", "title": "相关证书评分（5分）",
            "check_rule": "满分5分，按证书类别独立计分：①质量管理体系认证证书有效得1分",
            "source_text": "（1）投标人具有有效的质量管理体系认证证书，得1分",
            "scoring": {"max_score": 5},
        }])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        ai_rule = next(item for item in rules if item["title"] == "相关证书评分（5分）")
        edited = storage.update_rule(self.app, self.project["project_id"], ai_rule["rule_id"], {
            "check_rule": "核验投标文件是否附有效期内管理体系认证证书复印件：(1)质量管理体系认证证书得1分",
        })
        self.assertEqual(edited["source_type"], "ai_edited")

        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-2", [{
            "category": "objective", "title": "相关证书评分（5分）",
            "check_rule": "满分5分，按证书类别独立计分：①质量管理体系认证证书有效得1分",
            "source_text": "（1）投标人具有有效的质量管理体系认证证书，得1分",
            "scoring": {"max_score": 5},
        }, {
            "category": "objective", "title": "相关业绩评分（8分）",
            "check_rule": "满分8分，按同类项目合同案例计分",
            "source_text": "投标人提供同类项目合同案例，每个得2分，满分8分",
            "scoring": {"max_score": 8},
        }])
        _, refreshed = storage.list_rules(self.app, self.project["project_id"])

        certificate_rules = [item for item in refreshed if item["title"] == "相关证书评分（5分）"]
        self.assertEqual(len(certificate_rules), 1)
        self.assertEqual(certificate_rules[0]["source_type"], "ai_edited")
        self.assertEqual(certificate_rules[0]["check_rule"], "核验投标文件是否附有效期内管理体系认证证书复印件：(1)质量管理体系认证证书得1分")
        performance_rules = [item for item in refreshed if item["title"] == "相关业绩评分（8分）"]
        self.assertEqual(len(performance_rules), 1)
        self.assertEqual(performance_rules[0]["source_type"], "ai")

    def test_reextract_keeps_ai_visual_rules_disabled_but_uses_global_default_selection(self):
        storage.create_global_rule(self.app, {
            "category": "qualification", "title": "通用许可证", "check_rule": "核验许可证图像", "ocr_required": True,
        })
        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-ocr", [
            {"category": "qualification", "title": "营业执照", "check_rule": "核验营业执照文字", "source_text": "提供营业执照。"},
            {"category": "qualification", "title": "签字盖章", "check_rule": "核验签章图像", "source_text": "签字盖章。", "ocr_required": True},
        ])

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        enabled = {item["title"]: item["enabled"] for item in rules}

        self.assertEqual(enabled["营业执照"], 1)
        self.assertEqual(enabled["签字盖章"], 0)
        self.assertEqual(enabled["通用许可证"], 1)

    def test_force_rerun_is_persisted_in_task_payload(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "已提供资质。")
        storage.create_task(self.app, self.project["project_id"], "parse_documents")
        self._run_next_task()
        storage.add_rule(self.app, self.project["project_id"], {"category": "qualification", "title": "资质"})
        storage.confirm_rule_set(self.app, self.project["project_id"])

        with patch("dashboard.blueprints.evaluation_workbench._start_worker_if_needed"):
            response = self.app.test_client().post(
                f"/api/evaluation-workbench/projects/{self.project['project_id']}/tasks",
                json={"task_type": "evaluate_all", "force_rerun": True},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["task"]["payload"]["force_rerun"])
        self.assertTrue(response.get_json()["task"]["payload"]["input_fingerprint"])

    def test_printable_report_is_generated_on_demand(self):
        self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案。")
        task = storage.create_task(self.app, self.project["project_id"], "compare_documents")
        analysis = build_cross_bid_analysis(task["task_id"], [], tender_loaded=False)
        storage.update_task(
            self.app, task["task_id"], status="success",
            result={"pair_count": 0, "pairs": [], "cross_bid_analysis": analysis},
        )

        response = self.app.test_client().get(f"/pingbiao/projects/{self.project['project_id']}/report")

        self.assertEqual(response.status_code, 200)
        self.assertIn("评标辅助汇总报告", response.get_data(as_text=True))
        self.assertIn("不构成串通投标认定", response.get_data(as_text=True))

    def test_sparse_scan_document_does_not_turn_missing_text_into_negative_or_positive_conclusions(self):
        document = {"page_count": 110, "text_length": 1652}
        rule = {"rule_id": "license", "title": "资格证书", "check_rule": "核验证书是否有效"}

        review = worker._apply_document_evidence_guard(document, "review", rule, {
            "rule_id": "license", "status": "satisfied", "risk_level": "low", "confidence": "low",
            "evidence_quality": "missing", "reason": "未定位直接证据。",
        })
        score = worker._apply_document_evidence_guard(document, "objective", rule, {
            "rule_id": "license", "suggested_score": 0.0, "effective_score": None, "confidence": "low",
            "reason": "未提供证书。",
        })

        self.assertEqual(review["status"], "ocr_required")
        self.assertEqual(review["risk_level"], "low")
        self.assertEqual(review["coverage_status"], "uncovered")
        self.assertIn("未覆盖不等同于材料缺失", review["reason"])
        self.assertIsNone(score["suggested_score"])
        self.assertEqual(score["coverage_status"], "uncovered")

    def test_sparse_scan_document_accepts_rule_after_complete_ocr_or_vision_coverage(self):
        document = {"page_count": 110, "text_length": 1652}
        rule = {"rule_id": "cert", "title": "管理体系认证", "check_rule": "核验三项认证"}
        result = worker._apply_document_evidence_guard(document, "objective", rule, {
            "rule_id": "cert", "suggested_score": 3.0, "confidence": "high", "coverage_status": "covered",
            "reason": "已核验三项证书。",
        })

        self.assertEqual(result["suggested_score"], 3.0)
        self.assertEqual(result["coverage_status"], "covered")

    def test_machine_readable_document_keeps_normal_text_conclusion(self):
        document = {"page_count": 55, "text_length": 37935}
        rule = {"rule_id": "scope", "title": "项目范围", "check_rule": "核验项目范围一致性"}
        result = worker._apply_document_evidence_guard(document, "review", rule, {
            "rule_id": "scope", "status": "satisfied", "risk_level": "low", "confidence": "high",
            "evidence_quality": "sufficient", "reason": "范围一致。",
        })

        self.assertEqual(result["status"], "satisfied")
        self.assertEqual(result["coverage_status"], "covered")

    def test_rule_set_changes_do_not_hide_most_recent_review_and_score_results(self):
        """新增/编辑规则或重新提取生成新版本后，最近一次有结果的运行仍应可见。"""
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "技术方案：稳定运行。")
        rule = storage.add_rule(self.app, self.project["project_id"], {
            "category": "qualification", "title": "资质", "check_rule": "核验资质",
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        storage.update_task(self.app, task["task_id"], status="success")
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], "profile-1")
        storage.save_review_results(self.app, review_run["review_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "status": "satisfied", "evidence": "已提供资质",
            "risk_level": "low", "confidence": "high", "evidence_quality": "sufficient",
        }])
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", "profile-1")
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [{
            "rule_id": rule["rule_id"], "suggested_score": 5.0, "max_score": 5.0,
        }])
        # 用户随后新增规则会生成更高版本的待确认草稿；结果仍应可见。
        storage.add_rule(self.app, self.project["project_id"], {"category": "compliance", "title": "响应"})

        run, results = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertIsNotNone(run)
        self.assertEqual([item["rule_id"] for item in results], [rule["rule_id"]])
        score_run, score_rows = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        self.assertIsNotNone(score_run)
        self.assertEqual([item["rule_id"] for item in score_rows], [rule["rule_id"]])

        # 重新提取规则会把旧确认版本标记为 superseded 并生成新草稿；历史结果仍保留展示。
        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-2", [{
            "category": "qualification", "title": "投标人资质", "check_rule": "核验投标人资质",
        }])
        run, results = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertIsNotNone(run)
        self.assertEqual([item["rule_id"] for item in results], [rule["rule_id"]])

    def test_acquisition_setting_change_locks_ai_rule_across_reextract(self):
        """用户调整 AI 规则的图片取证设置后，重新提取时该规则应整体保留。"""
        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-1", [{
            "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照", "source_text": "应提供营业执照。",
        }])
        _, rules = storage.list_rules(self.app, self.project["project_id"])
        ai_rule = next(item for item in rules if item["source_type"] == "ai")
        tuned = storage.update_rule(self.app, self.project["project_id"], ai_rule["rule_id"], {
            "acquisition_preset": "smart", "image_mode": "auto",
            "vision_trigger": "text_fallback", "vision_level": "standard",
        })
        self.assertEqual(tuned["source_type"], "ai_edited")

        storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-2", [{
            "category": "qualification", "title": "营业执照", "check_rule": "核验营业执照", "source_text": "应提供营业执照。",
        }])
        _, refreshed = storage.list_rules(self.app, self.project["project_id"])
        preserved = next(item for item in refreshed if item["title"] == "营业执照")
        self.assertEqual(preserved["source_type"], "ai_edited")
        self.assertEqual(preserved["vision_trigger"], "text_fallback")
        self.assertEqual(len([item for item in refreshed if item["title"] == "营业执照"]), 1)

    def test_profile_parallel_limit_scales_with_document_count(self):
        """并行档位按投标人数量自适应：1~2 家不受影响，3 家及以上才用满上限。"""
        profile = {"base_url": "https://api.minimaxi.com/v1", "model_name": "MiniMax-M3"}

        self.assertEqual(worker._profile_parallel_limit(profile, 1), 1)
        self.assertEqual(worker._profile_parallel_limit(profile, 2), 2)
        self.assertEqual(worker._profile_parallel_limit(profile, 3), 3)
        self.assertEqual(worker._profile_parallel_limit(profile, 8), 3)

        with patch.dict(os.environ, {"MINIMAX_PARALLEL_LIMIT": "1"}, clear=False):
            self.assertEqual(worker._profile_parallel_limit(profile, 8), 1)
        with patch.dict(os.environ, {"MINIMAX_PARALLEL_LIMIT": "4"}, clear=False):
            self.assertEqual(worker._profile_parallel_limit(profile, 8), 4)

    def test_vision_parallel_limit_defaults_to_two_and_respects_env(self):
        """视觉并发默认 2 路，并支持环境变量 1-4 收敛。"""
        with patch.dict(os.environ, {"VISION_PARALLEL_LIMIT": "1"}, clear=False):
            self.assertEqual(worker._vision_parallel_limit(), 1)
        with patch.dict(os.environ, {"VISION_PARALLEL_LIMIT": "9"}, clear=False):
            self.assertEqual(worker._vision_parallel_limit(), 4)
        with patch.dict(os.environ, {"VISION_PARALLEL_LIMIT": "abc"}, clear=False):
            self.assertEqual(worker._vision_parallel_limit(), 2)
        with patch.dict(os.environ, {"VISION_PARALLEL_LIMIT": "2"}, clear=False):
            self.assertEqual(worker._vision_parallel_limit(), 2)

    def test_full_scan_chunk_size_reduces_call_count(self):
        """全文扫描块从 11K 提升到 14K，同文本的调用块数应减少。"""
        self.assertEqual(worker.FULL_SCAN_CHUNK_CHARS, 14_000)
        path = self.temp_dir / "chunk-scale.txt"
        path.write_text(
            "\n\n".join(f"[第{i}页]\n" + "内容" * 300 for i in range(1, 41)),
            encoding="utf-8",
        )
        chunks_large = split_full_text_chunks(path, worker.FULL_SCAN_CHUNK_CHARS, overlap_pages=1)
        chunks_small = split_full_text_chunks(path, 11_000, overlap_pages=1)
        self.assertLess(len(chunks_large), len(chunks_small))
        self.assertLessEqual(len(chunks_large), 3)

    def test_scan_document_fulltext_parallel_chunks_preserve_order(self):
        """单文件页块并行扫描后，findings 仍按页块顺序归并。"""
        document = self._add_pdf("scan.pdf", "bid", "甲公司", "投标方案正文")
        document.update({"text_length": 50_000})
        chunks = [
            {"chunk_id": "c1", "start_page": 1, "end_page": 2, "text": "第一段" * 20},
            {"chunk_id": "c2", "start_page": 3, "end_page": 4, "text": "第二段" * 20},
            {"chunk_id": "c3", "start_page": 5, "end_page": 6, "text": "第三段" * 20},
        ]

        def fake_piece(app, task, profile, document, catalog, chunk, project_scope, system_prompt):
            time.sleep(0.01)
            return ({"findings": [{"rule_id": "r1", "page_hint": chunk["chunk_id"]}], "scope_anomalies": []}, 0, 0, [])

        task = {"task_id": "t", "project_id": self.project["project_id"], "payload": {"force_rerun": False}}
        with patch.object(worker, "_document_evidence_chunks", return_value=chunks), \
             patch.object(worker, "_run_full_scan_piece", side_effect=fake_piece) as piece:
            result = worker._scan_document_fulltext(
                self.app, task, {"profile_id": "p"}, document,
                [{"rule_id": "r1", "category": "qualification", "title": "资质", "check_rule": "核验资质"}],
                {"scope_summary": "x"}, "sys",
            )

        self.assertEqual([item["page_hint"] for item in result["findings"]], ["c1", "c2", "c3"])
        self.assertEqual(piece.call_count, 3)

    def test_acquisition_recommendation_defaults_low_for_non_visual_rules(self):
        """非签章/外观类规则默认“快速”档，视觉事实规则保持“标准”。"""
        certificate = storage.rule_acquisition_recommendation({
            "title": "认证证书编号核验", "check_rule": "核验证书编号与有效期", "source_text": "提供认证证书复印件",
            "check_mode": "ocr", "ocr_required": True,
            "execution_meta_json": {"evidence_requirements": ["document", "field"]},
        })
        self.assertEqual(certificate["acquisition_preset"], "smart")
        self.assertEqual(certificate["vision_level"], "low")

        signature = storage.rule_acquisition_recommendation({
            "title": "签章核验", "check_rule": "核验响应函是否签字并加盖公章", "source_text": "",
        })
        self.assertNotEqual(signature["acquisition_preset"], "off")
        self.assertEqual(signature["vision_level"], "standard")

    def test_vision_render_setting_downscales_standard_for_non_visual_rules(self):
        """材料/字段类规则标准档降采样；纯视觉（签章）规则保持高清晰。"""
        certificate = {"title": "认证证书", "check_rule": "核验证书编号及扫描件完整性"}
        self.assertEqual(worker._rule_image_strategy(certificate), "hybrid")
        setting = worker._vision_render_setting(certificate, "standard")
        self.assertEqual(setting["scale"], 1.2)
        self.assertEqual(setting["quality"], 76)

        signature = {"title": "签字盖章", "check_rule": "核验响应函是否签字并加盖公章"}
        self.assertEqual(worker._rule_image_strategy(signature), "vision")
        seal_setting = worker._vision_render_setting(signature, "standard")
        self.assertEqual(seal_setting["scale"], 1.8)
        self.assertEqual(seal_setting["quality"], 82)

    def test_ocr_batch_enabled_flag_defaults_off(self):
        """OCR 归纳批量合并默认关闭，需 A/B 验证后再开启。"""
        with patch.dict(os.environ, {"EVALUATION_WORKBENCH_OCR_BATCH": ""}, clear=True):
            self.assertFalse(worker._ocr_batch_enabled())
        with patch.dict(os.environ, {"EVALUATION_WORKBENCH_OCR_BATCH": "1"}, clear=True):
            self.assertTrue(worker._ocr_batch_enabled())
        with patch.dict(os.environ, {"EVALUATION_WORKBENCH_OCR_BATCH": "off"}, clear=True):
            self.assertFalse(worker._ocr_batch_enabled())

    def test_ocr_batch_supplement_uses_one_model_call_and_merges_per_rule(self):
        """同组件多条规则合并为一次 OCR 归纳调用，并按 rule_id 逐条合并。"""
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 50,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rules = [
            {"rule_id": "r1", "title": "证书评分", "check_rule": "证书有效得2分", "scoring": {"max_score": 2}},
            {"rule_id": "r2", "title": "业绩评分", "check_rule": "每个业绩得1分", "scoring": {"max_score": 3}},
        ]
        bases = [
            {"rule_id": "r1", "suggested_score": 0, "max_score": 2, "confidence": "low", "evidence": "文字层未见"},
            {"rule_id": "r2", "suggested_score": 0, "max_score": 3, "confidence": "low", "evidence": "文字层未见"},
        ]
        parsed = {"results": [
            {"rule_id": "r1", "coverage": "covered", "conclusion_scope": "full", "evidence_pages": [12],
             "suggested_score": 2, "confidence": "high", "evidence": "P12证书可见", "reason": "满足"},
            {"rule_id": "r2", "coverage": "covered", "conclusion_scope": "full", "evidence_pages": [20],
             "suggested_score": 1, "confidence": "high", "evidence": "P20业绩可见", "reason": "满足"},
        ]}
        with patch.object(worker, "_ocr_candidate_pages", return_value=[12, 20]), \
             patch.object(worker, "_ocr_discovery_page_count", return_value=10), \
             patch.object(worker, "_ocr_page_texts", return_value=([
                 {"page": 12, "service": "accurate", "text": "证书编号A123"},
                 {"page": 20, "service": "accurate", "text": "业绩合同"},
             ], "")), \
             patch.object(worker, "_request_task_json", return_value=parsed) as request_json:
            results = worker._run_ocr_batch_supplement(
                self.app, {"task_id": "t"}, document, "objective",
                [(rules[0], bases[0], True), (rules[1], bases[1], True)], {"profile_id": "m"},
            )

        self.assertEqual(request_json.call_count, 1)
        self.assertEqual(results["r1"]["suggested_score"], 2)
        self.assertEqual(results["r2"]["suggested_score"], 1)
        self.assertEqual(results["r1"]["vision_status"], "ocr_applied")

    def test_ocr_batch_adopts_covered_rules_and_falls_back_only_missing(self):
        """批量模型漏掉某个 rule_id 时，只回退缺失规则，已 covered 规则直接采纳。"""
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 50,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rules = [
            {"rule_id": "r1", "title": "证书评分", "check_rule": "证书有效得2分", "scoring": {"max_score": 2}},
            {"rule_id": "r2", "title": "业绩评分", "check_rule": "每个业绩得1分", "scoring": {"max_score": 3}},
        ]
        bases = [
            {"rule_id": "r1", "suggested_score": 0, "max_score": 2, "confidence": "low", "evidence": "文字层未见"},
            {"rule_id": "r2", "suggested_score": 0, "max_score": 3, "confidence": "low", "evidence": "文字层未见"},
        ]
        parsed_r1 = {"rule_id": "r1", "coverage": "covered", "conclusion_scope": "full", "evidence_pages": [12],
                     "suggested_score": 2, "confidence": "high", "evidence": "P12证书可见", "reason": "满足"}
        parsed_r2 = {"rule_id": "r2", "coverage": "covered", "conclusion_scope": "full", "evidence_pages": [20],
                     "suggested_score": 1, "confidence": "high", "evidence": "P20业绩可见", "reason": "满足"}
        parsed_missing = {"results": [parsed_r1]}
        with patch.object(worker, "_ocr_candidate_pages", return_value=[12, 20]), \
             patch.object(worker, "_ocr_discovery_page_count", return_value=10), \
             patch.object(worker, "_ocr_page_texts", return_value=([
                 {"page": 12, "service": "accurate", "text": "证书编号A123"},
                 {"page": 20, "service": "accurate", "text": "业绩合同"},
             ], "")), \
             patch.object(worker, "_request_task_json", side_effect=[parsed_missing, parsed_r2]) as request_json:
            results = worker._run_ocr_batch_supplement(
                self.app, {"task_id": "t"}, document, "objective",
                [(rules[0], bases[0], True), (rules[1], bases[1], True)], {"profile_id": "m"},
            )

        # 部分采纳：r1 用批量结果，r2 才回退逐条（1 次批量 + 1 次逐条）。
        self.assertEqual(request_json.call_count, 2)
        self.assertEqual(results["r1"]["suggested_score"], 2)
        self.assertEqual(results["r2"]["suggested_score"], 1)


if __name__ == "__main__":
    unittest.main()
