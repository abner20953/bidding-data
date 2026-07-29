import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
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
        sleep.assert_called_once_with(2)
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
        self.assertEqual(sleep.call_args_list[0].args, (2,))
        self.assertEqual(sleep.call_args_list[1].args, (4,))
        self.assertEqual(storage.project_token_usage(self.app, self.project["project_id"])["call_count"], 3)

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

    def test_token_usage_breaks_down_vision_and_ocr_families(self):
        task = storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_batch", None,
                                  usage={"total_tokens": 100})
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_vision_1", None,
                                  context_mode="vision_standard_1", usage={"total_tokens": 40})
        storage.record_model_call(self.app, task["task_id"], self.project["project_id"], "evaluate_all_review_ocr", None,
                                  context_mode="tencent_ocr", usage={"total_tokens": 20})
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
        self.assertEqual(usage["ocr_requests"], 2)
        # 汇总口径保持不变，新增字段不影响既有调用方。
        self.assertEqual(usage["call_count"], 3)

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
        self.assertEqual(next(item for item in rules if item["title"] == "技术方案评分")["check_mode"], "ocr")

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

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=ValueError("模型接口繁忙")):
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

    def test_local_ocr_configuration_defaults_to_safe_fallback_and_can_be_disabled(self):
        initial = storage.ocr_configuration(self.app)
        self.assertTrue(initial["local"]["enabled"])
        self.assertFalse(initial["local"]["readiness"]["ready_for_manual_validation"])
        updated = storage.update_ocr_configuration(self.app, {"enabled": False, "local": {"enabled": False}})
        self.assertFalse(updated["local"]["enabled"])
        self.assertEqual(updated["local"]["mode"], "fallback")

    def test_local_ocr_is_used_when_tencent_ocr_is_not_enabled(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        rule = {"title": "证书编号", "check_rule": "核验证书编号", "vision_level": "standard"}
        with patch("dashboard.evaluation_workbench.worker._local_ocr_page_texts", return_value=([
            {"page": 1, "service": local_ocr_gateway.LOCAL_OCR_SERVICE, "text": "证书编号 A123"},
        ], "")) as local_pages:
            values, failure = worker._ocr_page_texts(self.app, {"task_id": "task"}, document, rule, {}, "standard", pages=[1])

        self.assertEqual(failure, "腾讯 OCR 未启用，已由本地 RapidOCR 回退")
        self.assertEqual(values[0]["service"], local_ocr_gateway.LOCAL_OCR_SERVICE)
        local_pages.assert_called_once()

    def test_tencent_ocr_success_never_starts_local_fallback(self):
        document = self._add_pdf("bid.pdf", "bid", "甲公司", "扫描件候选页")
        storage.update_ocr_configuration(self.app, {
            "enabled": True, "secret_id": "AKID-example", "secret_key": "secret-example", "region": "ap-guangzhou",
            "local": {"enabled": True}, "services": {"accurate": {"enabled": True, "monthly_limit": 10}},
        })
        rule = {"title": "证书编号", "check_rule": "核验证书编号", "vision_level": "standard"}
        response = {"service": "accurate", "text": "证书编号 A123", "parser_version": ocr_gateway.OCR_PARSER_VERSION}
        with patch("dashboard.evaluation_workbench.worker.request_tencent_ocr", return_value=(response, "")), \
             patch("dashboard.evaluation_workbench.worker._local_ocr_page_texts") as local_pages:
            values, failure = worker._ocr_page_texts(self.app, {"task_id": "task", "project_id": self.project["project_id"]}, document, rule, {}, "standard", pages=[1])

        self.assertEqual(failure, "")
        self.assertEqual(values[0]["text"], "证书编号 A123")
        local_pages.assert_not_called()

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
                "stdout": "RapidOCR initializing\n" + json.dumps({"ok": True, "pages": [{"page": 3, "text": "本地识别文字", "line_count": 1, "confidence": 96.5}]}, ensure_ascii=False),
                "stderr": "",
            })()
            with patch("dashboard.evaluation_workbench.local_ocr_gateway.subprocess.run", return_value=completed) as run:
                values, error = local_ocr_gateway.request_local_ocr([{"page": 3, "path": str(image_path)}])

        self.assertIsNone(error)
        self.assertEqual(values[0]["service"], local_ocr_gateway.LOCAL_OCR_SERVICE)
        self.assertEqual(values[0]["parser_version"], local_ocr_gateway.LOCAL_OCR_PARSER_VERSION)
        run.assert_called_once()

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

    def test_report_compacts_legacy_objective_ocr_raw_text(self):
        value = (
            "文字层建议1分。"
            "【腾讯OCR原文·高精度版·P12】身份证号码410000000000000000\n合同金额9001781元"
        )

        compact = evaluation_workbench_module._report_compact_objective_ocr_text(value)

        self.assertIn("腾讯OCR摘要", compact)
        self.assertNotIn("身份证号码", compact)
        self.assertIn("文字层建议1分", compact)

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
            ["compare_ai_assessment", "extract_rules_guidance", "extract_rules_package_scope", "extract_rules_validation_guidance", "evaluate_all_guidance"],
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

    def test_global_rules_require_password_and_are_automatically_imported_for_new_projects(self):
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
            "category": "other", "title": "不导入项", "check_rule": "不应自动导入", "enabled": False, "password": "108",
        })
        self.assertEqual(disabled.status_code, 201)

        new_project = storage.create_project(self.app, "自动导入项目")
        rule_set, rules = storage.list_rules(self.app, new_project["project_id"])

        self.assertEqual(rule_set["status"], "draft")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["source_type"], "global")
        self.assertEqual(rules[0]["check_rule"], "核验是否提供有效营业执照")
        self.assertEqual(rules[0]["check_mode"], "ocr")

        self.assertEqual(client.patch(f"/api/evaluation-workbench/global-rules/{created.get_json()['rule']['global_rule_id']}", json={"title": "更新名称"}).status_code, 403)
        client.patch(f"/api/evaluation-workbench/global-rules/{created.get_json()['rule']['global_rule_id']}", json={"title": "更新名称", "password": "108"})
        self.assertEqual(storage.list_rules(self.app, new_project["project_id"])[1][0]["title"], "营业执照有效性")

    def test_rule_extraction_merges_enabled_global_rules_without_exact_duplicates(self):
        storage.create_global_rule(self.app, {
            "category": "qualification", "title": "通用营业执照", "check_rule": "核验是否提供有效营业执照", "source_text": "通用基线",
        })
        storage.create_global_rule(self.app, {
            "category": "compliance", "title": "完全重复规则", "check_rule": "核验响应文件是否完整", "source_text": "通用基线",
        })

        rule_set = storage.replace_rules_from_extraction(self.app, self.project["project_id"], "task-1", [
            {"category": "compliance", "title": "完全重复规则", "check_rule": "核验响应文件是否完整", "source_text": "招标原文"},
            {"category": "substantive", "title": "报价限制", "check_rule": "核验报价未超过最高限价", "source_text": "最高限价"},
        ])
        _, rules = storage.list_rules(self.app, self.project["project_id"])

        self.assertEqual(rule_set["global_rule_count"], 1)
        self.assertEqual(len(rules), 3)
        self.assertEqual({item["source_type"] for item in rules}, {"ai", "global"})
        self.assertEqual(sum(item["title"] == "完全重复规则" for item in rules), 1)

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
        review_run = storage.create_review_run(self.app, self.project["project_id"], task["task_id"], None)
        score_run = storage.create_score_run(self.app, self.project["project_id"], task["task_id"], "objective", None)
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

    def test_evidence_pack_is_shadow_only_and_records_page_provenance(self):
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
        self.assertEqual(payload["mode"], "shadow_only")
        self.assertIn({"source": "ocr_evidence", "page": 12}, payload["page_provenance"])
        self.assertEqual(payload["ocr_findings"][0]["evidence_pages"], [12])

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
            {"results": [{"rule_id": review_rule["rule_id"], "status": "satisfied", "evidence": "具备资质", "reason": "已提供", "risk_level": "low"}]},
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
        self.assertIn("1项×3分=3分", results[0]["reason"])

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
        self.assertIn("OCR", results[0]["reason"])

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

    def test_visual_rule_policy_is_generic_and_default_disabled(self):
        rules = worker._normalise_visual_rule_policies([{
            "rule_id": "any-visual-rule", "category": "qualification", "title": "任意扫描材料",
            "check_rule": "核验扫描件上的盖章状态", "source_text": "应加盖单位章",
            "check_mode": "ocr", "evidence_requirements": ["visual"],
        }])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["vision_trigger"], "required")
        # 提取阶段只给出建议条件，真正的图片调用必须由人工选择强度后启用。
        self.assertEqual(rules[0]["vision_level"], "off")

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

    def test_page_specific_business_license_route_falls_back_without_disabling_later_license_page(self):
        rule = {"title": "营业执照", "check_rule": "核验营业执照和统一社会信用代码"}

        wrong_page = worker._ocr_service_candidates_for_page(rule, "standard", "法定代表人身份证明及授权委托书")
        actual_license = worker._ocr_service_candidates_for_page(rule, "standard", "营业执照\n统一社会信用代码：91110108")

        self.assertNotIn("biz_license", wrong_page)
        self.assertEqual(actual_license[0], "biz_license")

    def test_visual_first_is_limited_to_material_pages_that_need_picture_judgment(self):
        certificate = {
            "title": "认证证书评分", "check_rule": "核验证书扫描件、编号和有效期",
            "vision_trigger": "required", "vision_level": "standard",
        }
        declaration = {
            "title": "中小企业声明函", "check_rule": "核对所属行业和企业类型填写内容",
            "vision_trigger": "required", "vision_level": "standard",
        }
        base = {"suggested_score": None, "confidence": "low"}

        self.assertTrue(worker._prefer_vision_first(certificate, "objective", base, "hybrid", "required"))
        self.assertFalse(worker._prefer_vision_first(declaration, "objective", base, "ocr", "required"))
        self.assertFalse(worker._prefer_vision_first(certificate, "objective", base, "hybrid", "off"))

    def test_post_vision_ocr_only_verifies_image_evidence_pages_and_keeps_objective_concise(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 200,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "认证证书评分", "check_rule": "核验证书编号与有效期",
                "vision_trigger": "required", "vision_level": "standard", "scoring": {"max_score": 2}}
        visual = {
            "rule_id": "cert", "suggested_score": 2, "max_score": 2, "confidence": "high",
            "evidence": "【图片识别·standard·P144】证书名称、编号和有效期可见。",
            "reason": "证书满足计分条件。", "vision_status": "applied",
            "vision_pages": [7, 144], "vision_evidence_pages": [144], "vision_message": "图片识别已形成完整补充。",
        }
        values = [{"page": 144, "service": "accurate", "text": "证书编号A123，有效期至2029-12-31"}]

        with patch("dashboard.evaluation_workbench.worker._ocr_page_texts", return_value=(values, "")) as ocr_pages:
            result = worker._run_ocr_verification_after_vision(
                self.app, {"task_id": "task"}, document, "objective", rule, visual,
            )

        self.assertEqual(ocr_pages.call_args.kwargs["pages"], [144])
        self.assertEqual(result["suggested_score"], 2)
        self.assertEqual(result["ocr_verification_status"], "completed")
        self.assertEqual(result["ocr_verification_pages"], [144])
        self.assertIn("OCR关键文字复核", result["evidence"])
        self.assertNotIn("A123", result["evidence"])
        layer = next(item for item in result["evidence_layers"] if item["source"] == "tencent_ocr")
        self.assertEqual(layer["evidence_pages"], [144])

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

    def test_post_vision_ocr_verification_respects_selected_page_budget(self):
        document = {"document_id": "doc", "extension": ".pdf", "page_count": 50,
                    "original_name": "投标.pdf", "bidder_name": "甲"}
        rule = {"rule_id": "cert", "title": "认证证书", "check_rule": "核验证书编号与有效期",
                "vision_trigger": "required", "vision_level": "high"}
        result = {"vision_status": "applied", "vision_evidence_pages": list(range(1, 13)),
                  "vision_message": "图片识别已完成。", "evidence": "证书可见"}

        with patch("dashboard.evaluation_workbench.worker._ocr_page_texts", return_value=([], "未识别到文字")) as ocr_pages:
            updated = worker._run_ocr_verification_after_vision(
                self.app, {"task_id": "task"}, document, "objective", rule, result,
            )

        self.assertEqual(ocr_pages.call_args.kwargs["pages"], list(range(1, 11)))
        self.assertEqual(updated["ocr_verification_status"], "unavailable")

    def test_ocr_visual_field_shadow_check_only_confirms_direct_text_hits(self):
        check = worker._shadow_ocr_visual_field_check([
            {"field": "证书编号", "image_value": "ISO-14001-2025"},
            {"field": "有效期", "image_value": "2030-12-31"},
        ], [{"text": "证书编号：ISO-14001-2025\n有效期至2029-12-31"}])

        self.assertEqual(check["mode"], "shadow_only")
        self.assertEqual(check["matched_count"], 1)
        self.assertEqual(check["items"][0]["ocr_status"], "matched")
        self.assertEqual(check["items"][1]["ocr_status"], "unconfirmed")

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
        repaired = {"results": [{"rule_id": rule["rule_id"], "status": "satisfied", "evidence": "有效资质"}]}

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
            ValueError("模型连接失败：timeout"),
        ]):
            finished = self._run_next_task()

        review_run, results = storage.latest_review_results(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "error")
        self.assertEqual(finished["progress"], 50)
        self.assertEqual(review_run["task_status"], "error")
        self.assertEqual(len(results), 8)

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
            "reason": "全文确认已提供", "risk_level": "low",
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

    def test_reextract_preserves_edited_content_but_resets_all_selection_states(self):
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
        self.assertEqual(edited["enabled"], 1)
        self.assertEqual(manual["enabled"], 1)

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

    def test_reextract_defaults_visual_verification_rules_to_disabled(self):
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
        self.assertEqual(enabled["通用许可证"], 0)

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


if __name__ == "__main__":
    unittest.main()
