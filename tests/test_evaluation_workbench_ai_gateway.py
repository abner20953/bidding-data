import unittest
from unittest.mock import Mock, patch

from dashboard.evaluation_workbench.ai_gateway import test_connection


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
        with patch("dashboard.evaluation_workbench.ai_gateway.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "API Key 含有中文"):
                test_connection(self._profile(_api_key="测试-key"))

        post.assert_not_called()

    def test_connection_explains_authentication_failure_without_echoing_response(self):
        response = Mock(ok=False, status_code=401, text='{"error":"invalid api key"}')
        with patch("dashboard.evaluation_workbench.ai_gateway.requests.post", return_value=response):
            with self.assertRaisesRegex(ValueError, "鉴权失败（HTTP 401）") as error:
                test_connection(self._profile())

        self.assertNotIn("invalid api key", str(error.exception))
        self.assertIn("重新创建并完整复制", str(error.exception))

    def test_minimax_compatible_profile_can_omit_optional_parameters(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with patch("dashboard.evaluation_workbench.ai_gateway.requests.post", return_value=response) as post:
            message = test_connection(self._profile(model_name="MiniMax-M2.7"))

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
        with patch("dashboard.evaluation_workbench.ai_gateway.requests.post", return_value=response) as post:
            test_connection(profile)

        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "adaptive"})

    def test_minimax_m2_omits_unsupported_disabled_thinking_parameter(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        profile = self._profile(
            base_url="https://api.minimaxi.com/v1", model_name="MiniMax-M2.7", thinking_mode="disabled"
        )
        with patch("dashboard.evaluation_workbench.ai_gateway.requests.post", return_value=response) as post:
            test_connection(profile)

        self.assertNotIn("thinking", post.call_args.kwargs["json"])
