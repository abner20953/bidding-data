import io
import json
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

from dashboard.blueprints.evaluation_workbench import create_worker_app, evaluation_workbench_bp
from dashboard.evaluation_workbench import storage, worker
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.prompt_context import (
    _anchors, build_rule_context, select_rule_chunk_map, select_rule_chunks, split_full_text_chunks,
)
from dashboard.evaluation_workbench.prompt_templates import PROMPT_TEMPLATES


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
            "metadata": {"auxiliary": {"matches": [
                {"field": "author", "label": "作者/创建者", "value": "Same Author", "strength": "reference", "also_in_tender": False},
                {"field": "creator", "label": "创建软件", "value": "Common Tool", "strength": "weak", "also_in_tender": True},
            ]}},
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
        self.assertEqual(len(metadata["evidence"]), 1)

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
        response = {
            "drops": [
                {"rule_id": "R3", "reason": "not_file_verifiable"},
                {"rule_id": "R5", "reason": "duplicate"},
            ],
            "rewrites": [
                {"rule_id": "R4", "reason": "partial_boundary", "title": "电子签章与签字形式", "check_rule": "核验电子签章、扫描签字及涂改确认。", "ocr_required": True},
                {"rule_id": "R5", "reason": "partial_boundary", "title": "错误评分改写", "check_rule": "不得生效", "ocr_required": False},
            ],
            "merges": [{
                "rule_ids": ["R1", "R2"], "keep_rule_id": "R1", "reason": "duplicate",
                "title": "生产或代理资格与授权材料", "check_rule": "核验代理资格条件及制造商授权书。", "ocr_required": True,
            }],
        }

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value=response):
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
        self.assertEqual(stats["failure_count"], 1)
        self.assertFalse(stats["applied"])

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

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[primary, supplement, {"drops": []}]) as request_json:
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["score_clause_count"], 2)
        self.assertEqual(finished["result"]["scoring_supplement_count"], 1)
        self.assertEqual(request_json.call_count, 3)
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

        with patch("dashboard.evaluation_workbench.worker.request_json", side_effect=[primary, supplement, {"drops": []}]) as request_json:
            finished = self._run_next_task()

        _, rules = storage.list_rules(self.app, self.project["project_id"])
        supplement_prompt = request_json.call_args_list[1].args[2]
        self.assertEqual(finished["result"]["uncovered_score_clause_count"], 1)
        self.assertEqual(request_json.call_count, 3)
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
            ["compare_ai_assessment", "extract_rules_guidance", "extract_rules_validation_guidance", "evaluate_all_guidance"],
        )
        self.assertTrue(all(item["section"] and item["change_level"] for item in templates))
        extraction_template = next(item for item in templates if item["template_id"] == "extract_rules_user")
        self.assertIn("不得逐条复述招标原文", extraction_template["content"])
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
        self.assertIn("时间边界", extraction_system)
        self.assertIn("不得把普通技术描述", extraction_system)
        for template_id in (
            "compare_ai_assessment_user", "review_documents_user", "score_objective_user",
            "score_subjective_user", "evaluate_all_review_user", "evaluate_all_objective_user",
            "evaluate_all_subjective_user",
        ):
            self.assertIn("恰好返回一次", PROMPT_TEMPLATES[template_id]["content"], template_id)
        self.assertIn("不得自行给分", PROMPT_TEMPLATES["evaluate_all_review_user"]["content"])
        self.assertIn('"page_hint":"页码或null"', PROMPT_TEMPLATES["evaluate_all_review_user"]["content"])
        extraction_guidance = PROMPT_TEMPLATES["extract_rules_guidance"]["content"]
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
        self.assertIn('"drops"', quality_gate_template)
        self.assertIn("受保护规则", quality_gate_template)
        self.assertIn("勾选或取消勾选状态不是本轮提取依据", quality_gate_template)
        self.assertIn('"rewrites"', finalise_template)
        self.assertIn('"merges"', finalise_template)
        self.assertIn("objective/subjective", finalise_template)
        self.assertIn("不接受联合体", extraction_validation)
        self.assertIn("平台子账号", extraction_validation)
        self.assertIn("必须为 manual", extraction_validation)

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
            {"rule_id": review_rule["rule_id"], "status": "satisfied", "confidence": "high", "evidence_quality": "sufficient", "risk_level": "low", "requires_review": False, "automation_status": "ready_for_batch_confirmation"},
        ])
        storage.save_score_results(self.app, score_run["score_run_id"], document["document_id"], [
            {"rule_id": score_rule["rule_id"], "suggested_score": 5, "effective_score": 5, "max_score": 5, "confidence": "high", "evidence": "资质证书", "requires_review": False, "automation_status": "ready_for_batch_confirmation"},
        ])

        reviews = self.app.test_client().post(f"/api/evaluation-workbench/projects/{self.project['project_id']}/review-results/confirm-auto")
        scores = self.app.test_client().post(f"/api/evaluation-workbench/projects/{self.project['project_id']}/score-results/confirm-auto", json={"score_type": "objective"})
        _, review_rows = storage.latest_review_results(self.app, self.project["project_id"])
        _, score_rows = storage.latest_score_results(self.app, self.project["project_id"], "objective")

        self.assertEqual(reviews.get_json()["confirmed_count"], 1)
        self.assertEqual(scores.get_json()["confirmed_count"], 1)
        self.assertEqual(review_rows[0]["final_status"], "satisfied")
        self.assertEqual(score_rows[0]["final_score"], 5.0)

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
            ],
        ) as request_json:
            finished = self._run_next_task()

        _, results = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["result"]["full_scan_document_count"], 1)
        self.assertEqual(finished["result"]["full_scan_batch_count"], 1)
        self.assertEqual(request_json.call_count, 2)
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
            "check_rule": "最低评审价得10分，其他报价按最低价比例得分。",
            "source_text": "最低评审价得10分", "scoring": {"kind": "manual", "max_score": 10},
        })
        storage.confirm_rule_set(self.app, self.project["project_id"])
        storage.create_task(self.app, self.project["project_id"], "evaluate_all")
        cross = {"results": [
            {"document_id": bid_a["document_id"], "rule_id": rule["rule_id"], "quoted_price": 100,
             "suggested_score": 10, "evidence": "投标报价100万元", "calculation": "100/100×10=10", "confidence": "high"},
            {"document_id": bid_b["document_id"], "rule_id": rule["rule_id"], "quoted_price": 120,
             "suggested_score": 8.33, "evidence": "投标报价120万元", "calculation": "100/120×10=8.33", "confidence": "high"},
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
