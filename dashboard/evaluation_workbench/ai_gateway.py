"""面向 OpenAI-compatible 文本模型的最小 AI 网关。"""

from __future__ import annotations

import json
import os
import re
import threading

import requests


_REQUEST_SESSIONS = threading.local()


def _http_post(*args, **kwargs):
    """按工作线程复用 HTTP 连接；评审并发时不共享 Session 可变状态。"""
    session = getattr(_REQUEST_SESSIONS, "session", None)
    if session is None:
        session = requests.Session()
        _REQUEST_SESSIONS.session = session
    return session.post(*args, **kwargs)


class InvalidJsonResponse(ValueError):
    """结构化响应无法解析；正文仅在当前进程内用于低成本 JSON 修复。"""

    def __init__(self, content: object, finish_reason: object = None):
        self.raw_content = content if isinstance(content, str) else ""
        self.finish_reason = str(finish_reason or "")
        super().__init__(_invalid_json_error(content, finish_reason))


class ModelResponseEnvelopeError(ValueError):
    """接口 HTTP 成功但未返回 OpenAI-compatible 正文。"""

    def __init__(self, message: str, *, retryable: bool = True, provider_code: object = None):
        self.retryable = bool(retryable)
        self.provider_code = str(provider_code or "")
        super().__init__(message)


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
    # 个别兼容接口会在文件路径、编号等普通文本中留下无效反斜杠。只把无效转义
    # 变为字面量反斜杠，不补全字段、不猜测业务内容。
    repaired = re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", repaired)
    if repaired != value:
        attempts.append(repaired)
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            pass
    # 保留最早的严格 JSON 异常，调用方只记录安全诊断，不持久化正文。
    return json.loads(attempts[0])


def _balanced_object_candidates(value: str) -> list[str]:
    """从附带说明的响应中找出完整对象，避免贪婪截取到后续的花括号。"""
    candidates: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(value):
        if start < 0:
            if character == "{":
                start, depth = index, 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                candidates.append(value[start:index + 1])
                start = -1
    return candidates


def _normalise_json_response_text(content: str) -> str:
    """去掉兼容模型常见包装；只移除非 JSON 外壳，不改动业务正文。"""
    value = content.strip().lstrip("\ufeff")
    value = re.sub(r"^\s*<think>.*?</think>\s*", "", value, count=1, flags=re.IGNORECASE | re.DOTALL)
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _recover_complete_json_array(content: object, expected_field: str) -> dict | None:
    """从被截断的顶层数组中回收完整对象，绝不补写或猜测截断对象。"""
    if not isinstance(content, str) or not expected_field:
        return None
    value = _normalise_json_response_text(content)
    match = re.search(rf'"{re.escape(expected_field)}"\s*:\s*\[', value)
    if not match:
        return None
    recovered: list[dict] = []
    object_start: int | None = None
    object_depth = 0
    in_string = False
    escaped = False
    for index in range(match.end(), len(value)):
        character = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if object_depth == 0:
                object_start = index
            object_depth += 1
        elif character == "}" and object_depth:
            object_depth -= 1
            if object_depth == 0 and object_start is not None:
                try:
                    item = _load_json_candidate(value[object_start:index + 1])
                except json.JSONDecodeError:
                    item = None
                if isinstance(item, dict):
                    recovered.append(item)
                object_start = None
        elif character == "]" and object_depth == 0:
            break
    return {expected_field: recovered} if recovered else None


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
    value = _normalise_json_response_text(content)
    try:
        parsed = _load_json_candidate(value)
    except json.JSONDecodeError as original_error:
        # 少量兼容接口仍可能在 JSON 前后附带简短说明；只尝试结构完整的对象，
        # 不以“第一个 { 到最后一个 }”的贪婪方式吞入说明文字。
        for candidate in _balanced_object_candidates(value):
            try:
                parsed = _load_json_candidate(candidate)
                break
            except json.JSONDecodeError:
                continue
        else:
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


def _minimax_response_error(body: dict) -> ModelResponseEnvelopeError | InvalidJsonResponse | None:
    """识别 MiniMax HTTP 200 业务错误；将输出触顶交给调用方的拆分恢复流程。"""
    base_resp = body.get("base_resp")
    if not isinstance(base_resp, dict):
        return None
    code = base_resp.get("status_code")
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        return None
    if numeric_code == 0:
        return None
    if numeric_code == 1039:
        return InvalidJsonResponse("", "length")
    # 这些是服务端繁忙、超时、频率限制或下游短暂错误；其余错误不能靠重试修复。
    retryable = numeric_code in {1000, 1001, 1002, 1024, 1033}
    detail = str(base_resp.get("status_msg") or "").strip()
    label = f"（服务商代码 {numeric_code}）"
    if detail:
        label += f"：{detail[:160]}"
    return ModelResponseEnvelopeError(f"模型接口业务错误{label}", retryable=retryable, provider_code=numeric_code)


def _response_reached_output_limit(body: dict, requested_output_tokens: int | None) -> bool:
    """兼容接口有时仅返回 usage 而省略 choices；命中上限时按截断处理。"""
    if not requested_output_tokens:
        return False
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return False
    value = usage.get("completion_tokens", usage.get("output_tokens"))
    try:
        return int(value) >= max(1, int(requested_output_tokens) - 2)
    except (TypeError, ValueError):
        return False


def _response_choice(body: object, *, requested_output_tokens: int | None = None) -> tuple[dict, object]:
    """读取兼容接口的正文；不持久化异常响应，避免泄露模型或业务正文。"""
    if not isinstance(body, dict):
        raise ModelResponseEnvelopeError("模型接口响应格式异常，未返回 JSON 对象")
    minimax_error = _minimax_response_error(body)
    if minimax_error:
        raise minimax_error
    if body.get("input_sensitive") is True or body.get("output_sensitive") is True:
        raise ModelResponseEnvelopeError("模型接口因内容安全限制未返回可用正文", retryable=False)
    try:
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        # MiniMax 等兼容接口在瞬时繁忙时可能返回 HTTP 200 的错误/空包，
        # 不应被误判为业务 JSON 异常；调用层会仅重试该最小工作分组。
        error = body.get("error")
        detail = ""
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("type") or "").strip()
        elif isinstance(error, str):
            detail = error.strip()
        if _response_reached_output_limit(body, requested_output_tokens):
            # M3 adaptive thinking 可能先耗尽全部生成预算，未留下最终 JSON；不能把它
            # 当作网络空包原样重试，应走既有的规则拆分/紧凑恢复路径。
            raise InvalidJsonResponse("", "length") from exc
        suffix = f"：{detail[:160]}" if detail else ""
        permanent_terms = ("invalid", "authentication", "api key", "balance", "insufficient", "参数", "鉴权", "余额")
        retryable = not detail or not any(term in detail.lower() for term in permanent_terms)
        raise ModelResponseEnvelopeError(
            f"模型接口响应不完整，缺少 choices/message/content{suffix}", retryable=retryable,
        ) from exc
    if not isinstance(choice, dict):
        raise ModelResponseEnvelopeError("模型接口响应不完整，choices 条目格式异常")
    return choice, content


def _requested_output_tokens(profile: dict, max_tokens: int | None, *, reserve_for_adaptive_thinking: bool = True) -> int | None:
    if max_tokens is None:
        return None
    limit = max(16, int(max_tokens))
    if _is_minimax_m3(profile) and reserve_for_adaptive_thinking and profile.get("thinking_mode") != "disabled":
        # 业务 JSON 本身通常只需数千 token；M3 adaptive 还会从同一生成预算中消耗
        # 推理 token。给出 16K~24K 的上限以避免“只完成思考、没有最终正文”，但不
        # 采用官方 128K 建议值，防止小规格工作台出现不受控的单次成本。
        limit = max(16_000, min(24_000, limit * 3))
    return limit


def _record_response_metadata(callback, body: object, requested_output_tokens: int | None) -> None:
    if not callback:
        return
    choice = None
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
    content = (choice.get("message") or {}).get("content") if isinstance(choice, dict) else None
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    if not finish_reason and isinstance(body, dict):
        base_resp = body.get("base_resp")
        if (isinstance(base_resp, dict) and str(base_resp.get("status_code")) == "1039") or _response_reached_output_limit(body, requested_output_tokens):
            finish_reason = "length"
    callback({
        "requested_max_tokens": requested_output_tokens,
        "finish_reason": finish_reason,
        "response_chars": len(content) if isinstance(content, str) else 0,
    })


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
    requested_output_tokens = _requested_output_tokens(profile, max_tokens)
    if requested_output_tokens is not None:
        # MiniMax M3 已将 max_tokens 标为废弃参数；使用新字段并为 adaptive thinking
        # 预留预算，其他 OpenAI-compatible 模型保持原字段以兼容既有配置。
        payload["max_completion_tokens" if _is_minimax_m3(profile) else "max_tokens"] = requested_output_tokens
    try:
        response = _http_post(
            f"{base_url}/chat/completions",
            headers=_headers(api_key),
            json=payload,
            timeout=min(1800, max(30, int(profile.get("timeout_seconds") or 600))),
        )
    except (requests.RequestException, UnicodeEncodeError) as exc:
        raise ValueError(f"模型连接失败：{exc}") from exc
    if not response.ok:
        _raise_http_error(response, operation="模型请求失败")
    try:
        body = response.json()
    except (requests.JSONDecodeError, ValueError) as exc:
        raise ModelResponseEnvelopeError("模型接口响应不是有效 JSON") from exc
    if usage_callback:
        usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
        usage_callback(usage)
    # 无论 choices 是否缺失，都记录长度、结束原因和请求上限；绝不保存正文。
    _record_response_metadata(response_metadata_callback, body, requested_output_tokens)
    choice, content = _response_choice(body, requested_output_tokens=requested_output_tokens)
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
    }
    if _is_minimax_m3(profile):
        # M3 adaptive thinking 与最终短回答共用生成预算；16 token 不足以完成一次真实
        # 兼容性验证。该测试仍保持很小，不影响常规评审成本。
        payload["max_completion_tokens"] = 1024
    else:
        payload["max_tokens"] = 16
    if profile.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    thinking = _thinking_payload(profile)
    if thinking:
        payload["thinking"] = thinking
    if _is_minimax_m3(profile):
        payload["reasoning_split"] = True
    try:
        response = _http_post(
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
