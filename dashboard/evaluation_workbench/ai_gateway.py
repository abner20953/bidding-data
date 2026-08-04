"""面向 OpenAI-compatible 文本模型的最小 AI 网关。"""

from __future__ import annotations

import json
import os
import re
import threading
import copy
import base64

import requests

try:
    from json_repair import repair_json
except ImportError:  # 部署升级中的短暂兼容；正式镜像由 requirements.txt 安装依赖。
    repair_json = None


_REQUEST_SESSIONS = threading.local()
_VISION_TEST_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "wYHfWQAAAABJRU5ErkJggg=="
)


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


def _load_json_candidate(value: str, *, allow_structural_repair: bool = True,
                         diagnostics: dict | None = None) -> object:
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
            parsed = json.loads(repaired, strict=False)
            if diagnostics is not None:
                diagnostics["local_json_repaired"] = True
            return parsed
        except json.JSONDecodeError:
            pass
    # MiniMax 等兼容模型常在自然语言证据中的引号、逗号或闭合括号上出错。先做本地
    # 语法修复，随后仍由各业务阶段按 rule_id、枚举值和分数边界严格校验；长度截断
    # 响应绝不在这里补尾，仍交给原有的回收/拆分流程。
    if allow_structural_repair and repair_json is not None:
        try:
            parsed = repair_json(value, return_objects=True, skip_json_loads=True)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            if diagnostics is not None:
                diagnostics["local_json_repaired"] = True
            return parsed
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
            elif character == "\\":
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


def _json_error_kind(error: Exception) -> str:
    if isinstance(error, json.JSONDecodeError):
        value = re.sub(r"[^a-z0-9]+", "_", error.msg.lower()).strip("_")
        return f"json_{value[:56] or 'syntax'}"
    return "json_invalid"


def _decode_json_content(content, *, allow_structural_repair: bool = True,
                         diagnostics: dict | None = None) -> dict:
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
    # 若正文前带有自然语言说明，不能让宽松修复器把说明误当 JSON 字段；先从完整
    # 对象候选中修复。真正以 JSON 开头的响应才允许直接做结构化本地修复。
    can_repair_whole_value = value.lstrip().startswith(("{", "[", '"'))
    try:
        parsed = _load_json_candidate(
            value,
            allow_structural_repair=allow_structural_repair and can_repair_whole_value,
            diagnostics=diagnostics,
        )
    except json.JSONDecodeError as original_error:
        # 少量兼容接口仍可能在 JSON 前后附带简短说明；只尝试结构完整的对象，
        # 不以“第一个 { 到最后一个 }”的贪婪方式吞入说明文字。
        for candidate in _balanced_object_candidates(value):
            try:
                parsed = _load_json_candidate(candidate, allow_structural_repair=allow_structural_repair, diagnostics=diagnostics)
                break
            except json.JSONDecodeError:
                continue
        else:
            if diagnostics is not None:
                diagnostics.update({"parse_status": "invalid_json", "parse_error_kind": _json_error_kind(original_error)})
            raise original_error
    # 某些兼容接口会把 JSON 对象再次序列化成字符串。
    if isinstance(parsed, str):
        parsed = _load_json_candidate(parsed, allow_structural_repair=allow_structural_repair, diagnostics=diagnostics)
    if not isinstance(parsed, dict):
        if diagnostics is not None:
            diagnostics.update({"parse_status": "invalid_json", "parse_error_kind": "json_top_level_not_object"})
        raise ValueError("模型返回的 JSON 顶层必须是对象")
    if diagnostics is not None:
        diagnostics.setdefault("parse_status", "local_repaired" if diagnostics.get("local_json_repaired") else "strict_json")
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


def _is_minimax_profile(profile: dict) -> bool:
    return "api.minimaxi.com" in str(profile.get("base_url") or "").lower()


def _is_minimax_m3(profile: dict) -> bool:
    return (
        _is_minimax_profile(profile)
        and str(profile.get("model_name") or "").lower() == "minimax-m3"
    )


def _vision_detail_for_profile(profile: dict, detail: object) -> str:
    """将内部质量档位映射为当前 OpenAI-compatible 服务商可接受的 detail 值。

    业务层只表达 low/standard/high，不能把某家模型的 default 语义散落在图片
    渲染和规则逻辑中。MiniMax 保持已验证的 default 行为；其他兼容接口采用
    OpenAI 规范的 auto，避免因 default 参数而拒绝图片请求。
    """
    value = str(detail or "standard").lower()
    if value == "standard":
        return "default" if _is_minimax_profile(profile) else "auto"
    return value if value in {"low", "high"} else "auto"


def build_vision_user_content(profile: dict, prompt: str, images: list[dict]) -> list[dict]:
    """构造当前 OpenAI-compatible 图片内容块，并集中处理协议细节。

    这是服务商适配边界的第一层：worker 只传稳定页号和内部质量档位。后续接入
    Responses/Anthropic 等协议时，只扩展此处及网关，不改动评审候选、OCR 或结论链。
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        page = image.get("page")
        if isinstance(page, (int, float)) and not isinstance(page, bool):
            content.append({"type": "text", "text": f"以下图片对应投标文件第{int(page)}页。"})
        raw_bytes = image.get("image_bytes")
        mime_type = str(image.get("mime_type") or "image/jpeg")
        if isinstance(raw_bytes, bytes):
            item = {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64," + base64.b64encode(raw_bytes).decode("ascii"),
                    "detail": image.get("detail") or "standard",
                },
            }
        else:
            # 兼容现有测试、历史调用方和未来由远程 URL 提供的图片资产。
            item = copy.deepcopy({key: value for key, value in image.items() if key != "page"})
        image_url = item.get("image_url")
        if isinstance(image_url, dict):
            image_url["detail"] = _vision_detail_for_profile(profile, image_url.get("detail"))
        content.append(item)
    return content


def model_capabilities(profile: dict) -> dict:
    """返回可展示、可路由的模型能力，不把服务商差异散落在业务流程里。

    这是保守能力声明：未知模型只获得提示词约束的 JSON，不会被误当成支持严格
    Schema 或视觉输入。后续连接测试可在此基础上补充主动探测结果。
    """
    model_name = str(profile.get("model_name") or "").lower()
    base_url = str(profile.get("base_url") or "").lower()
    minimax = "api.minimaxi.com" in base_url
    m3 = minimax and model_name == "minimax-m3"
    declared_vision = bool(profile.get("supports_vision"))
    vision_protocol = str(profile.get("vision_protocol") or "")
    deepseek = "api.deepseek.com" in base_url or model_name.startswith("deepseek-")
    if deepseek:
        return {
            "structured_output": "json_object",
            "strict_tool_schema": True,
            "vision": declared_vision,
            "vision_protocol": vision_protocol if declared_vision else "",
            "thinking_modes": ["enabled", "disabled"],
            "parallel_limit": 3,
            "prompt_cache": "prefix",
        }
    if minimax:
        return {
            # M2/M3 在当前 OpenAI-compatible 路径不声明 json_object/schema；保留
            # 本地 JSON 恢复兜底，而不是让界面产生“严格 JSON 已开启”的错觉。
            "structured_output": "prompt_constrained",
            "strict_tool_schema": False,
            "vision": declared_vision,
            "vision_protocol": vision_protocol if declared_vision else "",
            "thinking_modes": ["adaptive", "disabled"] if m3 else ["default"],
            "parallel_limit": 2,
            "prompt_cache": "prefix",
        }
    return {
        "structured_output": "prompt_constrained",
        "strict_tool_schema": False,
        "vision": declared_vision,
        "vision_protocol": vision_protocol if declared_vision else "",
        "thinking_modes": ["default", "enabled", "disabled"],
        "parallel_limit": 1,
        "prompt_cache": "unknown",
    }


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


def request_json(profile: dict, system_prompt: str, user_prompt: object, *, usage_callback=None,
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
    # MiniMax 公开文档没有将 json_object 列为 M2/M3 的受支持结构化输出方式。
    # 继续发送该参数会制造“已开启 JSON 模式”的错觉，实际仍只能依赖提示词；
    # 后续工具调用协议会单独接管 MiniMax 的严格结构化输出。
    if profile.get("json_mode") and not _is_minimax_profile(profile):
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
    try:
        choice, content = _response_choice(body, requested_output_tokens=requested_output_tokens)
    except ModelResponseEnvelopeError as exc:
        if response_metadata_callback:
            response_metadata_callback({"parse_status": "envelope_error", "parse_error_kind": "missing_choice_content"})
        raise exc
    diagnostics: dict = {}
    try:
        result = _decode_json_content(
            content,
            allow_structural_repair=str(choice.get("finish_reason") or "").lower() not in {"length", "max_tokens"},
            diagnostics=diagnostics,
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        if response_metadata_callback:
            response_metadata_callback(diagnostics or {"parse_status": "invalid_json", "parse_error_kind": _json_error_kind(exc)})
        raise InvalidJsonResponse(content, choice.get("finish_reason")) from exc
    if response_metadata_callback:
        response_metadata_callback(diagnostics)
    return result


def test_connection(profile: dict, prompt_text: str) -> str:
    """发送极小请求验证模型地址、密钥和兼容参数；不写入业务数据。"""
    api_key = _api_key_for(profile)
    user_content: object = prompt_text
    if profile.get("supports_vision"):
        # 仅发送一张 1×1 PNG，验证“人工标记为多模态”的档案确实接受图片内容块；
        # 失败只说明图片能力不可用，绝不影响同一档案的文本调用。
        user_content = build_vision_user_content(profile, prompt_text, [{
            "page": 1, "type": "image_url",
            "image_url": {"url": _VISION_TEST_IMAGE_DATA_URL, "detail": "low"},
        }])
    payload = {
        "model": profile["model_name"],
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0,
    }
    if _is_minimax_m3(profile):
        # M3 adaptive thinking 与最终短回答共用生成预算；16 token 不足以完成一次真实
        # 兼容性验证。该测试仍保持很小，不影响常规评审成本。
        payload["max_completion_tokens"] = 1024
    else:
        payload["max_tokens"] = 16
    if profile.get("json_mode") and not _is_minimax_profile(profile):
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
        body = response.json()
        choice, content = _response_choice(body, requested_output_tokens=None)
        value = _decode_json_content(content, allow_structural_repair=False)
        if not isinstance(value.get("message"), str) or not value["message"].strip():
            raise ValueError("缺少 message 字段")
    except (ValueError, requests.JSONDecodeError, TypeError) as exc:
        raise ValueError("模型测试未返回有效的结构化 JSON 数据") from exc
    return "连接成功：模型接口已响应，图片与结构化 JSON 测试通过" if profile.get("supports_vision") else "连接成功：模型接口已响应，结构化 JSON 测试通过"
