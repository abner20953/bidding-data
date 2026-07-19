"""面向 OpenAI-compatible 文本模型的最小 AI 网关。"""

from __future__ import annotations

import json
import os

import requests


def _decode_json_content(content) -> dict:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("模型响应正文为空")
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的 JSON 顶层必须是对象")
    return parsed


def request_json(profile: dict, system_prompt: str, user_prompt: str, *, usage_callback=None, max_tokens: int | None = None) -> dict:
    api_key = str(profile.get("_api_key") or os.environ.get(profile.get("api_key_env", ""), "")).strip()
    if not api_key:
        raise ValueError(f"模型档案“{profile['display_name']}”尚未配置 API Key")
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
    if profile.get("thinking_mode") in {"enabled", "disabled"}:
        payload["thinking"] = {"type": profile["thinking_mode"]}
    if max_tokens is not None:
        payload["max_tokens"] = max(16, int(max_tokens))
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=min(1800, max(30, int(profile.get("timeout_seconds") or 600))),
        )
    except requests.RequestException as exc:
        raise ValueError(f"模型连接失败：{exc}") from exc
    if not response.ok:
        raise ValueError(f"模型请求失败（HTTP {response.status_code}）：{response.text[:500]}")
    body = response.json()
    try:
        content = body["choices"][0]["message"]["content"]
        result = _decode_json_content(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型未返回有效 JSON，建议检查模型档案或稍后重试") from exc
    if usage_callback:
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        usage_callback(usage)
    return result


def test_connection(profile: dict) -> str:
    """发送极小请求验证模型地址、密钥和兼容参数；不写入业务数据。"""
    api_key = str(profile.get("_api_key") or os.environ.get(profile.get("api_key_env", ""), "")).strip()
    if not api_key:
        raise ValueError(f"模型档案“{profile['display_name']}”尚未配置 API Key")
    payload = {
        "model": profile["model_name"],
        "messages": [{"role": "user", "content": "请仅返回 JSON 对象：{\"message\":\"连接成功\"}"}],
        "temperature": 0,
        "max_tokens": 16,
    }
    if profile.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    if profile.get("thinking_mode") in {"enabled", "disabled"}:
        payload["thinking"] = {"type": profile["thinking_mode"]}
    try:
        response = requests.post(
            f"{profile['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=min(60, max(10, int(profile.get("timeout_seconds") or 30))),
        )
    except requests.RequestException as exc:
        raise ValueError(f"模型连接失败：{exc}") from exc
    if not response.ok:
        raise ValueError(f"模型测试失败（HTTP {response.status_code}）：{response.text[:300]}")
    try:
        if not response.json().get("choices"):
            raise ValueError
    except (ValueError, requests.JSONDecodeError) as exc:
        raise ValueError("模型测试未返回有效 choices 数据") from exc
    return "连接成功：模型接口已响应"
