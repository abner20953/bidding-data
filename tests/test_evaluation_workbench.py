import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash

from dashboard.blueprints.evaluation_workbench import create_worker_app, evaluation_workbench_bp
from dashboard.evaluation_workbench import storage, worker
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.prompt_context import build_rule_context


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
            {"category": "qualification", "title": "具备有效资质", "source_text": "投标人应具备有效资质", "check_mode": "auto"},
            {"category": "subjective", "title": "技术方案评分", "source_text": "技术方案满分十分", "check_mode": "manual", "scoring": {"max_score": 10}},
        ]}):
            finished = self._run_next_task()

        rule_set, rules = storage.list_rules(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(rule_set["status"], "draft")
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(item["source_type"] == "ai" for item in rules))

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

    def test_global_default_model_is_persisted_and_cannot_be_deleted(self):
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
        blocked = client.delete(f"/api/evaluation-workbench/model-profiles/{profile['profile_id']}")
        self.assertEqual(blocked.status_code, 400)

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

        with patch("dashboard.evaluation_workbench.worker.request_json", return_value={
            "review_results": [{"rule_id": review_rule["rule_id"], "status": "satisfied", "evidence": "具备资质", "reason": "已提供", "risk_level": "low"}],
            "objective_scores": [{"rule_id": objective_rule["rule_id"], "met": True, "evidence": "具备资质", "reason": "已提供"}],
            "subjective_scores": [{"rule_id": subjective_rule["rule_id"], "suggested_score": 8, "evidence": "技术方案完整", "reason": "较完整"}],
        }):
            finished = self._run_next_task()

        _, reviews = storage.latest_review_results(self.app, self.project["project_id"])
        _, objectives = storage.latest_score_results(self.app, self.project["project_id"], "objective")
        _, subjectives = storage.latest_score_results(self.app, self.project["project_id"], "subjective")
        usage = storage.project_token_usage(self.app, self.project["project_id"])
        self.assertEqual(finished["status"], "success")
        self.assertEqual(reviews[0]["status"], "satisfied")
        self.assertEqual(objectives[0]["suggested_score"], 5.0)
        self.assertEqual(subjectives[0]["suggested_score"], 8.0)
        self.assertEqual(usage["call_count"], 1)
        self.assertEqual(usage["input_chars"] > 0, True)

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
        fingerprint = storage.task_input_fingerprint(self.app, self.project["project_id"], "evaluate_all", None, "token-optimized-v1")
        prior = storage.create_task(self.app, self.project["project_id"], "evaluate_all", {"profile_id": None, "input_fingerprint": fingerprint})
        storage.update_task(self.app, prior["task_id"], status="success", result={"cached": True})

        response = self.app.test_client().post(
            f"/api/evaluation-workbench/projects/{self.project['project_id']}/tasks",
            json={"task_type": "evaluate_all", "reuse_completed": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["reused"])
        self.assertEqual(response.get_json()["task"]["task_id"], prior["task_id"])

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
