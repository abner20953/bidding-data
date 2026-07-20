"""面向 OpenAI-compatible 文本模型的最小 AI 网关。"""

from __future__ import annotations

import json
import os
import re

import requests


class InvalidJsonResponse(ValueError):
    """结构化响应无法解析；正文仅在当前进程内用于低成本 JSON 修复。"""

    def __init__(self, content: object, finish_reason: object = None):
        self.raw_content = content if isinstance(content, str) else ""
        self.finish_reason = str(finish_reason or "")
        super().__init__(_invalid_json_error(content, finish_reason))


def _load_json_candidate(value: str) -> object:
    """解析模型常见的轻微 JSON 瑕疵，不猜测缺失的业务内容。"""
    attempts = [value]
    # 部分模型会输出未转义的换行/制表符，strict=False 可安全接受这类控制字符。
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(value, strict=False)
    except json.JSONDecodeError:
        pass
    # 仅修复不改变字段含义的语法噪声：尾逗号、全角结构符和不可见空格。
    repaired = value.replace("\ufeff", "").replace("\u00a0", " ")
    repaired = repaired.translate(str.maketrans({"：": ":", "，": ",", "｛": "{", "｝": "}", "［": "[", "］": "]"}))
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    if repaired != value:
        attempts.append(repaired)
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            pass
    # 保留最早的严格 JSON 异常，调用方只记录安全诊断，不持久化正文。
    return json.loads(attempts[0])


def _decode_json_content(content) -> dict:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        # 兼容少量 OpenAI-compatible 接口返回的文本内容块。
        content = "".join(
            str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    if not isinstance(content, str):
        raise ValueError("模型响应正文为空")
    value = content.strip().lstrip("\ufeff")
    # MiniMax 在开启 thinking 时会将 <think>...</think> 放在 content 前面；
    # 评标任务只解析其后的结构化结论，不保存或展示思考过程。
    value = re.sub(r"^\s*<think>.*?</think>\s*", "", value, count=1, flags=re.IGNORECASE | re.DOTALL)
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = _load_json_candidate(value)
    except json.JSONDecodeError as original_error:
        # 少量兼容接口仍可能在 JSON 前后附带简短说明；仅在能完整定位对象时兜底解析。
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise original_error
        try:
            parsed = _load_json_candidate(value[start:end + 1])
        except json.JSONDecodeError:
            raise original_error
    # 某些兼容接口会把 JSON 对象再次序列化成字符串。
    if isinstance(parsed, str):
        parsed = _load_json_candidate(parsed)
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的 JSON 顶层必须是对象")
    return parsed


def _api_key_for(profile: dict) -> str:
    api_key = str(profile.get("_api_key") or os.environ.get(profile.get("api_key_env", ""), "")).strip()
    if not api_key:
        raise ValueError(f"模型档案“{profile['display_name']}”尚未配置 API Key")
    if any(not (0x21 <= ord(character) <= 0x7E) for character in api_key):
        raise ValueError(
            f"模型档案“{profile['display_name']}”的 API Key 含有中文、全角符号、空格或不可见字符；"
            "请在模型配置中重新粘贴服务商控制台生成的纯文本 Key"
        )
    return api_key


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _thinking_payload(profile: dict) -> dict | None:
    """返回服务商兼容的 thinking 参数，避免 OpenAI-compatible 的方言差异。"""
    mode = profile.get("thinking_mode")
    model_name = str(profile.get("model_name") or "").lower()
    base_url = str(profile.get("base_url") or "").lower()
    if "api.minimaxi.com" in base_url:
        if model_name.startswith("minimax-m2"):
            # MiniMax M2.x 无法关闭 thinking，传入 disabled 也不会生效，因此直接省略。
            return None
        if model_name == "minimax-m3":
            if mode == "enabled":
                return {"type": "adaptive"}
            if mode in {"adaptive", "disabled"}:
                return {"type": mode}
            return None
    if mode in {"enabled", "disabled"}:
        return {"type": mode}
    return None


def _is_minimax_m3(profile: dict) -> bool:
    return (
        "api.minimaxi.com" in str(profile.get("base_url") or "").lower()
        and str(profile.get("model_name") or "").lower() == "minimax-m3"
    )


def _invalid_json_error(content, finish_reason) -> str:
    """返回不含模型正文的诊断，便于排查而不留存招标文件或模型原文。"""
    details = []
    if str(finish_reason or "").lower() in {"length", "max_tokens"}:
        details.append("模型输出达到长度上限，JSON 可能未完整返回")
    if isinstance(content, str):
        stripped = content.lstrip().lower()
        if stripped.startswith("<think>") and "</think>" not in stripped:
            details.append("模型思考内容未闭合，最终 JSON 未完整返回")
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"模型未返回有效 JSON{suffix}，建议检查模型档案或稍后重试"


def _raise_http_error(response, *, operation: str) -> None:
    if response.status_code == 401:
        raise ValueError(
            f"{operation}鉴权失败（HTTP 401）：API Key 无效、已失效，或不属于当前服务商。"
            "请从对应服务商控制台重新创建并完整复制 API Key；不要填入 API 地址、邮箱或带引号的文本。"
        )
    raise ValueError(f"{operation}（HTTP {response.status_code}）：{response.text[:500]}")


def request_json(profile: dict, system_prompt: str, user_prompt: str, *, usage_callback=None,
                 response_metadata_callback=None, max_tokens: int | None = None) -> dict:
    api_key = _api_key_for(profile)
    base_url = profile["base_url"].rstrip("/")
    payload = {
        "model": profile["model_name"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }
    if profile.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    thinking = _thinking_payload(profile)
    if thinking:
        payload["thinking"] = thinking
    if _is_minimax_m3(profile):
        # MiniMax M3 将思考内容置于独立字段，content 仅保留最终结构化结论。
        payload["reasoning_split"] = True
    if max_tokens is not None:
        payload["max_tokens"] = max(16, int(max_tokens))
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=_headers(api_key),
            json=payload,
            timeout=min(1800, max(30, int(profile.get("timeout_seconds") or 600))),
        )
    except (requests.RequestException, UnicodeEncodeError) as exc:
        raise ValueError(f"模型连接失败：{exc}") from exc
    if not response.ok:
        _raise_http_error(response, operation="模型请求失败")
    body = response.json()
    if usage_callback:
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        usage_callback(usage)
    try:
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("模型响应缺少 choices/message/content") from exc
    if response_metadata_callback:
        # 只记录长度与结束原因，绝不保存模型正文、提示词或思考内容。
        response_metadata_callback({
            "requested_max_tokens": payload.get("max_tokens"),
            "finish_reason": choice.get("finish_reason"),
            "response_chars": len(content) if isinstance(content, str) else 0,
        })
    try:
        result = _decode_json_content(content)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise InvalidJsonResponse(content, choice.get("finish_reason")) from exc
    return result


def test_connection(profile: dict, prompt_text: str) -> str:
    """发送极小请求验证模型地址、密钥和兼容参数；不写入业务数据。"""
    api_key = _api_key_for(profile)
    payload = {
        "model": profile["model_name"],
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0,
        "max_tokens": 16,
    }
    if profile.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    thinking = _thinking_payload(profile)
    if thinking:
        payload["thinking"] = thinking
    if _is_minimax_m3(profile):
        payload["reasoning_split"] = True
    try:
        response = requests.post(
            f"{profile['base_url'].rstrip('/')}/chat/completions",
            headers=_headers(api_key),
            json=payload,
            timeout=min(60, max(10, int(profile.get("timeout_seconds") or 30))),
        )
    except (requests.RequestException, UnicodeEncodeError) as exc:
        raise ValueError(f"模型连接失败：{exc}") from exc
    if not response.ok:
        _raise_http_error(response, operation="模型测试失败")
    try:
        if not response.json().get("choices"):
            raise ValueError
    except (ValueError, requests.JSONDecodeError) as exc:
        raise ValueError("模型测试未返回有效 choices 数据") from exc
    return "连接成功：模型接口已响应"
