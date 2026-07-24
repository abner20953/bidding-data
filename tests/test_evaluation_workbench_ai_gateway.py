import unittest
from unittest.mock import Mock, patch

from dashboard.evaluation_workbench.ai_gateway import (
    InvalidJsonResponse, ModelResponseEnvelopeError, _decode_json_content, _recover_complete_json_array,
    request_json, test_connection,
)


CONNECTION_TEST_PROMPT = '请仅返回 JSON 对象：{"message":"连接成功"}'


class EvaluationWorkbenchAiGatewayTests(unittest.TestCase):
    @staticmethod
    def _profile(**overrides):
        profile = {
            "display_name": "测试模型",
            "base_url": "https://example.test/v1",
            "model_name": "test-model",
            "_api_key": "test-key-123",
            "json_mode": False,
            "thinking_mode": "default",
            "timeout_seconds": 30,
        }
        profile.update(overrides)
        return profile

    def test_connection_rejects_non_ascii_api_key_before_network_request(self):
        with patch("dashboard.evaluation_workbench.ai_gateway._http_post") as post:
            with self.assertRaisesRegex(ValueError, "API Key 含有中文"):
                test_connection(self._profile(_api_key="测试-key"), CONNECTION_TEST_PROMPT)

        post.assert_not_called()

    def test_request_json_marks_http_success_without_choice_content_as_retriable_envelope_error(self):
        response = Mock(ok=True)
        response.json.return_value = {"error": {"message": "temporary upstream failure"}}

        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response):
            with self.assertRaises(ModelResponseEnvelopeError) as error:
                request_json(self._profile(), "system", "user")

        self.assertIn("choices/message/content", str(error.exception))
        self.assertIn("temporary upstream failure", str(error.exception))

    def test_request_json_treats_usage_at_output_limit_without_choices_as_length(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [], "usage": {"completion_tokens": 5120}}
        metadata = {}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M3", thinking_mode="disabled",
        )

        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response) as post:
            with self.assertRaises(InvalidJsonResponse) as error:
                request_json(profile, "system", "user", max_tokens=5120, response_metadata_callback=metadata.update)

        self.assertEqual(error.exception.finish_reason, "length")
        self.assertEqual(post.call_args.kwargs["json"]["max_completion_tokens"], 5120)
        self.assertNotIn("max_tokens", post.call_args.kwargs["json"])
        self.assertEqual(metadata["requested_max_tokens"], 5120)
        self.assertEqual(metadata["response_chars"], 0)

    def test_request_json_treats_minimax_token_limit_business_code_as_length(self):
        response = Mock(ok=True)
        response.json.return_value = {"base_resp": {"status_code": 1039, "status_msg": "token limit"}}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M3", thinking_mode="disabled",
        )

        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response):
            with self.assertRaises(InvalidJsonResponse) as error:
                request_json(profile, "system", "user", max_tokens=5120)

        self.assertEqual(error.exception.finish_reason, "length")

    def test_minimax_m3_adaptive_reserves_completion_budget_for_reasoning(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": '{"results":[]}'}}]}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M3", thinking_mode="adaptive",
        )

        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response) as post:
            result = request_json(profile, "system", "user", max_tokens=5440)

        self.assertEqual(result, {"results": []})
        self.assertEqual(post.call_args.kwargs["json"]["max_completion_tokens"], 16320)
        self.assertNotIn("max_tokens", post.call_args.kwargs["json"])

    def test_json_decoder_ignores_minimax_thinking_block_before_json(self):
        content = '<think>先分析规则与招标文件的对应关系。</think>\n\n```json\n{"rules": []}\n```'

        decoded = _decode_json_content(content)

        self.assertEqual(decoded, {"rules": []})

    def test_json_decoder_recovers_object_after_a_short_non_json_prefix(self):
        decoded = _decode_json_content('提取结果如下：{"rules": []}')

        self.assertEqual(decoded, {"rules": []})

    def test_json_decoder_uses_first_balanced_object_not_trailing_braces_in_explanation(self):
        decoded = _decode_json_content('结果：{"rules": []}，字段说明 {rules}。')

        self.assertEqual(decoded, {"rules": []})

    def test_json_decoder_repairs_safe_model_syntax_noise(self):
        decoded = _decode_json_content('{"results":[{"reason":"第一行\n第二行",}],}')

        self.assertEqual(decoded, {"results": [{"reason": "第一行\n第二行"}]})

    def test_json_decoder_repairs_an_invalid_literal_backslash_without_changing_fields(self):
        decoded = _decode_json_content('{"evidence":"编号\\A-01"}')

        self.assertEqual(decoded, {"evidence": "编号\\A-01"})

    def test_json_decoder_accepts_text_content_blocks_and_double_encoded_object(self):
        blocked = _decode_json_content([{"type": "text", "text": '{"results":[]}'}])
        encoded = _decode_json_content('"{\\"results\\":[]}"')

        self.assertEqual(blocked, {"results": []})
        self.assertEqual(encoded, {"results": []})

    def test_json_recovery_keeps_only_complete_array_items(self):
        recovered = _recover_complete_json_array(
            '```json\n{"rules":[{"title":"营业执照","source_text":"提供有效营业执照"},{"title":"截断',
            "rules",
        )

        self.assertEqual(recovered, {"rules": [{"title": "营业执照", "source_text": "提供有效营业执照"}]})

    def test_connection_explains_authentication_failure_without_echoing_response(self):
        response = Mock(ok=False, status_code=401, text='{"error":"invalid api key"}')
        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response):
            with self.assertRaisesRegex(ValueError, "鉴权失败（HTTP 401）") as error:
                test_connection(self._profile(), CONNECTION_TEST_PROMPT)

        self.assertNotIn("invalid api key", str(error.exception))
        self.assertIn("重新创建并完整复制", str(error.exception))

    def test_minimax_compatible_profile_can_omit_optional_parameters(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response) as post:
            message = test_connection(self._profile(model_name="MiniMax-M2.7"), CONNECTION_TEST_PROMPT)

        self.assertEqual(message, "连接成功：模型接口已响应")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "MiniMax-M2.7")
        self.assertNotIn("response_format", post.call_args.kwargs["json"])
        self.assertNotIn("thinking", post.call_args.kwargs["json"])

    def test_minimax_m3_maps_legacy_enabled_thinking_to_adaptive(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M3", thinking_mode="enabled"
        )
        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response) as post:
            test_connection(profile, CONNECTION_TEST_PROMPT)

        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "adaptive"})
        self.assertTrue(post.call_args.kwargs["json"]["reasoning_split"])

    def test_minimax_m3_separates_reasoning_when_thinking_uses_default_mode(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M3", thinking_mode="default"
        )
        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response) as post:
            test_connection(profile, CONNECTION_TEST_PROMPT)

        self.assertTrue(post.call_args.kwargs["json"]["reasoning_split"])

    def test_minimax_m2_omits_unsupported_disabled_thinking_parameter(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M2.7", thinking_mode="disabled"
        )
        with patch("dashboard.evaluation_workbench.ai_gateway._http_post", return_value=response) as post:
            test_connection(profile, CONNECTION_TEST_PROMPT)

        self.assertNotIn("thinking", post.call_args.kwargs["json"])
