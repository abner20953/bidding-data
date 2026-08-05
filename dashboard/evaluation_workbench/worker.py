"""按需启动的工作台任务进程。"""

from __future__ import annotations

import itertools
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree

import fitz

from dashboard.evaluation_workbench import storage
from dashboard.evaluation_workbench.ai_gateway import (
    InvalidJsonResponse, ModelResponseEnvelopeError, _recover_complete_json_array, build_vision_user_content,
    model_capabilities, request_json,
)
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.ocr_gateway import OCR_PARSER_VERSION, request_tencent_ocr
from dashboard.evaluation_workbench.local_ocr_gateway import (
    LOCAL_OCR_PARSER_VERSION, LOCAL_OCR_SERVICE, local_ocr_max_workers, request_local_ocr,
)
from dashboard.evaluation_workbench.prompt_context import (
    build_rule_context, select_rule_chunk_evidence_map, select_rule_chunk_map, select_rule_chunks,
    split_full_text_chunks,
)
from dashboard.evaluation_workbench.prompt_templates import EVALUATION_PROMPT_VERSION
from dashboard.blueprints.evaluation_workbench import create_worker_app
from dashboard.utils.comparator import CollusionDetector, ComparisonLimitError


MAX_PARSE_PAGES = 2000
MAX_PARSED_CHARS = 2_000_000
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024
PROMPT_VERSION = EVALUATION_PROMPT_VERSION
COMPARE_AI_PROMPT_VERSION = "compare-evidence-ai-v3"
# 单条线索的证据包虽小，但查重往往同时命中多种维度；以较小批次起步，并在
# 截断时继续局部拆分，避免某一批过长导致整批线索都只能降级为人工核验。
COMPARE_AI_BATCH_SIZE = 8


class _EvaluationRequestGate:
    """规则提取/综合评审的模型请求闸门；稳定时按档案上限升档，限流时逐级回退。"""

    def __init__(self, limit: int = 2, max_limit: int | None = None):
        self.limit = max(1, int(limit))
        self.max_limit = max(self.limit, int(max_limit or self.limit))
        self.active = 0
        self.success_count = 0
        self.condition = threading.Condition()

    def acquire(self) -> None:
        with self.condition:
            while self.active >= self.limit:
                self.condition.wait()
            self.active += 1

    def release(self) -> None:
        with self.condition:
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    def record_success(self) -> bool:
        """稳定完成若干次请求后才逐级开放一条并行位，避免小规格服务器突发放量。"""
        with self.condition:
            self.success_count += 1
            if self.limit < self.max_limit and self.success_count >= 6:
                self.limit += 1
                self.success_count = 0
                self.condition.notify_all()
                return True
            return False

    def reduce_after_rate_limit(self) -> bool:
        """按 3→2→1 逐级回退；下一批任务重新从保守并行度开始。"""
        with self.condition:
            next_limit = 2 if self.limit > 2 else 1
            if next_limit >= self.limit:
                return False
            self.limit = next_limit
            self.success_count = 0
            self.condition.notify_all()
            return True


def _is_rate_limit_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(term in message for term in (
        "http 408", "http 429", "http 529", "http 502", "http 503", "http 504",
        "rate limit", "too many requests", "overloaded", "temporarily unavailable", "timeout", "timed out",
    )) or "限流" in str(error) or "接口繁忙" in str(error)


# 重试只覆盖服务端或链路的短暂故障。鉴权、余额、参数、内容安全和输出长度分别有
# 明确的处理路径，不能靠重复同一请求掩盖真实问题。
_TRANSIENT_TRANSPORT_MARKERS = (
    "remotedisconnected", "remote end closed", "connection reset", "connection aborted",
    "connection broken", "connection refused", "connection timed out", "read timed out",
    "connect timeout", "temporary failure in name resolution", "name or service not known",
    "network is unreachable", "broken pipe", "eof occurred", "ssl eof",
)
_REQUEST_RETRY_BACKOFF_SECONDS = (5, 15, 45)
_HTTP_STATUS_PATTERN = re.compile(r"(?:http|status(?:\s+code)?)\s*[:=]?\s*(\d{3})", re.I)


def _is_recoverable_model_error(error: Exception) -> bool:
    if isinstance(error, ModelResponseEnvelopeError):
        return bool(error.retryable)
    raw_message = str(error)
    # 明确的客户端 4xx（408/429 除外）是配置、鉴权或参数问题，不能因为错误文案
    # 中带有“timeout”等词就重复同一请求。
    statuses = [int(value) for value in _HTTP_STATUS_PATTERN.findall(raw_message)]
    if any(400 <= status < 500 and status not in {408, 429} for status in statuses):
        return False
    message = raw_message.lower()
    return _is_rate_limit_error(error) or any(marker in message for marker in _TRANSIENT_TRANSPORT_MARKERS)


def _is_minimax_profile(profile: dict) -> bool:
    """MiniMax 的令牌计划按并发突发计量，统一保守地最多两路请求。"""
    return "api.minimaxi.com" in str(profile.get("base_url") or "").lower()


def _profile_parallel_limit(profile: dict, available: int) -> int:
    """只限制同一任务的远端请求数，不额外常驻线程或模型。"""
    ceiling = int(model_capabilities(profile).get("parallel_limit") or 1)
    return max(1, min(ceiling, max(1, int(available))))


def _prompt_char_limit(profile: dict, default: int, ceiling: int) -> int:
    """以保守字符数近似上下文，给提示和输出预留空间。"""
    try:
        context_limit = int(profile.get("context_limit") or 0)
    except (TypeError, ValueError):
        context_limit = 0
    return min(ceiling, max(8_000, int(context_limit * 0.7))) if context_limit else default


def _lock_path(app) -> Path:
    return storage.data_dir(app) / "worker.lock"


def _prompt_input_chars(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(value or ""))


def _stable_prompt_json(value: object) -> str:
    """提示词内结构化数据采用固定键序，便于支持前缀缓存的模型稳定命中。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _task_request_profile(profile: dict, phase: str, thinking_mode: str | None = None) -> dict:
    """为小规格工作台给可恢复的综合评审请求设定边界，不改变模型档案的保存值。"""
    effective = {**profile, "thinking_mode": thinking_mode} if thinking_mode else dict(profile)
    # M3 发生网络卡死时，旧的600秒读取超时会让整项任务长期停在原地。综合评审已具备
    # 当前规则组重试/拆分和已完成结果落库能力，240秒后交给该恢复链路更稳妥。
    if phase.startswith("evaluate_all") and _is_minimax_profile(effective):
        effective["timeout_seconds"] = min(240, max(30, int(effective.get("timeout_seconds") or 240)))
    return effective


def _request_task_json(app, task: dict, profile: dict, phase: str, system_prompt: str, user_prompt: object,
                       *, document_id: str | None = None, context_mode: str = "full_prefix",
                       max_tokens: int | None = None, thinking_mode: str | None = None) -> dict:
    """调用模型并只记录用量元数据，不记录正文或提示词。"""
    gate = task.get("_evaluation_request_gate")
    effective_profile = _task_request_profile(profile, phase, thinking_mode)
    for attempt in range(len(_REQUEST_RETRY_BACKOFF_SECONDS) + 1):
        # 每一次真实请求单独落一行用量。此前重试会覆盖上一轮 usage，导致 token
        # 统计偏低，也难以区分服务暂时繁忙与模型输出触顶。
        usage: dict = {}
        response_metadata: dict = {"requested_max_tokens": max_tokens}

        def record_usage(value: dict) -> None:
            usage.update(value if isinstance(value, dict) else {})

        def record_response_metadata(value: dict) -> None:
            response_metadata.update(value if isinstance(value, dict) else {})

        retry_after_failure = False
        if gate:
            gate.acquire()
        try:
            try:
                result = request_json(
                    effective_profile, system_prompt, user_prompt, usage_callback=record_usage,
                    response_metadata_callback=record_response_metadata, max_tokens=max_tokens,
                )
                if gate:
                    gate.record_success()
                return result
            except ValueError as exc:
                # 规则提取和综合评审共用该闸门。服务商限流时让后续请求
                # 自动改为单路，并只重试当前这一小次模型调用，不重发整份文件。
                incomplete_envelope = isinstance(exc, ModelResponseEnvelopeError)
                envelope_retryable = incomplete_envelope and exc.retryable
                rate_limited = _is_rate_limit_error(exc)
                # InvalidJsonResponse(length) 由上层拆分；其余可恢复网络、过载或空包
                # 故障最多补三次。每次只重试当前调用，不会重发整份投标文件。
                should_retry = attempt < len(_REQUEST_RETRY_BACKOFF_SECONDS) and _is_recoverable_model_error(exc)
                if should_retry:
                    # 仅限流/超时才降低并发。响应结构异常、业务错误包与并行度无必然关系。
                    if rate_limited and gate and gate.reduce_after_rate_limit():
                        message = "模型接口限流或暂时繁忙，已自动降低并行度后继续"
                        storage.update_task(app, task["task_id"], message=message)
                    elif envelope_retryable:
                        storage.update_task(app, task["task_id"], message=f"模型接口返回不完整响应，正在第 {attempt + 1}/{len(_REQUEST_RETRY_BACKOFF_SECONDS)} 次重试当前分组")
                    else:
                        storage.update_task(app, task["task_id"], message=f"模型连接暂时中断，正在第 {attempt + 1}/{len(_REQUEST_RETRY_BACKOFF_SECONDS)} 次重试当前分组")
                    retry_after_failure = True
                else:
                    raise
            finally:
                # 部分兼容接口不返回 usage；仍保留发送字符数与截断元数据以便统计和优化。
                storage.record_model_call(
                    app, task["task_id"], task["project_id"], phase, profile.get("profile_id"),
                    document_id=document_id, input_chars=len(system_prompt) + _prompt_input_chars(user_prompt),
                    context_mode=context_mode, usage=usage, response_metadata=response_metadata,
                )
        finally:
            if gate:
                gate.release()
        if retry_after_failure:
            # 必须先释放并发位；否则失败请求在退避期间会无谓阻塞另一家投标人的收尾。
            time.sleep(_REQUEST_RETRY_BACKOFF_SECONDS[attempt])
            continue


def _system_prompt(app, template_id: str) -> str:
    base = storage.render_prompt_template(app, template_id)
    # 将长期维护的业务判断原则与可变的 JSON/任务模板分开。这样即使用户仍在使用
    # 历史任务模板，新的通用原则也可独立查看、编辑和升级，不依赖业务硬编码。
    overlay_ids = {
        "extract_rules": ("extract_rules_guidance", "extract_rules_validation_guidance"),
        "evaluate_all": ("evaluate_all_guidance", "evaluate_all_output_contract"),
    }.get(template_id, ())
    if overlay_ids:
        overlays = "\n\n".join(
            f"【{'通用业务指令' if index == 0 else '系统与结果约束'}】\n{storage.render_prompt_template(app, overlay_id)}"
            for index, overlay_id in enumerate(overlay_ids)
        )
        return f"{base}\n\n{overlays}"
    return base


def _repair_invalid_json(app, task: dict, profile: dict, phase: str, error: InvalidJsonResponse,
                         expected_field: str, *, document_id: str | None = None) -> dict:
    """只回传异常响应修复 JSON，避免格式问题导致整份投标文件被重复发送。"""
    if not error.raw_content.strip():
        raise error
    if error.finish_reason.lower() in {"length", "max_tokens"}:
        # 输出已被截断时不存在可可靠修复的尾部，交由调用方拆小规则组。
        raise error
    prompt = storage.render_prompt_template(
        app, "json_repair_user", expected_field=expected_field,
        raw_response=error.raw_content[:80_000],
    )
    return _request_task_json(
        app, task, profile, phase, _system_prompt(app, "json_repair"), prompt,
        document_id=document_id, context_mode="response_only_json_repair",
        max_tokens=_output_token_budget(profile, min(8_000, max(2_000, len(error.raw_content) // 2))),
        thinking_mode="disabled",
    )


def _extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("DOCX 正文解压后过大，无法在当前服务器规格下安全解析")
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    parts = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        text = "".join(node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        if text.strip():
            parts.append(text.strip())
    result = "\n".join(parts)
    if len(result) > MAX_PARSED_CHARS:
        raise ValueError("文件可提取文本过长，超过低资源解析限制")
    return result


def _parse_document(app, task: dict) -> dict:
    documents = storage.list_documents(app, task["project_id"])
    pending_documents = [
        item for item in documents
        if item.get("parse_status") != "success"
        or not item.get("parsed_path")
        or not Path(item["parsed_path"]).is_file()
    ]
    if not pending_documents:
        storage.update_task(app, task["task_id"], progress=100, message="全部文件已有有效解析缓存")
        return {"document_count": len(documents), "parsed_count": 0, "skipped_count": len(documents)}
    total = len(pending_documents)
    parsed = 0
    errors = []
    for document in pending_documents:
        source = storage.document_path(app, document)
        storage.update_task(app, task["task_id"], progress=int(parsed * 100 / total), message=f"正在解析：{document['original_name']}")
        try:
            if document["extension"] == ".pdf":
                with fitz.open(source) as pdf:
                    if pdf.page_count > MAX_PARSE_PAGES:
                        raise ValueError(f"PDF 页数超过 {MAX_PARSE_PAGES} 页限制")
                    pages = []
                    text_length = 0
                    for page_number, page in enumerate(pdf, start=1):
                        page_text = page.get_text("text", sort=True)
                        page_text = f"[第{page_number}页]\n{page_text}"
                        text_length += len(page_text)
                        if text_length > MAX_PARSED_CHARS:
                            raise ValueError("文件可提取文本过长，超过低资源解析限制")
                        pages.append(page_text)
                    page_count = pdf.page_count
                text = "\n\n".join(pages)
            else:
                text = _extract_docx_text(source)
                page_count = None
            if not text.strip():
                raise ValueError("未提取到可检索文本；扫描件暂不支持 OCR")
            parsed_path = storage.project_dir(app, task["project_id"]) / "parsed" / f"{document['document_id']}.txt"
            parsed_path.write_text(text, encoding="utf-8")
            with storage.connection(app) as conn:
                conn.execute(
                    "UPDATE ew_documents SET page_count=?, text_length=?, parse_status='success', parse_error=NULL, parsed_path=?, updated_at=? WHERE document_id=?",
                    (page_count, len(text), str(parsed_path), storage.now_iso(), document["document_id"]),
                )
        except Exception as exc:
            errors.append(f"{document['original_name']}：{exc}")
            with storage.connection(app) as conn:
                conn.execute(
                    "UPDATE ew_documents SET parse_status='error', parse_error=?, updated_at=? WHERE document_id=?",
                    (str(exc), storage.now_iso(), document["document_id"]),
                )
        parsed += 1
    if errors:
        raise ValueError("；".join(errors[:5]))
    return {
        "document_count": len(documents),
        "parsed_count": parsed,
        "skipped_count": len(documents) - len(pending_documents),
    }


def _compare_documents(app, task: dict) -> dict:
    documents = storage.list_documents(app, task["project_id"])
    tender = next((item for item in documents if item["role"] == "tender"), None)
    bids = [item for item in documents if item["role"] == "bid"]
    if len(bids) < 2:
        raise ValueError("至少需要两份投标文件才能开始查重")
    non_pdf = [item["original_name"] for item in ([tender] if tender else []) + bids if item and item["extension"] != ".pdf"]
    if non_pdf:
        raise ValueError("当前多文件查重仅支持 PDF；DOCX 已可解析和管理，通用文本查重将在后续阶段接入")

    tender_path = str(storage.document_path(app, tender)) if tender else None
    detector = CollusionDetector(tender_path, build_text_index=True)
    pairs = list(itertools.combinations(bids, 2))
    summaries = []
    analyzed_pairs = []
    for index, (left, right) in enumerate(pairs, start=1):
        storage.update_task(app, task["task_id"], progress=int((index - 1) * 100 / len(pairs)), message=f"正在比较 {index}/{len(pairs)}：{left['original_name']} 与 {right['original_name']}")
        result = detector.find_collisions(
            str(storage.document_path(app, left)),
            str(storage.document_path(app, right)),
            check_entity=True,
            check_text=True,
            check_spelling=True,
        )
        storage.save_compare_pair(app, task["task_id"], left["document_id"], right["document_id"], result)
        analyzed_pairs.append((left, right, result))
        summaries.append({
            "document_a_id": left["document_id"],
            "document_b_id": right["document_id"],
            "summary": result.get("summary", {}),
        })
    analysis = build_cross_bid_analysis(task["task_id"], analyzed_pairs, tender_loaded=bool(tender))
    storage.initialize_compare_signal_reviews(app, task["task_id"], analysis["signals"])
    _assess_compare_signals_with_ai(app, task, analysis)
    return {"pair_count": len(pairs), "pairs": summaries, "cross_bid_analysis": analysis}


def _compare_evidence_packet(signal: dict) -> dict:
    """只向模型传递固定规则已命中的短证据，不传完整投标文件。"""
    evidence = []
    for item in signal.get("evidence", [])[:3]:
        evidence.append({key: str(value)[:280] for key, value in item.items()
                         if key in {
                             "page_a", "page_b", "text_a", "text_b", "similarity",
                             "tender_similarity", "tender_coverage_a",
                             "tender_coverage_b", "segment_count", "shared_edits",
                             "error_kind", "entity_kind", "field", "value", "strength",
                         }})
    return {
        "signal_id": signal["signal_id"], "bidders": [signal.get("bidder_a"), signal.get("bidder_b")],
        "fixed_rule": signal.get("dimension_label"), "basis": str(signal.get("basis", ""))[:420],
        "evidence": evidence, "counter_evidence": [str(item)[:220] for item in signal.get("counter_evidence", [])[:2]],
    }


def _output_token_budget(profile: dict, target: int) -> int | None:
    """为结构化输出设置保守上限；规则提取已在调用前分段，不依赖放大总上限。"""
    model_name = str(profile.get("model_name") or "").lower()
    base_url = str(profile.get("base_url") or "").lower()
    if "api.minimaxi.com" in base_url and model_name.startswith("minimax-m2"):
        return None
    return max(512, min(12_000, int(target)))


def _assess_compare_signals_with_ai(app, task: dict, analysis: dict) -> None:
    signals = analysis.get("signals") or []
    if not signals:
        analysis["ai_assessment"] = {"status": "skipped", "reason": "未发现固定规则线索，未调用模型。", "prompt_version": COMPARE_AI_PROMPT_VERSION}
        return
    try:
        profile = storage.get_model_profile(app, task.get("payload", {}).get("profile_id"), "deepseek-v4-flash")
    except ValueError as exc:
        analysis["ai_assessment"] = {"status": "unavailable", "reason": f"AI 判定未执行：{exc}", "prompt_version": COMPARE_AI_PROMPT_VERSION}
        return
    by_id = {item["signal_id"]: item for item in signals}
    completed_ids, failures = set(), []
    system_prompt = _system_prompt(app, "compare_ai_assessment")

    def apply_assessments(values: object, batch: list[dict]) -> None:
        """只接收当前批次内、字段完整的结论，模型漏回的 ID 留给局部重试。"""
        allowed_ids = {item["signal_id"] for item in batch}
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict) or not isinstance(value.get("signal_id"), str) or value.get("signal_id") not in allowed_ids:
                continue
            decision = value.get("decision")
            if not isinstance(decision, str) or decision not in {"confirmed_clue", "suspected_clue", "excluded", "unassessable"}:
                decision = "unassessable"
            signal = by_id[value["signal_id"]]
            signal["ai_assessment"] = {
                "decision": decision,
                "risk_level": _enum_text(value.get("risk_level"), {"low", "medium", "high"}, "medium"),
                "confidence": _enum_text(value.get("confidence"), {"high", "medium", "low"}, "medium"),
                "reason": str(value.get("reason", ""))[:1000],
                "suggested_check": str(value.get("suggested_check", ""))[:700],
            }
            completed_ids.add(value["signal_id"])

    def assess_batch(batch: list[dict], *, depth: int = 0, leaf_retry: bool = False,
                     retried_missing_group: bool = False) -> bool:
        """截断时先回收完整对象，再仅对剩余 ID 二分；绝不重发成功线索。"""
        if not batch:
            return True
        packets = [_compare_evidence_packet(item) for item in batch]
        user_prompt = storage.render_prompt_template(app, "compare_ai_assessment_user", packets=json.dumps(packets, ensure_ascii=False, separators=(",", ":")))
        try:
            parsed = _request_task_json(app, task, profile, "compare_ai_assessment", system_prompt, user_prompt,
                                        context_mode="evidence_batch",
                                        # 每条需要五个结构字段和判断理由；按较充足预算起步，
                                        # 再由局部拆批兜底，避免过低上限本身制造截断。
                                        max_tokens=_output_token_budget(profile, 900 + len(batch) * 240))
            apply_assessments(parsed.get("assessments") if isinstance(parsed, dict) else [], batch)
        except InvalidJsonResponse as exc:
            # 长度截断时不可能可靠补齐最后半条，但数组中已经闭合的对象仍完全可用。
            recovered = _recover_complete_json_array(exc.raw_content, "assessments")
            apply_assessments(recovered.get("assessments") if recovered else [], batch)
            if exc.finish_reason.lower() not in {"length", "max_tokens"}:
                try:
                    repaired = _repair_invalid_json(
                        app, task, profile, "compare_ai_assessment_json_repair", exc, "assessments",
                    )
                    apply_assessments(repaired.get("assessments") if isinstance(repaired, dict) else [], batch)
                except ValueError:
                    pass
        except Exception as exc:  # 保留确定性查重结果，不能因 AI 暂不可用而丢失证据。
            message = str(exc)[:180]
            failures.append(message)
            if "鉴权失败" in message or "尚未配置 API Key" in message or "HTTP 4" in message:
                return False
            return True
        missing = [item for item in batch if item["signal_id"] not in completed_ids]
        if not missing:
            return True
        # 本批已回收部分结论时，先把“仅缺失 ID”作为一个更小批次再试一次；它通常
        # 已足以避开输出上限，不必马上拆到单条，兼顾速度和结论上下文。
        if len(missing) < len(batch) and not retried_missing_group:
            storage.update_task(app, task["task_id"], message=f"已回收部分查重结论，正在仅重试 {len(missing)} 条遗漏线索")
            return assess_batch(missing, depth=depth + 1, retried_missing_group=True)
        if len(missing) == 1:
            if not leaf_retry:
                storage.update_task(app, task["task_id"], message="部分查重线索未返回，正在仅重试该线索")
                return assess_batch(missing, depth=depth + 1, leaf_retry=True)
            failures.append("模型未返回单条查重线索的有效 JSON")
            return True
        # 模型漏回或输出截断都只影响当前小组；二分后已获得结论的线索不会被重发。
        midpoint = len(missing) // 2
        storage.update_task(app, task["task_id"], message=f"查重 AI 输出不完整，正在仅拆分 {len(missing)} 条未返回线索重试")
        return assess_batch(missing[:midpoint], depth=depth + 1) and assess_batch(missing[midpoint:], depth=depth + 1)

    for start in range(0, len(signals), COMPARE_AI_BATCH_SIZE):
        if not assess_batch(signals[start:start + COMPARE_AI_BATCH_SIZE]):
            break
    for signal in signals:
        signal.setdefault("ai_assessment", {"decision": "unassessable", "risk_level": "medium", "confidence": "low", "reason": "AI 未返回该线索的可用判定。", "suggested_check": "请结合原始文件人工核验。"})
    for summary in analysis.get("pair_summaries", []):
        pair_ids = {summary.get("document_a_id"), summary.get("document_b_id")}
        pair_signals = [
            signal for signal in signals
            if {signal.get("document_a_id"), signal.get("document_b_id")} == pair_ids
        ]
        decisions = [signal["ai_assessment"]["decision"] for signal in pair_signals]
        if "confirmed_clue" in decisions:
            summary["assessment_result"] = "confirmed_clue"
        elif "suspected_clue" in decisions:
            summary["assessment_result"] = "suspected_clue"
        elif decisions and all(decision == "excluded" for decision in decisions):
            summary["assessment_result"] = "excluded"
        elif decisions:
            summary["assessment_result"] = "unassessable"
        # “被模型列为疑似”不等于“足以抬高整对文件的复核级别”。低风险或低
        # 置信线索继续展示，但不参与维度计数；高优先级只由强线索组合产生，
        # 避免三个低信息格式候选叠加成一个高风险配对。
        reportable_signals = []
        strong_signals = []
        direct_entity_dimensions = {
            "contact", "email", "person_name", "person_identity", "address",
        }
        for signal in pair_signals:
            assessment = signal.get("ai_assessment") or {}
            decision = assessment.get("decision")
            risk = assessment.get("risk_level")
            confidence = assessment.get("confidence")
            if (
                decision not in {"confirmed_clue", "suspected_clue"}
                or risk == "low"
                or confidence == "low"
                or signal.get("voice_adaptation_only")
            ):
                continue
            reportable_signals.append(signal)
            if (
                decision == "confirmed_clue"
                or risk == "high"
                or signal.get("dimension") in direct_entity_dimensions
            ):
                strong_signals.append(signal)
        retained_dimensions = sorted({
            signal.get("dimension") for signal in reportable_signals
        })
        strong_dimensions = {
            signal.get("dimension") for signal in strong_signals
        }
        summary["independent_dimension_count"] = len(retained_dimensions)
        summary["strong_dimension_count"] = len(strong_dimensions)
        summary["dimensions"] = retained_dimensions
        summary["dimension_labels"] = list(dict.fromkeys(
            str(signal.get("dimension_label") or signal.get("dimension"))
            for signal in reportable_signals
        ))
        summary["raw_signal_count"] = len(pair_signals)
        summary["signal_count"] = len(reportable_signals)
        if len(strong_dimensions) >= 3:
            summary["review_priority"] = "high"
        elif len(strong_dimensions) >= 2 or (
            strong_dimensions and len(retained_dimensions) >= 2
        ):
            summary["review_priority"] = "medium"
        elif retained_dimensions:
            summary["review_priority"] = "normal"
        else:
            summary["review_priority"] = "none"
    analysis.get("pair_summaries", []).sort(
        key=lambda item: (-int(item.get("independent_dimension_count") or 0), item.get("bidder_a") or "", item.get("bidder_b") or ""),
    )
    analysis["ai_assessment"] = {
        "status": "partial" if failures else "success", "assessed_count": len(completed_ids), "signal_count": len(signals),
        "failure_count": len(failures), "reason": "；".join(failures), "profile": profile["display_name"],
        "prompt_version": COMPARE_AI_PROMPT_VERSION, "input_mode": "fixed_rule_evidence_packets_only",
    }


_SCORE_CLAUSE_PATTERN = re.compile(
    r"(?:得|计|为|每项|每个|每人|每处)\s*\d+(?:\.\d+)?\s*分|"
    r"最高(?:得|为)?\s*\d+(?:\.\d+)?\s*分|满分(?:为)?\s*\d+(?:\.\d+)?\s*分|"
    r"(?:分值|总计|合计)\s*[:：为]?\s*\d+(?:\.\d+)?\s*分?|扣\s*\d+(?:\.\d+)?\s*分"
    r"|[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]"
)
_SCORE_COVERAGE_IGNORED_TERMS = {"项目", "评分", "标准", "要求", "供应", "服务", "能力", "部分", "内容", "提供", "文件", "采购", "投标", "技术", "商务"}
_QUALIFICATION_SOURCE_PATTERN = re.compile(
    r"(?:投标人|供应商|申请人|响应人|竞标人)(?:的)?(?:资格(?:条件|要求)|应具备的资格条件)|"
    r"资格(?:性)?审查(?:标准|要求|办法|表)|资格评审(?:标准|要求|办法|表)"
)
_PACKAGE_MARKER_PATTERN = re.compile(r"(?:采购\s*包|标\s*包|包)\s*([0-9０-９一二三四五六七八九十]+)|第\s*([0-9０-９一二三四五六七八九十]+)\s*包")
_PACKAGE_HEADING_PATTERN = re.compile(r"^\s*(?:采购\s*包|标\s*包|第\s*)\s*([0-9０-９一二三四五六七八九十]+)\s*(?:包)?\s*[：:]")
_CHINESE_PACKAGE_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _normalise_package_number(value: object) -> int | None:
    """识别常见包号写法；未明确填写包号时保持全项目提取。"""
    text = str(value or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not text:
        return None
    match = _PACKAGE_MARKER_PATTERN.search(text)
    raw = (match.group(1) or match.group(2)) if match else ""
    if not raw:
        return None
    if raw.isdigit():
        return max(1, int(raw))
    if raw in _CHINESE_PACKAGE_NUMBERS:
        return _CHINESE_PACKAGE_NUMBERS[raw]
    if len(raw) == 2 and raw.startswith("十") and raw[1] in _CHINESE_PACKAGE_NUMBERS:
        return 10 + _CHINESE_PACKAGE_NUMBERS[raw[1]]
    if len(raw) == 2 and raw.endswith("十") and raw[0] in _CHINESE_PACKAGE_NUMBERS:
        return _CHINESE_PACKAGE_NUMBERS[raw[0]] * 10
    return None


def _package_numbers_in_text(value: object) -> set[int]:
    text = str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    values = set()
    for match in _PACKAGE_MARKER_PATTERN.finditer(text):
        number = _normalise_package_number(match.group(0))
        if number is not None:
            values.add(number)
    return values


def _score_packet_package_numbers(lines: list[str], index: int) -> set[int]:
    """以最近的评分表包标题标记计分行，避免相同公式在不同包之间误判覆盖。"""
    # 评分表标题通常在同页或前一页；向上限定窗口，不能把数十页前的包号带入。
    for position in range(index, max(-1, index - 220), -1):
        heading = _PACKAGE_HEADING_PATTERN.match(lines[position])
        if heading:
            number = _normalise_package_number(heading.group(0))
            return {number} if number is not None else set()
    return set()


def _filter_score_packets_for_package(packets: list[dict], package_number: int | None) -> list[dict]:
    """只过滤能被本地明确归属到其他包的评分条款；归属不明时宁可保留给模型判断。"""
    if package_number is None:
        return packets
    return [
        packet for packet in packets
        if not packet.get("package_numbers") or package_number in set(packet.get("package_numbers") or [])
    ]


def _filter_rules_for_package(rules: list[dict], package_number: int | None) -> list[dict]:
    """防止模型把其他包的明确专属规则写入当前项目；通用或归属不明规则不误删。"""
    if package_number is None:
        return rules
    values = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        text = "\n".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
        mentioned = _package_numbers_in_text(text)
        if mentioned and package_number not in mentioned:
            continue
        values.append(rule)
    return values


def _has_concrete_star_requirement(tender_text: str) -> bool:
    """仅“★号条款响应”交叉引用不代表第五章真的存在星号叶子条款。"""
    without_cross_reference = re.sub(r"★\s*号\s*条款\s*响应", "", tender_text or "")
    return "★" in without_cross_reference


_TECHNICAL_STAR_EVIDENCE_TERMS = (
    "证明材料", "证明文件", "技术资料", "产品资料", "技术参数", "功能截图",
    "检测报告", "检验报告", "资质证书", "认证证书", "说明书",
)
_TECHNICAL_STAR_OUTCOME_TERMS = ("投标无效", "无效投标", "投标被拒绝", "响应无效", "予以否决")
_TECHNICAL_STAR_LEAF_TERMS = ("技术参数", "产品指标", "参数表", "指标项", "技术要求", "产品规格", "硬件规格", "性能指标")


def _technical_star_evidence_items(tender_text: str) -> list[dict]:
    """从本地已定位的★叶子行保留取证清单，作为模型遗漏时的安全回退。

    它不判断是否满足、不新增规则，也不从相邻普通参数推导★项；只把原文实际带有
    星号且看起来是具体参数/产品指标的行保存为同一父规则的子项。
    """
    values: list[dict] = []
    seen: set[str] = set()
    for page in [value.strip() for value in _PARSED_PAGE_MARKER.split(tender_text or "") if value.strip()]:
        page_match = re.search(r"\[第(\d+)页\]", page)
        source_page = int(page_match.group(1)) if page_match else None
        for raw_line in page.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            compact = re.sub(r"\s+", "", line)
            if "★" not in compact or len(compact) < 10:
                continue
            # “★代表实质性指标”等总则不属于待逐项找材料的叶子项。
            if any(term in compact for term in ("代表实质性", "重要性分为", "★号条款响应")):
                continue
            if not any(term in compact for term in _TECHNICAL_STAR_LEAF_TERMS) and not re.search(r"(?:型号|规格|参数|性能|接口|功能|支持|不少于|不低于)", compact):
                continue
            key = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", compact).casefold()
            if key in seen:
                continue
            seen.add(key)
            clean = line.replace("★", "").strip(" ：:；;")
            name = clean[:100] or "★技术叶子项"
            values.append({
                "item_id": f"star_{len(values) + 1}", "name": name,
                "requirement": line[:500], "source_page": source_page,
                "evidence_requirements": ["text", "document", "field", "visual"],
            })
            if len(values) >= 12:
                return values
    return values


def _technical_star_requirement_seed(tender_text: str, package_number: int | None = None) -> dict | None:
    """为明确“技术★ + 证明材料 + 明确后果”的条款生成保守兜底候选。

    这不是按“★”一律补规则：必须同时在招标原文中找到技术叶子项、证明材料要求及
    实质性/无效后果。它只防止分段模型把这类条款误压缩进普通技术覆盖或评分规则，
    不替代模型对具体技术内容的正常提取。
    """
    if not _has_concrete_star_requirement(tender_text):
        return None
    # 多包文件的★技术表可能完全不同，而纯本地文本无法稳定判断跨页包边界。
    # 此时宁可只依赖已经带分包提示词的模型提取，不能把某一包的实质性技术要求
    # 自动写入另一包；单包或未显式标出多包的文件仍可得到该兜底保障。
    if package_number is not None and len(_package_numbers_in_text(tender_text)) > 1:
        return None
    pages = [value.strip() for value in _PARSED_PAGE_MARKER.split(tender_text or "") if value.strip()]
    if not pages:
        return None
    compact_pages = [(page, re.sub(r"\s+", "", page)) for page in pages]

    basis_page = ""
    for page, compact in compact_pages:
        has_star_basis = "★" in compact and any(term in compact for term in ("实质性", "必备要求", "必须满足"))
        has_outcome = any(term in compact for term in _TECHNICAL_STAR_OUTCOME_TERMS)
        has_evidence = any(term in compact for term in _TECHNICAL_STAR_EVIDENCE_TERMS)
        if has_star_basis and has_outcome and has_evidence:
            basis_page = page
            break
    if not basis_page:
        return None

    # 仅“资格文件带★”或格式中的★交叉引用不触发；还需在采购需求中实际定位到
    # 带★的技术/产品叶子项，避免把普通格式要求错误升级为实质性技术规则。
    if not any(
        "★" in compact and any(term in compact for term in _TECHNICAL_STAR_LEAF_TERMS)
        for _, compact in compact_pages
    ):
        return None

    page_match = re.search(r"\[第(\d+)页\]", basis_page)
    source_page = int(page_match.group(1)) if page_match else None
    source_text = re.sub(r"\s+", " ", basis_page).strip()
    # 保留原文依据的完整语义，但避免把整页表格塞进规则卡片或后续模型上下文。
    star_index = source_text.find("★")
    source_text = source_text[max(0, star_index - 90):max(0, star_index - 90) + 620]
    return {
        "category": "substantive",
        "title": "★技术参数及证明材料响应",
        "check_rule": (
            "逐项核验当前采购包技术参数/产品指标表中标注“★”的全部叶子指标是否在投标文件中"
            "作出实质响应，并针对所投型号提供相应证明材料（如产品技术资料、技术参数、功能截图、"
            "检测报告、产品资质证书或说明书等）。任一★指标未满足、未作实质响应或未按要求提供"
            "相应证明材料时，仅按招标文件已明确的无效/拒绝后果提示人工复核。"
        ),
        "source_text": source_text,
        "source_page": source_page,
        # 证明材料常同时含可检索文字与扫描件；默认允许先做文字审查，人工开启图片识别时
        # 再补视觉事实，不把整条技术参数规则强制卡在 OCR 状态。
        "ocr_required": False,
        "execution_strategy": "section",
        "evidence_requirements": ["text", "visual"],
        "evidence_items": _technical_star_evidence_items(tender_text),
    }


def _has_technical_star_requirement_rule(rules: list[dict]) -> bool:
    """判断已有规则是否已完整承接技术★及其证明材料，避免兜底规则重复。"""
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("category") not in {"compliance", "substantive", "rejection"}:
            continue
        combined = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
        if (
            "★" in combined
            and any(term in combined for term in _TECHNICAL_STAR_LEAF_TERMS)
            and any(term in combined for term in _TECHNICAL_STAR_EVIDENCE_TERMS)
        ):
            return True
    return False


def _ensure_technical_star_requirement_rule(rules: list[dict], tender_text: str,
                                            package_number: int | None = None) -> list[dict]:
    """仅在模型遗漏且原文存在完整强制链条时补入一条合并的技术★规则。"""
    if _has_technical_star_requirement_rule(rules):
        return rules
    seed = _technical_star_requirement_seed(tender_text, package_number)
    if not seed:
        return rules
    return _dedupe_rule_candidates([*rules, seed])


def _has_explicit_import_restriction(tender_text: str) -> bool:
    """进口限制必须在当前文件中被明确启用，不能由条件模板反推。"""
    compact = re.sub(r"\s+", "", tender_text or "")
    return bool(re.search(
        r"(?:■|☑|√)?(?:本项目|本包|采购包\d+).{0,18}(?:不接受|不允许|禁止).{0,12}进口产品"
        r"|(?:■|☑|√)不接受进口产品",
        compact,
    ))


def _filter_inapplicable_template_rules(rules: list[dict], tender_text: str) -> list[dict]:
    """剔除没有实际触发条件的模板规则，保守地保留其他候选。"""
    has_star = _has_concrete_star_requirement(tender_text)
    has_import_restriction = _has_explicit_import_restriction(tender_text)
    kept: list[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        combined = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
        if re.search(r"★\s*号|星号条款", combined) and not has_star:
            continue
        if "进口产品" in combined and re.search(r"如有|内容时|如.*?接受", combined) and not has_import_restriction:
            continue
        if "报价修正" in combined and re.search(r"如有|不涉及报价修正", combined):
            continue
        kept.append(rule)
    return kept


_NON_FILE_SCORING_PROCESS_PATTERN = re.compile(
    r"异常低价|澄清(?:说明)?|补正|谈判|投诉|算术(?:更正|修正)|评审现场"
)


def _is_non_file_scoring_process(rule: dict) -> bool:
    """拦截被模型误归为评分项、但只能在评审过程处理的事项。

    共同特征是没有有效分值，且触发后需要评委会、供应商解释或后续程序才能完成，
    无法只凭已上传投标文件作出评分结论。
    """
    if str(rule.get("category") or "") not in {"objective", "subjective"}:
        return False
    scoring = rule.get("scoring") if isinstance(rule.get("scoring"), dict) else {}
    if storage._valid_max_score(scoring) is not None:
        return False
    text = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    return bool(_NON_FILE_SCORING_PROCESS_PATTERN.search(text))


def _project_package_scope_instruction(app, project: dict) -> tuple[int | None, str]:
    section_name = str(project.get("section_name") or "").strip()
    package_number = _normalise_package_number(section_name)
    if package_number is None:
        scope = "当前项目未填写可识别的包号/标段号；请提取招标文件中全部适用的规则。"
    else:
        scope = f"当前项目仅对应采购包{package_number}；必须排除其他采购包的专属规则。"
    instruction = storage.render_prompt_template(
        app, "extract_rules_package_scope",
        project_name=str(project.get("name") or "未命名项目"),
        project_number=str(project.get("project_number") or "未填写"),
        section_name=section_name or "未填写",
        package_scope=scope,
    )
    return package_number, instruction

def _score_clause_packets(text: str, limit: int = 240) -> list[dict]:
    """为每个明确计分行构造独立、稳定的覆盖条款，不合并相邻评分项。"""
    lines = [line.strip() for line in text.splitlines()]
    packets: list[dict] = []
    for index, line in enumerate(lines):
        if not _SCORE_CLAUSE_PATTERN.search(re.sub(r"\s+", "", line)):
            continue
        # PDF 评分表在分页处常把同一评分项拆为“标题及前两个子项”与
        # “（1.5分）、其余子项”。后者不是新的计分事实，应续接到前一个完整
        # 评分包，防止模型生成错误标题的重复评分规则。
        if packets and _is_score_fragment_continuation(lines, index):
            continuation = str(line or "").strip()
            if continuation and continuation not in packets[-1]["text"]:
                packets[-1]["text"] = (packets[-1]["text"] + "\n" + continuation)[:1_400]
                packets[-1]["score_line"] = (packets[-1]["score_line"] + " " + continuation)[:520]
            continue
        # 评分表经 PDF 文本抽取后，项目名称、证明材料和计分行可能被分页符和大量空行
        # 分开。不能把分页符当作新评分项边界；最近一条明确计分行才是可靠边界。
        # 仍限制回看窗口，避免把整张长表误并为一个条款。
        start = max(0, index - 48)
        for previous in range(index - 1, start - 1, -1):
            compact_previous = re.sub(r"\s+", "", lines[previous])
            if _SCORE_CLAUSE_PATTERN.search(compact_previous):
                start = previous + 1
                break
        value = "\n".join(item for item in lines[start:index + 1] if item)[:900]
        if value:
            compact = re.sub(r"\s+", "", value)
            page_marker = next((
                lines[position] for position in range(index, max(-1, index - 300), -1)
                if re.fullmatch(r"\[第\d+页\]", lines[position])
            ), "")
            # 使用完整条款而非仅末两行生成 ID；否则“每提供一类得 1 分”这类通用
            # 计分行在跨页时会失去证书/业绩/人员等区分信息，进而误判为已覆盖。
            identity = f"{page_marker}\n{value}"
            identity_digest_source = re.sub(r"\s+", "", identity or compact)
            packets.append({
                "clause_id": f"SC-{hashlib.sha1(identity_digest_source.encode('utf-8')).hexdigest()[:10]}",
                "text": value,
                "score_line": line[:360],
                "package_numbers": sorted(_score_packet_package_numbers(lines, index)),
            })
        if len(packets) >= limit:
            break
    return packets


def _score_packet_text(packet: object) -> str:
    return str(packet.get("text") or "") if isinstance(packet, dict) else str(packet or "")


def _score_packet_id(packet: object) -> str:
    return str(packet.get("clause_id") or "") if isinstance(packet, dict) else ""


def _score_rule_title_terms(rule: dict) -> set[str]:
    """为本地覆盖校验提取规则名称中的低歧义词组，不参与评分或规则生成。"""
    title = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(rule.get("title", "")))
    title = title.replace("评分标准", "").replace("评分", "").replace("得分", "")
    terms: set[str] = set()
    for width in range(2, min(6, len(title)) + 1):
        for index in range(len(title) - width + 1):
            term = title[index:index + width]
            if term not in _SCORE_COVERAGE_IGNORED_TERMS:
                terms.add(term)
    return terms


def _score_packet_is_covered(packet: object, score_rules: list[dict]) -> bool:
    """按条款 ID 或原文与计分数字的双重交集核验，避免标题短词造成误覆盖。"""
    packet_text = _score_packet_text(packet)
    compact_packet = re.sub(r"\s+", "", packet_text)
    packet_id = _score_packet_id(packet)
    packet_numbers = set(re.findall(r"\d+(?:\.\d+)?", compact_packet))
    for rule in score_rules:
        clause_ids = rule.get("source_clause_ids")
        if packet_id and isinstance(clause_ids, list) and packet_id in {str(item) for item in clause_ids}:
            return True
        source = re.sub(r"\s+", "", str(rule.get("source_text") or ""))
        if len(source) < 6:
            continue
        rule_numbers = set(re.findall(r"\d+(?:\.\d+)?", source + re.sub(r"\s+", "", str(rule.get("check_rule") or ""))))
        source_overlap = source in compact_packet or compact_packet in source
        if not source_overlap:
            fragments = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{8,}", source)
            source_overlap = any(fragment in compact_packet for fragment in fragments)
        title_overlap = any(term in compact_packet for term in _score_rule_title_terms(rule))
        if source_overlap and title_overlap and (not packet_numbers or bool(packet_numbers & rule_numbers)):
            return True
    return False


def _qualification_clause_packets(text: str, limit: int = 24) -> list[dict]:
    """从正式资格章节/评审表构造小型覆盖包，补上主提取遗漏的资格口径。

    定位只依赖资格章节的通用标题，不假设条款必须使用 ``1.x`` 编号，也不使用
    业绩、社保、财务或证书等业务关键词。每个命中页最多携带后两页作为跨页上下文；
    相邻的非资格段落由补充提示词排除。
    """
    pages = [value.strip() for value in _PARSED_PAGE_MARKER.split(text) if value.strip()]
    anchor_indexes = [
        index for index, page in enumerate(pages)
        if _QUALIFICATION_SOURCE_PATTERN.search(re.sub(r"\s+", "", page))
    ]
    if not anchor_indexes:
        return []

    # 先保留所有正式锚点页，再按距离补充跨页上下文。这样即使文件多次出现资格章节，
    # limit 也不会先被第一页后面的普通内容耗尽，导致后部资格评审表永远进不了提示词。
    prioritised_indexes: list[int] = []
    seen_indexes: set[int] = set()
    for offset in range(3):
        for anchor_index in anchor_indexes:
            page_index = anchor_index + offset
            if page_index >= len(pages) or page_index in seen_indexes:
                continue
            prioritised_indexes.append(page_index)
            seen_indexes.add(page_index)
    selected_indexes = sorted(prioritised_indexes[:limit])

    packets: list[dict] = []
    for page_index in sorted(selected_indexes):
        page = pages[page_index]
        marker_match = re.match(r"\[第(\d+)页\]\s*", page)
        page_number = marker_match.group(1) if marker_match else str(page_index + 1)
        pieces = _split_rule_extraction_text(page, 2_400)
        for piece_index, value in enumerate(pieces, start=1):
            if not value:
                continue
            digest = re.sub(r"\s+", "", value)
            packets.append({
                "clause_id": f"QF-{hashlib.sha1(digest.encode('utf-8')).hexdigest()[:10]}",
                "label": f"第{page_number}页·{piece_index}/{len(pieces)}",
                "text": value,
            })
            if len(packets) >= limit:
                return packets
    return packets


def _qualification_packet_prompt_text(packets: list[object]) -> str:
    values = []
    for index, packet in enumerate(packets, start=1):
        if not isinstance(packet, dict):
            continue
        clause_id = str(packet.get("clause_id") or f"QF-{index}")
        label = str(packet.get("label") or "资格条款")
        values.append(f"【资格条款 {clause_id} / {label}】\n{packet.get('text') or ''}")
    return "\n\n".join(values)


RULE_EXTRACTION_BATCH_CHARS = 11_000
RULE_EXTRACTION_MIN_SPLIT_CHARS = 3_500


def _split_rule_extraction_text(text: str, max_chars: int) -> list[str]:
    """按页/段落切分原文，避免截断页面标记和评分表行。"""
    value = text.strip()
    if len(value) <= max_chars:
        return [value] if value else []
    parts = re.split(r"(?=\[第\d+页\])", value)
    if len(parts) <= 1:
        parts = re.split(r"(?<=\n)", value)
    chunks: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        if len(part) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            page_prefix_match = re.match(r"\[第\d+页\]\s*", part)
            page_prefix = page_prefix_match.group(0).strip() if page_prefix_match else ""
            for start in range(0, len(part), max_chars):
                piece = part[start:start + max_chars].strip()
                if piece:
                    if start and page_prefix and not piece.startswith(page_prefix):
                        piece = f"{page_prefix}\n{piece}"
                    chunks.append(piece)
            continue
        if current and len(current) + len(part) > max_chars:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    return chunks or [value[:max_chars]]


def _score_packet_prompt_text(score_packets: list[object]) -> str:
    values = []
    for index, packet in enumerate(score_packets, start=1):
        clause_id = _score_packet_id(packet) or f"SC-{index}"
        package_numbers = packet.get("package_numbers") if isinstance(packet, dict) else None
        package_label = f"；适用采购包：{','.join(str(value) for value in package_numbers)}" if package_numbers else ""
        values.append(f"【评分条款 {clause_id}{package_label}】\n{_score_packet_text(packet)}")
    return "\n".join(values)


INITIAL_REVIEW_ANCHOR_TERMS = (
    "评标办法前附表", "形式评审", "资格评审", "响应性评审", "初步评审",
    "实质性要求", "否决投标", "评审标准",
)
_PARSED_PAGE_MARKER = re.compile(r"(?=\[第\d+页\]\s*)")


def _initial_review_anchor_catalog(text: str, max_chars: int = 6_500) -> str:
    """从招标原文中提取一次性的初步评审依据目录，供所有分段规则映射共用。

    这不是基于业务词的规则过滤器，只是把通常位于第三章、而与技术需求页相隔很远的
    评审依据一并交给模型，避免模型把“应当/参数/★”本身误当成独立符合性结论。
    """
    pages = [value.strip() for value in _PARSED_PAGE_MARKER.split(text) if value.strip()]
    if not pages:
        return "未定位到初步评审目录；非评分规则必须在当前原文中自行找到明确评审或否决依据。"

    selected_indexes: set[int] = set()
    for index, page in enumerate(pages):
        if not any(term in page for term in INITIAL_REVIEW_ANCHOR_TERMS):
            continue
        selected_indexes.add(index)

    values: list[str] = []
    size = 0
    for index in sorted(selected_indexes):
        value = pages[index]
        if not value:
            continue
        # 保留整页而非按关键词截句，表格相邻行和“★条款”交叉引用才不会断裂。
        if values and size + len(value) + 2 > max_chars:
            continue
        values.append(value)
        size += len(value) + 2
    if not values:
        return "未定位到初步评审目录；非评分规则必须在当前原文中自行找到明确评审或否决依据。"
    return "\n\n".join(values)


def _rule_extraction_prompt(app, text: str, *, compact: bool, score_packets: list[object],
                            review_anchor_catalog: str, max_rules: int = 45) -> str:
    limits = (
        f"这是格式异常后的紧凑重试。最多返回 {max_rules} 条规则；title 最多 30 字，普通规则的 check_rule 尽量控制在 180 字内，source_text 最多 120 字；"
        "层级评分规则不得为缩短输出而省略叶子评分项、分值、公式或扣分条件。"
        if compact else
        f"最多返回 {max_rules} 条规则；title 最多 40 字，普通规则的 check_rule 尽量控制在 260 字内，source_text 最多 220 字；"
        "层级评分规则允许为完整表达叶子评分项、分值、公式和扣分条件而超过普通长度。"
    )
    score_audit = _score_packet_prompt_text(score_packets)
    score_requirement = (
        "本地已定位以下疑似评分条款。必须逐项核验并为每个不同的明确计分条款输出一条 objective 或 subjective 规则；"
        "不得遗漏业绩、报价、人员、资质、方案等评分项。"
        if score_audit else "未定位到明确评分条款时，不要臆造评分规则。"
    )
    return storage.render_prompt_template(
        app, "extract_rules_user", limits=limits, score_requirement=score_requirement,
        score_audit=score_audit or "无", review_anchor_catalog=review_anchor_catalog, text=text,
    )


def _score_rule_supplement_prompt(app, score_packets: list[object], existing_rules: list[dict]) -> str:
    existing = [
        {"category": item.get("category"), "title": item.get("title"), "check_rule": item.get("check_rule"), "max_score": (item.get("scoring") or {}).get("max_score")}
        for item in existing_rules if item.get("category") in {"objective", "subjective"}
    ]
    packet_text = _score_packet_prompt_text(score_packets)
    return storage.render_prompt_template(app, "extract_rules_supplement_user",
                                          existing_rules=json.dumps(existing, ensure_ascii=False, separators=(",", ":")), packet_text=packet_text)


def _qualification_rule_supplement_prompt(app, qualification_packets: list[object], existing_rules: list[dict]) -> str:
    """将正式资格表的缺漏核验交给小上下文补充调用，避免重发整份采购文件。"""
    existing = [
        {
            "category": item.get("category"), "title": item.get("title"),
            "check_rule": item.get("check_rule"), "source_text": item.get("source_text"),
        }
        for item in existing_rules if item.get("category") == "qualification"
    ]
    return storage.render_prompt_template(
        app, "extract_rules_qualification_supplement_user",
        existing_rules=json.dumps(existing, ensure_ascii=False, separators=(",", ":")),
        packet_text=_qualification_packet_prompt_text(qualification_packets),
    )


def _scoring_reconciliation_packet(score_packets: list[object], char_limit: int) -> str | None:
    """以完整条款为单位压缩评分表，不能把半条 JSON 送给结构复核。"""
    values, size = [], 2
    for index, packet in enumerate(score_packets, start=1):
        value = {
            "clause_id": _score_packet_id(packet) or f"SC-{index}",
            # 评分行及其最近上下文优先，保留父项标题、计分对象和分值。
            "text": _score_packet_text(packet)[:900],
        }
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if values and size + len(encoded) + 1 > char_limit:
            return None
        values.append(value)
        size += len(encoded) + 1
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _normalise_reconciled_scoring_rules(value: object, score_packets: list[object]) -> list[dict] | None:
    """仅接受可完整覆盖原评分条款的结构复核结果，异常时仍使用上一阶段结果。"""
    if not isinstance(value, list) or not value:
        return None
    known_clause_ids = {_score_packet_id(packet) for packet in score_packets if _score_packet_id(packet)}
    rules = _dedupe_rule_candidates([item for item in value if isinstance(item, dict)])
    if not rules:
        return None
    for rule in rules:
        if rule.get("category") not in {"objective", "subjective"}:
            return None
        if not str(rule.get("title") or "").strip() or not str(rule.get("check_rule") or "").strip():
            return None
        scoring = rule.get("scoring") if isinstance(rule.get("scoring"), dict) else {}
        if storage._valid_max_score(scoring) is None:
            return None
        scoring = dict(scoring)
        scoring["kind"] = "manual" if rule["category"] == "subjective" else (
            "boolean" if scoring.get("kind") == "boolean" and not scoring.get("items") else "manual"
        )
        rule["scoring"] = scoring
        clause_ids = rule.get("source_clause_ids")
        if not isinstance(clause_ids, list) or not clause_ids:
            return None
        if any(str(clause_id) not in known_clause_ids for clause_id in clause_ids):
            return None
    # 不以“分数相加恰好 100”代替结构完整性；各项目总分可能不是 100。
    # 每个原始评分条款均须明确映射，防止模型为删重而静默丢掉叶子项。
    if any(not _score_packet_is_covered(packet, rules) for packet in score_packets):
        return None
    return rules


def _reconcile_scoring_rules(app, task: dict, profile: dict, system_prompt: str,
                             rules: list[dict], score_packets: list[object]) -> tuple[list[dict], dict]:
    """对评分表做一次独立结构复核，修正类别/归属而不触碰非评分规则。"""
    stats = {"applied": False, "failure_count": 0}
    scoring_rules = [item for item in rules if item.get("category") in {"objective", "subjective"}]
    if not score_packets or not scoring_rules:
        return rules, stats
    input_limit = min(_prompt_char_limit(profile, 90_000, 140_000), 140_000)
    packets = _scoring_reconciliation_packet(score_packets, input_limit)
    score_rules_packet = _rule_compilation_packet(scoring_rules, input_limit)
    try:
        if packets is None:
            raise ValueError("评分条款过长，未执行结构复核")
        parsed_score_rules = json.loads(score_rules_packet)
        if not isinstance(parsed_score_rules, list) or len(parsed_score_rules) != len(scoring_rules):
            raise ValueError("当前评分规则过长，未执行结构复核")
        storage.update_task(app, task["task_id"], progress=76, message="正在复核评分表的分部、类别与重复项")
        try:
            response = _request_task_json(
                app, task, profile, "extract_rules_scoring_reconcile", system_prompt,
                storage.render_prompt_template(
                    app, "extract_rules_scoring_reconcile_user", score_packets=packets,
                    score_rules=score_rules_packet,
                ),
                context_mode="rule_scoring_structure_reconcile",
                max_tokens=_output_token_budget(profile, max(4_500, min(10_000, 1_200 + len(scoring_rules) * 460))),
                thinking_mode="disabled",
            )
        except InvalidJsonResponse as exc:
            response = _repair_invalid_json(
                app, task, profile, "extract_rules_scoring_reconcile_json_repair", exc, "rules",
            )
        reconciled = _normalise_reconciled_scoring_rules(
            response.get("rules") if isinstance(response, dict) else None, score_packets,
        )
        if reconciled is None:
            raise ValueError("评分结构复核未返回完整、可追溯的评分规则")
        non_scoring_rules = [item for item in rules if item.get("category") not in {"objective", "subjective"}]
        stats["applied"] = True
        return _dedupe_rule_candidates(non_scoring_rules + reconciled), stats
    except ValueError as exc:
        stats["failure_count"] = 1
        storage.update_task(app, task["task_id"], message=f"评分结构复核未完成，已保留原评分规则：{exc}")
        return rules, stats


def _rule_recovery_continue_prompt(app, text: str, recovered_rules: list[dict], review_anchor_catalog: str) -> str:
    """仅把已完整解析的必要字段交给续提，避免截断正文或重复输出放大上下文。"""
    recovered = [
        {
            "category": item.get("category"), "title": item.get("title"),
            "check_rule": item.get("check_rule"), "source_text": item.get("source_text"),
            "source_page": item.get("source_page"), "source_clause_ids": item.get("source_clause_ids"),
            "ocr_required": item.get("ocr_required"), "evidence_items": item.get("evidence_items"), "scoring": item.get("scoring"),
        }
        for item in recovered_rules if isinstance(item, dict)
    ]
    return storage.render_prompt_template(
        app, "extract_rules_continue_user",
        existing_rules=json.dumps(recovered, ensure_ascii=False, separators=(",", ":")),
        review_anchor_catalog=review_anchor_catalog, text=text,
    )


def _rule_batch_output_tokens(text: str, compact: bool = False) -> int:
    """小批次按内容量分配输出；紧凑重试绝不降低输出上限。"""
    target = max(2_500, min(6_000, 1_400 + len(text) // 3))
    return max(target, 3_500) if compact else target


def _extract_rule_batch(app, task: dict, profile: dict, system_prompt: str, text: str,
                        *, document_id: str, batch_label: str, review_anchor_catalog: str = "",
                        depth: int = 0) -> tuple[list[dict], int, int]:
    """提取一个小批次；截断时只二分当前批次，最小批次才紧凑重试。"""
    packets = _score_clause_packets(text, limit=24)
    # 评分表密集页在 11k 字内也可能包含大量独立计分项。与其依赖模型在固定条数
    # 上限内取舍，不如在首次调用前把这一小批次继续按页/段落二分，保证每项都能输出。
    if len(packets) > 12 and len(text) > RULE_EXTRACTION_MIN_SPLIT_CHARS and depth < 3:
        pieces = _split_rule_extraction_text(text, max(RULE_EXTRACTION_MIN_SPLIT_CHARS, (len(text) + 1) // 2))
        if len(pieces) > 1:
            rules: list[dict] = []
            compact_retries = split_retries = 0
            for index, piece in enumerate(pieces, start=1):
                value, compact_count, split_count = _extract_rule_batch(
                    app, task, profile, system_prompt, piece, document_id=document_id,
                    batch_label=f"{batch_label}/评分密集拆分{index}", review_anchor_catalog=review_anchor_catalog,
                    depth=depth + 1,
                )
                rules.extend(value)
                compact_retries += compact_count
                split_retries += split_count
            return rules, compact_retries, split_retries + 1
    max_rules = 16 if depth == 0 else 10
    user_prompt = _rule_extraction_prompt(
        app, text, compact=False, score_packets=packets, review_anchor_catalog=review_anchor_catalog, max_rules=max_rules,
    )
    try:
        parsed = _request_task_json(
            app, task, profile, "extract_rules_batch", system_prompt, user_prompt,
            document_id=document_id, context_mode=batch_label,
            max_tokens=_output_token_budget(profile, _rule_batch_output_tokens(text)), thinking_mode="disabled",
        )
        rules = parsed.get("rules") if isinstance(parsed, dict) else None
        if not isinstance(rules, list):
            raise ValueError("模型返回格式不符合规则提取要求")
        return [item for item in rules if isinstance(item, dict)], 0, 0
    except InvalidJsonResponse as exc:
        # 响应已到达但尾部截断时，先回收每个边界完整、能独立 json.loads 的规则对象。
        # 这一步绝不补全半截条款；随后仅请求模型补足遗漏项，避免把已成功的长输出整段重发。
        recovered_payload = _recover_complete_json_array(exc.raw_content, "rules")
        recovered_rules = recovered_payload.get("rules") if isinstance(recovered_payload, dict) else None
        if isinstance(recovered_rules, list) and recovered_rules:
            storage.update_task(
                app, task["task_id"],
                message=f"{batch_label} 正在规范化模型返回，已在本地回收 {len(recovered_rules)} 条完整规则，正在补充遗漏项",
            )
            try:
                continued = _request_task_json(
                    app, task, profile, "extract_rules_local_recovery_continue", system_prompt,
                    _rule_recovery_continue_prompt(app, text, recovered_rules, review_anchor_catalog), document_id=document_id,
                    context_mode=f"{batch_label}_local_json_continue",
                    max_tokens=_output_token_budget(profile, _rule_batch_output_tokens(text, compact=True)),
                    thinking_mode="disabled",
                )
                missing_rules = continued.get("rules") if isinstance(continued, dict) else None
                if not isinstance(missing_rules, list):
                    raise ValueError("模型返回格式不符合规则续提要求")
                return _dedupe_rule_candidates(
                    [item for item in recovered_rules + missing_rules if isinstance(item, dict)]
                ), 0, 0
            except ValueError:
                # 续提只是节省重发的优先路径；它异常时仍完整回到原有拆分/紧凑重试，
                # 不能以局部恢复替代全量提取而遗漏规则。
                storage.update_task(app, task["task_id"], message=f"{batch_label} 规则续提异常，正在按完整策略重试")
        if exc.finish_reason.lower() not in {"length", "max_tokens"}:
            try:
                repaired = _repair_invalid_json(
                    app, task, profile, "extract_rules_batch_json_repair", exc, "rules", document_id=document_id,
                )
                repaired_rules = repaired.get("rules") if isinstance(repaired, dict) else None
                if not isinstance(repaired_rules, list):
                    raise ValueError("模型返回格式不符合规则提取要求")
                return [item for item in repaired_rules if isinstance(item, dict)], 0, 0
            except ValueError:
                storage.update_task(app, task["task_id"], message=f"{batch_label} 本地修复未完成，正在按完整策略重试")
        if len(text) > RULE_EXTRACTION_MIN_SPLIT_CHARS and depth < 3:
            pieces = _split_rule_extraction_text(text, max(RULE_EXTRACTION_MIN_SPLIT_CHARS, (len(text) + 1) // 2))
            if len(pieces) > 1:
                storage.update_task(app, task["task_id"], message=f"{batch_label} 输出过长，正在仅拆分该批次重试")
                rules: list[dict] = []
                compact_retries = split_retries = 0
                for index, piece in enumerate(pieces, start=1):
                    value, compact_count, split_count = _extract_rule_batch(
                        app, task, profile, system_prompt, piece, document_id=document_id,
                        batch_label=f"{batch_label}/拆分{index}", review_anchor_catalog=review_anchor_catalog,
                        depth=depth + 1,
                    )
                    rules.extend(value)
                    compact_retries += compact_count
                    split_retries += split_count
                return rules, compact_retries, split_retries + 1
        storage.update_task(app, task["task_id"], message=f"{batch_label} 正在按更紧凑结构继续处理")
        retry_prompt = _rule_extraction_prompt(
            app, text, compact=True, score_packets=packets, review_anchor_catalog=review_anchor_catalog,
            max_rules=max(8, max_rules),
        )
        parsed = _request_task_json(
            app, task, profile, "extract_rules_compact_retry", system_prompt, retry_prompt,
            document_id=document_id, context_mode=f"{batch_label}_compact_retry",
            max_tokens=_output_token_budget(profile, _rule_batch_output_tokens(text, compact=True)), thinking_mode="disabled",
        )
        rules = parsed.get("rules") if isinstance(parsed, dict) else None
        if not isinstance(rules, list):
            raise ValueError("模型返回格式不符合规则提取要求")
        return [item for item in rules if isinstance(item, dict)], 1, 0
    except ValueError as exc:
        # 暂时性 HTTP 200 错误包在网关已完成局部退避后，仍可走紧凑/拆分恢复；
        # 鉴权、余额、参数等不可重试错误仍会立即清晰报出。
        if not _is_model_format_error(exc):
            raise
        if len(text) > RULE_EXTRACTION_MIN_SPLIT_CHARS and depth < 3:
            pieces = _split_rule_extraction_text(text, max(RULE_EXTRACTION_MIN_SPLIT_CHARS, (len(text) + 1) // 2))
            if len(pieces) > 1:
                storage.update_task(app, task["task_id"], message=f"{batch_label} 输出过长，正在仅拆分该批次重试")
                rules: list[dict] = []
                compact_retries = split_retries = 0
                for index, piece in enumerate(pieces, start=1):
                    value, compact_count, split_count = _extract_rule_batch(
                        app, task, profile, system_prompt, piece, document_id=document_id,
                        batch_label=f"{batch_label}/拆分{index}", review_anchor_catalog=review_anchor_catalog,
                        depth=depth + 1,
                    )
                    rules.extend(value)
                    compact_retries += compact_count
                    split_retries += split_count
                return rules, compact_retries, split_retries + 1
        storage.update_task(app, task["task_id"], message=f"{batch_label} 正在按更紧凑结构继续处理")
        retry_prompt = _rule_extraction_prompt(
            app, text, compact=True, score_packets=packets, review_anchor_catalog=review_anchor_catalog,
            max_rules=max(8, max_rules),
        )
        parsed = _request_task_json(
            app, task, profile, "extract_rules_compact_retry", system_prompt, retry_prompt,
            document_id=document_id, context_mode=f"{batch_label}_compact_retry",
            max_tokens=_output_token_budget(profile, _rule_batch_output_tokens(text, compact=True)), thinking_mode="disabled",
        )
        rules = parsed.get("rules") if isinstance(parsed, dict) else None
        if not isinstance(rules, list):
            raise ValueError("模型返回格式不符合规则提取要求")
        return [item for item in rules if isinstance(item, dict)], 1, 0


def _extract_rule_batches(app, task: dict, profile: dict, system_prompt: str, batches: list[str], *,
                          document_id: str, review_anchor_catalog: str = "") -> tuple[list[dict], int, int]:
    """在受闸门保护的至多三路工作位中映射原文，按原文顺序汇总结果。"""
    if not batches:
        return [], 0, 0
    total = len(batches)
    results: list[tuple[list[dict], int, int] | None] = [None] * total
    # 初始闸门仍是两路；工作位数量始终不超过当前模型档案允许的远端并发数。
    gate = task.get("_evaluation_request_gate")
    workers = min(gate.max_limit if gate else 2, total)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index = {
            executor.submit(
                _extract_rule_batch, app, task, profile, system_prompt, batch,
                document_id=document_id, batch_label=f"rule_batch_{index + 1}_of_{total}",
                review_anchor_catalog=review_anchor_catalog,
            ): index
            for index, batch in enumerate(batches)
        }
        completed = 0
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
            completed += 1
            progress = 15 + int(completed * 45 / total)
            storage.update_task(
                app, task["task_id"], progress=progress,
                message=f"正在分段提取评审规则（已完成 {completed}/{total} 批，按模型档案动态并发）",
            )
    raw_rules: list[dict] = []
    compact_retries = split_retries = 0
    for result in results:
        if result is None:
            continue
        extracted, compact_count, split_count = result
        raw_rules.extend(extracted)
        compact_retries += compact_count
        split_retries += split_count
    return raw_rules, compact_retries, split_retries


def _rule_signature(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("category", "")).strip(),
        re.sub(r"\s+", "", str(item.get("title", ""))).casefold(),
        re.sub(r"\s+", "", str(item.get("check_rule", "") or item.get("title", ""))).casefold(),
    )


def _normalise_rule_title(value: object) -> str:
    text = re.sub(r"[（(]\s*(?:满分|最高(?:得)?分?)\s*\d+(?:\.\d+)?\s*分?\s*[)）]", "", str(value or ""))
    text = re.sub(r"(?:[-—–:：\s]*)(?:满分|最高(?:得)?分?)\s*\d+(?:\.\d+)?\s*分?", "", text)
    # 分段提取时，模型有时会把“商务部分-”“评分-”这类章节标签写进标题，另一次
    # 又只保留实际计分对象。它们不是两个评分事实，归一化时去掉这些纯导航前缀。
    text = re.sub(r"^\s*(?:(?:商务|技术|价格|服务|资格)部分|(?:客观|主观)?评分|评分项?)\s*[-—–:：_]*\s*", "", text)
    # “（9分）”“（6分，含服务团队/响应时间）”等带分值括注也只是分值的另一种写法，
    # 去掉后同一评分对象的标题才有一致锚点；不含数字分值的括注保持原样。
    text = re.sub(r"[（(][^()（）]*\d+(?:\.\d+)?\s*分[^()（）]*[)）]\s*$", "", text)
    return re.sub(r"[\s\W_]+", "", text).casefold()


def _score_source_fingerprint(value: object) -> str:
    """评分原文的截断容忍指纹：省略号只是压缩标记，截断副本与完整原文共享前缀。"""
    text = re.sub(r"(?:…|\.\.\.|......)+", "", str(value or ""))
    text = re.sub(r"[\s\W_]+", "", text).casefold()
    return text[:60]


def _score_rule_dedupe_key(item: dict) -> tuple[object, ...] | None:
    """只合并来源、分值和对象同时一致的评分规则，避免标题相似导致漏分。"""
    category = str(item.get("category") or "")
    if category not in {"objective", "subjective"}:
        return None
    scoring = item.get("scoring") if isinstance(item.get("scoring"), dict) else {}
    max_score = storage._valid_max_score(scoring)
    title = _normalise_rule_title(item.get("title"))
    clause_ids = tuple(sorted({str(value).strip() for value in item.get("source_clause_ids") or [] if str(value).strip()}))
    source = _score_source_fingerprint(item.get("source_text"))
    if max_score is None or not title:
        return None
    # 评分条款ID是最可靠锚点；没有它时，以“同一原文前缀 + 同一对象 + 同一分值”合并，
    # 使同一条款的完整版与被截断/带括注版不会被重复计分。
    if clause_ids:
        return ("score", category, title, float(max_score), clause_ids)
    if source:
        return ("score_source", category, title, float(max_score), source)
    return None


def _rule_candidate_richness(item: dict) -> tuple[int, int, int]:
    scoring = item.get("scoring") if isinstance(item.get("scoring"), dict) else {}
    items = scoring.get("items") if isinstance(scoring.get("items"), list) else []
    return (len(items), len(str(item.get("check_rule") or "")), len(str(item.get("source_text") or "")))


def _merge_duplicate_score_rule(existing: dict, candidate: dict) -> dict:
    """合并同一评分事实的互补字段，选择信息更全者为主体。"""
    primary, secondary = (candidate, existing) if _rule_candidate_richness(candidate) > _rule_candidate_richness(existing) else (existing, candidate)
    merged = dict(primary)
    merged["source_clause_ids"] = list(dict.fromkeys([
        *[str(value) for value in primary.get("source_clause_ids") or [] if str(value).strip()],
        *[str(value) for value in secondary.get("source_clause_ids") or [] if str(value).strip()],
    ]))
    merged["ocr_required"] = bool(primary.get("ocr_required") or secondary.get("ocr_required"))
    if not merged.get("source_page"):
        merged["source_page"] = secondary.get("source_page")
    return merged


def _score_rule_soft_key(item: dict) -> tuple[object, ...] | None:
    """评分软键：只看对象与分值，用于第二趟的截断/完整原文合并。"""
    category = str(item.get("category") or "")
    if category not in {"objective", "subjective"}:
        return None
    scoring = item.get("scoring") if isinstance(item.get("scoring"), dict) else {}
    max_score = storage._valid_max_score(scoring)
    title = _normalise_rule_title(item.get("title"))
    if max_score is None or not title:
        return None
    return (category, title, float(max_score))


def _score_sources_compatible(left: object, right: object) -> bool:
    """两段评分原文是否同源：一方为另一方去省略号后的完整前缀（截断副本）。

    至少要求 20 个归一化字符的完整前缀重合，且必须由标题软键先行限定同一对象
    与同一分值；缺原文或仅有松散相似时不合并，留待确认前预检提示人工核对。
    """
    def normalised(value: object) -> str:
        text = re.sub(r"(?:…|\.\.\.|……)+", "", str(value or ""))
        return re.sub(r"[\s\W_]+", "", text).casefold()

    shorter, longer = sorted((normalised(left), normalised(right)), key=len)
    return len(shorter) >= 20 and longer.startswith(shorter)


def _dedupe_rule_candidates(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    score_indexes: dict[tuple[object, ...], int] = {}
    result: list[dict] = []
    for item in items:
        signature = _rule_signature(item)
        if not all(signature) or signature in seen:
            continue
        seen.add(signature)
        score_key = _score_rule_dedupe_key(item)
        if score_key is not None and score_key in score_indexes:
            index = score_indexes[score_key]
            result[index] = _merge_duplicate_score_rule(result[index], item)
            continue
        if score_key is not None:
            score_indexes[score_key] = len(result)
        result.append(item)
    # 第二趟：同一评分对象、同一分值且原文互为“完整版/截断版”的规则合并。
    # 分段提取常把同一条评分条款一次带全原文、一次只带截断原文或不同分值括注，
    # 精确键抓不住它们，但软键加完整前缀重合可以证明同源；售后等原文真正不同
    # 的同名规则不会合并，由确认前预检提示人工核对。
    merged: list[dict] = []
    for item in result:
        soft_key = _score_rule_soft_key(item)
        target_index = None
        if soft_key is not None:
            for index, existing in enumerate(merged):
                if _score_rule_soft_key(existing) != soft_key:
                    continue
                if _score_sources_compatible(existing.get("source_text"), item.get("source_text")):
                    target_index = index
                    break
        if target_index is not None:
            merged[target_index] = _merge_duplicate_score_rule(merged[target_index], item)
        else:
            merged.append(item)
    return merged


def _score_label_key(value: object) -> str:
    """提取评分对象的稳定名称，忽略“评分/满分/分”等导航文字。"""
    text = _normalise_rule_title(value)
    text = re.sub(r"(?:评分|得分|分值|满分|最高分|分)$", "", text)
    text = re.sub(r"\d+(?:\.\d+)?$", "", text)
    return text


def _score_item_max(item: object) -> float | None:
    if not isinstance(item, dict):
        return None
    try:
        value = float(item.get("max_score"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _score_child_matches_parent_item(child: dict, item: dict) -> bool:
    """只以明确名称与分值对应父项叶子项，避免把相近评分项误收进总分项。"""
    child_score = storage._valid_max_score(child.get("scoring"))
    item_score = _score_item_max(item)
    if child_score is None or item_score is None or abs(float(child_score) - item_score) > 0.001:
        return False
    child_label = _score_label_key(child.get("title"))
    item_label = _score_label_key(item.get("name"))
    if len(child_label) < 3 or len(item_label) < 3:
        return False
    return child_label in item_label or item_label in child_label


def _prune_overlapping_score_aggregates(rules: list[dict]) -> list[dict]:
    """移除被同一评分父项完整包含的重复子项。

    评分表常同时出现“服务部分29分”与培训、售后、实施等已计入该29分的分项。若两
    者并存，综合评审会重复计分。只有父项至少明确列出两个子项、子项名称与分值均能
    一一对应时才压缩；单一子项、相近名称或用户手工规则均不触碰。
    """
    values = [dict(item) for item in rules if isinstance(item, dict)]
    removable: set[int] = set()
    for parent_index, parent in enumerate(values):
        if parent.get("category") not in {"objective", "subjective"}:
            continue
        if str(parent.get("source_type") or "") in {"manual", "ai_edited", "global"}:
            continue
        scoring = parent.get("scoring") if isinstance(parent.get("scoring"), dict) else {}
        items = [item for item in scoring.get("items") or [] if isinstance(item, dict) and _score_item_max(item) is not None]
        parent_max = storage._valid_max_score(scoring)
        if parent_max is None or len(items) < 2:
            continue
        # 只有叶子合计确实构成父项满分时才认为它是可替代的总分父项。
        if abs(sum(float(_score_item_max(item) or 0) for item in items) - float(parent_max)) > 0.01:
            continue
        matched_children: list[int] = []
        matched_items: set[int] = set()
        for child_index, child in enumerate(values):
            if child_index == parent_index or child_index in removable:
                continue
            if child.get("category") != parent.get("category") or str(child.get("source_type") or "") in {"manual", "ai_edited", "global"}:
                continue
            for item_index, item in enumerate(items):
                if item_index not in matched_items and _score_child_matches_parent_item(child, item):
                    matched_children.append(child_index)
                    matched_items.add(item_index)
                    break
        # 至少两个叶子项同时重合才删除子项，单条偶然相似的评分规则绝不自动移除。
        if len(matched_children) >= 2:
            removable.update(matched_children)
    return [item for index, item in enumerate(values) if index not in removable]


# 这些模式描述的是“必须看图像外观才能核验”的证据形态，而不是某个项目的业务词。
# AI 仍负责理解规则；这里仅作为保守兜底，避免把未执行 OCR 的证照、签章或凭证
# 因文本未命中直接判成高风险不满足。
VISUAL_EVIDENCE_PATTERNS = (
    r"签字|签章|盖章|公章|印章|骑缝章|手写|指印",
    r"截图|复印件|扫描件|影印件",
    r"营业执照|许可证|合格证|资质证书|资格证书|执业证书|操控员执照|身份证",
    r"转账凭证|缴款凭证|支付凭证|银行回单|支票|汇票|保函",
)
DECISIVE_VISUAL_EVIDENCE_PATTERN = re.compile(
    r"(?:核验|审查|检查|确认|辨认|比对|提供|附(?:有|具)?|提交|包含|齐备).{0,45}"
    r"(?:签字|签章|盖章|公章|印章|骑缝章|手写|指印|截图|复印件|扫描件|影印件|照片|保函|票据|银行回单)"
    r"|(?:签字|签章|盖章|公章|印章|骑缝章|手写|指印|截图|复印件|扫描件|影印件|照片|保函|票据|银行回单).{0,45}"
    r"(?:核验|审查|检查|确认|辨认|比对|提供|附(?:有|具)?|提交|包含|齐备)",
    flags=re.IGNORECASE,
)


def _rule_requires_visual_verification(item: dict) -> bool:
    # 提取模型已经明确给出布尔判断时，不能再因规则文字中提及“证照”“签章”等
    # 触发词把整条规则强行升级为 OCR。混合型规则可能以文字为决定性证据，视觉
    # 兜底只服务于旧规则或没有给出明确分类的输入。
    try:
        execution_meta = storage.rule_execution_meta(item)
    except (TypeError, ValueError):
        execution_meta = {}
    baseline_ocr_mode = str(item.get("baseline_ocr_mode") or execution_meta.get("baseline_ocr_mode") or "auto")
    # 人工明确选择纯文字/本地 OCR 时，优先级高于历史 check_mode 与关键词兜底。
    # 这样旧规则无需迁移数据库，也能按新选择稳定执行。
    if baseline_ocr_mode == "text_only":
        return False
    if baseline_ocr_mode == "local_ocr":
        return True
    configured_trigger = str(item.get("vision_trigger") or "")
    if not configured_trigger:
        configured_trigger = str(execution_meta.get("vision_trigger") or "")
    if configured_trigger == "text_fallback":
        return False
    if configured_trigger == "required":
        return True
    requirements = item.get("evidence_requirements")
    if not isinstance(requirements, list):
        try:
            requirements = json.loads(item.get("execution_meta_json") or "{}").get("evidence_requirements", [])
        except (TypeError, json.JSONDecodeError):
            requirements = []
    requirements = {str(value) for value in requirements if value}
    # 新规则可以同时声明文本和图片证据。此时文本层仍应正常给出建议，图片仅作为
    # 单独待核验点；只有视觉是唯一决定性来源时才把整条回落为“需 OCR”。
    if "visual" in requirements and "text" not in requirements:
        return True
    if "text" in requirements:
        return False
    explicit = item.get("ocr_required")
    if explicit is True:
        return True
    if item.get("check_mode") == "ocr":
        return True
    if explicit is False:
        # 模型给出 false 时仍保留一个窄而通用的兜底：只有检查指令明确要求核验
        # 签章、截图、复印件等视觉形态才升级 OCR；仅提到证书名称或承诺内容不升级。
        return bool(DECISIVE_VISUAL_EVIDENCE_PATTERN.search(str(item.get("check_rule") or "")))
    text = " ".join(str(item.get(key) or "") for key in ("title", "check_rule", "source_text"))
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in VISUAL_EVIDENCE_PATTERNS)


def _normalise_visual_rule_policies(rules: list[dict]) -> list[dict]:
    """为任意规则补齐通用的图片识别策略，不按材料名称打补丁。

    规则语义仍由提取模型和人工维护；这里仅把已有的 text/visual 证据类型转换为
    默认关闭的执行策略。用户随后可在规则集里选择是否启用图片识别及其强度。
    """
    result: list[dict] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        rule = dict(item)
        requirements = rule.get("evidence_requirements")
        if not isinstance(requirements, list):
            requirements = []
        requirements = {str(value) for value in requirements if str(value) in {"text", "document", "field", "visual", "cross_bid", "external"}}
        instruction = str(rule.get("check_rule") or "")
        # 仅要求提供证书、报告、复印件或扫描件时，已解析文字可能已经足够；
        # 只有签章、勾选、手写及版式外观才默认视为必须看图。
        decisive_visual = bool(DECISIVE_VISUAL_FACT_PATTERN.search(instruction))
        # 编译/补充模型若漏回证据维度，以最保守的通用语义补齐：默认可先走文字；
        # 只有检查指令明确要求辨认签章、扫描件等外观时，才把 visual 作为决定性证据。
        if not requirements:
            requirements.add("visual" if decisive_visual else "text")
        if requirements & {"document", "field"}:
            requirements.add("text")
        visual = "visual" in requirements
        text = "text" in requirements
        # “提到证书/复印件”不等于每次必须 OCR。只在 visual 是唯一证据，或检查指令
        # 明确要求核验图片外观时标为强制；混合材料先全文，证据不足再按需 OCR。
        force_ocr = bool((visual and not text) or decisive_visual)
        rule["ocr_required"] = force_ocr
        rule["check_mode"] = "ocr" if force_ocr else "auto"
        rule["evidence_requirements"] = [
            value for value in ("text", "document", "field", "visual", "cross_bid", "external")
            if value in requirements
        ]
        # 提取出的规则默认均为“仅基础识别”：全文文字审查优先，材料/字段或整页扫描
        # 导致文字不足时最多启动有限本地 OCR（baseline_ocr_mode=auto），不默认消耗
        # 腾讯云额度或多模态 token。AI 对增强通道的建议仍由 acquisition_recommendation
        # 按 evidence_requirements 动态给出并展示为“系统建议”，是否升级由人工在
        # 规则确认时逐条选择。只有人工明确选择 text_only 才完全禁止 OCR。
        rule.update({
            "baseline_ocr_mode": "auto",
            "acquisition_preset": "off", "image_mode": "off", "vision_trigger": "off", "vision_level": "off",
        })
        result.append(rule)
    return result


def _rule_compilation_packet(items: list[dict], char_limit: int) -> str:
    """为规则编译阶段准备紧凑且可追溯的原始条款包。"""
    values = []
    for item in items:
        if not isinstance(item, dict):
            continue
        values.append({
            "category": item.get("category"), "title": item.get("title"),
            "check_rule": item.get("check_rule") or item.get("title"),
            "source_text": item.get("source_text"), "source_page": item.get("source_page"),
            "ocr_required": bool(item.get("ocr_required") or item.get("check_mode") == "ocr"),
            "execution_strategy": item.get("execution_strategy"),
            "evidence_requirements": item.get("evidence_requirements"),
            "evidence_items": item.get("evidence_items"),
            "applicability": item.get("applicability"),
            "source_clause_ids": item.get("source_clause_ids") if isinstance(item.get("source_clause_ids"), list) else [],
            "scoring": item.get("scoring"),
        })
    packet = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    # 不截断 JSON 数组，防止模型误把残缺条款当成完整依据；超量时以完整条款为单位收缩。
    if len(packet) <= char_limit:
        return packet
    compact = []
    size = 2
    for item in values:
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if compact and size + len(encoded) + 1 > char_limit:
            break
        compact.append(item)
        size += len(encoded) + 1
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


RULE_COMPILATION_INPUT_CHARS = 48_000


def _split_rule_compilation_groups(items: list[dict], max_chars: int) -> list[list[dict]]:
    """按完整规则切分编译输入，绝不截断 JSON 或静默丢弃尾部规则。"""
    groups: list[list[dict]] = []
    current: list[dict] = []
    size = 2
    for item in items:
        encoded = _rule_compilation_packet([item], max_chars)
        item_size = len(encoded) - 2
        if current and size + item_size + 1 > max_chars:
            groups.append(current)
            current, size = [], 2
        current.append(item)
        size += item_size + 1
    if current:
        groups.append(current)
    return groups


def _rule_compilation_output_tokens(item_count: int) -> int:
    """规则编译必须容纳完整规范规则；不足时由递归分组处理，而不是回退原始条款。"""
    return max(4_500, min(10_000, 1_200 + max(1, item_count) * 420))


def _merge_compiled_rule_groups(app, task: dict, profile: dict, system_prompt: str,
                                rules: list[dict], char_limit: int) -> list[dict]:
    """对拆分后的规范规则做一次全局轻量语义合并；失败时完整保留子组结果。"""
    values = _dedupe_rule_candidates(rules)
    if len(values) <= 1:
        return values
    input_limit = min(char_limit, 140_000)
    packet = _rule_compilation_packet(values, input_limit)
    try:
        packet_values = json.loads(packet)
    except json.JSONDecodeError:
        return values
    # 不能把不完整数组送去做“全局”合并，否则尾部规则会被无提示遗漏。
    if not isinstance(packet_values, list) or len(packet_values) != len(values):
        return values
    storage.update_task(app, task["task_id"], message="正在全局合并拆分后的评审规则")
    try:
        response = _request_task_json(
            app, task, profile, "extract_rules_global_compile", system_prompt,
            storage.render_prompt_template(app, "extract_rules_compile_user", candidates=packet),
            context_mode="rule_global_semantic_compile",
            max_tokens=_output_token_budget(profile, _rule_compilation_output_tokens(len(values))),
            thinking_mode="disabled",
        )
        merged = response.get("rules") if isinstance(response, dict) else None
        if not isinstance(merged, list):
            raise ValueError("全局规则编译未返回有效规则")
        merged = _dedupe_rule_candidates([item for item in merged if isinstance(item, dict)])
        if not merged:
            raise ValueError("全局规则编译返回空规则集")
        # 全局合并允许减少重复规则，但明确计分条款不能因此失去覆盖。
        for original in values:
            if original.get("category") not in {"objective", "subjective"}:
                continue
            packet_like = {
                "clause_id": next(iter(original.get("source_clause_ids") or []), ""),
                "text": str(original.get("source_text") or original.get("check_rule") or ""),
            }
            if not _score_packet_is_covered(packet_like, merged):
                merged.append(original)
        return _dedupe_rule_candidates(merged)
    except ValueError as exc:
        storage.update_task(app, task["task_id"], message=f"全局规则合并未完成，已完整保留分组结果：{exc}")
        return values


def _compile_rule_group(app, task: dict, profile: dict, system_prompt: str,
                        candidates: list[dict], char_limit: int, *, depth: int = 0) -> tuple[list[dict], list[dict], bool]:
    """编译一个完整规则组；长度或格式异常时仅二分该组，保留每个子组的覆盖审计。"""
    if len(candidates) <= 1:
        return candidates, [], False
    input_limit = min(char_limit, RULE_COMPILATION_INPUT_CHARS)
    groups = _split_rule_compilation_groups(candidates, input_limit)
    if len(groups) > 1:
        compiled, missing, used = [], [], False
        # 原始映射完成后，多个大规则组之间互不依赖。只在顶层动态至多三路，子组内
        # 保持串行，最终仍由全局合并统一消重和保留评分覆盖，避免嵌套并发打满接口。
        parallel_groups = depth == 0 and task.get("_evaluation_request_gate") is not None
        if parallel_groups:
            storage.update_task(app, task["task_id"], message=f"正在分组编译评审规则（{len(groups)} 组，按模型档案动态并发）")
            group_results: list[tuple[list[dict], list[dict], bool] | None] = [None] * len(groups)
            gate = task.get("_evaluation_request_gate")
            with ThreadPoolExecutor(max_workers=min(gate.max_limit if gate else 2, len(groups))) as executor:
                future_to_index = {
                    executor.submit(
                        _compile_rule_group, app, task, profile, system_prompt, group, char_limit, depth=depth + 1,
                    ): index
                    for index, group in enumerate(groups)
                }
                for future in as_completed(future_to_index):
                    group_results[future_to_index[future]] = future.result()
            for result in group_results:
                if result is None:
                    continue
                values, uncovered, group_used = result
                compiled.extend(values)
                missing.extend(uncovered)
                used = used or group_used
        else:
            for index, group in enumerate(groups, start=1):
                storage.update_task(app, task["task_id"], message=f"正在分组编译评审规则（{index}/{len(groups)}）")
                values, uncovered, group_used = _compile_rule_group(
                    app, task, profile, system_prompt, group, char_limit, depth=depth + 1,
                )
                compiled.extend(values)
                missing.extend(uncovered)
                used = used or group_used
        return _merge_compiled_rule_groups(
            app, task, profile, system_prompt, compiled, char_limit,
        ), _dedupe_rule_candidates(missing), used

    packet = _rule_compilation_packet(candidates, input_limit)
    try:
        compiled_response = _request_task_json(
            app, task, profile, "extract_rules_compile", system_prompt,
            storage.render_prompt_template(app, "extract_rules_compile_user", candidates=packet),
            context_mode=f"rule_semantic_compile_d{depth}",
            max_tokens=_output_token_budget(profile, _rule_compilation_output_tokens(len(candidates))), thinking_mode="disabled",
        )
        compiled = compiled_response.get("rules") if isinstance(compiled_response, dict) else None
        if not isinstance(compiled, list):
            raise ValueError("模型返回格式不符合规则编译要求")
        compiled = _dedupe_rule_candidates([item for item in compiled if isinstance(item, dict)])
        if not compiled:
            raise ValueError("规则编译未返回有效规则")
    except ValueError as exc:
        # 输出截断或 JSON 偶发异常只影响当前规则组。二分后每一半仍会走语义合并和覆盖审计，
        # 不能再把整套规则退回未经编译的原始候选。
        if _is_invalid_json_model_response(exc) or str(exc).startswith("模型返回格式不符合规则编译要求") or str(exc).startswith("规则编译未返回有效规则"):
            if len(candidates) > 1 and depth < 6:
                midpoint = len(candidates) // 2
                storage.update_task(app, task["task_id"], message="规则编译输出异常，正在仅拆分该规则组重试")
                left = _compile_rule_group(app, task, profile, system_prompt, candidates[:midpoint], char_limit, depth=depth + 1)
                right = _compile_rule_group(app, task, profile, system_prompt, candidates[midpoint:], char_limit, depth=depth + 1)
                return (
                    _merge_compiled_rule_groups(app, task, profile, system_prompt, left[0] + right[0], char_limit),
                    _dedupe_rule_candidates(left[1] + right[1]),
                    left[2] or right[2],
                )
        raise

    try:
        storage.update_task(app, task["task_id"], message="正在审计规则覆盖范围")
        coverage_response = _request_task_json(
            app, task, profile, "extract_rules_coverage_audit", system_prompt,
            storage.render_prompt_template(
                app, "extract_rules_coverage_user", candidates=packet,
                compiled_rules=_rule_compilation_packet(compiled, input_limit),
            ),
            context_mode=f"rule_coverage_audit_d{depth}",
            max_tokens=_output_token_budget(profile, 4_500), thinking_mode="disabled",
        )
        missing = coverage_response.get("missing_rules") if isinstance(coverage_response, dict) else None
        if missing is None and isinstance(coverage_response, dict):
            missing = coverage_response.get("rules")  # 兼容少数模型的确定字段偏差。
        if not isinstance(missing, list):
            raise ValueError("模型返回格式不符合规则覆盖审计要求")
        missing = [item for item in missing if isinstance(item, dict)]
    except ValueError as exc:
        # 编译结果本身已经是规范规则集；覆盖审计单独失败时保留它，并将失败上抛给调用方的
        # 非格式降级路径处理。格式问题则把当前组拆小，争取完成审计而非静默遗漏。
        if (_is_invalid_json_model_response(exc) or str(exc).startswith("模型返回格式不符合规则覆盖审计要求")) and len(candidates) > 1 and depth < 6:
            midpoint = len(candidates) // 2
            storage.update_task(app, task["task_id"], message="规则覆盖审计输出异常，正在仅拆分该规则组重试")
            left = _compile_rule_group(app, task, profile, system_prompt, candidates[:midpoint], char_limit, depth=depth + 1)
            right = _compile_rule_group(app, task, profile, system_prompt, candidates[midpoint:], char_limit, depth=depth + 1)
            return (
                _merge_compiled_rule_groups(app, task, profile, system_prompt, left[0] + right[0], char_limit),
                _dedupe_rule_candidates(left[1] + right[1]),
                left[2] or right[2],
            )
        raise
    return _dedupe_rule_candidates(compiled + missing), _dedupe_rule_candidates(missing), True


def _compile_rule_candidates(app, task: dict, profile: dict, system_prompt: str,
                             raw_rules: list[dict], char_limit: int) -> tuple[list[dict], list[dict], bool]:
    """用 AI 做语义归并和覆盖审计，取代跨批次的字符串去重。

    小文件只有极少条规则时，直接保留映射结果，避免为了无收益的归并增加一次模型调用。
    """
    candidates = _dedupe_rule_candidates(raw_rules)
    if len(candidates) < 12:
        return candidates, [], False
    storage.update_task(app, task["task_id"], progress=68, message="正在统一编译并合并评审规则")
    return _compile_rule_group(app, task, profile, system_prompt, candidates, char_limit)


RULE_QUALITY_GATE_MIN_RULES = 2
RULE_FINALISATION_MIN_RULES = 12
RULE_QUALITY_GATE_REASONS = {
    "duplicate", "not_file_verifiable", "procedural", "umbrella",
    "not_scoring_rule", "unsupported_cross_reference",
}
_EXPLICIT_SCORE_TEXT_PATTERN = re.compile(r"(?:满分|最高(?:得)?分|得\s*\d+(?:\.\d+)?\s*分|每.{0,20}\d+(?:\.\d+)?\s*分|扣\s*\d+(?:\.\d+)?\s*分|分值)")


def _quality_gate_rule_packet(items: list[dict], *, include_ids: bool) -> str:
    """构造小而完整的最终审计输入；限制单字段长度，但绝不截断规则数组。"""
    values = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        value = {
            "category": item.get("category"),
            "title": str(item.get("title") or "")[:120],
            "check_rule": str(item.get("check_rule") or item.get("title") or "")[:700],
            "source_text": str(item.get("source_text") or "")[:260],
            "source_page": item.get("source_page"),
            "ocr_required": bool(item.get("ocr_required") or item.get("check_mode") == "ocr"),
            "source_clause_ids": item.get("source_clause_ids") if isinstance(item.get("source_clause_ids"), list) else [],
            "scoring": item.get("scoring"),
        }
        if include_ids:
            value = {"rule_id": f"R{index}", **value}
        else:
            value.update({
                "source_type": item.get("source_type"),
                "enabled": bool(item.get("enabled", True)),
            })
        values.append(value)
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _protected_rules_for_quality_gate(app, project_id: str) -> list[dict]:
    """返回不得由重新提取覆盖的用户规则，以及本轮会自动导入的通用规则。"""
    _, current_rules = storage.list_rules(app, project_id)
    protected = []
    for item in current_rules:
        if item.get("source_type") not in {"manual", "ai_edited"}:
            continue
        value = dict(item)
        if item.get("scoring_json"):
            try:
                value["scoring"] = json.loads(item["scoring_json"])
            except (TypeError, json.JSONDecodeError):
                value["scoring"] = None
        protected.append(value)
    protected.extend(
        {**item, "source_type": "global"}
        for item in storage.list_global_rules(app)
    )
    return protected


def _recover_dropped_scoring_rules(rules: list[dict], kept_indexes: set[int],
                                   score_packets: list[str]) -> int:
    """质量门控只能做减法；若减法造成明确评分条款失去覆盖，恢复最匹配的原规则。"""
    recovered = 0
    score_categories = {"objective", "subjective"}
    for packet in score_packets:
        kept_scores = [
            item for index, item in enumerate(rules)
            if index in kept_indexes and item.get("category") in score_categories
        ]
        if _score_packet_is_covered(packet, kept_scores):
            continue
        recover_index = next((
            index for index, item in enumerate(rules)
            if index not in kept_indexes and item.get("category") in score_categories
            and _score_packet_is_covered(packet, [item])
        ), None)
        if recover_index is not None:
            kept_indexes.add(recover_index)
            recovered += 1
    return recovered


def _has_explicit_scoring_basis(item: dict) -> bool:
    """识别原文明确计分的规则；只用于防止最终减法误删，不据此创建或分类规则。"""
    if item.get("category") not in {"objective", "subjective"}:
        return False
    if storage._valid_max_score(item.get("scoring")) is None:
        return False
    return bool(_EXPLICIT_SCORE_TEXT_PATTERN.search(str(item.get("source_text") or "")))


def _final_rule_quality_gate(app, task: dict, profile: dict, system_prompt: str,
                             rules: list[dict], score_packets: list[object]) -> tuple[list[dict], dict]:
    """在保存前做一次全局只减不改审计；任何模型或格式异常均安全降级为保留原规则。"""
    stats = {"applied": False, "dropped_count": 0, "failure_count": 0, "recovered_score_count": 0}
    if len(rules) < RULE_QUALITY_GATE_MIN_RULES:
        return rules, stats
    protected = _protected_rules_for_quality_gate(app, task["project_id"])
    prompt = storage.render_prompt_template(
        app, "extract_rules_quality_gate_user",
        candidates=_quality_gate_rule_packet(rules, include_ids=True),
        protected_rules=_quality_gate_rule_packet(protected, include_ids=False),
    )
    storage.update_task(app, task["task_id"], progress=78, message="正在对完整规则集做最终质量审计")
    try:
        try:
            response = _request_task_json(
                app, task, profile, "extract_rules_quality_gate", system_prompt, prompt,
                context_mode="rule_quality_gate", max_tokens=_output_token_budget(
                    profile, max(1_800, min(5_000, 900 + len(rules) * 70)),
                ), thinking_mode="disabled",
            )
        except InvalidJsonResponse as exc:
            response = _repair_invalid_json(
                app, task, profile, "extract_rules_quality_gate_json_repair", exc, "drops",
            )
        drops = response.get("drops") if isinstance(response, dict) else None
        if not isinstance(drops, list):
            raise ValueError("模型返回格式不符合规则质量审计要求")
        drop_indexes: set[int] = set()
        for drop in drops:
            if not isinstance(drop, dict) or drop.get("reason") not in RULE_QUALITY_GATE_REASONS:
                continue
            match = re.fullmatch(r"R([1-9]\d*)", str(drop.get("rule_id") or ""))
            if not match:
                continue
            index = int(match.group(1)) - 1
            if 0 <= index < len(rules):
                # 原文有明确分值的评分规则，只有模型同时指出具体重复对象时才允许进入
                # 待剔除集；其余误判继续交给评分覆盖兜底，避免清理噪声时丢分。
                if _has_explicit_scoring_basis(rules[index]) and (
                    drop.get("reason") != "duplicate" or not str(drop.get("duplicate_of") or "").strip()
                ):
                    continue
                drop_indexes.add(index)
        if len(drop_indexes) >= len(rules):
            raise ValueError("规则质量审计试图剔除全部候选，已安全保留原结果")
        kept_indexes = set(range(len(rules))) - drop_indexes
        recovered = _recover_dropped_scoring_rules(rules, kept_indexes, score_packets)
        result = [item for index, item in enumerate(rules) if index in kept_indexes]
        stats.update({
            "applied": True,
            "dropped_count": len(rules) - len(result),
            "recovered_score_count": recovered,
        })
        return result, stats
    except ValueError as exc:
        stats["failure_count"] = 1
        storage.update_task(app, task["task_id"], message=f"规则最终质量审计未完成，已完整保留编译结果：{exc}")
        return rules, stats


def _finalise_rule_operations_pass(app, task: dict, profile: dict, system_prompt: str,
                                   rules: list[dict], *, focus_key: str, focus: str) -> tuple[list[dict], dict]:
    """以可追溯操作规范化完整规则集；任何越界操作或格式异常都保留原规则。"""
    stats = {
        "applied": False, "dropped_count": 0, "rewritten_count": 0,
        "merged_count": 0, "failure_count": 0,
    }
    protected = _protected_rules_for_quality_gate(app, task["project_id"])
    prompt = storage.render_prompt_template(
        app, "extract_rules_finalise_user",
        focus=focus,
        candidates=_quality_gate_rule_packet(rules, include_ids=True),
        protected_rules=_quality_gate_rule_packet(protected, include_ids=False),
    )
    storage.update_task(app, task["task_id"], progress=79, message=f"正在执行规则最终规范化：{focus_key}")
    try:
        try:
            response = _request_task_json(
                app, task, profile, f"extract_rules_finalise_{focus_key}", system_prompt, prompt,
                context_mode=f"rule_finalise_{focus_key}",
                max_tokens=_output_token_budget(profile, max(2_500, min(6_000, 1_200 + len(rules) * 85))),
                thinking_mode="disabled",
            )
        except InvalidJsonResponse as exc:
            response = _repair_invalid_json(
                app, task, profile, f"extract_rules_finalise_{focus_key}_json_repair", exc, "drops",
            )
        if not isinstance(response, dict):
            raise ValueError("模型返回格式不符合规则最终规范化要求")
        operations = {key: response.get(key, []) for key in ("drops", "rewrites", "merges")}
        if any(not isinstance(value, list) for value in operations.values()):
            raise ValueError("模型返回格式不符合规则最终规范化要求")

        id_to_index = {f"R{index + 1}": index for index in range(len(rules))}
        working = [dict(item) for item in rules]
        removed: set[int] = set()
        merged_removed: set[int] = set()
        rewritten: set[int] = set()
        merged_groups = 0
        allowed_drop_reasons = {"duplicate", "not_file_verifiable", "procedural", "umbrella"}

        for operation in operations["rewrites"]:
            if not isinstance(operation, dict) or not isinstance(operation.get("reason"), str) or operation.get("reason") not in {"partial_boundary", "umbrella"}:
                continue
            index = id_to_index.get(str(operation.get("rule_id") or ""))
            if index is None or rules[index].get("category") in {"objective", "subjective"}:
                continue
            title = str(operation.get("title") or "").strip()
            check_rule = str(operation.get("check_rule") or "").strip()
            if not title or not check_rule or len(title) > 120 or len(check_rule) > 1_200:
                continue
            working[index]["title"] = title
            working[index]["check_rule"] = check_rule
            if operation.get("ocr_required") is True:
                working[index]["ocr_required"] = True
            rewritten.add(index)

        used_merge_indexes: set[int] = set()
        for operation in operations["merges"]:
            if not isinstance(operation, dict) or operation.get("reason") != "duplicate":
                continue
            raw_ids = operation.get("rule_ids")
            if not isinstance(raw_ids, list):
                continue
            indexes = []
            for rule_id in raw_ids:
                index = id_to_index.get(str(rule_id or ""))
                if index is not None and index not in indexes:
                    indexes.append(index)
            keep_index = id_to_index.get(str(operation.get("keep_rule_id") or ""))
            if len(indexes) < 2 or keep_index not in indexes or used_merge_indexes.intersection(indexes):
                continue
            score_merge = any(rules[index].get("category") in {"objective", "subjective"} for index in indexes)
            # 评分规则只允许合并可由本地结构证明的重复项：同一类别、同一归一化
            # 评分锚点（同一条款ID/来源、对象与满分）。不同子项绝不因模型建议合并。
            if score_merge and (
                len({rules[index].get("category") for index in indexes}) != 1
                or any(_score_rule_dedupe_key(rules[index]) is None for index in indexes)
                or len({_score_rule_dedupe_key(rules[index]) for index in indexes}) != 1
            ):
                continue
            title = str(operation.get("title") or "").strip()
            check_rule = str(operation.get("check_rule") or "").strip()
            if not title or not check_rule or len(title) > 120 or len(check_rule) > 1_500:
                continue
            source_texts = []
            clause_ids = []
            for index in indexes:
                source_text = str(rules[index].get("source_text") or "").strip()
                if source_text and source_text not in source_texts:
                    source_texts.append(source_text)
                for clause_id in rules[index].get("source_clause_ids") or []:
                    if clause_id not in clause_ids:
                        clause_ids.append(clause_id)
            if score_merge:
                merged_score = dict(working[keep_index])
                for index in indexes:
                    if index != keep_index:
                        merged_score = _merge_duplicate_score_rule(merged_score, working[index])
                working[keep_index] = merged_score
            working[keep_index]["title"] = title
            working[keep_index]["check_rule"] = check_rule
            working[keep_index]["source_text"] = " / ".join(source_texts)[:1_500]
            working[keep_index]["source_clause_ids"] = clause_ids
            if operation.get("ocr_required") is True or any(_rule_requires_visual_verification(rules[index]) for index in indexes):
                working[keep_index]["ocr_required"] = True
            group_removed = {index for index in indexes if index != keep_index}
            removed.update(group_removed)
            merged_removed.update(group_removed)
            used_merge_indexes.update(indexes)
            rewritten.difference_update(indexes)
            merged_groups += 1

        for operation in operations["drops"]:
            if not isinstance(operation, dict) or not isinstance(operation.get("reason"), str) or operation.get("reason") not in allowed_drop_reasons:
                continue
            index = id_to_index.get(str(operation.get("rule_id") or ""))
            if index is None or index in used_merge_indexes or rules[index].get("category") in {"objective", "subjective"}:
                continue
            removed.add(index)
            rewritten.discard(index)

        if len(removed) >= len(rules) or len(rules) - len(removed) < max(1, len(rules) // 2):
            raise ValueError("规则最终规范化删减比例异常，已安全保留原结果")
        result = [item for index, item in enumerate(working) if index not in removed]
        stats.update({
            "applied": bool(removed or rewritten or merged_groups),
            "dropped_count": len(removed - merged_removed),
            "rewritten_count": len(rewritten),
            "merged_count": merged_groups,
        })
        return result, stats
    except ValueError as exc:
        stats["failure_count"] = 1
        storage.update_task(app, task["task_id"], message=f"规则最终规范化未完成，已完整保留质量审计结果：{exc}")
        return rules, stats


def _finalise_rule_operations(app, task: dict, profile: dict, system_prompt: str,
                              rules: list[dict]) -> tuple[list[dict], dict]:
    """分两轮完成边界清理与语义归并，降低长列表多目标审计的漏判率。"""
    stats = {
        "applied": False, "dropped_count": 0, "rewritten_count": 0,
        "merged_count": 0, "failure_count": 0,
    }
    if len(rules) < RULE_FINALISATION_MIN_RULES:
        return rules, stats
    passes = (
        ("文件边界", "extract_rules_finalise_boundary_focus"),
        ("重复归并", "extract_rules_finalise_merge_focus"),
    )
    result = rules
    for focus_key, focus_template_id in passes:
        result, pass_stats = _finalise_rule_operations_pass(
            app, task, profile, system_prompt, result, focus_key=focus_key,
            focus=storage.render_prompt_template(app, focus_template_id),
        )
        stats["applied"] = stats["applied"] or pass_stats["applied"]
        for key in ("dropped_count", "rewritten_count", "merged_count", "failure_count"):
            stats[key] += pass_stats[key]
    return result, stats


def _extract_rules(app, task: dict) -> dict:
    project = storage.get_project(app, task["project_id"])
    if not project:
        raise ValueError("评标项目不存在")
    package_number, package_scope_instruction = _project_package_scope_instruction(app, project)
    documents = storage.list_documents(app, task["project_id"])
    tender = next((item for item in documents if item["role"] == "tender"), None)
    if not tender or tender.get("parse_status") != "success" or not tender.get("parsed_path"):
        raise ValueError("请先上传并成功解析主招标文件")
    main_text = Path(tender["parsed_path"]).read_text(encoding="utf-8", errors="ignore").strip()
    if not main_text:
        raise ValueError("主招标文件未提取到可用文本，扫描件需要先提供可检索版本")
    profile = storage.get_model_profile(app, task.get("payload", {}).get("profile_id"), "deepseek-v4-flash")
    source_documents = [(f"主招标文件：{tender['original_name']}", main_text)]
    attachments = [item for item in documents if item["role"] == "tender_attachment" and item.get("parse_status") == "success" and item.get("parsed_path")]
    for attachment in attachments:
        attachment_text = Path(attachment["parsed_path"]).read_text(encoding="utf-8", errors="ignore").strip()
        if attachment_text:
            source_documents.append((f"招标附件：{attachment['original_name']}", attachment_text))
    # 规则映射按 11k 字小批次执行，无需先把全部招标文件塞进单次上下文。这里保留
    # 主文件和全部附件的完整可检索文本，避免固定关键词窗口在 AI 调用前丢掉后部评分表。
    source_parts = [f"【{label}】\n{value}" for label, value in source_documents]
    text = "\n\n".join(source_parts)
    all_score_packets = _score_clause_packets(text, limit=400)
    # 多包文件的评分公式往往相同；只把当前包或本地无法安全归属的条款送入覆盖审计，
    # 不能让包1公式“覆盖”包3评分项。未填写包号时保持原有全文件行为。
    score_packets = _filter_score_packets_for_package(all_score_packets, package_number)
    qualification_packets = _qualification_clause_packets(text)
    review_anchor_catalog = _initial_review_anchor_catalog(main_text)
    batches = []
    for label, value in source_documents:
        batches.extend(
            f"【{label}】\n{piece}"
            for piece in _split_rule_extraction_text(value, RULE_EXTRACTION_BATCH_CHARS)
        )
    if not batches:
        raise ValueError("招标文件未提取到可供规则识别的正文")
    storage.update_task(app, task["task_id"], progress=15, message=f"正在分段提取评审规则（共 {len(batches)} 批）")
    system_prompt = f"{_system_prompt(app, 'extract_rules')}\n\n【当前项目分包范围】\n{package_scope_instruction}"
    # 规则映射和顶层规则组编译共用同一限流闸门：默认两路，连续成功后按模型档案升档，
    # 接口繁忙时自动回落。只限制远端请求，不额外增加本地解析并行度。
    # 这只限制远端请求，并不创建常驻线程或后台进程。
    task["_evaluation_request_gate"] = _EvaluationRequestGate(
        limit=min(2, _profile_parallel_limit(profile, len(batches))),
        max_limit=_profile_parallel_limit(profile, len(batches)),
    )
    raw_rules, compact_retry_count, split_retry_count = _extract_rule_batches(
        app, task, profile, system_prompt, batches, document_id=tender["document_id"],
        review_anchor_catalog=review_anchor_catalog,
    )
    raw_rules = _filter_inapplicable_template_rules(
        _filter_rules_for_package(_dedupe_rule_candidates(raw_rules), package_number), text,
    )
    primary_score_rules = [item for item in raw_rules if isinstance(item, dict) and item.get("category") in {"objective", "subjective"}]
    uncovered_score_packets = [
        packet for packet in score_packets
        if not _score_packet_is_covered(packet, primary_score_rules)
    ]
    scoring_supplement_count = 0
    scoring_supplement_failures = 0
    if uncovered_score_packets:
        storage.update_task(app, task["task_id"], progress=60, message="正在核验评分条款覆盖并补充遗漏项")
        for index in range(0, len(uncovered_score_packets), 6):
            try:
                current_score_rules = [
                    item for item in raw_rules
                    if isinstance(item, dict) and item.get("category") in {"objective", "subjective"}
                ]
                packet_batch = [
                    packet for packet in uncovered_score_packets[index:index + 6]
                    if not _score_packet_is_covered(packet, current_score_rules)
                ]
                if not packet_batch:
                    continue
                supplement = _request_task_json(
                    app, task, profile, "extract_rules_scoring_supplement", system_prompt,
                    _score_rule_supplement_prompt(app, packet_batch, current_score_rules), document_id=tender["document_id"],
                    context_mode=f"score_clause_batch_{index // 6 + 1}",
                    max_tokens=_output_token_budget(profile, 3_500), thinking_mode="disabled",
                )
                supplement_rules = supplement.get("rules") if isinstance(supplement, dict) else None
                if isinstance(supplement_rules, list):
                    kept_supplement_rules = _filter_rules_for_package(
                        [item for item in supplement_rules if isinstance(item, dict)], package_number,
                    )
                    raw_rules.extend(kept_supplement_rules)
                    scoring_supplement_count += len(kept_supplement_rules)
            except ValueError as exc:
                # 主规则已提取成功时，单个评分补充批次异常不应丢弃已得到的规则集。
                scoring_supplement_failures += 1
                storage.update_task(app, task["task_id"], message=f"部分评分条款补充未完成：{exc}")
    qualification_supplement_count = 0
    qualification_supplement_failures = 0
    if qualification_packets:
        storage.update_task(app, task["task_id"], progress=63, message="正在核验正式资格条款覆盖并补充遗漏项")
        try:
            qualification_rules = [
                item for item in raw_rules if isinstance(item, dict) and item.get("category") == "qualification"
            ]
            try:
                supplement = _request_task_json(
                    app, task, profile, "extract_rules_qualification_supplement", system_prompt,
                    _qualification_rule_supplement_prompt(app, qualification_packets, qualification_rules),
                    document_id=tender["document_id"], context_mode="qualification_clause_coverage",
                    max_tokens=_output_token_budget(profile, max(3_500, min(6_000, 900 + len(qualification_packets) * 700))),
                    thinking_mode="disabled",
                )
            except InvalidJsonResponse as exc:
                supplement = _repair_invalid_json(
                    app, task, profile, "extract_rules_qualification_supplement_json_repair", exc, "rules",
                    document_id=tender["document_id"],
                )
            supplement_rules = supplement.get("rules") if isinstance(supplement, dict) else None
            if isinstance(supplement_rules, list):
                kept_supplement_rules = _filter_rules_for_package(
                    [item for item in supplement_rules if isinstance(item, dict)], package_number,
                )
                raw_rules.extend(kept_supplement_rules)
                qualification_supplement_count = len(kept_supplement_rules)
        except ValueError as exc:
            # 正式资格覆盖是增强路径；异常时保留已映射规则，并在任务结果中透明记录。
            qualification_supplement_failures = 1
            storage.update_task(app, task["task_id"], message=f"部分资格条款补充未完成：{exc}")
    mapped_candidates = [
        item for item in raw_rules if isinstance(item, dict) and str(item.get("title", "")).strip()
        and isinstance(item.get("category"), str)
        and item.get("category") in {"qualification", "compliance", "substantive", "rejection", "objective", "subjective"}
    ]
    mapped_candidates = _filter_rules_for_package(mapped_candidates, package_number)
    compilation_failure_count = 0
    try:
        candidates, coverage_missing_rules, compilation_used = _compile_rule_candidates(
            app, task, profile, system_prompt, mapped_candidates,
            _prompt_char_limit(profile, 100_000, 180_000),
        )
    except ValueError as exc:
        # 映射阶段已有可用规则时，语义编译/审计不应成为单点失败而丢掉整份规则集。
        # 保留原始候选供人工确认，并在任务结果中明确记录本次未完成编译。
        candidates, coverage_missing_rules, compilation_used = _dedupe_rule_candidates(mapped_candidates), [], False
        compilation_failure_count = 1
        storage.update_task(app, task["task_id"], progress=76, message=f"规则编译未完成，已保留原始提取结果：{exc}")
    # 是否可由投标文件核验交给完整提示词与人工确认判断；不以词表硬过滤，避免误删业绩有效期等规则。
    rules = _filter_inapplicable_template_rules(_filter_rules_for_package(candidates, package_number), text)
    excluded_rule_count = 0
    for item in rules:
        if item.get("category") not in {"objective", "subjective"}:
            continue
        scoring = item.get("scoring") if isinstance(item.get("scoring"), dict) else {}
        if storage._valid_max_score(scoring) is None:
            inferred = storage.infer_max_score(item.get("source_text", ""))
            if inferred is not None:
                scoring = {"max_score": inferred, "source": "source_text_inferred"}
        if storage._valid_max_score(scoring) is not None:
            if item["category"] == "objective":
                # 带有叶子评分项的客观分必然需要逐项汇总；即使模型错误标为 boolean，
                # 也不能在综合评审中把“每类/每项计分”误按满足即满分处理。
                score_items = scoring.get("items")
                has_score_items = isinstance(score_items, list) and any(isinstance(value, dict) for value in score_items)
                scoring["kind"] = "boolean" if scoring.get("kind") == "boolean" and not has_score_items else "manual"
            else:
                scoring["kind"] = "manual"
            item["scoring"] = scoring
    rules, scoring_reconciliation = _reconcile_scoring_rules(
        app, task, profile, system_prompt, rules, score_packets,
    )
    # 结构复核可能同时返回总分父项和其已包含的分项；在进入后续门控前先做可证明
    # 的父子重叠消除，避免同一评分事实被综合评审重复执行和重复计分。
    rules = _prune_overlapping_score_aggregates(rules)
    rules = _filter_inapplicable_template_rules(_filter_rules_for_package(rules, package_number), text)
    rules, quality_gate = _final_rule_quality_gate(
        app, task, profile, system_prompt, rules, score_packets,
    )
    rules, finalisation = _finalise_rule_operations(
        app, task, profile, system_prompt, rules,
    )
    rules = _prune_overlapping_score_aggregates(rules)
    rules = _filter_inapplicable_template_rules(_filter_rules_for_package(rules, package_number), text)
    # 模型分段提取和后续去重均可能把“技术★实质性指标 + 证明材料 + 明确无效后果”
    # 错压缩进普通技术覆盖项。仅在三项原文条件同时存在、且规则集尚未承接时补一条；
    # 不对一般技术参数、格式★或只有交叉引用的条款做任何推断。
    rules = _ensure_technical_star_requirement_rule(rules, text, package_number)
    # 质量门控偶尔会遗漏“异常低价解释”等评审过程事项。它们既没有可执行分值，
    # 也不能由投标文件单独完成，不能以零分评分规则的形式进入待确认规则集。
    before_procedural_filter = len(rules)
    rules = [item for item in rules if not _is_non_file_scoring_process(item)]
    excluded_rule_count += before_procedural_filter - len(rules)
    rules = _normalise_visual_rule_policies(rules)
    if not rules:
        raise ValueError("模型未提取到可确认的有效规则，请检查招标文件文本或更换模型")
    storage.update_task(app, task["task_id"], progress=80, message="正在保存待确认规则")
    rule_set = storage.replace_rules_from_extraction(app, task["project_id"], task["task_id"], rules)
    global_rule_count = rule_set.get("global_rule_count", 0)
    return {"rule_set_id": rule_set["rule_set_id"], "version": rule_set["version"], "rule_count": len(rules) + global_rule_count,
            "ai_rule_count": len(rules), "global_rule_count": global_rule_count,
            "excluded_rule_count": excluded_rule_count, "profile": profile["display_name"],
            "compact_retry_count": compact_retry_count, "score_clause_count": len(score_packets),
            "all_score_clause_count": len(all_score_packets), "package_scope": package_number,
            "uncovered_score_clause_count": len(uncovered_score_packets), "scoring_supplement_count": scoring_supplement_count,
            "scoring_supplement_failure_count": scoring_supplement_failures, "batch_count": len(batches),
            "qualification_clause_count": len(qualification_packets), "qualification_supplement_count": qualification_supplement_count,
            "qualification_supplement_failure_count": qualification_supplement_failures,
            "semantic_compilation_used": compilation_used, "coverage_missing_rule_count": len(coverage_missing_rules),
            "semantic_compilation_failure_count": compilation_failure_count,
            "scoring_reconciliation_applied": scoring_reconciliation["applied"],
            "scoring_reconciliation_failure_count": scoring_reconciliation["failure_count"],
            "quality_gate_applied": quality_gate["applied"],
            "quality_gate_dropped_count": quality_gate["dropped_count"],
            "quality_gate_failure_count": quality_gate["failure_count"],
            "quality_gate_recovered_score_count": quality_gate["recovered_score_count"],
            "finalisation_applied": finalisation["applied"],
            "finalisation_dropped_count": finalisation["dropped_count"],
            "finalisation_rewritten_count": finalisation["rewritten_count"],
            "finalisation_merged_count": finalisation["merged_count"],
            "finalisation_failure_count": finalisation["failure_count"],
            "preserved_rule_count": rule_set.get("preserved_rule_count", 0), "split_retry_count": split_retry_count}


def _review_documents(app, task: dict) -> dict:
    rule_set, rules = storage.list_rules(app, task["project_id"])
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始实质性审查")
    rules = [item for item in rules if item["enabled"] and item["category"] in {"qualification", "compliance", "substantive", "rejection", "other"}]
    if not rules:
        raise ValueError("当前已确认规则集内没有可执行的资格、符合、实质性或废标规则")
    documents = [item for item in storage.list_documents(app, task["project_id"]) if item["role"] == "bid"]
    if not documents or any(item["parse_status"] != "success" or not item["parsed_path"] for item in documents):
        raise ValueError("请先成功解析全部投标文件")
    profile = storage.get_model_profile(app, task.get("payload", {}).get("profile_id"), "deepseek-v4-flash")
    char_limit = _prompt_char_limit(profile, 260_000, 600_000)
    review_run = storage.create_review_run(app, task["project_id"], task["task_id"], profile["profile_id"])
    rule_prompt = [{"rule_id": item["rule_id"], "category": item["category"], "title": item["title"],
                    "check_rule": item.get("check_rule") or item["title"], "source_text": item["source_text"],
                    "ocr_required": _rule_requires_visual_verification(item)} for item in rules]
    for index, document in enumerate(documents, start=1):
        storage.update_task(app, task["task_id"], progress=int((index - 1) * 100 / len(documents)), message=f"正在审查 {index}/{len(documents)}：{document['bidder_name'] or document['original_name']}")
        text = Path(document["parsed_path"]).read_text(encoding="utf-8", errors="ignore")
        system_prompt = _system_prompt(app, "review_documents")
        user_prompt = storage.render_prompt_template(app, "review_documents_user", rules=json.dumps(rule_prompt, ensure_ascii=False, separators=(",", ":")),
                                                     document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写", text=text[:char_limit])
        parsed = _request_task_json(app, task, profile, "review_documents", system_prompt, user_prompt,
                                    document_id=document["document_id"], context_mode="full_prefix",
                                    max_tokens=_output_token_budget(profile, 700 + len(rules) * 220))
        output = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(output, list):
            raise ValueError("模型返回格式不符合审查要求")
        normalized = _normalise_review_results(output, rules)
        storage.save_review_results(app, review_run["review_run_id"], document["document_id"], normalized)
    return {"review_run_id": review_run["review_run_id"], "document_count": len(documents), "rule_count": len(rules), "profile": profile["display_name"]}


def _score_documents(app, task: dict, score_type: str) -> dict:
    rule_set, all_rules = storage.list_rules(app, task["project_id"])
    rules = [item for item in all_rules if item["enabled"] and item["category"] == score_type]
    if not rules:
        raise ValueError(f"当前规则集内没有可执行的{'客观' if score_type == 'objective' else '主观'}评分项")
    documents = [item for item in storage.list_documents(app, task["project_id"]) if item["role"] == "bid"]
    if not documents or any(item["parse_status"] != "success" or not item["parsed_path"] for item in documents):
        raise ValueError("请先成功解析全部投标文件")
    profile = storage.get_model_profile(app, task.get("payload", {}).get("profile_id"), "deepseek-v4-flash")
    char_limit = _prompt_char_limit(profile, 260_000, 600_000)
    score_run = storage.create_score_run(app, task["project_id"], task["task_id"], score_type, profile["profile_id"])
    rule_payload = []
    for rule in rules:
        try:
            scoring = json.loads(rule["scoring_json"]) if rule.get("scoring_json") else {}
        except json.JSONDecodeError:
            scoring = {}
        rule_payload.append({"rule_id": rule["rule_id"], "title": rule["title"], "source_text": rule["source_text"],
                             "ocr_required": _rule_requires_visual_verification(rule), "scoring": scoring})
    for index, document in enumerate(documents, start=1):
        storage.update_task(app, task["task_id"], progress=int((index - 1) * 100 / len(documents)), message=f"正在{'客观' if score_type == 'objective' else '主观'}评分 {index}/{len(documents)}：{document['bidder_name'] or document['original_name']}")
        context = build_rule_context(document["parsed_path"], rules, char_limit)
        text = context["text"]
        system_prompt = _system_prompt(app, f"score_{score_type}")
        user_prompt = storage.render_prompt_template(app, f"score_{score_type}_user", rules=json.dumps(rule_payload, ensure_ascii=False, separators=(",", ":")),
                                                     document_name=document["original_name"], text=text)
        parsed = _request_task_json(app, task, profile, f"score_{score_type}", system_prompt, user_prompt,
                                    document_id=document["document_id"], context_mode=context["mode"],
                                    max_tokens=_output_token_budget(profile, 600 + len(rules) * 180))
        output = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(output, list):
            raise ValueError("模型返回格式不符合评分要求")
        output_map = {item.get("rule_id"): item for item in output if isinstance(item, dict)}
        results = []
        for item in rule_payload:
            raw = output_map.get(item["rule_id"], {})
            try:
                max_score = float(item["scoring"].get("max_score") or 0)
                if not (0 < max_score < float("inf")):
                    max_score = 0.0
            except (TypeError, ValueError):
                max_score = 0.0
            suggested = _suggested_score(item, raw, score_type, max_score)
            results.append(_score_result_from_model(item["rule_id"], suggested, max_score, raw))
        storage.save_score_results(app, score_run["score_run_id"], document["document_id"], results)
    return {"score_run_id": score_run["score_run_id"], "score_type": score_type, "document_count": len(documents), "rule_count": len(rules), "profile": profile["display_name"]}


def _is_explicit_ocr_gap(item: dict, rule: dict) -> bool:
    """只将明确的图像识别缺口标为 OCR，避免把一般人工复核误分类。"""
    if _rule_requires_visual_verification(rule):
        return True
    text = f"{item.get('reason') or ''} {item.get('evidence') or ''}".lower()
    return any(term in text for term in ("ocr", "扫描件", "扫描图片", "图像识别", "图片识别"))


def _implicit_ocr_is_only_pending_fact(item: dict) -> bool:
    """非视觉规则提到 OCR 时，判断它是否只是其中一个待核验子事实。

    大型逐项覆盖规则可同时包含文字可判的缺项和少量证照图片待核验；不能让后者
    覆盖前者并把整条结论降为低风险。只有模型本身没有给出独立文字结论时，才把
    非视觉规则隐式回落为 ocr_required。
    """
    status = str(item.get("status") or "").strip().lower()
    if status in {"partial", "not_satisfied"}:
        return False
    if status == "not_found" and _clean_model_text(item.get("evidence")):
        return False
    return status in {"manual", "ocr_required", "not_found"}


def _status_conflicts_with_positive_reason(status: str, reason: object) -> bool:
    """阻止模型把“符合/满足”的理由与“不满足”状态同时保存。"""
    if status != "not_satisfied":
        return False
    text = _clean_model_text(reason)
    if not text:
        return False
    negative_markers = ("不符合", "不满足", "未满足", "未提供", "未提交", "缺失", "无效", "不一致", "矛盾", "负偏离", "不响应")
    if any(marker in text for marker in negative_markers):
        return False
    return any(marker in text for marker in ("符合", "满足要求", "满足", "未发现"))


_BOUNDARY_COMPARISON_PATTERN = re.compile(
    r"(?:招标(?:文件)?(?:要求)?|采购(?:文件)?(?:要求)?|要求|规定)[^。；;\n]{0,70}?"
    r"([≥≤])\s*(\d+(?:\.\d+)?)\s*(mm|cm|m|kg|g|w|kw|v|a|hz|%|年|月|日|天|小时|分钟|次|个|项|台|套|件|页)"
    r"[^。；;\n]{0,90}?(?:投标(?:文件|响应)?|响应(?:值|内容)?|填写|提供)[^。；;\n]{0,36}?"
    r"(?:Φ|φ)?\s*(\d+(?:\.\d+)?)\s*\3",
    flags=re.IGNORECASE,
)
# 执行级决定性外观判定：刻意保持比 storage.RULE_VISUAL_SUGGESTION_TERMS 更窄的集合，
# 只有确凿的外观事实才把整条规则标为强制 OCR；建议层词汇是该集合的超集，保证
# 凡被此正则命中的规则，rule_acquisition_recommendation 不会落回“仅基础识别”。
DECISIVE_VISUAL_FACT_PATTERN = re.compile(
    r"签字|签章|盖章|公章|印章|骑缝章|手写|指印|勾选|涂改|图片外观|照片外观|版式外观",
    flags=re.IGNORECASE,
)


_TENDER_TECHNICAL_BASELINE_CACHE: dict[tuple[str, str], str] = {}


def _project_tender_technical_baseline(app, project_id: str) -> str:
    """按项目缓存招标原文，供技术矛盾结论做来源归因，不增加模型调用。"""
    try:
        tenders = [item for item in storage.list_documents(app, project_id) if item.get("role") == "tender"]
    except Exception:
        return ""
    for tender in tenders:
        path = str(tender.get("parsed_path") or "")
        key = (project_id, path)
        if key in _TENDER_TECHNICAL_BASELINE_CACHE:
            return _TENDER_TECHNICAL_BASELINE_CACHE[key]
        if not path or not Path(path).is_file():
            continue
        value = Path(path).read_text(encoding="utf-8", errors="ignore")
        # 技术参数、采购需求和评分附件都可能出现在较后位置；保留完整解析文本只作
        # 本地字符串校验，不会发送给模型。缓存数量受限，避免小规格服务器长期占用内存。
        if len(_TENDER_TECHNICAL_BASELINE_CACHE) >= 6:
            _TENDER_TECHNICAL_BASELINE_CACHE.clear()
        _TENDER_TECHNICAL_BASELINE_CACHE[key] = value
        return value
    return ""


def _technical_source_provenance(evidence: object, tender_baseline: object) -> str:
    """识别技术异常是否来自招标原文或仅变更比较符号。

    只在参数矛盾类结论中使用。完全相同的数值/单位表达属于招标原文继承；仅
    省略“≤/≥”等比较符号的表达保留为低级表述疑点，不再误称投标人自相矛盾。
    """
    source = _clean_model_text(tender_baseline)
    value = _clean_model_text(evidence)
    if not source or not value:
        return ""
    source_compact = re.sub(r"\s+", "", source).lower()
    value_compact = re.sub(r"\s+", "", value).lower()
    # 对参数异常最具判别力的是“数值＋单位/型号”的短串；同一串已出现在招标原文
    # 即不能仅凭常识把投标人沿用的文字判成技术缺陷。
    signatures = re.findall(
        r"(?:[φΦ]?[0-9]+(?:\.\d+)?(?:mm|cm|m|hz|khz|℃c|℃|%|w|kw|v|a)|"
        r"-?[0-9]+(?:\.\d+)?(?:℃c|℃|hz|khz)(?:[-~～至][0-9a-zφΦ℃.]+)+)",
        value_compact,
    )
    if any(len(signature) >= 4 and signature in source_compact for signature in signatures):
        return "inherited"
    # 比较符号是否存在会改变技术口径，因此只降级为表述待核而不直接宣称完全一致。
    source_without_ops = re.sub(r"[≤≥<>＝=]", "", source_compact)
    value_without_ops = re.sub(r"[≤≥<>＝=]", "", value_compact)
    for width in (48, 36, 28, 20):
        for start in range(0, max(0, len(value_without_ops) - width + 1)):
            fragment = value_without_ops[start:start + width]
            if any(char.isdigit() for char in fragment) and fragment in source_without_ops:
                if fragment not in source_compact:
                    return "boundary_variant"
    return ""


def _satisfied_boundary_comparison(item: dict) -> str:
    """识别“响应值恰等于 ≥/≤ 边界”被模型误报为偏离的通用情形。"""
    text = "\n".join(str(item.get(key) or "") for key in ("evidence", "reason"))
    for match in _BOUNDARY_COMPARISON_PATTERN.finditer(text):
        operator, required, unit, actual = match.groups()
        try:
            required_value, actual_value = Decimal(required), Decimal(actual)
        except InvalidOperation:
            continue
        if (operator == "≥" and actual_value >= required_value) or (operator == "≤" and actual_value <= required_value):
            return f"数值边界复核：响应值{actual}{unit}满足招标{operator}{required}{unit}，该比较本身不构成偏离。"
    return ""


def _is_technical_consistency_rule(rule: dict) -> bool:
    """仅将招标原文基线用于技术参数矛盾类规则，避免干扰一般符合性结论。"""
    text = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    has_technical_subject = any(term in text for term in ("技术参数", "技术方案", "技术响应", "技术要求"))
    has_consistency_check = any(term in text for term in ("矛盾", "不一致", "不合理", "相互冲突", "前后不一"))
    return has_technical_subject and has_consistency_check


def _is_copying_only_rule(rule: dict) -> bool:
    """逐项响应/偏离表的复述本身不是风险，避免独立“照抄”规则制造噪声。"""
    text = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule"))
    return any(term in text for term in ("照抄", "照搬", "机械复制"))


def _normalise_review_results(output: object, rules: list[dict], tender_baseline: object = "") -> list[dict]:
    by_id = {item["rule_id"]: item for item in rules}
    normalized = []
    for item in output if isinstance(output, list) else []:
        rule_id = item.get("rule_id") if isinstance(item, dict) else None
        if not isinstance(rule_id, str) or rule_id not in by_id:
            continue
        status = item.get("status")
        if not isinstance(status, str) or status not in {"satisfied", "not_satisfied", "partial", "not_found", "manual", "ocr_required"}:
            status = "manual"
        original_status = status
        if _status_conflicts_with_positive_reason(status, item.get("reason")):
            status = "satisfied" if any(marker in _clean_model_text(item.get("reason")) for marker in ("符合", "满足")) else "manual"
            item = {**item, "risk_level": "low"}
        # 边界相等符合“≥/≤”要求。此时不能把这一个比较写成高风险偏离；若该
        # 规则还包含其他参数，保守降为 partial，交由后续证据继续判断。
        boundary_note = _satisfied_boundary_comparison(item)
        if boundary_note:
            original_reason = _clean_model_text(item.get("reason"))
            original_evidence = _clean_model_text(item.get("evidence"))
            item = {
                **item,
                "risk_level": "low",
                "confidence": "medium" if item.get("confidence") == "high" else item.get("confidence"),
                "reason": "；".join(value for value in (boundary_note, original_reason) if value)[:2000],
                "evidence": re.sub(r"(?:实质)?差异未(?:披露|说明)|未披露(?:偏离)?|负偏离", "", original_evidence),
            }
            if status == "not_satisfied":
                status = "partial"
        rule = by_id[rule_id]
        # 投标文件沿用招标参数原文（或仅在比较符号上存在版式差异）不能被当作
        # 投标人技术自相矛盾。这里不把它改为“满足”，仅将无直接偏离证据的噪声
        # 回落为低风险提示，保留人工复核与其他真实异常的空间。
        if _is_technical_consistency_rule(rule):
            provenance = _technical_source_provenance(item.get("evidence"), tender_baseline)
            if provenance:
                note = (
                    "关键参数表述可在招标原文中定位，不能仅据此认定投标文件存在技术矛盾。"
                    if provenance == "inherited"
                    else "该参数与招标原文仅见比较符号/版式差异，作为表述核验提示，不单独认定技术偏离。"
                )
                item = {
                    **item,
                    "risk_level": "low",
                    "confidence": "medium" if item.get("confidence") == "high" else item.get("confidence"),
                    "reason": "；".join(value for value in (note, _clean_model_text(item.get("reason"))) if value)[:2000],
                }
                if status == "not_satisfied":
                    status = "partial"
        # 对照表逐项复述招标参数是正常编制方式。真正的边界删改、无关内容或方案
        # 缺失仍会由对应的参数一致性/方案完整性规则检出，不在此重复放大。固定说明
        # 作为前置提示保留，但不再覆盖模型针对该投标人的具体分析，避免不同投标人
        # 的结论一字不差、看不出各自依据。
        if _is_copying_only_rule(rule):
            note = "技术响应或偏离表对招标参数的逐项复述属于正常对照，不单独构成照抄风险；如有实质删改或无关内容，应由对应技术规则单独核验。"
            original_reason = _clean_model_text(item.get("reason"))
            item = {
                **item,
                "risk_level": "low",
                "confidence": "medium" if item.get("confidence") == "high" else item.get("confidence"),
                "reason": "；".join([note] + ([original_reason] if original_reason and original_reason != note else []))[:2000],
            }
            if status == "not_satisfied":
                status = "partial"
        visual_rule = _rule_requires_visual_verification(rule)
        # OCR 规则在当前流程尚未真正识别图像时，不能因文本层未命中就输出高风险
        # 不满足；模型若在理由中明确提出 OCR 缺口，也统一回落到待 OCR。
        if visual_rule or (
            status != "satisfied" and _is_explicit_ocr_gap(item, rule)
            and _implicit_ocr_is_only_pending_fact(item)
        ):
            status = "ocr_required"
            if original_status != "ocr_required" and visual_rule:
                prior_reason = _clean_model_text(item.get("reason"))[:240]
                item = {
                    **item,
                    "reason": "关键证据必须查看证照、签章、凭证或其他图像外观；当前未执行 OCR，需识别后再判定。"
                    + (f" 文本层模型线索：{prior_reason}" if prior_reason else ""),
                }
        normalized.append(_review_result_from_model(item, rule_id, status))
    returned_ids = {item["rule_id"] for item in normalized}
    normalized.extend(
        _review_result_from_model(
            {"reason": "模型未返回该规则的可验证结论，请人工复核。"}, rule["rule_id"],
            "ocr_required" if _rule_requires_visual_verification(rule) else "manual",
        )
        for rule in rules if rule["rule_id"] not in returned_ids
    )
    return normalized


def _review_result_from_model(item: dict, rule_id: str, status: str) -> dict:
    confidence = _enum_text(item.get("confidence"), {"high", "medium", "low"}, "medium")
    evidence_quality = _enum_text(
        item.get("evidence_quality"), {"sufficient", "limited", "missing"},
        "sufficient" if str(item.get("evidence", "")).strip() else "missing",
    )
    risk = _enum_text(item.get("risk_level"), {"low", "medium", "high"}, "medium")
    # 仅对正向、低风险、证据充分的结论自动进入批量确认；否定/废标类风险不自动放行。
    if status == "ocr_required":
        # OCR 缺失仅说明当前无法读取图像证据，并非投标文件本身存在风险。
        confidence, evidence_quality, risk = "low", "missing", "low"
        if not str(item.get("reason", "")).strip():
            item = {**item, "reason": "该规则的关键证据需要 OCR 识别后才能判定。"}
    auto_ready = status == "satisfied" and risk == "low" and confidence == "high" and evidence_quality == "sufficient"
    return {"rule_id": rule_id, "status": status,
            "evidence": _truncate_field(_clean_model_text(item.get("evidence")), 2000),
            "page_hint": _clean_model_text(item.get("page_hint"))[:80] or None,
            "reason": _truncate_field(_clean_model_text(item.get("reason")), 2000),
            "risk_level": risk, "confidence": confidence, "evidence_quality": evidence_quality,
            "automation_status": "ready_for_batch_confirmation" if auto_ready else "needs_review",
            "requires_review": not auto_ready,
            "review_reason": "" if auto_ready else "非正向结论、证据不足、置信度不足或存在风险，需人工复核。"}


def _score_payload(rules: list[dict]) -> list[dict]:
    payload = []
    for rule in rules:
        try:
            scoring = json.loads(rule["scoring_json"]) if rule.get("scoring_json") else {}
        except json.JSONDecodeError:
            scoring = {}
        if isinstance(scoring.get("items"), list):
            scoring = {**scoring, "items": [
                {**item, "item_id": str(item.get("item_id") or f"SI-{index}")}
                for index, item in enumerate(scoring["items"], start=1) if isinstance(item, dict)
            ]}
        payload.append({"rule_id": rule["rule_id"], "title": rule["title"], "check_rule": rule.get("check_rule") or rule["title"], "source_text": rule["source_text"],
                        "ocr_required": _rule_requires_visual_verification(rule), "execution_strategy": _rule_execution_strategy(rule),
                        "evidence_requirements": rule.get("evidence_requirements") or [], "scoring": scoring})
    return payload


_INTERNAL_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])SI-\d+(?![0-9A-Za-z])")
_FIELD_NOTATION_PATTERN = re.compile(
    r"\b(?:status|risk_level|evidence_quality|confidence|suggested_score|max_score|"
    r"matched_count|needs_ocr|coverage_status|final_score|effective_score)\s*=\s*[^，。；;：:\s]+"
)
_TRUNCATE_MARKER = "…（内容过长已省略）"


def _clean_model_text(value: object) -> str:
    """移除只供内部编排使用的标记，保留模型的业务判断。"""
    text = str(value or "")
    text = re.sub(r"\bcontext_unmatched\s*=\s*true\b[，,。；;：:\s]*", "", text, flags=re.IGNORECASE)
    # 评分项兜底编号（SI-1/SI-2）与 JSON 字段名记法（status=、suggested_score= 等）
    # 只用于内部编排，用户界面无法理解；一律从结论文本中移除，结构化字段仍保留原始值。
    text = _INTERNAL_ID_PATTERN.sub("", text)
    text = _FIELD_NOTATION_PATTERN.sub("", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    # OCR 对不可辨认字符会输出替换符（U+FFFD）。连续乱码没有阅读价值，折叠为省略号；
    # 单个替换符直接移除，避免证据里出现无法理解的碎片。
    text = re.sub(r"\ufffd{2,}", "…", text)
    text = text.replace("\ufffd", "")
    return text.strip()


def _truncate_field(text: object, limit: int) -> str:
    """长字段截断时保留明确的省略标记，避免用户误以为展示的就是完整结论。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    keep = max(0, limit - len(_TRUNCATE_MARKER))
    return f"{value[:keep].rstrip()}{_TRUNCATE_MARKER}"


def _enum_text(value: object, allowed: set[str], default: str) -> str:
    """模型偶尔会把枚举字段返回成数组或对象；非字符串一律回落默认值，
    避免 ``value in {...}`` 成员判断对 list/dict 抛出 ``unhashable type`` 而中断整份评审。"""
    return value if isinstance(value, str) and value in allowed else default


# 页码只能从明确的页码表达取得，绝不能把“2 项”“3 分”“2029 年”等普通数字当成页码。
# 同时兼容 PDF 文本、模型输出和人工习惯中常见的 P224、P224-P227、P224、P227、
# 第224页、224页、以及“第P224页”等写法。
_EXPLICIT_PAGE_RANGE_PATTERN = re.compile(
    r"(?:第\s*)?[Pp]?\s*(\d{1,4})\s*(?:页)?\s*(?:[-—–~～至到]\s*(?:第\s*)?[Pp]?\s*(\d{1,4})\s*(?:页)?)"
)
_EXPLICIT_PAGE_SINGLE_PATTERN = re.compile(
    r"(?:第\s*)?[Pp]\s*(\d{1,4})(?:\s*页)?|第\s*(\d{1,4})\s*页|(?<!\d)(\d{1,4})\s*页"
)


def _explicit_page_references(value: object, page_count: int | None = None) -> list[int]:
    """按出现顺序提取明确标注的页码；范围仅保留两端，避免长范围挤占识图配额。"""
    text = str(value or "")
    matches: list[tuple[int, int, list[int]]] = []
    for match in _EXPLICIT_PAGE_RANGE_PATTERN.finditer(text):
        # “2029-12-17”等日期也含连字符；范围至少必须带 P / 第 / 页中的一个页码语义标记。
        if not re.search(r"[Pp第页]", match.group(0)):
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if page_count and (start > page_count or end > page_count):
            continue
        pages = [start] if start == end else [start, end]
        matches.append((match.start(), match.end(), pages))
    for match in _EXPLICIT_PAGE_SINGLE_PATTERN.finditer(text):
        if any(left <= match.start() < right for left, right, _ in matches):
            continue
        raw = next((item for item in match.groups() if item is not None), None)
        if raw is not None:
            matches.append((match.start(), match.end(), [int(raw)]))
    pages: list[int] = []
    for _, _, values in sorted(matches, key=lambda item: item[0]):
        for page in values:
            if page < 1 or (page_count and page > page_count) or page in pages:
                continue
            pages.append(page)
    return pages


def _score_visual_page_candidates(raw: dict) -> list[int]:
    """保留评分模型已结构化返回的证据页，避免从展示文本反向猜测页码。"""
    sources: list[object] = [raw.get("page_hint"), raw.get("page")]
    items = raw.get("evidence_items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                # 模型常把“（第144页）”写进证据名称而不是 page_hint 字段，一并解析。
                sources.extend((item.get("page_hint"), item.get("page"), item.get("name"), item.get("project_name")))
    pages: list[int] = []
    for source in sources:
        for page in _explicit_page_references(source):
            if page not in pages:
                pages.append(page)
    return pages


_VISUAL_PAGE_CONTEXT_PATTERN = re.compile(
    r"OCR|扫描|复印件|影印件|图片|图像|照片|签章|盖章|印章|签字|手写|勾选|"
    r"图纸|外观|截图|彩页|二维码|水印|骑缝章",
    re.IGNORECASE,
)
_VISUAL_DOCUMENT_CONTEXT_PATTERN = re.compile(r"证照|证书|执照|许可证|声明函|表单|检测报告|检验报告")
_VISUAL_GAP_CONTEXT_PATTERN = re.compile(
    r"待(?:OCR|图片|图像|人工)?核验|需(?:OCR|图片|图像)?核验|文字层.{0,12}(?:未|无法|不可)|"
    r"不可读|未呈现|空白页|页面不完整",
    re.IGNORECASE,
)
# 出现在“目录”语境中的页码是投标文件的印刷页码，不是 PDF 页序，需要先纠偏再使用。
_VISUAL_TOC_CONTEXT_PATTERN = re.compile(r"目\s*录")
_DIRECTORY_HEADER_PATTERN = re.compile(r"^\s*(?:第[一二三四五六七八九十百千0-9]+[章节]\s*)?目\s*录(?:\s|$)", re.IGNORECASE)
_DIRECTORY_TRAILING_PAGE_PATTERN = re.compile(
    r"(?:第\s*|页\s*)?(\d{1,4}(?:\s*(?:[/、,，]|至|[-—–~～])\s*\d{1,4}){0,5})\s*(?:页)?\s*$"
)


def _is_score_fragment_continuation(lines: list[str], index: int) -> bool:
    """识别跨页评分单元的续行，避免把“（1.5分）、保障措施…”拆成新评分项。

    这只处理以分值片段开头、且没有新的编号或标题的行；普通的“每项1分”条目
    仍保持独立评分条款。规则提取模型随后会收到完整的父项和全部叶子项。
    """
    line = str(lines[index] or "").strip()
    compact = re.sub(r"\s+", "", line)
    if not re.match(r"^[（(]?\d+(?:\.\d+)?分[）)]?[、，,；;]", compact):
        return False
    if re.match(r"^(?:\d+|[一二三四五六七八九十]+)[.、．]", compact):
        return False
    previous = ""
    for value in reversed(lines[max(0, index - 8):index]):
        if str(value or "").strip() and not re.fullmatch(r"\[第\d+页\]", str(value).strip()):
            previous = re.sub(r"\s+", "", str(value))
            break
    return bool(previous and ("分" in previous or "计划" in previous or "方案" in previous))
_DIRECTORY_LEADER_PATTERN = re.compile(r"[.．·…]{2,}|\s{3,}")
_DIRECTORY_CONTINUATION_MAX_PAGES = 8
_MATERIAL_ROLE_TERMS = {
    "directory": ("目录", "contents"),
    # 声明函、偏离表、报价表等均可能跨两三页。统一归为固定响应表单，后续按
    # 连续页补充候选；不针对任何特定声明函或行业材料做单独补丁。
    "response_form": ("响应函", "投标函", "报价函", "声明函", "承诺函", "偏离表", "报价表"),
    "authorization": ("授权委托书", "法定代表人身份证明", "法定代表人证明"),
    "business_license": ("营业执照", "统一社会信用代码"),
    "identity": ("身份证", "居民身份证"),
    "certificate": ("认证证书", "证书", "许可证", "资质证", "检测报告", "检验报告"),
    "platform_screenshot": ("查询截图", "信息公共服务平台", "查询结果"),
    "contract": ("合同", "协议书"),
    "acceptance": ("验收", "履约", "完成证明"),
    "personnel": ("人员简历", "人员证件", "执业证", "资格证", "社保证明"),
    "requirement": ("采购需求", "招标要求", "技术要求", "评分标准"),
}


def _rule_material_roles(rule: dict) -> set[str]:
    """从规则自身文本归纳所需材料角色，不依赖任何项目或行业关键词。"""
    text = "\n".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    roles: set[str] = set()
    for role, terms in _MATERIAL_ROLE_TERMS.items():
        if role == "directory":
            continue
        if any(term.lower() in text.lower() for term in terms):
            roles.add(role)
    # 签字、盖章是表单/授权材料的视觉事实，不能让合同或普通承诺页替代。
    if any(term in text for term in ("签字", "签章", "盖章", "电子签章")):
        roles.update({"response_form", "authorization"})
    return roles


def _directory_leader_line_count(text: object) -> int:
    """统计目录式引导符行，避免“产品目录”等普通正文被误认成目录。"""
    count = 0
    for line in str(text or "").splitlines():
        if not _DIRECTORY_LEADER_PATTERN.search(line):
            continue
        # 点线类引导符（……564）直接算目录条目；仅靠空白对齐的行还要求页码
        # 紧跟空白列间隙出现在行尾（“商务部分    3”），否则表格里“3.2”、
        # 数量等行尾数字会把应答表、报价表等材料页误判成目录。
        if re.search(r"[.．·…]{2,}", line) or re.search(r"\s{3,}\d{1,4}\s*页?\s*$", line):
            count += 1
    return count


def _looks_like_directory_page(text: object) -> bool:
    """以真正目录标题或足够密集的引导符识别目录及其跨页续页。

    目录续页通常不再重复“目录”标题，且条目本身会包含“认证证书”等材料词；
    因此不能先按材料关键词分类。反过来，正文中的“产品目录”也不能仅凭含有
    “目录”二字成为目录页。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if any(_DIRECTORY_HEADER_PATTERN.match(line) for line in lines[:2]):
        return True
    return _directory_leader_line_count(text) >= 3


def _page_material_role(text: object) -> str:
    """基于本地解析文本做保守页面角色判断；扫描页保持 unknown，不据此排除。"""
    value = _clean_model_text(text)
    if not value:
        return "scanned"
    lowered = value.lower()
    # 目录续页先于证书/合同等材料词判定，防止“证书复印件……564”这样的
    # 目录条目被当成证书本体页，导致后续无法解析其明确页码。
    if _looks_like_directory_page(value):
        return "directory"
    # 目录、证明材料和要求复述都可能含有同一关键词，先判断更具体的材料本体。
    ordered_roles = (
        "business_license", "response_form", "authorization", "platform_screenshot", "acceptance",
        "contract", "identity", "personnel", "certificate", "directory", "requirement",
    )
    for role in ordered_roles:
        if any(term.lower() in lowered for term in _MATERIAL_ROLE_TERMS[role]):
            return role
    return "other"


def _rule_material_terms(rule: dict) -> list[str]:
    """提取规则与目录条目之间可复用的长词片段，避免目录页只作为普通文本候选。"""
    raw = "".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    raw = re.sub(r"[\s\W_]+", "", raw)
    stop = {"投标文件", "招标文件", "采购文件", "投标人", "响应文件", "提供", "核验", "是否", "要求", "规则", "复印件"}
    values: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{3,}", raw):
        # 目录中常保留材料名称的一部分。4-8字片段足够区分，避免项目专属关键词补丁。
        for width in range(min(8, len(chunk)), 3, -1):
            for index in range(0, len(chunk) - width + 1):
                candidate = chunk[index:index + width]
                if candidate in stop or candidate in values:
                    continue
                values.append(candidate)
    return values[:240]


def _directory_line_page_references(line: str, page_count: int) -> list[int]:
    """目录条目末尾的裸页码具备明确目录语义，可安全作为印刷页码解析。"""
    pages = _explicit_page_references(line, page_count)
    if pages:
        return pages
    match = _DIRECTORY_TRAILING_PAGE_PATTERN.search(line)
    if not match:
        return []
    values: list[int] = []
    for raw in re.findall(r"\d{1,4}", match.group(1)):
        page = int(raw)
        if 1 <= page <= page_count and page not in values:
            values.append(page)
    return values


def _directory_material_candidates(page_texts: dict[int, str], rule: dict, page_count: int,
                                   printed_offset: int) -> list[int]:
    """从目录的“材料名称…页码”条目中找证明材料本体页。

    目录明示页码是高价值本地证据：先经印刷页偏移换算，再置于模型页码和普通证据语句之前。
    只匹配规则材料名的长词片段，未命中时完全不改变既有候选链。
    """
    terms = _rule_material_terms(rule)
    if not terms:
        return []
    directory_pages: set[int] = {
        page for page, text in page_texts.items()
        if _looks_like_directory_page(text)
    }
    # 目录经常跨多页，续页又未重复“目录”标题。以引导符连续性链式延伸；一旦
    # 中断立即停止，且每个目录首页最多延伸 8 页，避免正文中的零散点线扩张范围。
    for page in sorted(directory_pages):
        for nearby in range(page + 1, min(page_count, page + _DIRECTORY_CONTINUATION_MAX_PAGES) + 1):
            if _looks_like_directory_page(page_texts.get(nearby)):
                directory_pages.add(nearby)
                continue
            break
    candidates: list[int] = []
    for page in sorted(directory_pages):
        # PDF 表格、双栏目录常将“材料名称”和页码拆到相邻行。按逻辑条目累积到
        # 出现页码后再匹配，避免目录本身可读却漏掉证书/附件本体页。
        entry_lines: list[str] = []
        for line in str(page_texts.get(page) or "").splitlines():
            clean_line = str(line or "").strip()
            if not clean_line:
                continue
            entry_lines.append(clean_line)
            if len(entry_lines) > 4:
                entry_lines.pop(0)
            entry = " ".join(entry_lines)
            refs = _directory_line_page_references(entry, page_count)
            if not refs:
                continue
            material_text = re.sub(r"[\s\W_]+", "", _DIRECTORY_TRAILING_PAGE_PATTERN.sub("", entry))
            matches = [term for term in terms if term in material_text]
            # 一个较长的共同材料名，或两个独立的四字片段，才将目录页码提升为首选。
            if not matches or (max(map(len, matches)) < 5 and len([term for term in matches if len(term) >= 4]) < 2):
                continue
            for printed_page in refs:
                actual = printed_page + printed_offset if printed_offset and 1 <= printed_page + printed_offset <= page_count else printed_page
                if actual not in candidates:
                    candidates.append(actual)
            # 本条已闭合，下一行开始新的目录项；但保留本行可兼容“页码 + 下一项标题”
            # 被解析在同一文本行的情形。
            entry_lines = [clean_line]
    return candidates


def _prioritise_material_pages(document: dict, rule: dict, pages: list[int], *,
                                protected: object = None) -> list[int]:
    """将规则所需材料页排在普通/无关候选前；未知扫描页保留，避免漏证。"""
    page_texts = _document_page_texts(document)
    if not page_texts:
        return list(dict.fromkeys(pages))
    target_roles = _rule_material_roles(rule)
    protected_pages = set(_normalise_result_pages(protected))
    target: list[int] = []
    neutral: list[int] = []
    deferred: list[int] = []
    for page in pages:
        role = _page_material_role(page_texts.get(page))
        if page in protected_pages or role in target_roles:
            target.append(page)
        elif role in {"scanned", "other", "directory", "requirement"}:
            # 扫描件和本地文本难判页绝不删除，只是排在直接材料页之后。
            neutral.append(page)
        else:
            deferred.append(page)
    return list(dict.fromkeys(target + neutral + deferred))


def _sample_visual_page_range(start: int, end: int, page_count: int) -> list[int]:
    """在固定图片预算内代表性覆盖页段，兼顾首尾、中心与长区间内部。"""
    if start > end:
        start, end = end, start
    start, end = max(1, start), min(page_count, end)
    if start > end:
        return []
    if start == end:
        return [start]
    span = end - start
    candidates = [start, end]
    if span >= 2:
        candidates.append(start + span // 2)
    if span >= 6:
        candidates.extend((start + span // 3, start + (span * 2) // 3))
    pages: list[int] = []
    for page in candidates:
        if page not in pages:
            pages.append(page)
    return pages


def _visual_page_groups(value: object, page_count: int) -> list[list[int]]:
    """提取页码组并保留范围结构；多个页段采用轮询取样，避免首段耗尽图片名额。"""
    text = str(value or "")
    matches: list[tuple[int, int, list[int]]] = []
    for match in _EXPLICIT_PAGE_RANGE_PATTERN.finditer(text):
        if not re.search(r"[Pp第页]", match.group(0)):
            continue
        pages = _sample_visual_page_range(int(match.group(1)), int(match.group(2)), page_count)
        if pages:
            matches.append((match.start(), match.end(), pages))
    for match in _EXPLICIT_PAGE_SINGLE_PATTERN.finditer(text):
        if any(left <= match.start() < right for left, right, _ in matches):
            continue
        raw = next((item for item in match.groups() if item is not None), None)
        if raw is None:
            continue
        page = int(raw)
        if 1 <= page <= page_count:
            matches.append((match.start(), match.end(), [page]))
    return [pages for _, _, pages in sorted(matches, key=lambda item: item[0])]


def _round_robin_visual_pages(groups: list[list[int]]) -> list[int]:
    """让同一句中的不同页段优先各获得一次识图机会。"""
    pages: list[int] = []
    depth = max((len(group) for group in groups), default=0)
    for index in range(depth):
        for group in groups:
            if index < len(group) and group[index] not in pages:
                pages.append(group[index])
    return pages


def _visual_context_candidates(value: object, page_count: int, printed_offset: int = 0) -> tuple[list[int], list[int]]:
    """按“图片属性 + 尚待核验”对证据语句排序，返回高优先和普通候选页。"""
    ranked: list[tuple[int, int, list[int]]] = []
    for order, clause in enumerate(re.split(r"[\n。；;]+", str(value or ""))):
        groups = _visual_page_groups(clause, page_count)
        if not groups:
            continue
        if printed_offset and _VISUAL_TOC_CONTEXT_PATTERN.search(clause):
            # 目录语境引用的是印刷页码；按解析文本估计的固定偏移换算为 PDF 页序，
            # 换算出界时保留原值，避免静默丢弃候选。
            groups = [
                [page + printed_offset if 1 <= page + printed_offset <= page_count else page for page in group]
                for group in groups
            ]
        score = 0
        if _VISUAL_PAGE_CONTEXT_PATTERN.search(clause):
            score += 20
        if _VISUAL_GAP_CONTEXT_PATTERN.search(clause):
            score += 12
        if _VISUAL_DOCUMENT_CONTEXT_PATTERN.search(clause):
            score += 4
        ranked.append((score, order, _round_robin_visual_pages(groups)))
    priority: list[int] = []
    ordinary: list[int] = []
    for score, _, pages in sorted(ranked, key=lambda item: (-item[0], item[1])):
        target = priority if score >= 12 else ordinary
        for page in pages:
            if page not in target:
                target.append(page)
    return priority, ordinary


def _bounded_model_score(value: object, max_score: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not (score == score and abs(score) < float("inf")) or max_score <= 0:
        return None
    return min(max_score, max(0.0, score))


def _quantity_score(rule_payload: dict, raw: dict, max_score: float) -> float | None:
    count = raw.get("matched_count")
    if isinstance(count, bool) or not isinstance(count, (int, float)) or count < 0:
        return None
    rule_text = f"{rule_payload.get('title', '')} {rule_payload.get('check_rule', '')} {rule_payload.get('source_text', '')}"
    match = re.search(r"每(?:有|提供|具备)?(?:一|1|个|项)?[^，。；;]{0,12}?得\s*(\d+(?:\.\d+)?)\s*分", rule_text)
    if not match:
        return None
    return min(max_score, max(0.0, float(count) * float(match.group(1)))) if max_score > 0 else None


def _suggested_score(rule_payload: dict, raw: dict, score_type: str, max_score: float) -> float | None:
    direct = _bounded_model_score(raw.get("suggested_score"), max_score)
    if direct is not None:
        return direct
    if score_type == "objective":
        kind = rule_payload.get("scoring", {}).get("kind", "boolean")
        met = raw.get("met")
        if kind == "boolean" and met is True:
            return max_score or None
        if kind == "boolean" and met is False:
            return 0.0
        calculated = _quantity_score(rule_payload, raw, max_score)
        if calculated is not None:
            return calculated
        if met is False:
            return 0.0
        return _score_declared_in_text(f"{raw.get('calculation') or ''} {raw.get('reason') or ''}", max_score)
    return _score_declared_in_text(f"{raw.get('calculation') or ''} {raw.get('reason') or ''}", max_score)


def _score_evidence_text(raw: dict) -> str:
    parts: list[str] = []
    count = raw.get("matched_count")
    if isinstance(count, (int, float)) and not isinstance(count, bool):
        count_text = str(int(count)) if float(count).is_integer() else str(count)
        parts.append(f"AI共识别{count_text}项")
    items = raw.get("evidence_items")
    if isinstance(items, list):
        labels = {"valid": "建议有效", "uncertain": "待核验", "invalid": "建议无效"}
        for index, item in enumerate(items[:20], start=1):
            if not isinstance(item, dict):
                continue
            name = _clean_model_text(item.get("name") or item.get("project_name") or f"证据{index}")
            page = _clean_model_text(item.get("page_hint") or item.get("page"))
            # 页码统一为“第N页/第N-M页”，去掉模型偶发的 P55、第P55 前缀写法；
            # 只有原文带范围分隔符（-、—、～、至）才视为连续区间，散页用“、”连接，
            # 避免把“P55、P57”两个独立页误写成“第55-57页”。
            page_numbers = re.findall(r"\d+", page)
            if page_numbers:
                if re.search(r"[-—–～~至]", page):
                    if len(page_numbers) >= 2:
                        page = f"{page_numbers[0]}-{page_numbers[1]}"
                        if len(page_numbers) > 2:
                            page += "、" + "、".join(page_numbers[2:])
                    else:
                        page = page_numbers[0]
                else:
                    page = "、".join(page_numbers)
            validity = labels.get(str(item.get("validity") or ""), str(item.get("validity") or ""))
            reason = _clean_model_text(item.get("reason"))
            detail = "；".join(value for value in (validity, reason) if value)
            page_label = page if "页" in page else f"第{page}页"
            parts.append(f"{index}. {name}{f'（{page_label}）' if page else ''}{f'：{detail}' if detail else ''}")
    evidence = _clean_model_text(raw.get("evidence"))
    if evidence:
        parts.append(evidence)
    return "\n".join(parts)


def _score_reason_text(raw: dict, suggested: float | None) -> str:
    reason = _clean_model_text(raw.get("reason"))
    # 计分过程不再并入 reason：由 _score_result_from_model 单独写入证据层，
    # reason 只保留判断理由，避免“计分过程：SI-1…”顶到主表首句。
    return reason or ("AI未返回完整理由。" if suggested is not None else "模型未返回可用建议分。")


_SCORE_CALCULATION_RESULT_PATTERN = re.compile(r"(?:=|＝)\s*(\d+(?:\.\d+)?)\s*分?")
_SCORE_CONCLUSION_PATTERN = re.compile(r"(?:最终|建议|得分|合计|总计|封顶).{0,16}?(\d+(?:\.\d+)?)\s*分")


def _format_score(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _score_declared_in_text(text: object, max_score: float) -> float | None:
    """模型把建议分写进理由/计分过程却漏填结构化字段时回溯采用。

    只取“最终/建议/得分/合计/总计/封顶”结论性措辞后的分值；多个分值必须一致
    且不越界，否则保持空分由人工核对，不把猜测当结论。
    """
    cleaned = _clean_model_text(text)
    if not cleaned or max_score <= 0:
        return None
    values = {round(float(value), 2) for value in _SCORE_CONCLUSION_PATTERN.findall(cleaned)
              if 0 <= float(value) <= max_score}
    if len(values) != 1:
        return None
    return values.pop()


def _reason_declares_conflicting_score(reason: object, suggested: float | None) -> bool:
    if suggested is None:
        return False
    text = _clean_model_text(reason)
    if not text:
        return False
    values = [float(value) for value in _SCORE_CALCULATION_RESULT_PATTERN.findall(text)]
    values.extend(float(value) for value in _SCORE_CONCLUSION_PATTERN.findall(text))
    return any(abs(value - float(suggested)) > 1e-6 for value in values)


def _reconcile_score_reason(reason: object, suggested: float | None, *, adjusted: bool = False,
                            source: str = "") -> str:
    """当证据链调整最终建议分时，移除会与最终分数冲突的旧计算文字。"""
    text = _clean_model_text(reason)
    if suggested is None:
        return text
    if not adjusted and not _reason_declares_conflicting_score(text, suggested):
        return text
    retained = []
    for piece in re.split(r"(?<=[。；;])\s*|\n+", text):
        if not piece.strip():
            continue
        if _reason_declares_conflicting_score(piece, suggested) or "计分过程" in piece:
            continue
        retained.append(piece.strip())
    origin = f"；依据{source}已确认的材料" if source else ""
    retained.append(f"最终建议分：{_format_score(float(suggested))}分{origin}。")
    return _truncate_field(" ".join(dict.fromkeys(retained)), 2000)


def _normalise_score_results(output: object, rule_payload: list[dict], score_type: str) -> list[dict]:
    output_map = {item.get("rule_id"): item for item in output if isinstance(item, dict)} if isinstance(output, list) else {}
    results = []
    for item in rule_payload:
        raw = output_map.get(item["rule_id"], {})
        try:
            max_score = float(item["scoring"].get("max_score") or 0)
            if not (0 < max_score < float("inf")):
                max_score = 0.0
        except (TypeError, ValueError):
            max_score = 0.0
        suggested = _suggested_score(item, raw, score_type, max_score)
        results.append(_score_result_from_model(
            item["rule_id"], suggested, max_score, raw,
            force_needs_ocr=bool(item.get("ocr_required")),
        ))
    return results


def _score_result_from_model(rule_id: str, suggested: float | None, max_score: float, raw: dict,
                             *, force_needs_ocr: bool = False) -> dict:
    if force_needs_ocr and raw.get("needs_ocr") is not True:
        raw = {**raw, "needs_ocr": True}
    confidence = _enum_text(raw.get("confidence"), {"high", "medium", "low"}, "medium")
    needs_ocr = raw.get("needs_ocr") is True
    evidence = _truncate_field(_score_evidence_text(raw), 2000)
    has_evidence = bool(evidence)
    auto_ready = suggested is not None and confidence == "high" and has_evidence and not needs_ocr
    reason = _reconcile_score_reason(_score_reason_text(raw, suggested), suggested)
    result = {
        "rule_id": rule_id, "suggested_score": suggested, "final_score": None,
        "effective_score": suggested if auto_ready else None, "max_score": max_score or None,
        "evidence": evidence,
        "reason": reason,
        # 仅在当前任务内供图片识别定位使用；存储层会忽略该临时字段，避免改变历史结果 API。
        "visual_page_candidates": _score_visual_page_candidates(raw),
        "confidence": confidence, "automation_status": "ready_for_batch_confirmation" if auto_ready else "needs_review",
        "requires_review": not auto_ready,
        "review_reason": "" if auto_ready else "未得到高置信、可引用的建议分，需人工复核。",
    }
    # 计分过程以结构化证据层保留（前端证据链可展开），不再与判断理由混在同一字段。
    calculation = _clean_model_text(raw.get("calculation"))
    if calculation:
        result = _append_evidence_layer(
            result, source="score_calculation", summary=calculation,
            checked_pages=[], evidence_pages=[], service="", model="",
        )
    return result


def _is_invalid_json_model_response(exc: ValueError) -> bool:
    return str(exc).startswith("模型未返回有效 JSON")


def _is_model_format_error(exc: ValueError) -> bool:
    message = str(exc)
    return (
        _is_invalid_json_model_response(exc)
        or message.startswith("模型返回格式不符合综合评审要求")
        or (isinstance(exc, ModelResponseEnvelopeError) and exc.retryable)
    )


EVALUATION_BATCH_SIZES = {"review": 8, "objective": 8, "subjective": 6}
# 超过阈值的文件先按连续页块做全文覆盖扫描；阈值以下的短文件直接随最终规则
# 组发送全文，同样满足全文覆盖。所有索引都只存在于当前工作进程内。
FULL_SCAN_THRESHOLD_CHARS = 24_000
# 全文扫描块字符预算：11K 已实测调用次数过多（8 家项目 208 次扫描），14K 在
# 调用数（约减少 1/3）与单次上下文/截断风险之间取平衡；输出超长时系统只拆
# 规则目录重试，不会重发全文或丢页。
FULL_SCAN_CHUNK_CHARS = 14_000
# 首轮只建立候选证据索引：每个页块携带一次完整的精简规则目录。正常情况下
# 不再形成“页块 × 规则批次”的矩阵；若模型确实输出超长，才仅拆规则目录一次。
FULL_SCAN_CATALOG_RULE_CHARS = 220
SCOPE_ANOMALY_CACHE_VERSION = "scope-anomaly-candidates-v1"
EVIDENCE_MANIFEST_VERSION = "page-chunks-v1"
# 二次复核上下文上限。全文首轮已覆盖所有页面，此处只装入候选证据和重点原文。
EVALUATION_BATCH_CONTEXT_CHARS = 64_000
EVALUATION_STRATEGY_CONTEXT_CHARS = {
    "point": 42_000,
    "consistency": 55_000,
    "counting": 64_000,
    "section": 64_000,
}


def _document_evidence_chunks(app, document: dict) -> list[dict]:
    """建立可复用的轻量证据索引骨架，并按需返回当前任务所需的原文页块。

    清单只保存页码边界、哈希和长度，既可在规则或提示词变化后校验页面一致性，
    又不会把解析正文重复写进 SQLite；未运行任务时不会占用内存。
    """
    path = str(document.get("parsed_path") or "")
    if not path:
        return []
    chunks = split_full_text_chunks(path, FULL_SCAN_CHUNK_CHARS, overlap_pages=1)
    manifest = [
        {
            "chunk_id": chunk.get("chunk_id"), "start_page": chunk.get("start_page"),
            "end_page": chunk.get("end_page"), "chars": len(str(chunk.get("text") or "")),
            "text_hash": hashlib.sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest(),
        }
        for chunk in chunks
    ]
    document_hash = str(document.get("sha256") or "")
    if document_hash:
        cached = storage.get_document_evidence_manifest(app, document["document_id"], document_hash, EVIDENCE_MANIFEST_VERSION)
        if cached != manifest:
            storage.save_document_evidence_manifest(
                app, document["document_id"], document_hash, EVIDENCE_MANIFEST_VERSION, manifest,
            )
    return chunks


def _rule_batches(rules: list[dict], size: int) -> list[list[dict]]:
    return [rules[index:index + size] for index in range(0, len(rules), size)]


def _rule_scoring(rule: dict) -> dict:
    value = rule.get("scoring")
    if isinstance(value, dict):
        return value
    try:
        return json.loads(rule.get("scoring_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _rule_execution_strategy(rule: dict) -> str:
    explicit = str(rule.get("execution_strategy") or "").strip()
    if explicit in {"point", "counting", "section", "consistency", "cross_bid", "visual", "external"}:
        # visual/external 的最终文本审查仍按点状路由；它们的证据要求由独立字段控制。
        return "point" if explicit in {"visual", "external", "cross_bid"} else explicit
    raw = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    if any(term in raw for term in ("公司名称", "项目名称", "前后", "一致", "无关公司", "无关项目", "全文")):
        return "consistency"
    if any(term in raw for term in ("业绩", "数量", "累计", "每个", "每项", "项目数", "份数", "得分")):
        return "counting"
    if rule.get("category") == "subjective" or any(
        term in raw for term in ("技术方案", "实施方案", "服务方案", "组织方案", "功能", "模块", "章节")
    ):
        return "section"
    return "point"


def _rule_complexity(rule: dict) -> float:
    """用结构复杂度而非固定条数估算一次模型输出负担，不参与业务判断。"""
    scoring = _rule_scoring(rule)
    items = scoring.get("items") if isinstance(scoring.get("items"), list) else []
    text_length = len(str(rule.get("check_rule") or "")) + len(str(rule.get("source_text") or ""))
    complexity = 1.0 + min(3.0, len(items) * 0.45) + min(2.0, max(0, text_length - 350) / 700)
    if _rule_execution_strategy(rule) in {"counting", "section", "consistency"}:
        complexity += 0.35
    return complexity


def _evaluation_rule_batches(component: str, rules: list[dict], scan_index: dict | None = None) -> list[list[dict]]:
    """先按证据策略归组，再按复杂度预算装箱。

    全文扫描完成后，同策略规则会优先和证据页重合度高的规则同组。每条规则仍由
    后续上下文构造器保留自身直接页块，分组仅减少多组重复发送同一原文。
    """
    if not rules:
        return []
    max_count = EVALUATION_BATCH_SIZES[component]
    buckets: dict[str, list[dict]] = {}
    for rule in rules:
        buckets.setdefault(_rule_execution_strategy(rule), []).append(rule)
    chunks = scan_index.get("chunks", []) if isinstance(scan_index, dict) else []
    ledger = scan_index.get("evidence_ledger", {}) if isinstance(scan_index, dict) else {}
    if isinstance(ledger, dict) and ledger:
        chunk_map = {
            rule["rule_id"]: [
                str(item.get("chunk_id") or "") for item in ledger.get(str(rule.get("rule_id") or ""), {}).get("candidates", [])
                if str(item.get("chunk_id") or "")
            ][:6]
            for rule in rules
        }
    else:
        chunk_map = select_rule_chunk_map(chunks, rules, per_rule=6) if chunks else {}
    groups: list[list[dict]] = []
    for strategy_rules in buckets.values():
        if not chunk_map:
            current: list[dict] = []
            current_cost = 0.0
            for rule in strategy_rules:
                cost = _rule_complexity(rule)
                if current and (len(current) >= max_count or current_cost + cost > max_count):
                    groups.append(current)
                    current, current_cost = [], 0.0
                current.append(rule)
                current_cost += cost
            if current:
                groups.append(current)
            continue
        # 以最早未分组规则为锚点；随后只在同策略、同复杂度预算内选取和已有页块
        # 重合最多的规则。未命中页块的规则不会被丢弃，只按原始顺序作为兜底加入。
        remaining = list(strategy_rules)
        while remaining:
            current = [remaining.pop(0)]
            current_cost = _rule_complexity(current[0])
            current_chunks = set(chunk_map.get(current[0]["rule_id"], []))
            while remaining and len(current) < max_count:
                options = []
                for index, candidate in enumerate(remaining):
                    cost = _rule_complexity(candidate)
                    if current_cost + cost > max_count:
                        continue
                    candidate_chunks = set(chunk_map.get(candidate["rule_id"], []))
                    overlap = len(current_chunks & candidate_chunks)
                    options.append((-overlap, index, candidate, cost, candidate_chunks))
                if not options:
                    break
                _, index, candidate, cost, candidate_chunks = min(options, key=lambda value: (value[0], value[1]))
                current.append(candidate)
                current_cost += cost
                current_chunks.update(candidate_chunks)
                remaining.pop(index)
            groups.append(current)
    return groups


def _combined_batch_output_budget(component: str, rules: list[dict]) -> int:
    """按规则数量和叶子评分复杂度共同分配输出，避免单条复合规则仍被截断。"""
    count = max(1, len(rules))
    item_count = sum(len((_rule_scoring(rule).get("items") or [])) for rule in rules)
    if component == "review":
        return max(4_000, 1_600 + count * 650 + item_count * 120)
    if component == "subjective":
        return max(4_500, 1_800 + count * 700 + item_count * 320)
    return max(2_000, 800 + count * 300 + item_count * 220)


def _combined_batch_prompt(app, component: str, document: dict, payload: list[dict], text: str, *, compact: bool) -> str:
    template_id = f"evaluate_all_{component}_user"
    retry_note = (
        "这是格式异常后的严格 JSON 重试：必须只输出一个 JSON 对象；不得使用 Markdown、注释或前后说明；"
        "字符串内不得出现未转义的英文双引号、换行或制表符；每条规则仅保留一句证据和一句理由。\n"
        if compact else ""
    )
    prompt = storage.render_prompt_template(
        app, template_id, rules=_stable_prompt_json(payload),
        document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写", text=text,
        retry_note=retry_note,
    )
    if _document_text_coverage_status(document) == "uncovered":
        # 将扫描件边界直接告诉模型；后端仍有独立守卫，二者互为校验，避免模型把
        # “全文未命中”误写成“未提供”或“满足”。
        prompt += (
            "\n\n【机器可读文本覆盖不足】本文件大部分页面可能为扫描件，当前文本包未覆盖整份材料。"
            "文本未命中不等于材料缺失，也不等于规则满足；未含实际 OCR/图片证据时须按提示词返回待 OCR 结论。"
        )
    # 范围一致性规则需要同时呈现不同类型的偏离对象；不把该额外约束发送给其他
    # 规则组，避免无关上下文和输出长度占用。模板本身可在提示词配置中维护。
    if component == "review" and any(_is_scope_consistency_rule(item) for item in payload):
        scope_guidance = storage.render_prompt_template(app, "evaluate_all_scope_anomaly_guidance")
        prompt = (
            "【项目范围偏离结果摘要原则】\n"
            f"{scope_guidance}\n"
            "本规则的 evidence 可保留最多四项不同类型的代表性原文，合计不超过300字；"
            "其他规则仍遵守原有证据长度限制。\n\n"
            + prompt
        )
    # 价格事实由同一任务内的本地保守解析统一提供给符合性审查和客观评分；它位于
    # 可变尾部，不影响前面规则组与正文的既有提示词协议。
    if document.get("_shared_price_facts") and any(storage.is_price_rule(
        f"{item.get('title', '')} {item.get('check_rule', '')} {item.get('source_text', '')}"
    ) for item in payload):
        prompt += f"\n\n【已核验价格事实】\n{document['_shared_price_facts']}"
    return prompt


def _combined_batch_payload(component: str, rules: list[dict]) -> list[dict]:
    if component == "review":
        return [{"rule_id": item["rule_id"], "category": item["category"], "title": item["title"],
                 "check_rule": item.get("check_rule") or item["title"], "source_text": item["source_text"],
                 "ocr_required": _rule_requires_visual_verification(item),
                 "execution_strategy": _rule_execution_strategy(item),
                 "evidence_requirements": item.get("evidence_requirements") or [],
                 "evidence_items": _rule_evidence_items(item)} for item in rules]
    return _score_payload(rules)


def _full_scan_catalog(rules: list[dict]) -> list[dict]:
    """生成首轮扫描专用的精简规则目录，详细评分规则留给最终汇总阶段。"""
    catalog = []
    for rule in rules:
        query = re.sub(r"\s+", " ", f"{rule.get('title') or ''}；{rule.get('check_rule') or rule.get('title') or ''}").strip()
        evidence_items = _rule_evidence_items(rule)
        if evidence_items:
            item_text = "；".join(
                _clean_model_text(item.get("name") or item.get("requirement"))[:100]
                for item in evidence_items[:12]
            )
            if item_text:
                query = f"{query}；逐项取证清单：{item_text}"
        # 主观评分表常把多个有独立分值的子项写在一条规则中。首轮只截取通用长度
        # 会丢掉末尾的子项，导致后续评分只能看到“总分”而不能看到完整评分维度。
        is_coverage_rule = rule["category"] == "other" and (
            "逐项响应覆盖" in str(rule.get("title") or "") or "叶子要求" in str(rule.get("check_rule") or "")
        )
        # 覆盖规则的末尾常是最关键的连续编号要求；保留较长目录只影响少数规则，
        # 可避免首轮扫描在模型调用前就丢失叶子项。
        query_limit = 1_200 if is_coverage_rule else (420 if rule["category"] == "subjective" else FULL_SCAN_CATALOG_RULE_CHARS)
        item = {
            "id": rule["rule_id"],
            # 保留旧字段，避免用户在提示词配置中保留了旧版 findings 模板时无法对应规则。
            "rule_id": rule["rule_id"],
            "q": query[:query_limit],
            "type": rule["category"],
            "strategy": _rule_execution_strategy(rule),
            "coverage": 1 if is_coverage_rule else 0,
            "evidence_requirements": rule.get("evidence_requirements") or [],
        }
        if _rule_requires_visual_verification(rule):
            item["ocr"] = 1
        # 对业绩等数量/累计评分项保留极短的计分线索，避免首轮遗漏每一项材料；
        # 不在此阶段给分或做有效性裁断。
        if rule["category"] in {"objective", "subjective"}:
            try:
                scoring = json.loads(rule.get("scoring_json") or "{}")
            except json.JSONDecodeError:
                scoring = {}
            if scoring:
                hint_limit = 420 if rule["category"] == "subjective" else 220
                item["score_hint"] = json.dumps(scoring, ensure_ascii=False, separators=(",", ":"))[:hint_limit]
        catalog.append(item)
    return catalog


def _full_scan_chunk_label(chunk: dict) -> str:
    start_page, end_page = chunk.get("start_page"), chunk.get("end_page")
    if start_page and end_page:
        return f"第{start_page}-{end_page}页" if start_page != end_page else f"第{start_page}页"
    return str(chunk.get("chunk_id") or "连续文本块")


def _full_scan_prompt(app, document: dict, catalog: list[dict], chunk: dict, project_scope: dict, *, compact: bool) -> str:
    retry_note = (
        "这是格式异常后的严格 JSON 重试：只输出一个 JSON 对象；matches 最多 16 条、scope_anomalies 最多 4 条，每段摘录最多 60 字；"
        "复合评分规则的不同叶子项可分别返回，但同一规则最多 6 条；若后文出现不同的数量限制，以本段限制为准；"
        "不得使用 Markdown、注释或前后说明。\n"
        if compact else
        "本次正常扫描 matches 最多 24 条、scope_anomalies 最多 6 条；复合评分规则的不同叶子项可分别返回，"
        "同一规则最多 3 条；优先保留投标人自主内容或直接影响判断的原文。若后文的通用限制与本段冲突，以本段为准。\n"
    )
    prompt = storage.render_prompt_template(
        app, "evaluate_all_full_scan_user", retry_note=retry_note,
        project_scope=_stable_prompt_json(_scope_prompt_profile(project_scope)),
        rules=_stable_prompt_json(catalog),
        document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写",
        chunk_label=_full_scan_chunk_label(chunk), text=chunk["text"],
    )
    # 范围判断原则独立于用户可能保留的旧版全文扫描模板，既可在右上角查看和编辑，
    # 又能保证规则目录变动后仍执行相同的“上位主题/具体对象”两层判断。
    scope_guidance = storage.render_prompt_template(app, "evaluate_all_scope_anomaly_guidance")
    prompt = f"【项目范围偏离独立判断原则】\n{scope_guidance}\n\n{prompt}"
    # 同步兼容云端尚未恢复默认的旧自定义模板。正常扫描的输出上限从 36 收为 24，
    # 但每条规则仍有本地章节召回兜底；这样减少 M3 因长 JSON 截断而触发的整块拆分。
    prompt = prompt.replace("最多36条", "最多24条").replace("最多 36 条", "最多 24 条")
    prompt = prompt.replace("最多8条", "最多6条").replace("最多 8 条", "最多 6 条")
    if compact:
        prompt = prompt.replace("最多24条", "最多16条").replace("最多 24 条", "最多 16 条")
        prompt = prompt.replace("最多6条", "最多4条").replace("最多 6 条", "最多 4 条")
    return prompt


def _normalise_scan_findings(output: object, allowed_ids: set[str], chunk: dict) -> list[dict]:
    """兼容新版紧凑数组及用户遗留模板的 findings 对象。"""
    findings = []
    for raw in output if isinstance(output, list) else []:
        if isinstance(raw, list) and len(raw) >= 4:
            rule_id, page_hint, evidence, status = raw[:4]
            # 第六项为证据来源标签；旧模板没有该项时仍按原有默认值兼容。
            observation = raw[5] if len(raw) >= 6 else ""
            needs_ocr, confidence = False, "medium"
            evidence_priority = raw[4] if len(raw) >= 5 else "medium"
        elif isinstance(raw, dict):
            rule_id = raw.get("rule_id") or raw.get("id")
            page_hint = raw.get("page_hint") or raw.get("page")
            evidence = raw.get("evidence") or raw.get("quote")
            status = raw.get("tentative_status") or raw.get("polarity") or raw.get("status")
            observation, needs_ocr = raw.get("observation"), raw.get("needs_ocr") is True
            confidence = raw.get("confidence")
            evidence_priority = raw.get("evidence_priority") or raw.get("priority")
        else:
            continue
        if not isinstance(rule_id, str) or rule_id not in allowed_ids:
            continue
        if not isinstance(status, str) or status not in {"supports", "contradicts", "partial", "suspected"}:
            status = {"support": "supports", "contradict": "contradicts", "suspect": "suspected"}.get(str(status).lower(), "suspected")
        confidence = _enum_text(confidence, {"high", "medium", "low"}, "medium")
        evidence_priority = _enum_text(evidence_priority, {"high", "medium", "low"}, "medium")
        findings.append({
            "rule_id": rule_id,
            "chunk_id": chunk["chunk_id"],
            "page_range": _full_scan_chunk_label(chunk),
            "page_hint": _clean_model_text(page_hint)[:80],
            "evidence": _clean_model_text(evidence)[:240],
            "observation": _clean_model_text(observation)[:120],
            "tentative_status": status,
            "matched_count": None,
            "suggested_score": None,
            "needs_ocr": needs_ocr,
            "confidence": confidence,
            "evidence_priority": evidence_priority,
        })
    return findings


_SCOPE_NON_ANOMALY_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:与|和)(?:本|该|当前)?项目(?:范围|需求|采购内容|技术要求)(?:完全)?(?:一致|相符|匹配)(?:[，。；;]|$)",
    r"(?<!不)(?<!未)(?:属于|符合|契合|对应)(?:本|该|当前)?项目(?:范围|需求|采购内容|技术要求)(?!之外|以外)(?:[，。；;]|$)",
    r"(?:未发现|未见|没有发现).{0,12}(?:偏离|异常|无关|不一致)",
    r"(?:不构成|无需作为|不应视为).{0,12}(?:偏离|异常|无关|问题)",
    r"(?:合理|正常)(?:出现|引用|列示|使用).{0,12}(?:不作为|不构成|不是|非)(?:范围)?(?:异常|偏离)",
))


def _scope_candidate_is_actionable(candidate: dict) -> bool:
    """滤掉模型误放进异常数组的明确正常项，不对具体业务名词作硬编码判断。"""
    if not str(candidate.get("evidence") or "").strip():
        return False
    conclusion = "；".join(str(candidate.get(field) or "") for field in ("relation", "observation"))
    return not any(pattern.search(conclusion) for pattern in _SCOPE_NON_ANOMALY_PATTERNS)


def _scope_candidate_matches_tender_material(candidate: dict, tender_baseline: object) -> bool:
    """排除招标清单/技术需求已明确允许的对象，避免范围画像抽样造成误报。

    不维护行业词表，而是只比对本项目招标文件的完整机器可读文本。候选中至少有一个
    五字以上的具体对象短语原样出现，才将其视为已在采购范围内；地区、项目名或泛化
    词不会触发该保护，因此真正的跨项目、跨技术或无关内容仍会进入模型复核。
    """
    baseline = re.sub(r"[\s\W_]+", "", str(tender_baseline or ""))
    if len(baseline) < 4:
        return False
    evidence = " ".join(str(candidate.get(key) or "") for key in ("evidence", "observation"))
    stop = {"投标文件", "响应文件", "招标文件", "采购项目", "技术要求", "项目范围", "无关内容", "相关内容"}
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,}", evidence):
        compact = re.sub(r"[\s\W_]+", "", token)
        if compact in stop:
            continue
        # 优先完整对象名；较长句子再滑窗，覆盖“设备名称 + 描述”被模型一并摘录。
        widths = range(min(18, len(compact)), 4, -1)
        for width in widths:
            for index in range(0, len(compact) - width + 1):
                if compact[index:index + width] in baseline:
                    return True
    return False


def _tender_scope_baseline(documents: list[dict]) -> str:
    """供本地范围误报回收使用的完整招标文本，不发送给模型、不额外占用 token。"""
    parts: list[str] = []
    for document in documents:
        if document.get("role") not in {"tender", "tender_attachment"}:
            continue
        path = Path(str(document.get("parsed_path") or ""))
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    # 正常招标文件远低于此上限；上限仅防止异常解析文件在 2GB 服务器上占用过多内存。
    return "\n".join(parts)[:1_500_000]


def _scope_prompt_profile(value: dict) -> dict:
    """去掉仅供本地回收使用的字段，保持发送给模型的范围画像紧凑且稳定。"""
    return {key: value.get(key) for key in SCOPE_PROFILE_FIELDS if key in value}


def _merge_scope_anomalies(current: list[dict], previous: list[dict]) -> list[dict]:
    """合并当前与历史候选；历史项仅作为待复核线索，并只恢复中高优先级。"""
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source, values in (("current_scan", current), ("prior_scan", previous)):
        for raw in values if isinstance(values, list) else []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if source == "prior_scan" and item.get("candidate_priority") not in {"high", "medium"}:
                continue
            if not _scope_candidate_is_actionable(item):
                continue
            signature = (
                str(item.get("chunk_id") or "").split(".", 1)[0],
                re.sub(r"\s+", "", str(item.get("evidence") or ""))[:180],
            )
            if signature in seen:
                continue
            seen.add(signature)
            item["candidate_source"] = source
            merged.append(item)
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(merged, key=lambda item: priority_rank.get(item.get("candidate_priority"), 1))[:24]


def _normalise_scope_anomalies(output: object, chunk: dict) -> list[dict]:
    """范围偏离为独立候选通道，不强制映射到任何既有规则或预设类型。"""
    candidates = []
    for raw in output if isinstance(output, list) else []:
        if isinstance(raw, list) and len(raw) >= 5:
            page_hint, dimension, priority, evidence, relation = raw[:5]
            observation = raw[5] if len(raw) >= 6 else ""
        elif isinstance(raw, dict):
            page_hint = raw.get("page_hint") or raw.get("page")
            dimension = raw.get("dimension") or raw.get("type") or "其他范围偏离"
            priority = raw.get("priority") or raw.get("risk")
            evidence = raw.get("evidence") or raw.get("quote")
            relation = raw.get("relation") or raw.get("mismatch")
            observation = raw.get("observation") or raw.get("reason")
        else:
            continue
        priority = str(priority or "medium").lower()
        if priority not in {"high", "medium", "low"}:
            priority = "medium"
        evidence = _clean_model_text(evidence)[:240]
        dimension = _clean_model_text(dimension)[:80] or "其他范围偏离"
        if not evidence:
            continue
        candidate = {
            "chunk_id": chunk["chunk_id"], "page_range": _full_scan_chunk_label(chunk),
            "page_hint": _clean_model_text(page_hint)[:80], "dimension": dimension,
            "candidate_priority": priority, "evidence": evidence,
            "relation": _clean_model_text(relation)[:160],
        }
        observation = _clean_model_text(observation)[:120]
        if observation:
            candidate["observation"] = observation
        if _scope_candidate_is_actionable(candidate):
            candidates.append(candidate)
    return candidates


SCOPE_PROFILE_FIELDS = (
    "project_identity", "scope_summary", "service_targets", "core_tasks", "technical_topics",
    "equipment_or_materials", "deliverables", "standards_or_rules", "regions", "keywords",
)

# 项目范围画像必须优先依据业务章节，而不能只抽样全文的前中后位置；这些是章节
# 定位词，不参与任何正负结论，也不会以固定词表过滤异常内容。
_SCOPE_SECTION_TERMS = (
    "项目概况", "项目背景", "采购范围", "招标范围", "服务范围", "工作范围",
    "技术需求", "技术要求", "建设内容", "采购需求", "设备清单", "服务内容",
    "交付", "成果", "实施地点", "服务地点", "项目地点", "适用标准",
)


def _scope_excerpt(text: str, budget: int) -> str:
    """以项目范围章节为主、全篇均衡抽样为兜底地构造范围画像原文。"""
    value = str(text or "").strip()
    if len(value) <= budget:
        return value
    pages = [item.strip() for item in re.split(r"(?=\[第\d+页\])", value) if item.strip()]
    if len(pages) <= 1:
        # 无页码文本也优先抽取包含范围章节词的段落；其余位置只承担兜底覆盖。
        pages = [item.strip() for item in re.split(r"\n{2,}", value) if item.strip()]
    ranked: list[tuple[int, int]] = []
    for index, item in enumerate(pages):
        score = sum(1 for term in _SCOPE_SECTION_TERMS if term in item)
        if score:
            ranked.append((score, index))
    selected: list[int] = []
    # 每个范围章节最多取一个最相关片段，避免“技术需求”长表挤掉项目概况或交付要求。
    for _, index in sorted(ranked, key=lambda item: (-item[0], item[1])):
        if index not in selected:
            selected.append(index)
        if len(selected) >= 6:
            break
    # 章节解析不完整时仍保留前、中、后三处覆盖，避免把范围画像退化为关键词窗口。
    for index in (0, len(pages) // 2, max(0, len(pages) - 1)):
        if index not in selected:
            selected.append(index)
    selected.sort()
    if not selected:
        selected = [0]
    per_piece = max(800, budget // len(selected))
    pieces = []
    for index in selected:
        label = "范围相关章节" if any(term in pages[index] for term in _SCOPE_SECTION_TERMS) else "全文覆盖样本"
        pieces.append(f"【{label}】\n{pages[index][:per_piece]}")
    return "\n\n".join(pieces)[:budget]


def _scope_source(documents: list[dict], char_limit: int) -> str:
    """构造项目范围画像依据；优先保留范围章节，并以全篇覆盖作兜底。"""
    sources = []
    tender_documents = [item for item in documents if item.get("role") in {"tender", "tender_attachment"}]
    # 平均分配预算，避免多个招标附件时前几份文件挤占全部上下文。
    per_document = max(1, char_limit // max(1, len(tender_documents)))
    for document in tender_documents:
        path = Path(str(document.get("parsed_path") or ""))
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = _scope_excerpt(text, per_document)
        sources.append(f"【{document.get('original_name') or '招标文件'}】\n{text}")
    return "\n\n".join(sources)[:char_limit]


def _normalise_scope_profile(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    profile: dict = {}
    for field in SCOPE_PROFILE_FIELDS:
        item = raw.get(field)
        if field in {"project_identity", "scope_summary"}:
            profile[field] = _clean_model_text(item)[:1200]
        elif isinstance(item, list):
            values = []
            for candidate in item:
                text = _clean_model_text(candidate)[:180]
                if text and text not in values:
                    values.append(text)
                if len(values) >= 24:
                    break
            profile[field] = values
        else:
            profile[field] = []
    return profile


def _project_scope_profile(app, task: dict, profile: dict, documents: list[dict], rules: list[dict]) -> dict:
    """按招标依据一次生成范围画像并缓存，不在投标文件间重复调用。"""
    source_limit = _prompt_char_limit(profile, 100_000, 160_000)
    tender_text = _scope_source(documents, source_limit)
    if not tender_text:
        return _normalise_scope_profile({})
    project = storage.get_project(app, task["project_id"]) or {}
    rule_packet = [{"title": item.get("title"), "check_rule": item.get("check_rule"), "source_text": item.get("source_text")}
                   for item in rules]
    scope_key = hashlib.sha256(json.dumps({
        "version": PROMPT_VERSION,
        # 范围画像只依赖本阶段实际使用的资料、模型和模板。评分或输出格式提示词
        # 的局部调整不应迫使已正确建立的招标范围画像重新消耗模型调用。
        # 用户明确强制重跑时仍刻意绕过缓存，保留“重新完整判断”的原有语义。
        "force_run": task.get("task_id") if task.get("payload", {}).get("force_rerun") else None,
        "profile": profile.get("profile_id"), "model": profile.get("model_name"),
        "tender": tender_text, "rules": rule_packet,
        "system": storage.prompt_template(app, "evaluate_all_scope_profile"),
        "user": storage.prompt_template(app, "evaluate_all_scope_profile_user"),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    cached = storage.get_project_scope_checkpoint(app, task["project_id"], scope_key)
    if cached is not None:
        return _normalise_scope_profile(cached)
    prompt = storage.render_prompt_template(
        app, "evaluate_all_scope_profile_user", project_name=project.get("name") or "未填写",
        rules=_stable_prompt_json(rule_packet), tender_text=tender_text,
    )
    try:
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_scope_profile", _system_prompt(app, "evaluate_all_scope_profile"), prompt,
            context_mode="project_scope_source", max_tokens=_output_token_budget(profile, 2_800), thinking_mode="disabled",
        )
    except InvalidJsonResponse as exc:
        parsed = _repair_invalid_json(app, task, profile, "evaluate_all_scope_profile_json_repair", exc, "project_identity")
    scope = _normalise_scope_profile(parsed)
    storage.save_project_scope_checkpoint(app, task["project_id"], scope_key, scope)
    return scope


def _full_scan_output_risk(catalog: list[dict], chunk: dict, max_tokens: int) -> dict:
    """估算全文扫描发生输出格式异常的风险，仅用于影子观测。

    评分只依赖规则目录和页块长度等元数据，不读取或保存正文。当前结果绝不参与
    拆分、重试或 Token 预算决策；积累足够云端样本后才能评估是否值得灰度启用。
    """
    rule_count = len(catalog)
    chunk_chars = len(str(chunk.get("text") or ""))
    catalog_chars = len(_stable_prompt_json(catalog))
    complex_rules = sum(
        1 for item in catalog
        if item.get("coverage") or item.get("score_hint") or len(item.get("evidence_requirements") or []) >= 3
    )
    score = 0
    score += min(42, rule_count * 2)
    score += min(24, chunk_chars // 550)
    score += min(16, catalog_chars // 1_200)
    score += min(12, complex_rules * 2)
    if max_tokens <= 2_200 and rule_count >= 16:
        score += 8
    score = max(0, min(100, int(score)))
    return {
        "score": score,
        "recommend_split": score >= 70 and rule_count > 12,
        "input_chars": chunk_chars + catalog_chars,
        "rule_count": rule_count,
    }


def _record_full_scan_risk(app, task: dict, document: dict, phase: str, context_mode: str,
                           max_tokens: int, risk: dict, *, actual_format_error: bool,
                           finish_reason: str = "", error_kind: str = "",
                           recovery_action: str = "none") -> None:
    """影子台账失败不能影响真实评审。"""
    try:
        storage.record_output_risk_observation(
            app, task["task_id"], task["project_id"], phase,
            document_id=document.get("document_id"), context_mode=context_mode,
            input_chars=risk.get("input_chars") or 0,
            rule_count=risk.get("rule_count") or 0,
            requested_max_tokens=max_tokens,
            predicted_risk_score=risk.get("score") or 0,
            shadow_split_recommended=bool(risk.get("recommend_split")),
            actual_format_error=actual_format_error,
            actual_finish_reason=finish_reason,
            actual_error_kind=error_kind,
            recovery_action=recovery_action,
        )
    except Exception:
        traceback.print_exc()


def _run_full_scan_piece(app, task: dict, profile: dict, document: dict, catalog: list[dict], chunk: dict,
                          project_scope: dict, system_prompt: str, depth: int = 0) -> tuple[dict, int, int, list[dict]]:
    """扫描一个连续页块；只在输出异常时拆分规则目录，绝不递归重发全文。"""
    allowed_ids = {item["id"] for item in catalog}
    # 首轮只输出紧凑候选索引。24 条短证据的预算足以闭合 JSON，又比旧的 36 条
    # 结构显著减少长度截断；最终结论仍读取本地全文片段，不以此处的条数代替覆盖率。
    max_tokens = _output_token_budget(profile, min(3_200, max(1_800, 900 + len(catalog) * 48)))
    output_risk = _full_scan_output_risk(catalog, chunk, max_tokens)
    context_mode = f"full_scan:{chunk['chunk_id']}"

    def findings_from(parsed: object) -> dict:
        values = parsed.get("matches") if isinstance(parsed, dict) else None
        if values is None and isinstance(parsed, dict):
            values = parsed.get("findings")  # 兼容用户尚未重置的旧自定义模板。
        if not isinstance(values, list):
            raise ValueError("模型返回格式不符合全文扫描要求")
        anomalies = parsed.get("scope_anomalies") if isinstance(parsed, dict) else []
        # 旧版用户自定义提示词不含该字段时仍可继续完成规则审查。
        if anomalies is None:
            anomalies = []
        if not isinstance(anomalies, list):
            raise ValueError("模型返回的项目范围候选格式不正确")
        return {
            "findings": _normalise_scan_findings(values, allowed_ids, chunk),
            "scope_anomalies": _normalise_scope_anomalies(anomalies, chunk),
        }

    format_error: ValueError | None = None
    try:
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_full_scan", system_prompt,
            _full_scan_prompt(app, document, catalog, chunk, project_scope, compact=False),
            document_id=document["document_id"], context_mode=context_mode,
            max_tokens=max_tokens, thinking_mode="disabled",
        )
        findings = findings_from(parsed)
        _record_full_scan_risk(
            app, task, document, "evaluate_all_full_scan", context_mode, max_tokens, output_risk,
            actual_format_error=False,
        )
        return findings, 0, 0, []
    except InvalidJsonResponse as exc:
        format_error = exc
        recovery_action = (
            "split_catalog" if exc.finish_reason.lower() in {"length", "max_tokens"}
            and len(catalog) > 12 and depth < 1 else
            "json_repair" if exc.finish_reason.lower() not in {"length", "max_tokens"} else
            "compact_retry"
        )
        _record_full_scan_risk(
            app, task, document, "evaluate_all_full_scan", context_mode, max_tokens, output_risk,
            actual_format_error=True, finish_reason=exc.finish_reason,
            error_kind=type(exc).__name__, recovery_action=recovery_action,
        )
        if exc.finish_reason.lower() not in {"length", "max_tokens"}:
            storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 扫描结果正在规范化")
            try:
                repaired = _repair_invalid_json(
                    app, task, profile, "evaluate_all_full_scan_json_repair", exc, "matches",
                    document_id=document["document_id"],
                )
                return findings_from(repaired), 1, 0, []
            except ValueError as repair_exc:
                if not _is_model_format_error(repair_exc) and not str(repair_exc).startswith("模型返回格式不符合全文扫描要求"):
                    raise
                format_error = repair_exc
    except ValueError as exc:
        if not _is_model_format_error(exc) and not str(exc).startswith("模型返回格式不符合全文扫描要求"):
            raise
        format_error = exc
        _record_full_scan_risk(
            app, task, document, "evaluate_all_full_scan", context_mode, max_tokens, output_risk,
            actual_format_error=True, error_kind=type(exc).__name__, recovery_action="compact_retry",
        )

    # 截断说明候选目录过密，直接拆目录比完整重发同一页块更快。
    if isinstance(format_error, InvalidJsonResponse) and format_error.finish_reason.lower() in {"length", "max_tokens"} and len(catalog) > 12 and depth < 1:
        midpoint = len(catalog) // 2
        storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 扫描输出达到上限，正在仅拆分规则目录")
        left = _run_full_scan_piece(app, task, profile, document, catalog[:midpoint], chunk, project_scope, system_prompt, depth + 1)
        right = _run_full_scan_piece(app, task, profile, document, catalog[midpoint:], chunk, project_scope, system_prompt, depth + 1)
        return {
            "findings": left[0]["findings"] + right[0]["findings"],
            "scope_anomalies": left[0]["scope_anomalies"] + right[0]["scope_anomalies"],
        }, left[1] + right[1], left[2] + right[2] + 1, left[3] + right[3]

    storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 全文扫描正在按紧凑结构继续")
    try:
        compact_context_mode = f"full_scan_compact:{chunk['chunk_id']}"
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_full_scan_compact_retry", system_prompt,
            _full_scan_prompt(app, document, catalog, chunk, project_scope, compact=True),
            document_id=document["document_id"], context_mode=compact_context_mode,
            max_tokens=max_tokens, thinking_mode="disabled",
        )
        findings = findings_from(parsed)
        _record_full_scan_risk(
            app, task, document, "evaluate_all_full_scan_compact_retry", compact_context_mode,
            max_tokens, output_risk, actual_format_error=False,
        )
        return findings, 1, 0, []
    except ValueError as retry_exc:
        if not _is_model_format_error(retry_exc) and not str(retry_exc).startswith("模型返回格式不符合全文扫描要求"):
            raise
        _record_full_scan_risk(
            app, task, document, "evaluate_all_full_scan_compact_retry",
            f"full_scan_compact:{chunk['chunk_id']}", max_tokens, output_risk,
            actual_format_error=True,
            finish_reason=retry_exc.finish_reason if isinstance(retry_exc, InvalidJsonResponse) else "",
            error_kind=type(retry_exc).__name__,
            recovery_action="split_catalog" if len(catalog) > 12 and depth < 1 else "failed_chunk",
        )
        if len(catalog) > 12 and depth < 1:
            midpoint = len(catalog) // 2
            storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 扫描仍异常，正在仅拆分规则目录")
            left = _run_full_scan_piece(app, task, profile, document, catalog[:midpoint], chunk, project_scope, system_prompt, depth + 1)
            right = _run_full_scan_piece(app, task, profile, document, catalog[midpoint:], chunk, project_scope, system_prompt, depth + 1)
            return {
                "findings": left[0]["findings"] + right[0]["findings"],
                "scope_anomalies": left[0]["scope_anomalies"] + right[0]["scope_anomalies"],
            }, left[1] + right[1] + 1, left[2] + right[2] + 1, left[3] + right[3]
        # 不能再拆分时不静默丢页：最终复核会把此页块原文发送给相关规则组。
        return {"findings": [], "scope_anomalies": []}, 1, 0, [{**chunk, "scan_error": str(retry_exc)[:300]}]


def _scan_document_fulltext(app, task: dict, profile: dict, document: dict, rules: list[dict],
                            project_scope: dict, system_prompt: str, *,
                            progress_offset: int = 0, progress_total: int = 1,
                            progress_callback=None) -> dict | None:
    if not rules:
        return None
    try:
        text_length = int(document.get("text_length") or 0)
    except (TypeError, ValueError):
        text_length = 0
    if text_length <= 0 and document.get("parsed_path"):
        text_length = len(Path(document["parsed_path"]).read_text(encoding="utf-8", errors="ignore"))
    if text_length <= FULL_SCAN_THRESHOLD_CHARS:
        return None
    chunks = _document_evidence_chunks(app, document)
    if not chunks:
        return None
    catalog = _full_scan_catalog(rules)
    scan_key = hashlib.sha256(json.dumps({
        "version": PROMPT_VERSION,
        # 全文扫描只绑定它实际使用的系统指令、扫描模板、规则目录和范围画像。
        # 这样修改最终评分/展示提示词时可复用已完成的全文证据扫描，不减少任何页块。
        # 强制重跑仍代表用户要求重新获得模型扫描判断，不能复用旧页块扫描结果。
        "force_run": task.get("task_id") if task.get("payload", {}).get("force_rerun") else None,
        "profile": profile.get("profile_id"), "model": profile.get("model_name"),
        "catalog": catalog, "project_scope": project_scope,
        "system": system_prompt,
        "template": storage.prompt_template(app, "evaluate_all_full_scan_user"),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    # 范围候选的稳定缓存不绑定评审规则目录。规则变化仍会重新生成规则证据，但不会
    # 让同一原文中已经发现的高价值范围线索消失；候选会在最终规则中重新判断。
    scope_scan_key = hashlib.sha256(json.dumps({
        "version": SCOPE_ANOMALY_CACHE_VERSION,
        "project_scope": project_scope,
        "scope_guidance": storage.prompt_template(app, "evaluate_all_scope_anomaly_guidance"),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    findings: list[dict] = []
    scope_anomalies: list[dict] = []
    failed_chunks: list[dict] = []
    compact_retry_count = split_retry_count = 0
    total = max(1, len(chunks))
    # 单份文件内页块最多 2 路并行：少家投标人项目只有 1-2 份文件并行时，这能让
    # 单文件内部的模型请求也重叠；总并发仍受任务请求闸门限制，不会超过服务商
    # 并发上限。结果按页块序号归并，保持原有顺序语义与断点缓存不变。
    per_chunk: dict[int, tuple[dict, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scan-chunk") as executor:
        futures: dict = {}
        for index, chunk in enumerate(chunks):
            chunk_hash = hashlib.sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest()
            checkpoint = storage.get_evaluation_scan_checkpoint(app, document["document_id"], scan_key, chunk["chunk_id"], chunk_hash)
            stable_scope = storage.get_evaluation_scan_checkpoint(
                app, document["document_id"], scope_scan_key, chunk["chunk_id"], chunk_hash,
            )
            stable_candidates = stable_scope.get("scope_anomalies", []) if isinstance(stable_scope, dict) else []
            historical_candidates = storage.previous_scope_anomalies(
                app, document["document_id"], chunk["chunk_id"], chunk_hash,
            )
            if checkpoint is not None:
                # 兼容 v3 已落库的纯 findings 检查点，避免升级时浪费一次扫描。
                if isinstance(checkpoint, list):
                    value = {"findings": checkpoint, "scope_anomalies": _merge_scope_anomalies([], stable_candidates + historical_candidates)}
                elif isinstance(checkpoint, dict):
                    value = {"findings": checkpoint.get("findings") or [], "scope_anomalies": _merge_scope_anomalies(
                        checkpoint.get("scope_anomalies") or [], stable_candidates + historical_candidates,
                    )}
                else:
                    value = {"findings": [], "scope_anomalies": []}
                per_chunk[index] = (value, [])
                continue
            futures[executor.submit(
                _run_full_scan_piece, app, task, profile, document, catalog, chunk, project_scope, system_prompt,
            )] = (index, chunk, chunk_hash, stable_candidates, historical_candidates)
        for future in as_completed(futures):
            index, chunk, chunk_hash, stable_candidates, historical_candidates = futures[future]
            result = future.result()
            merged_scope = _merge_scope_anomalies(
                result[0]["scope_anomalies"], stable_candidates + historical_candidates,
            )
            result[0]["scope_anomalies"] = merged_scope
            compact_retry_count += result[1]
            split_retry_count += result[2]
            if not result[3]:
                storage.save_evaluation_scan_checkpoint(
                    app, task["project_id"], document["document_id"], scope_scan_key,
                    chunk["chunk_id"], chunk_hash, {"scope_anomalies": merged_scope},
                )
                storage.save_evaluation_scan_checkpoint(
                    app, task["project_id"], document["document_id"], scan_key, chunk["chunk_id"], chunk_hash, result[0],
                )
            per_chunk[index] = (result[0], result[3])
    completed = 0
    for index in sorted(per_chunk):
        value, failed = per_chunk[index]
        completed += 1
        message = f"正在全文证据扫描 {document['bidder_name'] or document['original_name']}：{_full_scan_chunk_label(chunks[index])}（{completed}/{total}）"
        if progress_callback:
            progress_callback(message)
        else:
            progress = int((progress_offset + completed - 1) * 100 / max(1, progress_total))
            storage.update_task(app, task["task_id"], progress=progress, message=message)
        findings.extend(value["findings"])
        scope_anomalies.extend(value["scope_anomalies"])
        failed_chunks.extend(failed)
    return {
        "chunks": chunks,
        "findings": findings,
        "project_scope": project_scope,
        "scope_anomalies": scope_anomalies,
        "failed_chunks": failed_chunks,
        "chunk_count": len(chunks),
        "scan_batch_count": total,
        "compact_retry_count": compact_retry_count,
        "split_retry_count": split_retry_count,
    }


def _full_scan_chunk_count(document: dict) -> int:
    """返回需要 AI 全文证据扫描的页块数；只在任务启动时短暂读取解析文本。"""
    try:
        text_length = int(document.get("text_length") or 0)
    except (TypeError, ValueError):
        text_length = 0
    if text_length <= FULL_SCAN_THRESHOLD_CHARS or not document.get("parsed_path"):
        return 0
    return len(split_full_text_chunks(document["parsed_path"], FULL_SCAN_CHUNK_CHARS, overlap_pages=1))


def _scan_strategy(rules: list[dict]) -> str:
    """决定最终汇总优先携带哪类全文证据，不改变用户已确认的评审规则。"""
    strategies = {_rule_execution_strategy(item) for item in rules}
    if len(strategies) == 1:
        return next(iter(strategies))
    # 兼容异常调用方传入混合规则组；正常综合评审已在调用前按策略分组。
    for value in ("counting", "section", "consistency", "point"):
        if value in strategies:
            return value
    return "point"


_EVIDENCE_PACK_VERSION = "candidate-v2"
_ACQUISITION_PLAN_VERSION = "shadow-v1"


def _shadow_rule_fingerprint(rule: dict) -> str:
    """规则内容变化时让未来页级复用自然失效；当前影子包不参与任何决策。"""
    value = {
        "rule_id": rule.get("rule_id"), "title": rule.get("title"), "check_rule": rule.get("check_rule"),
        "source_text": rule.get("source_text"), "scoring": _rule_scoring(rule),
        "execution": storage.rule_execution_meta(rule),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _evidence_pack_material_key(rule: dict) -> str:
    """为可复用的“材料事实”生成保守键，不把不同规则的最终结论混在一起。

    键同时包含材料角色、规则要求的证据维度及若干长材料锚点。这样同一文件重跑时，
    例如同一张证书/检测报告可优先回到已确认页；相同的泛化词（如“材料”“证明”）
    则不会把无关规则错误关联。它只影响选页优先级，绝不复用分数、状态或理由。
    """
    try:
        requirements = storage.rule_execution_meta(rule).get("evidence_requirements") or []
    except (TypeError, ValueError):
        requirements = rule.get("evidence_requirements") or []
    roles = sorted(_rule_material_roles(rule))
    terms = [value for value in _rule_material_terms(rule) if len(value) >= 4][:6]
    if not roles and not terms:
        return ""
    value = {
        "roles": roles,
        "requirements": sorted(str(item) for item in requirements if str(item) in {"document", "field", "visual", "text"}),
        "terms": terms,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _with_evidence_pack_candidates(app, document: dict, rule: dict, result: dict) -> dict:
    """在同一文件重跑时优先放入已被直接取证的页；失败安全回退原候选链。"""
    key = _evidence_pack_material_key(rule)
    if not key or not document.get("sha256"):
        return result
    try:
        pages = storage.evidence_pack_pages(app, str(document.get("document_id") or ""), str(document.get("sha256") or ""), key)
    except Exception:
        return result
    page_count = int(document.get("page_count") or 0)
    pages = [page for page in _normalise_result_pages(pages) if not page_count or page <= page_count]
    if not pages:
        return result
    existing = _normalise_result_pages(result.get("evidence_pack_candidate_pages"))
    return {**result, "evidence_pack_candidate_pages": list(dict.fromkeys([*pages, *existing]))}


def _shadow_material_checklist(rule: dict) -> list[dict]:
    """从既有规则结构生成观察清单，不让模型自由生成材料需求并反向影响评审。"""
    try:
        evidence_items = storage.rule_execution_meta(rule).get("evidence_items") or []
    except (TypeError, ValueError):
        evidence_items = []
    # 规则提取阶段已明确保留的复合规则子项，优先作为观察分支。它们只是父规则
    # 的取证清单，不会成为独立规则、独立结论或独立评分。
    values = []
    for index, item in enumerate(evidence_items, start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("requirement") or "").strip()
        if not name:
            continue
        values.append({
            "material_id": str(item.get("item_id") or f"evidence_item_{index}")[:80],
            "name": name[:240], "criterion": str(item.get("requirement") or "").strip()[:500],
            "status": "unassessed",
        })
    if values:
        return values[:24]
    scoring = _rule_scoring(rule)
    items = scoring.get("items") if isinstance(scoring.get("items"), list) else []
    values = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        values.append({
            "material_id": f"score_item_{index}", "name": str(item.get("name") or "").strip()[:240],
            "criterion": str(item.get("criterion") or "").strip()[:500], "status": "unassessed",
        })
    if not values:
        values.append({
            "material_id": "rule_requirement", "name": str(rule.get("check_rule") or rule.get("title") or "规则要求")[:500],
            "criterion": "", "status": "unassessed",
        })
    return values[:24]


def _acquisition_required_fields(rule: dict) -> list[str]:
    """从规则及材料角色提炼取证字段，只用于影子计划与后续稳定性对比。

    不让模型在这里自由新增行业规则；字段仅描述常见材料核验所需的可见事实，
    具体满足与否仍由既有文字/OCR/图片链路和人工复核决定。"""
    roles = _rule_material_roles(rule)
    text = "\n".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    values: list[str] = []
    defaults = {
        "certificate": ("材料名称", "主体或产品", "编号", "日期或有效期"),
        "business_license": ("材料名称", "主体名称", "统一社会信用代码", "登记状态"),
        "identity": ("材料名称", "姓名", "证件号码"),
        "personnel": ("材料名称", "人员姓名", "资格或岗位"),
        "contract": ("合同主体", "项目名称", "日期", "金额或规模"),
        "response_form": ("表单名称", "对应项目", "填写内容"),
        "platform_screenshot": ("截图主体", "查询对象", "可见状态"),
    }
    for role in sorted(roles):
        for field in defaults.get(role, ()):
            if field not in values:
                values.append(field)
    field_terms = (
        ("型号", "型号"), ("品牌", "品牌"), ("编号", "编号"), ("有效期", "有效期"),
        ("日期", "日期"), ("金额", "金额"), ("数量", "数量"), ("签字", "签字"),
        ("签章", "签章"), ("盖章", "盖章"), ("勾选", "勾选"), ("印章", "印章"),
    )
    for term, field in field_terms:
        if term in text and field not in values:
            values.append(field)
    return values[:10] or ["规则相关材料"]


def _acquisition_plan_channel_order(rule: dict) -> list[str]:
    """生成通用通道路由建议；影子阶段绝不影响实际调用顺序。"""
    try:
        meta = storage.rule_execution_meta(rule)
    except (TypeError, ValueError):
        meta = {}
    preset = str(meta.get("acquisition_preset") or "smart")
    if preset == "text":
        return ["ocr"]
    if preset == "visual":
        return ["vision"]
    if preset == "dual":
        return ["ocr", "vision"]
    if preset == "off":
        return []
    requirements = set(meta.get("evidence_requirements") or [])
    if requirements & {"document", "field"}:
        return ["ocr", "vision"] if "visual" in requirements else ["ocr"]
    if "visual" in requirements:
        return ["vision"]
    if str(rule.get("check_mode") or "") == "ocr":
        return ["ocr"]
    return []


def _build_shadow_acquisition_plan(document: dict, rule: dict, result: dict) -> dict:
    """构造只读取证计划，观察“材料—字段—候选页—停止条件”的完整链路。

    该计划不会修改 result，不会改变 OCR/多模态选页，更不会参与评分或状态合并。
    它是第三轮受控启用前的可审计基线。"""
    try:
        meta = storage.rule_execution_meta(rule)
    except (TypeError, ValueError):
        meta = {}
    channels = _acquisition_plan_channel_order(rule)
    candidates = _normalise_result_pages(_vision_page_candidates(document, rule, result)) if channels else []
    checklist = _shadow_material_checklist(rule)
    branches = []
    for item in checklist[:12]:
        branches.append({
            "material_id": str(item.get("material_id") or "rule_requirement")[:80],
            "name": _clean_model_text(item.get("name"))[:240],
            "required_fields": _acquisition_required_fields(rule),
            # 影子计划只建议轮转首选页，不重排当前主链路；候选不足时明确空缺。
            "candidate_pages": candidates[:2] if len(checklist) == 1 else candidates[len(branches) - 1:len(branches) + 1],
        })
    level = str(rule.get("vision_level") or meta.get("vision_level") or "off")
    page_budget = {
        "ocr": _OCR_PAGE_LIMITS.get(level, 0),
        "vision": int(_VISION_LEVEL_SETTINGS.get(level, {}).get("max_pages", 0)),
        "vision_followup": int(_VISION_LEVEL_SETTINGS.get(level, {}).get("followup_pages", 0)),
    }
    actual = {
        "ocr_checked_pages": _shadow_pages(result.get("ocr_candidate_pages")),
        "ocr_evidence_pages": _shadow_pages(result.get("ocr_evidence_pages")),
        "vision_checked_pages": _shadow_pages(result.get("vision_pages")),
        "vision_evidence_pages": _shadow_pages(result.get("vision_evidence_pages")),
        "vision_status": str(result.get("vision_status") or "not_requested"),
    }
    return {
        "version": _ACQUISITION_PLAN_VERSION,
        "decision_participation": False,
        "preset": str(meta.get("acquisition_preset") or "smart"),
        "channels": channels,
        "coverage_level": level,
        "page_budget": page_budget,
        "candidate_pages": candidates[:16],
        "material_branches": branches,
        "stop_condition": "每个材料分支均已获得所需关键字段，或已穷尽可信候选页",
        "actual_execution": actual,
    }


def _shadow_pages(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    pages: list[int] = []
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 0:
            page = int(value)
            if page not in pages:
                pages.append(page)
    return pages


def _build_shadow_evidence_pack(task: dict, document: dict, component: str, rule: dict,
                                result: dict, scan_index: dict | None) -> dict:
    """构造只读 EvidencePack：完整记录当前路径，却绝不改变当前路径。"""
    scan = scan_index if isinstance(scan_index, dict) else {}
    rule_id = str(rule.get("rule_id") or "")
    chunks = {str(item.get("chunk_id") or ""): item for item in scan.get("chunks", []) if isinstance(item, dict)}
    findings: list[dict] = []
    for item in scan.get("findings", []) if isinstance(scan.get("findings"), list) else []:
        if not isinstance(item, dict) or str(item.get("rule_id") or "") != rule_id:
            continue
        chunk_id = str(item.get("chunk_id") or "").split(".", 1)[0]
        chunk = chunks.get(chunk_id, {})
        findings.append({
            "chunk_id": chunk_id,
            "page_range": [chunk.get("start_page"), chunk.get("end_page")],
            "evidence": _clean_model_text(item.get("evidence"))[:600],
            "tentative_status": str(item.get("tentative_status") or "")[:40],
            "priority": str(item.get("evidence_priority") or "")[:24],
            "confidence": str(item.get("confidence") or "")[:24],
        })
    findings = findings[:12]
    provenance: list[dict] = []
    seen_provenance: set[tuple[str, int | None, str]] = set()

    def add_page(source: str, page: int | None = None, *, detail: str = "", page_range: list | None = None) -> None:
        key = (source, page, detail)
        if key in seen_provenance:
            return
        seen_provenance.add(key)
        item = {"source": source}
        if page is not None:
            item["page"] = page
        if page_range:
            item["page_range"] = page_range
        if detail:
            item["detail"] = detail[:360]
        provenance.append(item)

    ledger = scan.get("evidence_ledger", {}) if isinstance(scan.get("evidence_ledger"), dict) else {}
    for candidate in (ledger.get(rule_id) or {}).get("candidates", [])[:8]:
        if not isinstance(candidate, dict):
            continue
        chunk = chunks.get(str(candidate.get("chunk_id") or ""), {})
        add_page(
            f"fulltext_{str(candidate.get('source') or 'scan')}", detail=str(candidate.get("anchor") or candidate.get("evidence") or ""),
            page_range=[chunk.get("start_page"), chunk.get("end_page")],
        )
    for field, source in (
        ("visual_page_candidates", "runtime_candidate"),
        ("ocr_candidate_pages", "ocr_candidate"),
        ("ocr_evidence_pages", "ocr_evidence"),
        ("vision_pages", "image_checked"),
        ("vision_evidence_pages", "image_evidence"),
    ):
        for page in _shadow_pages(result.get(field)):
            add_page(source, page)
    ocr_findings: list[dict] = []
    vision_findings: list[dict] = []
    for layer in result.get("evidence_layers") if isinstance(result.get("evidence_layers"), list) else []:
        if not isinstance(layer, dict):
            continue
        source = str(layer.get("source") or "")
        target = ocr_findings if source in {"tencent_ocr", "local_ocr"} else vision_findings if source == "vision" else None
        if target is None:
            continue
        target.append({
            "summary": _clean_model_text(layer.get("summary"))[:800],
            "checked_pages": _shadow_pages(layer.get("checked_pages")),
            "evidence_pages": _shadow_pages(layer.get("evidence_pages")),
            "service": str(layer.get("service") or "")[:160], "model": str(layer.get("model") or "")[:160],
        })
    snapshot = {
        "status": result.get("status"), "suggested_score": result.get("suggested_score"),
        "max_score": result.get("max_score"), "risk_level": result.get("risk_level"),
        "confidence": result.get("confidence"), "vision_status": result.get("vision_status"),
        "page_hint": result.get("page_hint"), "evidence": _clean_model_text(result.get("evidence"))[:900],
        "reason": _clean_model_text(result.get("reason"))[:700],
    }
    material_key = _evidence_pack_material_key(rule)
    payload = {
        "pack_version": _EVIDENCE_PACK_VERSION, "mode": "candidate_pages_only", "decision_participation": False,
        "document_sha256": str(document.get("sha256") or ""), "rule_id": rule_id, "component": component,
        "material_key": material_key,
        "text_findings": findings, "ocr_findings": ocr_findings[:4], "vision_findings": vision_findings[:4],
        "material_checklist": _shadow_material_checklist(rule), "page_provenance": provenance[:80],
        "acquisition_plan": _build_shadow_acquisition_plan(document, rule, result),
        "result_snapshot": snapshot,
    }
    return {"rule_id": rule_id, "component": component, "rule_fingerprint": _shadow_rule_fingerprint(rule),
            "material_key": material_key, "payload": payload}


def _build_rule_evidence_ledger(scan: dict, rules: list[dict]) -> dict[str, dict]:
    """将全文扫描和本地召回合成为逐规则证据账本。

    账本只在任务运行期间保存，不重复落入数据库或常驻内存；它把“模型首轮命中”
    和“本地可复核命中”一起交给最终组，避免任何一种来源单独失效时丢失证据。
    """
    chunks = scan.get("chunks", []) if isinstance(scan, dict) else []
    local_matches = select_rule_chunk_evidence_map(chunks, rules, per_rule=6)
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    polarity_rank = {"contradicts": 0, "partial": 1, "suspected": 2, "supports": 3}
    origin_rank = {"bidder_design": 0, "bidder_commitment": 1, "unknown": 2, "tender_quote": 4, "form_template": 5}
    findings_by_rule: dict[str, list[dict]] = {}
    for finding in scan.get("findings", []) if isinstance(scan, dict) else []:
        rule_id = str(finding.get("rule_id") or "")
        if rule_id:
            findings_by_rule.setdefault(rule_id, []).append(finding)
    ledger: dict[str, dict] = {}
    for rule in rules:
        rule_id = str(rule.get("rule_id") or "")
        candidates: list[dict] = []
        seen: set[str] = set()
        values = sorted(
            findings_by_rule.get(rule_id, []),
            key=lambda item: (
                priority_rank.get(item.get("evidence_priority"), 1),
                origin_rank.get(item.get("observation"), 2),
                polarity_rank.get(item.get("tentative_status"), 2),
                -{"high": 3, "medium": 2, "low": 1}.get(item.get("confidence"), 2),
            ),
        )
        for finding in values:
            chunk_id = str(finding.get("chunk_id") or "").split(".", 1)[0]
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            candidates.append({
                "chunk_id": chunk_id, "offset": 0, "source": "scan",
                "evidence": str(finding.get("evidence") or ""),
                "priority": finding.get("evidence_priority") or "medium",
            })
            if len(candidates) >= 6:
                break
        for match in local_matches.get(rule_id, []):
            chunk_id = str(match.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            candidates.append({
                "chunk_id": chunk_id, "offset": max(0, int(match.get("offset") or 0)),
                "source": "local", "anchor": str(match.get("anchor") or ""),
            })
            if len(candidates) >= 6:
                break
        ledger[rule_id] = {"strategy": _rule_execution_strategy(rule), "candidates": candidates}
    return ledger


_SCOPE_RULE_MARKERS = (
    "项目范围", "范围无关", "无关内容", "无关信息", "无关技术", "无关项目",
    "项目无关", "范围偏离", "范围一致", "混包", "其他项目",
)


def _is_scope_consistency_rule(rule: dict) -> bool:
    text = f"{rule.get('title') or ''} {rule.get('check_rule') or ''}"
    return any(marker in text for marker in _SCOPE_RULE_MARKERS) or (
        "全文" in text and "无关" in text
    ) or ("本项目" in text and "不一致" in text)


def _full_scan_review_context(scan: dict, rules: list[dict], char_limit: int, *, targeted: bool = False) -> dict:
    rule_ids = {item["rule_id"] for item in rules}
    findings = [item for item in scan.get("findings", []) if item.get("rule_id") in rule_ids]
    # 相同规则在同一页块的重复短摘录不重复送入最终模型；数量类保留不同项目名称，
    # 由最终评分阶段统一去重并解释其有效性。
    unique_findings, seen = [], set()
    for item in findings:
        signature = (item.get("rule_id"), item.get("chunk_id"), re.sub(r"\s+", "", str(item.get("evidence") or ""))[:160])
        if signature in seen:
            continue
        seen.add(signature)
        unique_findings.append(item)
    findings = unique_findings
    strategy = _scan_strategy(rules)
    # 先为每条规则保留一条最直接的证据，再按诊断价值补充其余页面。旧实现按
    # 扫描顺序加入，文档前部的普通命中会挤掉后部更有力的反证或计分材料。
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    polarity_rank = {"contradicts": 0, "partial": 1, "suspected": 2, "supports": 3}
    # 招标文件复述和表格模板不是投标人自主证明。仍保留它们以便最终模型判断
    # “只是复述”，但绝不能挤掉同一规则的真实方案、承诺或反证。
    origin_rank = {"bidder_design": 0, "bidder_commitment": 1, "unknown": 2, "tender_quote": 4, "form_template": 5}
    ranked_findings = sorted(
        findings,
        key=lambda item: (
            priority_rank.get(item.get("evidence_priority"), 1),
            origin_rank.get(item.get("observation"), 2),
            polarity_rank.get(item.get("tentative_status"), 2),
            -{"high": 3, "medium": 2, "low": 1}.get(item.get("confidence"), 2),
        ),
    )
    best_by_rule: dict[str, dict] = {}
    for finding in ranked_findings:
        best_by_rule.setdefault(str(finding.get("rule_id") or ""), finding)
    # 首轮 AI 候选优先，其次用本地章节词加强召回；失败页块始终进入复核。
    selected_ids: list[str] = []
    for rule in rules:
        finding = best_by_rule.get(str(rule.get("rule_id") or ""))
        if not finding:
            continue
        chunk_id = str(finding.get("chunk_id") or "")
        root_id = chunk_id.split(".", 1)[0]
        if root_id and root_id not in selected_ids:
            selected_ids.append(root_id)
    for finding in ranked_findings:
        chunk_id = str(finding.get("chunk_id") or "")
        root_id = chunk_id.split(".", 1)[0]
        if root_id and root_id not in selected_ids:
            selected_ids.append(root_id)
    # 缺失规则补评只需要最直接的证据页；首次综合评审保持原有全文级证据覆盖，
    # 以准确性优先。这样不会再为一条漏回规则重发约 6 万字上下文。
    if targeted:
        # 漏回规则通常只需重发直接证据，但复合评分/数量累计规则可能跨多个章节；
        # 按规则结构放宽到最多 6 个页块，不能以“补评”为由只给一页而破坏完整计分。
        max_items = max((
            len((_rule_scoring(rule).get("items") or []))
            for rule in rules
        ), default=0)
        per_rule = min(6, max(2, max_items)) if strategy in {"counting", "section"} else 2
    else:
        per_rule = 6 if strategy in {"counting", "section"} else 4
    for chunk_id in select_rule_chunks(scan.get("chunks", []), rules, per_rule=per_rule):
        if chunk_id not in selected_ids:
            selected_ids.append(chunk_id)
    review_categories = {"qualification", "compliance", "substantive", "rejection", "other"}
    is_review_group = any(item.get("category") in review_categories for item in rules)
    has_scope_rule = any(_is_scope_consistency_rule(item) for item in rules)
    scope_anomalies, seen_scope = [], set()
    for item in scan.get("scope_anomalies", []):
        signature = (
            item.get("chunk_id"), item.get("dimension"),
            re.sub(r"\s+", "", str(item.get("evidence") or ""))[:180],
        )
        if signature in seen_scope:
            continue
        seen_scope.add(signature)
        scope_anomalies.append(item)
    scan_scope = scan.get("project_scope") if isinstance(scan.get("project_scope"), dict) else {}
    tender_baseline = scan_scope.get("_tender_scope_baseline")
    if tender_baseline:
        scope_anomalies = [
            item for item in scope_anomalies
            if not _scope_candidate_matches_tender_material(item, tender_baseline)
        ]
    scope_anomalies.sort(key=lambda item: priority_rank.get(item.get("candidate_priority"), 1))
    if is_review_group and has_scope_rule:
        # 该通道不依赖“地区、项目名”等固定关键词：任何范围偏离候选的原页都会
        # 进入审查组，最终是否构成问题仍完全由 AI 结合规则和原文判断。
        anomaly_ids = []
        # 每条候选同时需要保留原页；过多的摘要会挤掉原文，故保留优先级最高的 12 条。
        for item in scope_anomalies[:(4 if targeted else 12)]:
            root_id = str(item.get("chunk_id") or "").split(".", 1)[0]
            if root_id and root_id not in anomaly_ids:
                anomaly_ids.append(root_id)
        for chunk_id in reversed(anomaly_ids):
            if chunk_id in selected_ids:
                selected_ids.remove(chunk_id)
            selected_ids.insert(0, chunk_id)
    failed_root_ids = []
    for chunk in scan.get("failed_chunks", []):
        root_id = str(chunk.get("chunk_id") or "").split(".", 1)[0]
        if root_id and root_id not in failed_root_ids:
            failed_root_ids.append(root_id)
        if root_id and root_id not in selected_ids:
            selected_ids.insert(0, root_id)
    # 最终组不需要重发首轮记录的所有内部字段。证据目录按规则轮转保留首条，再填充
    # 第二、三条，避免全局高优先级结果挤掉同组靠后规则的唯一证据。
    by_rule_findings: dict[str, list[dict]] = {}
    for item in ranked_findings:
        rule_id = str(item.get("rule_id") or "")
        if rule_id:
            by_rule_findings.setdefault(rule_id, []).append(item)
    compact_findings: list[dict] = []
    max_rounds = max((len(values) for values in by_rule_findings.values()), default=0)
    for round_index in range(max_rounds):
        for rule in rules:
            values = by_rule_findings.get(str(rule.get("rule_id") or ""), [])
            if round_index >= len(values):
                continue
            item = values[round_index]
            compact_findings.append({
                "rule_id": item.get("rule_id"), "page": item.get("page_hint") or item.get("page_range"),
                "evidence": item.get("evidence"), "status": item.get("tentative_status"),
                "priority": item.get("evidence_priority"), "evidence_origin": item.get("observation"),
            })
    scope_packet = ""
    if is_review_group and has_scope_rule:
        scope_packet = (
            "\n\n【项目范围画像（来自招标文件和已确认规则）】\n"
            + json.dumps(_scope_prompt_profile(scan.get("project_scope", {})), ensure_ascii=False, separators=(",", ":"))
            + "\n\n【项目范围偏离候选（仅供结合原页和规则核验，不是既成结论）】\n"
            + json.dumps(scope_anomalies[:(4 if targeted else 12)], ensure_ascii=False, separators=(",", ":"))
        )
    header = (
        f"【全文覆盖说明】已按连续页块扫描全文，共 {scan.get('chunk_count', 0)} 个页块；本规则组采用{strategy}汇总策略；"
        f"首轮为当前规则组报告 {len(findings)} 条候选证据。"
        "首轮未报告候选不等于技术失败，应结合规则给出‘全文扫描未发现’或其他最可能建议。"
    )
    if failed_root_ids:
        header += f"有 {len(failed_root_ids)} 个首轮格式异常页块，下面已附原文供本轮直接复核。"
    # 证据目录最多占 30%，且始终保留有效 JSON；原页至少获得约三分之二的上下文预算。
    prefix_budget = min(max(3_200, char_limit // 3), max(3_200, int(char_limit * 0.30)))
    static_prefix = f"{header}{scope_packet}\n\n【首轮 AI 候选证据】\n"
    if len(static_prefix) > prefix_budget:
        # 范围画像只是辅助线索；它过大时优先保留总览和候选原文，不能挤掉文件原页。
        static_prefix = f"{header}\n\n【首轮 AI 候选证据】\n"
    findings_budget = max(300, prefix_budget - len(static_prefix) - len("\n\n【重点原文】\n"))
    selected_finding_values: list[dict] = []
    findings_size = 2
    for item in compact_findings:
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if selected_finding_values and findings_size + len(encoded) + 1 > findings_budget:
            break
        selected_finding_values.append(item)
        findings_size += len(encoded) + 1
    findings_packet = json.dumps(selected_finding_values, ensure_ascii=False, separators=(",", ":"))
    prefix = f"{static_prefix}{findings_packet}\n\n【重点原文】\n"
    chunks_by_id = {str(item.get("chunk_id")): item for item in scan.get("chunks", [])}
    parts = [prefix]
    size = len(prefix)
    included = []
    # 每条规则先分到一个直接候选页块；未命中时分到本地章节检索页块。这样单个规则的
    # 原页不会被前面规则的大段材料饿死，最终模型仍可基于真实全文片段作判断。
    rule_evidence: dict[str, list[str]] = {}
    required_ids: list[str] = []
    ledger = scan.get("evidence_ledger", {}) if isinstance(scan.get("evidence_ledger"), dict) else {}
    fallback_by_rule = select_rule_chunk_evidence_map(scan.get("chunks", []), rules, per_rule=1)
    fallback_offsets: dict[str, int] = {}
    for rule in rules:
        rule_id = str(rule.get("rule_id") or "")
        finding = best_by_rule.get(rule_id)
        chunk_id = str((finding or {}).get("chunk_id") or "").split(".", 1)[0]
        if not chunk_id:
            ledger_candidate = next(iter((ledger.get(rule_id) or {}).get("candidates", [])), {})
            fallback = ledger_candidate or next(iter(fallback_by_rule.get(rule_id, [])), {})
            chunk_id = str(fallback.get("chunk_id") or "")
            if chunk_id:
                fallback_offsets[chunk_id] = int(fallback.get("offset") or 0)
        if not chunk_id:
            continue
        if finding and finding.get("evidence"):
            rule_evidence.setdefault(chunk_id, []).append(str(finding.get("evidence")))
        if chunk_id not in required_ids:
            required_ids.append(chunk_id)
    ordered_ids = required_ids + [item for item in selected_ids if item not in required_ids]

    def source_excerpt(chunk: dict, evidence_values: list[str], budget: int, fallback_offset: int | None = None) -> str:
        source = str(chunk.get("text") or "")
        if len(source) <= budget:
            return source
        offset = max(0, int(fallback_offset or 0))
        for evidence in evidence_values:
            fragments = sorted(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{6,}", evidence), key=len, reverse=True)
            for fragment in fragments:
                found = source.find(fragment)
                if found >= 0:
                    offset = found
                    break
            if offset:
                break
        start = max(0, offset - max(240, budget // 3))
        end = min(len(source), start + budget)
        start = max(0, end - budget)
        return source[start:end]

    for position, chunk_id in enumerate(ordered_ids):
        chunk = chunks_by_id.get(chunk_id)
        if not chunk:
            continue
        remaining = char_limit - size
        if remaining <= 0:
            break
        required_remaining = sum(1 for value in ordered_ids[position:] if value in required_ids)
        # 已为同组所有规则选中的页块预留公平配额；额外页只使用最后的剩余空间。
        fair_share = max(1_600, remaining // max(1, required_remaining)) if chunk_id in required_ids else remaining
        body_budget = max(300, min(remaining - 40, fair_share - 40))
        if body_budget <= 0:
            break
        body = source_excerpt(chunk, rule_evidence.get(chunk_id, []), body_budget, fallback_offsets.get(chunk_id))
        piece = f"\n\n【{_full_scan_chunk_label(chunk)}】\n{body}"
        if len(piece) > remaining:
            piece = piece[:remaining]
        parts.append(piece)
        size += len(piece)
        included.append(chunk_id)
    return {
        "text": "".join(parts),
        "mode": "full_scan_evidence",
        "pages": included,
        "unmatched_rule_ids": [],
    }


def _combined_batch_results(component: str, output: object, rules: list[dict], payload: list[dict],
                            tender_baseline: object = "") -> list[dict]:
    if component == "review":
        return _normalise_review_results(output, rules, tender_baseline)
    return _normalise_score_results(output, payload, component)


def _combined_manual_results(component: str, rules: list[dict], payload: list[dict], reason: str) -> list[dict]:
    """模型格式异常或无可用上下文时，仅将受影响规则标成人工核验。"""
    if component == "review":
        return [
            _review_result_from_model(
                {"reason": reason, "risk_level": "low", "confidence": "low", "evidence_quality": "missing"},
                rule["rule_id"], "ocr_required" if _rule_requires_visual_verification(rule) else "manual",
            )
            for rule in rules
        ]
    return _normalise_score_results(
        [{"rule_id": item["rule_id"], "reason": reason, "confidence": "low"} for item in payload],
        payload, component,
    )


def _is_minimax_m3_profile(profile: dict) -> bool:
    return str(profile.get("model_name") or "").lower() == "minimax-m3" and "api.minimaxi.com" in str(profile.get("base_url") or "").lower()


_VISION_LEVEL_SETTINGS = {
    # 强度控制清晰度和总页预算，不再等同于“机械截取前1/2/3页”。
    # 标准强度至少可一次覆盖三张并列证书，未覆盖时再补齐其余证据分支。
    # detail 是内部质量档位，不直接透传给服务商。协议适配器会将 standard 映射为
    # 当前服务商合法的参数，避免把 MiniMax 的 default 误发给其他兼容接口。
    "low": {"detail": "low", "max_pages": 2, "followup_pages": 0, "scale": 1.15, "quality": 72},
    "standard": {"detail": "standard", "max_pages": 4, "followup_pages": 4, "scale": 1.55, "quality": 82},
    "high": {"detail": "high", "max_pages": 6, "followup_pages": 6, "scale": 2.0, "quality": 88},
}


def _vision_parallel_limit() -> int:
    """视觉请求并发上限，默认 2 路。

    多份投标文件同时进入图片识别阶段时，2 路并发可明显缩短尾部等待；单份文件
    内部仍按规则顺序执行，因此单投标人项目不受影响。可用环境变量
    VISION_PARALLEL_LIMIT 在 1-4 之间调整。
    """
    try:
        requested = int(os.environ.get("VISION_PARALLEL_LIMIT", "2"))
    except (TypeError, ValueError):
        return 2
    return max(1, min(4, requested))


_VISION_REQUEST_GATE = threading.BoundedSemaphore(_vision_parallel_limit())
# RapidOCR/ONNX 的单个子进程峰值约 500-650 MB；2 核 2 GB 服务器上宁可让本地 OCR
# 排队，也不能让多份投标文件并行拉起多个模型进程。它只约束本地推理，不影响远端
# 文字或图片模型的动态并行。内存升级到 4 GB 以上后可设 LOCAL_OCR_MAX_WORKERS=2 放开一路。
_LOCAL_OCR_REQUEST_GATE = threading.BoundedSemaphore(local_ocr_max_workers())
_VISION_LOCATOR_THUMBNAILS_PER_SHEET = 12
_VISION_LOCATOR_SHEETS_PER_REQUEST = 6
_VISION_MAX_PIXELS_PER_PAGE = 10_000_000
# 仅作为压缩循环的停止阈值。预检本身允许更低的比例，以严格守住像素上限；
# 对超大图纸而言，优先返回较小但可用的图，而不是因最小比例导致瞬时内存峰值。
_VISION_MIN_RENDER_SCALE = 0.05
_VISION_TASK_RENDER_CACHE_BYTES = 24 * 1024 * 1024

_OCR_PAGE_LIMITS = {"low": 2, "standard": 6, "high": 10}
_OCR_ROUTE_TABLE_TERMS = ("评分表", "业绩表", "参数表", "清单", "报价表", "人员表", "明细表", "统计表")
_OCR_ROUTE_ACCURATE_TERMS = ("证书", "许可证", "资质", "编号", "有效期", "长串数字", "审计", "社保", "纳税", "声明函")
_OCR_TEXT_TERMS = (
    "证书", "证件", "执照", "许可证", "资质", "编号", "有效期", "日期", "金额", "姓名",
    "地址", "身份证", "社保", "纳税", "审计", "声明函", "表格", "清单", "截图文字",
)
_VISION_FACT_TERMS = (
    "签字", "签章", "盖章", "印章", "骑缝章", "勾选", "手写", "照片", "截图", "二维码",
    "外观", "版式", "公章", "复印件", "扫描件", "原件",
)


def _rule_evidence_requirements(rule: dict) -> list[str]:
    """统一读取新旧规则的证据需求，避免各链路各自解释元数据。"""
    try:
        meta = storage.rule_execution_meta(rule)
    except (TypeError, ValueError):
        meta = {}
    values = rule.get("evidence_requirements")
    if not isinstance(values, list):
        values = meta.get("evidence_requirements") or []
    return [str(value) for value in values if str(value) in {"text", "document", "field", "visual"}]


def _local_ocr_baseline_required(rule: dict, result: dict, component: str = "review") -> bool:
    """判断规则是否需要本地 OCR 基础取证，不受“增强核验”开关影响。

    基础能力不等于每条规则都调用：只有明确要求 OCR，或文字结论已显示证据不足
    （包括扫描型文件经守卫回落）时才处理有限候选页。这样不会把“材料/字段”这类
    普通文字规则仅因名称相符就重复 OCR，仍不扩张成全文逐页 OCR。
    """
    try:
        baseline_mode = str(storage.rule_execution_meta(rule).get("baseline_ocr_mode") or "auto")
    except (TypeError, ValueError):
        baseline_mode = str(rule.get("baseline_ocr_mode") or "auto")
    if baseline_mode == "text_only":
        return False
    if baseline_mode == "local_ocr":
        return True
    if bool(rule.get("ocr_required")) or str(rule.get("check_mode") or "") == "ocr":
        return True
    # 文字证据已足以支撑当前结论时，材料名称、字段名称或视觉元数据只用于将来
    # 的增强核验，不额外消耗本地 OCR CPU；扫描件/缺证据会在此处继续向下判断。
    if not _needs_visual_fallback(component, result):
        return False
    requirements = set(_rule_evidence_requirements(rule))
    if requirements & {"document", "field", "visual"}:
        return True
    if _rule_material_roles(rule):
        return True
    if _rule_image_strategy(rule) in {"ocr", "hybrid"}:
        return True
    return False


def _visual_advance_estimated(rule: dict) -> bool:
    """估算视觉取证循环中会产生进度推进的规则（静态上界）。

    本地 OCR 基础化后，启动方式为关闭的规则也可能因基础 OCR 而推进进度；分母
    只按增强规则估算会让进度提前封顶 100%，剩余 OCR 批次仍在刷新中间态文案。
    此处按规则元数据取静态上界：增强规则必推进；auto 基础模式是否真正 OCR
    取决于运行时文字充分性，按“可能执行”计入。宁可进度略滞后，也不可提前
    到 100%。
    """
    trigger, level = _rule_vision_policy(rule)
    if trigger != "off" and level != "off":
        return True
    try:
        baseline_mode = str(storage.rule_execution_meta(rule).get("baseline_ocr_mode") or "auto")
    except (TypeError, ValueError):
        baseline_mode = str(rule.get("baseline_ocr_mode") or "auto")
    if baseline_mode == "text_only":
        return False
    if baseline_mode == "local_ocr":
        return True
    if bool(rule.get("ocr_required")) or str(rule.get("check_mode") or "") == "ocr":
        return True
    requirements = set(_rule_evidence_requirements(rule))
    if requirements & {"document", "field", "visual"}:
        return True
    if _rule_material_roles(rule):
        return True
    return _rule_image_strategy(rule) in {"ocr", "hybrid"}


def _form_bundle_page_limit(rule: dict, level: str, pages: list[int], default_limit: int) -> int:
    """低强度固定表单也应完整覆盖紧邻的同一份表单，避免漏掉续页关键字段。

    只在候选队列的开头已经是三个连续页、且规则明确要求响应表单时放宽到三页；
    不扩展非表单规则，也不增加后续批次，因而不会把低档识别变成无边界的全文识图。
    """
    if level != "low" or "response_form" not in _rule_material_roles(rule) or len(pages) < 3:
        return default_limit
    first_three = pages[:3]
    if first_three == list(range(first_three[0], first_three[0] + 3)):
        return max(default_limit, 3)
    return default_limit


def _rule_image_strategy(rule: dict) -> str:
    """优先使用规则提取出的证据类型；旧规则才回退到文本关键词。"""
    requirements = set(_rule_evidence_requirements(rule))
    if "visual" in requirements and "text" in requirements:
        return "hybrid"
    if "visual" in requirements:
        # 旧版规则曾把 check_mode=ocr 只保存成 visual，导致关闭腾讯后直接走图片
        # 模型而跳过本地文字 OCR。兼容旧数据时将其恢复为混合取证：OCR 先给出可见
        # 文字事实，用户选 required 时仍会继续进行图片核验。
        if str(rule.get("check_mode") or "") == "ocr" or bool(rule.get("ocr_required")):
            return "hybrid"
        return "vision"
    if "text" in requirements:
        return "ocr"
    text = f"{rule.get('title') or ''}\n{rule.get('check_rule') or ''}\n{rule.get('source_text') or ''}"
    needs_text = any(term in text for term in _OCR_TEXT_TERMS)
    needs_visual = any(term in text for term in _VISION_FACT_TERMS)
    # 证书/证照类即使主要核对文字，也常同时依赖复印件外观、签章或查询截图。
    if any(term in text for term in ("证书", "证件", "执照", "许可证", "身份证")):
        needs_visual = True
    if needs_text and needs_visual:
        return "hybrid"
    if needs_text:
        return "ocr"
    return "vision"


def _rule_image_mode(rule: dict) -> str:
    """读取人工选择的取证通道；旧规则安全保持自动策略。"""
    try:
        value = storage.rule_execution_meta(rule).get("image_mode")
    except (TypeError, ValueError):
        value = rule.get("image_mode")
    mode = str(value or "auto")
    return mode if mode in {"auto", "ocr_only", "vision_only", "combined", "off"} else "auto"


def _uses_explicit_field_acquisition_plan(rule: dict) -> bool:
    """仅识别用户新配置过的“材料＋字段”取证规则。

    旧规则的 acquisition_preset 由读取时推导，不能据此改变其历史运行顺序；必须
    在 execution_meta_json 中实际保存过 preset，才允许第三轮计划器参与路由。"""
    raw = rule.get("execution_meta_json")
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict) or str(value.get("acquisition_preset") or "") not in {"smart", "text", "dual"}:
        return False
    requirements = {str(item) for item in value.get("evidence_requirements") or []}
    return bool(requirements & {"document", "field"})


def _ocr_service_candidates(rule: dict, level: str) -> list[str]:
    """按场景排列质量优先级；额度不足或接口不可用时自动顺延。"""
    text = f"{rule.get('title') or ''}\n{rule.get('check_rule') or ''}\n{rule.get('source_text') or ''}"
    if "营业执照" in text:
        return ["biz_license", "accurate", "basic", "fast", "efficient"]
    if any(term in text for term in _OCR_ROUTE_TABLE_TERMS):
        return ["table", "accurate", "basic", "fast", "efficient"]
    if level == "high" or any(term in text for term in _OCR_ROUTE_ACCURATE_TERMS):
        return ["accurate", "basic", "fast", "efficient"]
    if level == "low":
        return ["efficient", "fast", "basic", "accurate"]
    return ["fast", "basic", "efficient", "accurate"]


def _ocr_service_for_rule(rule: dict, level: str) -> str:
    """兼容既有测试/调用方，返回该场景的首选接口。"""
    return _ocr_service_candidates(rule, level)[0]


def _ocr_service_candidates_for_page(rule: dict, level: str, page_text: object) -> list[str]:
    """在规则级优先级上叠加页面类型，避免把非执照页送入营业执照专项接口。"""
    base = _ocr_service_candidates(rule, level)
    role = _page_material_role(page_text)
    text = _clean_model_text(page_text)
    if "biz_license" in base and role != "business_license":
        # 当前页面缺少营业执照/统一社会信用代码特征时，直接走通用链；
        # 不影响同一规则的其他候选页继续使用营业执照专项。
        return [service for service in base if service != "biz_license"]
    if role in {"certificate", "identity", "personnel", "platform_screenshot"}:
        preferred = ["accurate", "basic", "fast", "efficient"]
        return list(dict.fromkeys(preferred + base))
    if role == "contract" and any(term in text for term in _OCR_ROUTE_TABLE_TERMS):
        return list(dict.fromkeys(["table", "accurate", "basic", "fast", "efficient"] + base))
    return base


def _render_ocr_page(app, document: dict, page_number: int, service: str, task: dict | None = None) -> tuple[bytes, str] | None:
    """仅在内存中渲染候选页，并在 Pixmap 前限制超大页面内存。"""
    if document.get("extension") != ".pdf":
        return None
    # 同一页同一档位在同一任务内可能被多条规则重复选中；任务级缓存避免重复 fitz
    # 渲染占用 2 核 CPU。与图片模型链路共用同一容量上限，只缓存 JPEG 字节。
    task_cache = _task_vision_render_cache(task)
    cache_key = ("ocr", str(document.get("document_id") or ""), int(page_number), str(service))
    if task_cache:
        cache, lock = task_cache
        with lock:
            cached_content = cache["items"].get(cache_key)
        if cached_content is not None:
            return cached_content, hashlib.sha256(cached_content).hexdigest()
    source = storage.document_path(app, document)
    scale = 2.0 if service in {"accurate", "table", "biz_license"} else 1.5
    quality = 88 if scale >= 2 else 80
    try:
        with fitz.open(source) as pdf:
            if not 1 <= page_number <= pdf.page_count:
                return None
            page = pdf[page_number - 1]
            # 不要等 JPEG 压缩后才限尺寸：超大图纸在 get_pixmap 时就可能耗尽 2 GB
            # 容器内存。与图片模型链路共用相同的像素上限，保持 OCR/视觉行为一致。
            scale = _safe_vision_render_scale(page, scale)
            content = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes(
                "jpeg", jpg_quality=quality,
            )
            # 腾讯接口限制的是 Base64 后大小：营业执照 7M、通用/表格通常 10M。
            # 留出编码和协议余量，仅在超限时逐级降采样，避免白白消耗一次额度。
            raw_limit = 5 * 1024 * 1024 if service == "biz_license" else 7 * 1024 * 1024
            while len(content) > raw_limit and scale > 1.0:
                scale = max(1.0, scale * 0.8)
                quality = max(68, quality - 6)
                content = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes(
                    "jpeg", jpg_quality=quality,
                )
    except (OSError, RuntimeError, ValueError):
        return None
    if task_cache and len(content) <= 4 * 1024 * 1024:
        cache, lock = task_cache
        with lock:
            # FIFO 淘汰，与图片模型渲染缓存共用容量上限。
            while cache["items"] and cache["bytes"] + len(content) > _VISION_TASK_RENDER_CACHE_BYTES:
                oldest_key = next(iter(cache["items"]))
                removed = cache["items"].pop(oldest_key)
                cache["bytes"] -= len(removed)
            cache["items"][cache_key] = content
            cache["bytes"] += len(content)
    return content, hashlib.sha256(content).hexdigest()


def _ocr_response_coverage(value: dict) -> str:
    coverage = str(value.get("coverage") or "").lower()
    return coverage if coverage in {"covered", "not_covered", "uncertain"} else "uncertain"


def _ocr_response_scope(value: dict) -> str:
    scope = str(value.get("conclusion_scope") or "").lower()
    return scope if scope in {"full", "partial", "none"} else "partial"


def _ocr_candidate_pages(document: dict, rule: dict, result: dict, level: str) -> list[int]:
    pages = _prioritise_material_pages(
        document, rule, _acquisition_candidate_pages(document, rule, result),
        protected=result.get("ocr_evidence_pages"),
    )
    limit = _form_bundle_page_limit(
        rule, level, pages, _compound_acquisition_page_limit(rule, level, "ocr", _OCR_PAGE_LIMITS.get(level, 1)),
    )
    return pages[:limit]


def _rule_evidence_items(rule: dict) -> list[dict]:
    """读取已提取的复合规则子项；缺失时严格回退到原规则级链路。"""
    try:
        values = storage.rule_execution_meta(rule).get("evidence_items") or []
    except (TypeError, ValueError):
        values = []
    return [
        dict(item) for item in values
        if isinstance(item, dict) and str(item.get("name") or item.get("requirement") or "").strip()
    ]


def _evidence_item_rule(rule: dict, item: dict) -> dict:
    """以父规则为底座构造临时定位视图，不改变规则正文或评审语义。"""
    name = _clean_model_text(item.get("name"))[:180]
    requirement = _clean_model_text(item.get("requirement"))[:600]
    requirements = item.get("evidence_requirements")
    return {
        **rule,
        # 选页词应以子项本身为前缀。若把父规则的长篇总要求放在前面，通用片段会
        # 先耗尽本地材料词预算，反而压掉后部真正区分材料的叶子名称。
        "title": name or str(rule.get("title") or ""),
        "check_rule": requirement or str(rule.get("check_rule") or rule.get("title") or ""),
        "source_text": "；".join(value for value in (name, requirement, str(rule.get("source_text") or "")) if value),
        # 子项若明确证据类型，定位可以更贴近该材料；未声明时保留父规则全部维度。
        "evidence_requirements": requirements if isinstance(requirements, list) and requirements else rule.get("evidence_requirements"),
    }


def _compound_acquisition_plan(document: dict, rule: dict, result: dict) -> dict:
    """为一个复合规则按子项均衡候选页，旧规则返回空计划。

    此计划只改变 OCR/视觉的候选页顺序和受控上限；全文审查、结果合并、评分及已有
    页级缓存均不改写。因此子项元数据缺失、候选定位失败或旧规则均会自然退回原链路。
    """
    items = _rule_evidence_items(rule)
    if len(items) < 2:
        return {"items": [], "candidate_pages": []}
    base_pages = _vision_page_candidates(document, rule, result)
    branches: list[dict] = []
    for index, item in enumerate(items[:12], start=1):
        child_rule = _evidence_item_rule(rule, item)
        pages = _prioritise_material_pages(document, child_rule, _vision_page_candidates(document, child_rule, result))
        if not pages:
            pages = list(base_pages)
        branches.append({
            "item_id": str(item.get("item_id") or f"item_{index}")[:80],
            "name": _clean_model_text(item.get("name") or item.get("requirement"))[:180],
            "candidate_pages": pages[:12],
        })
    # 轮转而非“第一项吃完再看第二项”：即使总预算受限，每个独立材料也优先获得
    # 一个候选机会。随后接回父规则的通用候选，避免因子项定位不精确丢掉旧命中页。
    selected: list[int] = []
    for depth in range(12):
        progressed = False
        for branch in branches:
            pages = branch["candidate_pages"]
            if depth >= len(pages):
                continue
            progressed = True
            page = pages[depth]
            if page not in selected:
                selected.append(page)
        if not progressed:
            break
    for page in base_pages:
        if page not in selected:
            selected.append(page)
    return {"items": branches, "candidate_pages": selected}


def _acquisition_candidate_pages(document: dict, rule: dict, result: dict) -> list[int]:
    """在复合规则存在明确子项时使用均衡候选；否则逐字保留原候选链。"""
    plan = _compound_acquisition_plan(document, rule, result)
    if plan["candidate_pages"]:
        return plan["candidate_pages"]
    return _vision_page_candidates(document, rule, result)


def _compound_acquisition_page_limit(rule: dict, level: str, channel: str, default_limit: int) -> int:
    """给已结构化的独立子项少量公平配额，同时守住 2C/2G 的硬上限。"""
    count = len(_rule_evidence_items(rule))
    if count < 2 or level == "low":
        return default_limit
    if channel == "ocr":
        cap = {"standard": 8, "high": 12}.get(level, default_limit)
        per_item = 2
    else:
        # 图片首批保持小批量；标准档最多由4页增至6页，高档最多由6页增至8页，
        # 后续补页仍沿用既有设置，避免并发/内存行为发生突变。
        cap = {"standard": 6, "high": 8}.get(level, default_limit)
        per_item = 1
    return min(cap, max(default_limit, count * per_item))


def _ocr_discovery_page_count(rule: dict, level: str, pages: list[int]) -> int:
    """第一阶段只做足以判断“候选是否相关”的小批 OCR。

    计分累计、分段/表单规则不提前停止，避免少看一页导致少计材料；普通单一证照、
    声明或字段核验则先看 2–4 页，命中后无需把明显无关的尾页全部送去 OCR。
    """
    if level == "low":
        return min(len(pages), 2)
    scoring = _rule_scoring(rule)
    if (scoring.get("kind") == "manual" or scoring.get("items")
            or _rule_execution_strategy(rule) in {"counting", "section", "consistency"}
            or "response_form" in _rule_material_roles(rule)):
        return len(pages)
    return min(len(pages), 3 if level == "standard" else 4)


def _ocr_discovery_is_sufficient(rule: dict, values: list[dict]) -> bool:
    """保守判断首轮 OCR 是否已触及规则材料；只用于非计数、非表单规则早停。"""
    if not values:
        return False
    scoring = _rule_scoring(rule)
    if (scoring.get("kind") == "manual" or scoring.get("items")
            or _rule_execution_strategy(rule) in {"counting", "section", "consistency"}
            or "response_form" in _rule_material_roles(rule)):
        return False
    text = "\n".join(str(item.get("text") or "") for item in values)
    compact = re.sub(r"[\s\W_]+", "", text)
    # 证书/检测报告首页常只有名称、编号和二维码，不能因文字总长很短而强迫
    # 再扫描无关尾页；仍要求至少足以承载一个材料名称的长度。
    if len(compact) < 8:
        return False
    # 使用规则中可复用的材料长词和材料类别词作命中，不对任何行业/项目打补丁。
    terms = [term for term in _rule_material_terms(rule) if len(term) >= 4][:48]
    for role in _rule_material_roles(rule):
        terms.extend(term for term in _MATERIAL_ROLE_TERMS.get(role, ()) if len(term) >= 2)
    material_hit = any(re.sub(r"[\s\W_]+", "", term) in compact for term in dict.fromkeys(terms))
    if not material_hit:
        return False
    if not _uses_explicit_field_acquisition_plan(rule):
        return True
    # 新计划器不把“出现材料名称”当作“字段已经够用”。只在材料名之外至少识别到
    # 两类可验证字段时提前停止；否则继续既有候选页，优先保证完整性而非省页数。
    signals = 0
    if re.search(r"(?:编号|证书号|No\.?|序列号|统一社会信用代码)\s*[:：#]?[A-Za-z0-9\-]{3,}", text, re.I):
        signals += 1
    if re.search(r"(?:有效期|发证日期|日期|至)\s*[:：]?\s*(?:19|20)\d{2}", text) or re.search(r"(?:19|20)\d{2}[年./-]\d{1,2}", text):
        signals += 1
    if re.search(r"(?:型号|规格|Model|品牌|生产厂家)\s*[:：]?[A-Za-z0-9\-一-龥]{2,}", text, re.I):
        signals += 1
    return signals >= 2


def _ocr_runtime_enabled(configuration: dict) -> bool:
    """判断当前图片审查是否至少有一个可用 OCR 路径。

    腾讯云关闭时仍必须让本地 RapidOCR 承担直接识别；腾讯云开启但额度、凭据或
    服务异常时，具体页面级回退仍由 _ocr_page_texts 处理。
    """
    local = configuration.get("local") if isinstance(configuration, dict) else {}
    tencent_ready = bool(
        isinstance(configuration, dict)
        and configuration.get("tencent_enabled", configuration.get("enabled"))
        and configuration.get("credentials_configured")
    )
    return bool(tencent_ready or (isinstance(local, dict) and local.get("enabled") and local.get("runtime_available")))


def _evaluation_ocr_enabled(ocr_features_enabled: bool, configuration: dict) -> bool:
    """本地 OCR 是固定基线；保留第一个参数仅兼容旧任务与测试调用。"""
    return bool(_ocr_runtime_enabled(configuration))


def _ocr_parser_version_for_service(service: str) -> int:
    return LOCAL_OCR_PARSER_VERSION if service == LOCAL_OCR_SERVICE else OCR_PARSER_VERSION


def _local_ocr_page_texts(app, document: dict, pages: list[int], *, rule: dict | None = None,
                          level: str = "standard", task: dict | None = None) -> tuple[list[dict], str]:
    """在腾讯 OCR 无法执行时，用短生命周期 RapidOCR 子进程补充文字。

    这里只接收已经由原有候选页逻辑筛出的页面；不会扩张页面范围，也不替代
    腾讯 OCR 的专项接口、表格结构或多模态外观判断。
    """
    values: list[dict] = []
    empty_pages: list[int] = []
    failed_pages: list[int] = []
    page_texts = _document_page_texts(document) if rule else {}
    with tempfile.TemporaryDirectory(prefix="rapidocr-") as temp_dir:
        pending: list[dict] = []
        hashes: dict[int, str] = {}
        for index, page in enumerate(_normalise_result_pages(pages), start=1):
            # 本地没有腾讯的专项接口，但仍按规则强度与页面角色选择渲染质量：
            # 普通正文保持快速档，证照/表格/精细核验才使用高精度渲染。
            render_service = _ocr_service_candidates_for_page(rule or {}, level, page_texts.get(page, ""))[0]
            rendered = _render_ocr_page(app, document, page, render_service, task=task)
            if not rendered:
                failed_pages.append(page)
                continue
            image, image_hash = rendered
            cached = storage.get_ocr_page_cache(app, document["document_id"], page, image_hash, LOCAL_OCR_SERVICE)
            if cached and cached.get("parser_version") == LOCAL_OCR_PARSER_VERSION:
                if str(cached.get("text") or "").strip():
                    values.append({**cached, "page": page, "cached": True})
                elif cached.get("empty"):
                    empty_pages.append(page)
                continue
            path = Path(temp_dir) / f"page-{index}.jpg"
            path.write_bytes(image)
            # 释放当前页 bytes；主进程不保留整批渲染图，控制 2GB 服务器峰值。
            del image
            pending.append({"page": page, "path": str(path)})
            hashes[page] = image_hash
        if not pending:
            message_parts = []
            if failed_pages:
                message_parts.append("本地 RapidOCR 未完成 " + "、".join(f"P{page}" for page in failed_pages))
            if empty_pages and not values:
                message_parts.append("本地 RapidOCR 未在候选页识别到文字")
            return values, "；".join(message_parts)
        # 绝不并行启动 ONNX Runtime；其余投标文件会在此短暂排队，避免小规格服务器 OOM。
        runtime_metrics: dict = {}
        with _LOCAL_OCR_REQUEST_GATE:
            local_values, error = request_local_ocr(pending, metrics=runtime_metrics)
        try:
            storage.record_local_ocr_run(
                app,
                task_id=str((task or {}).get("task_id") or "") or None,
                project_id=str((task or {}).get("project_id") or document.get("project_id") or "") or None,
                document_id=str(document.get("document_id") or "") or None,
                requested_pages=len(pending),
                recognized_pages=int(runtime_metrics.get("recognized_pages") or 0),
                empty_pages=int(runtime_metrics.get("empty_pages") or 0),
                failed_pages=int(runtime_metrics.get("failed_pages") or 0),
                elapsed_ms=int(runtime_metrics.get("elapsed_ms") or 0),
                peak_rss_kb=runtime_metrics.get("peak_rss_kb"),
                status=str(runtime_metrics.get("status") or ("error" if error else "success")),
                error_kind=str(runtime_metrics.get("error_kind") or ((error or {}).get("kind") if isinstance(error, dict) else "")),
            )
        except Exception:
            # 监测台账绝不能影响 OCR 主结果；数据库短暂锁定时只放弃本条指标。
            traceback.print_exc()
    if error:
        return values, str(error.get("message") or "本地 RapidOCR 未获得可用文字")
    for value in local_values:
        page = int(value.get("page") or 0)
        text = str(value.get("text") or "").strip()
        if page <= 0 or page not in hashes:
            continue
        state = str(value.get("state") or ("recognized" if text else "empty"))
        if state == "failed":
            failed_pages.append(page)
            continue
        if not text:
            empty_pages.append(page)
            storage.save_ocr_page_cache(
                app, document["document_id"], page, hashes[page], LOCAL_OCR_SERVICE,
                {"service": LOCAL_OCR_SERVICE, "text": "", "empty": True,
                 "parser_version": LOCAL_OCR_PARSER_VERSION},
            )
            continue
        stored = {
            "service": LOCAL_OCR_SERVICE, "text": text, "line_count": int(value.get("line_count") or 0),
            "confidence": value.get("confidence"), "parser_version": LOCAL_OCR_PARSER_VERSION,
        }
        storage.save_ocr_page_cache(app, document["document_id"], page, hashes[page], LOCAL_OCR_SERVICE, stored)
        values.append({**stored, "page": page, "cached": False})
    message_parts = []
    if failed_pages:
        message_parts.append("本地 RapidOCR 未完成 " + "、".join(f"P{page}" for page in sorted(set(failed_pages))))
    if empty_pages and not values:
        message_parts.append("本地 RapidOCR 未在候选页识别到文字")
    return values, "；".join(message_parts) if message_parts else ""


def _tencent_ocr_page_texts(app, task: dict, document: dict, rule: dict, result: dict, level: str,
                            *, pages: list[int] | None = None) -> tuple[list[dict], str]:
    """腾讯 OCR 的精确复核层；调用方必须先完成本地 OCR 基线。"""
    configuration = storage.ocr_configuration(app)
    service_configs = {item["service"]: item for item in configuration["services"]}
    # 图片优先链路会在视觉模型已确认材料页后，仅复核这些目标页；普通链路仍使用
    # 原有候选页逻辑。显式页码必须经过规范化，防止模型页码或旧结果污染 OCR 调用。
    if pages is None:
        pages = _ocr_candidate_pages(document, rule, result, level)
    else:
        pages = _normalise_result_pages(pages)
    if not pages:
        return [], "未定位到可靠 OCR 候选页"
    tencent_ready = bool(configuration["enabled"] and configuration["credentials_configured"])
    if not tencent_ready:
        return [], "腾讯 OCR 未启用或凭据不可用"
    values: list[dict] = []
    failure = ""
    unavailable_services: set[str] = set()
    preferred_service = ""
    page_texts = _document_page_texts(document)
    for page in pages:
        page_candidates = [
            service for service in _ocr_service_candidates_for_page(rule, level, page_texts.get(page, ""))
            if service_configs.get(service, {}).get("enabled") and service_configs[service].get("remaining", 0) > 0
        ]
        if not page_candidates:
            failure = "腾讯 OCR 可用额度不足"
            continue
        ordered = ([preferred_service] if preferred_service in page_candidates else []) + page_candidates
        ordered = list(dict.fromkeys(service for service in ordered if service not in unavailable_services))
        page_completed = False
        page_empty = False
        for service in ordered:
            rendered = _render_ocr_page(app, document, page, service, task=task)
            if not rendered:
                continue
            image, image_hash = rendered
            cached = storage.get_ocr_page_cache(app, document["document_id"], page, image_hash, service)
            # 旧缓存可能来自 SDK 响应层级错误，或丢失了营业执照字段名/表格行列；
            # 只复用当前解析版本。成功但无文字的页面也缓存为 empty，避免纯图片/空白页
            # 在每次重跑时重复消耗 OCR 额度；它只跳过同一接口，不阻断后续页面和多模态。
            if cached and cached.get("parser_version") == _ocr_parser_version_for_service(service):
                if str(cached.get("text") or "").strip():
                    values.append({**cached, "page": page, "cached": True})
                    preferred_service = service
                    page_completed = True
                    break
                elif cached.get("empty"):
                    failure = "腾讯 OCR 未在候选页识别到文字"
                    page_empty = True
                    break
            response, error = request_tencent_ocr(app, task, service, image)
            error_info = error if isinstance(error, dict) else {"kind": "unknown", "message": str(error or "")}
            # 临时网络/负载错误只在当前页做一次受控重试；不把整项服务永久排除。
            if response is None and error_info.get("retryable"):
                response, error = request_tencent_ocr(app, task, service, image)
                error_info = error if isinstance(error, dict) else {"kind": "unknown", "message": str(error or "")}
            if response is not None:
                text = str(response.get("text") or "").strip()
                if text:
                    stored = {key: value for key, value in response.items() if key != "request_id"}
                    storage.save_ocr_page_cache(app, document["document_id"], page, image_hash, service, stored)
                    values.append({**stored, "page": page, "cached": False})
                    preferred_service = service
                    page_completed = True
                else:
                    failure = "腾讯 OCR 未在候选页识别到文字"
                    storage.save_ocr_page_cache(
                        app, document["document_id"], page, image_hash, service,
                        {"service": service, "text": "", "empty": True, "parser_version": OCR_PARSER_VERSION},
                    )
                # 接口成功但没有文字通常代表页面本身不适合OCR，直接交给多模态，不机械消耗其他额度。
                page_empty = not page_completed
                break
            error_kind = str(error_info.get("kind") or "unknown")
            error_message = str(error_info.get("message") or "腾讯 OCR 调用失败")
            failure = ("腾讯 OCR 额度不足：" + error_message) if error_kind == "quota" else error_message
            # 鉴权、套餐额度和接口能力属于服务级问题；单页图像异常、临时故障和未知错误
            # 仅影响当前页，下一页仍允许尝试原优先接口。
            if error_kind in {"auth", "quota", "unsupported"}:
                unavailable_services.add(service)
    return values, failure


def _tencent_upgrade_pages(rule: dict, local_values: list[dict], local_failure: str,
                           level: str) -> list[int]:
    """仅把本地 OCR 不足或关键字段页送腾讯高精度复核。

    该判断宁可保守升级，也不以节省额度换取漏检：高强度、证照/证件、明确字段
    核验，以及本地失败/低置信页都会升级；普通可读正文则停在本地基线。
    """
    pages = _normalise_result_pages([value.get("page") for value in local_values])
    if not pages:
        return []
    roles = _rule_material_roles(rule)
    requirements = set(_rule_evidence_requirements(rule))
    critical = bool(roles & {"certificate", "business_license", "identity", "personnel"}) or bool(
        requirements & {"field", "document"}
    )
    if level == "high" or critical or local_failure:
        return pages
    selected: list[int] = []
    for value in local_values:
        try:
            confidence = float(value.get("confidence"))
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < 0.72:
            selected.append(value.get("page"))
    return _normalise_result_pages(selected)


def _ocr_page_texts(app, task: dict, document: dict, rule: dict, result: dict, level: str,
                    *, pages: list[int] | None = None, allow_tencent: bool = True) -> tuple[list[dict], str]:
    """本地 RapidOCR 先行，腾讯 OCR 仅作为可选精确复核。

    无论腾讯凭据、额度或网络状态如何，本地可用时都会先产生可复用的页级文字。
    腾讯结果若成功则替换同页本地文本作为该页的权威文字，避免双份原文进入模型。
    """
    configuration = storage.ocr_configuration(app)
    if pages is None:
        pages = _ocr_candidate_pages(document, rule, result, level)
    else:
        pages = _normalise_result_pages(pages)
    if not pages:
        return [], "未定位到可靠 OCR 候选页"
    local_enabled = bool((configuration.get("local") or {}).get("enabled"))
    local_values: list[dict] = []
    local_failure = ""
    if local_enabled:
        local_values, local_failure = _local_ocr_page_texts(
            app, document, pages, rule=rule, level=level, task=task,
        )
    tencent_ready = bool(configuration.get("tencent_enabled", configuration.get("enabled")) and configuration.get("credentials_configured"))
    # “仅基础识别”不用于常规腾讯升级；但本地运行环境或子进程异常时，已配置
    # 的腾讯 OCR 仍作为容错路径，不能让单点本地故障中断本可完成的规则。
    local_runtime_failure = (not local_enabled) or any(
        marker in str(local_failure or "") for marker in ("运行环境", "未完成", "超时", "异常", "不可用", "请求失败")
    )
    if not allow_tencent and not local_runtime_failure:
        if local_values:
            return local_values, local_failure
        return [], local_failure or ("本地 RapidOCR 运行环境不可用" if not local_enabled else "本地 RapidOCR 未获得可用文字")
    # 本地运行环境临时不可用或候选页完全不可读时，腾讯仍可作为容错路径；不能因
    # “本地先行”把原有可用的云端能力一并阻断。
    if not local_values:
        if tencent_ready:
            tencent_values, tencent_failure = _tencent_ocr_page_texts(
                app, task, document, rule, result, level, pages=pages,
            )
            return tencent_values, "；".join(part for part in (local_failure, tencent_failure) if part)
        if not local_enabled:
            return [], "本地 RapidOCR 运行环境不可用"
        return [], local_failure or "本地 RapidOCR 未获得可用文字"
    upgrade_pages = _tencent_upgrade_pages(rule, local_values, local_failure, level)
    if not tencent_ready or not upgrade_pages:
        return local_values, local_failure
    tencent_values, tencent_failure = _tencent_ocr_page_texts(
        app, task, document, rule, result, level, pages=upgrade_pages,
    )
    merged_by_page = {int(value["page"]): value for value in local_values if value.get("page")}
    for value in tencent_values:
        page = int(value.get("page") or 0)
        if page:
            merged_by_page[page] = value
    failure = "；".join(part for part in (local_failure, tencent_failure) if part)
    return [merged_by_page[page] for page in sorted(merged_by_page)], failure


_OCR_CONTEXT_STOP_TERMS = {"投标", "文件", "规则", "检查", "要求", "是否", "提供", "相关", "材料", "内容", "本项目"}


def _ocr_context_terms(rule: dict | None) -> list[str]:
    """从当前规则提取本地检索词，不使用项目或行业特定补丁。"""
    if not isinstance(rule, dict):
        return []
    raw = "\n".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    terms: list[str] = []
    for value in re.findall(r"[A-Za-z]{2,}[A-Za-z0-9._/-]*|\d{2,}|[\u4e00-\u9fff]{2,}", raw):
        value = value.strip()
        if value in _OCR_CONTEXT_STOP_TERMS or len(value) < 2:
            continue
        candidates = [value]
        if len(value) > 8 and re.fullmatch(r"[\u4e00-\u9fff]+", value):
            candidates.extend(value[index:index + 4] for index in range(0, len(value) - 3, 2))
        for candidate in candidates:
            if candidate not in terms:
                terms.append(candidate)
    return terms[:36]


def _relevant_ocr_excerpt(text: str, budget: int, terms: list[str]) -> str:
    """长 OCR 页保留命中行及邻行，再均匀补足上下文，避免中段关键表格行被截掉。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(text) <= budget:
        return text[:budget]
    normalized_terms = [term.lower() for term in terms]
    selected: set[int] = {0, len(lines) - 1}
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(term in lowered for term in normalized_terms):
            selected.update(candidate for candidate in (index - 1, index, index + 1) if 0 <= candidate < len(lines))
    # 没有命中时仍均匀覆盖全文；有命中时也用稀疏锚点保留必要上下文。
    sample_count = max(2, min(8, budget // 900))
    for index in range(sample_count):
        selected.add(round(index * (len(lines) - 1) / max(1, sample_count - 1)))
    ordered = sorted(selected)
    parts: list[str] = []
    used = 0
    previous = -2
    for index in ordered:
        if index > previous + 1 and parts:
            marker = "…中段已压缩…"
            if used + len(marker) + 1 > budget:
                break
            parts.append(marker)
            used += len(marker) + 1
        line = lines[index]
        if used + len(line) + 1 > budget:
            remaining = budget - used
            if remaining > 24:
                parts.append(line[:remaining - 1].rstrip() + "…")
            break
        parts.append(line)
        used += len(line) + 1
        previous = index
    return "\n".join(parts)[:budget]


def _pack_ocr_page_texts(values: list[dict], max_chars: int = 60_000, *, rule: dict | None = None) -> str:
    """按页公平分配 OCR 上下文，并优先保留与当前规则有关的中段行。"""
    usable = [value for value in values if str(value.get("text") or "").strip()]
    if not usable:
        return ""
    headers = [
        f"[第{value['page']}页·{_ocr_service_label(str(value.get('service') or ''))}]"
        for value in usable
    ]
    fixed_chars = sum(len(header) + 2 for header in headers)
    per_page = max(800, (max_chars - fixed_chars) // len(usable))
    parts: list[str] = []
    for header, value in zip(headers, usable):
        text = _clean_model_text(value.get("text"))
        if len(text) > per_page:
            text = _relevant_ocr_excerpt(text, per_page, _ocr_context_terms(rule))
        parts.append(f"{header}\n{text}")
    return "\n\n".join(parts)[:max_chars]


def _ocr_service_label(service: str) -> str:
    if service == LOCAL_OCR_SERVICE:
        return "本地 RapidOCR"
    return storage.TENCENT_OCR_SERVICES.get(service, {}).get("label", "腾讯 OCR")


def _merge_supplement_text(base: object, supplement: object, limit: int = 2000, *,
                           supplement_first: bool = False) -> str:
    """合并原结论与补充证据，并保证较新的 OCR/图片事实不会被旧长文本截掉。"""
    base_text = _clean_model_text(base)
    supplement_text = _clean_model_text(supplement)
    if not base_text:
        return _truncate_field(supplement_text, limit)
    if not supplement_text:
        return _truncate_field(base_text, limit)
    separator = "\n"
    if len(base_text) + len(separator) + len(supplement_text) <= limit:
        return separator.join((supplement_text, base_text) if supplement_first else (base_text, supplement_text))
    # 至少为原结论保留三分之一；剩余空间优先给最新补充，避免 OCR/图片已经识别正确
    # 却在最终 evidence/reason 的 2000 字上限处被完整截掉。
    supplement_budget = min(len(supplement_text), max(limit // 3, limit - limit // 3 - len(separator)))
    base_budget = max(0, limit - supplement_budget - len(separator))
    clipped_base = base_text[:base_budget].rstrip()
    if len(base_text) > base_budget and clipped_base:
        clipped_base = clipped_base[:-1].rstrip() + "…"
    clipped_supplement = supplement_text[:supplement_budget].rstrip()
    values = (clipped_supplement, clipped_base) if supplement_first else (clipped_base, clipped_supplement)
    return _truncate_field(separator.join(value for value in values if value), limit)


_REASON_LAYER_PREFIX_PATTERN = re.compile(r"【(?:腾讯OCR|本地OCR|OCR|图片识别)[^】]*】")


def _reason_sentence_signature(value: object) -> str:
    """忽略识别层前缀和排版差异，用于理由中的保守重复消除。"""
    text = _REASON_LAYER_PREFIX_PATTERN.sub("", _clean_model_text(value))
    return re.sub(r"[\s，,。；;：:！!？?（）()\[\]【】]", "", text).casefold()


def _merge_reason_text(base: object, supplement: object, limit: int = 2000, *,
                       supplement_first: bool = False) -> str:
    """合并理由时仅消除同一句的重复，不合并内容不同的计分或判断依据。"""
    values = (supplement, base) if supplement_first else (base, supplement)
    retained: list[str] = []
    seen: set[str] = set()
    for value in values:
        for sentence in re.split(r"(?<=[。；;！？!?])\s*|\n+", _clean_model_text(value)):
            sentence = sentence.strip()
            signature = _reason_sentence_signature(sentence)
            if not signature or signature in seen:
                continue
            seen.add(signature)
            retained.append(sentence)
    return _truncate_field("\n".join(retained), limit)


_OCR_TABLE_HEADER_FRAGMENT_PATTERN = re.compile(
    r"(?:序号|分项|名称|规格|型号|数量|单位|单价|总价|金额|含税|报价|合计|综合)"
)
_OCR_SHORT_VALUE_PATTERN = re.compile(r"(?:\d|[A-Za-z]{2,}|[¥￥%])")
_OCR_UNIT_FRAGMENT_PATTERN = re.compile(r"^(?:套|台|份|项|个|包|张|页|年|月|日|元)$")


def _compact_ocr_raw_evidence(text: object, limit: int = 1400) -> str:
    """压缩 OCR 失败兜底中的表格断行，保留原 OCR 缓存和模型输入不变。"""
    lines = []
    for value in str(text or "").replace("\r", "").split("\n"):
        compact = re.sub(r"\s+", "", value)
        if compact:
            lines.append(compact)
    if not lines:
        return ""
    parts: list[str] = []
    fragments: list[str] = []

    def flush_fragments() -> None:
        if not fragments:
            return
        joined = "".join(fragments)
        header_hits = len(_OCR_TABLE_HEADER_FRAGMENT_PATTERN.findall(joined))
        # 仅压缩明显的表格表头碎片；型号、金额、编号等短值始终保留，避免丢证据。
        if len(fragments) >= 5 and header_hits >= 2:
            values = []
            has_value = False
            for item in fragments:
                if _OCR_SHORT_VALUE_PATTERN.search(item):
                    values.append(item)
                    has_value = True
                elif has_value and _OCR_UNIT_FRAGMENT_PATTERN.fullmatch(item):
                    values.append(item)
            if values:
                parts.append("表格字段：" + " ".join(values))
            else:
                parts.append("【表格表头碎片已折叠】")
        else:
            parts.append(joined)
        fragments.clear()

    for line in lines:
        if len(line) <= 4:
            fragments.append(line)
            continue
        flush_fragments()
        parts.append(line)
    flush_fragments()
    return " ".join(parts)[:limit]


def _with_ocr_raw_evidence(result: dict, prefix: str, text: str) -> dict:
    """JSON归纳失败时仍把已识别文字留作人工可核验的证据，不改变原判断。"""
    excerpt = _compact_ocr_raw_evidence(text)
    if not excerpt:
        return result
    return {
        **result,
        "evidence": _merge_supplement_text(result.get("evidence"), f"{prefix}{excerpt}"),
    }


def _with_ocr_fallback_evidence(result: dict, component: str, service_labels: str,
                                pages: list[int], text: str, *, uncovered: bool = False) -> dict:
    """OCR归纳失败时，客观分只保留执行摘要；其他模块仍保留可人工核验的原文。"""
    page_text = "、".join(f"P{page}" for page in pages)
    if component == "objective":
        outcome = "未覆盖可形成结论的关键文字" if uncovered else "未形成可采纳的结构化补充"
        summary = (
            f"【OCR摘要·{service_labels}·{page_text}】已完成{len(pages)}页候选材料文字识别，"
            f"{outcome}，原AI建议得分保持不变。"
        )
        return {
            **result,
            "evidence": _merge_supplement_text(result.get("evidence"), summary),
        }
    return _with_ocr_raw_evidence(
        result, f"【OCR原文·{service_labels}·{page_text}】", text,
    )


_OCR_BATCH_MAX_RULES = 6
_OCR_BATCH_MAX_CHARS = 40_000


def _ocr_batch_enabled() -> bool:
    """OCR 归纳批量合并开关；默认关闭，需在真实项目上做开/关 A/B 验证后再启用。"""
    return str(os.environ.get("EVALUATION_WORKBENCH_OCR_BATCH") or "").strip().lower() in {"1", "true", "yes", "on"}


def _ocr_supplement_extract(app, task, document, rule, result, level, *,
                            locator_profile=None, allow_tencent=True):
    """批量路径的 OCR 候选页定位与文字提取；返回归纳载荷或提前结束的最终结果。"""
    working_result = result
    if level == "high" and locator_profile and not _vision_page_candidates(document, rule, result):
        located = _locate_visual_pages(app, task, document, rule, locator_profile)
        if located:
            working_result = {**result, "visual_page_candidates": located}
    candidate_pages = _ocr_candidate_pages(document, rule, working_result, level)
    discovery_count = _ocr_discovery_page_count(rule, level, candidate_pages)
    discovery_pages = candidate_pages[:discovery_count]
    values, failure = _ocr_page_texts(
        app, task, document, rule, working_result, level, pages=discovery_pages, allow_tencent=allow_tencent,
    )
    remaining_pages = candidate_pages[discovery_count:]
    if remaining_pages and not _ocr_discovery_is_sufficient(rule, values):
        followup_values, followup_failure = _ocr_page_texts(
            app, task, document, rule, working_result, level, pages=remaining_pages, allow_tencent=allow_tencent,
        )
        values.extend(followup_values)
        if followup_failure:
            failure = "；".join(value for value in (failure, followup_failure) if value)
    if not values:
        status = "ocr_quota_exhausted" if "额度" in failure else "ocr_not_located" if "定位" in failure else "ocr_failed"
        return None, _set_result_coverage(_with_vision_execution(working_result, status, [], {}, failure or "OCR 未获得可用文字，已保留原文字结论。"), "uncovered")
    pages = [int(value["page"]) for value in values]
    ocr_text = _pack_ocr_page_texts(values, rule=rule)
    if not ocr_text.strip():
        return None, _set_result_coverage(_with_vision_execution(working_result, "ocr_failed", pages, {}, "OCR 未识别到可用文字，已保留原文字结论。"), "uncovered")
    service_labels = "、".join(dict.fromkeys(_ocr_service_label(str(value.get("service") or "")) for value in values))
    local_only = bool(values) and all(str(value.get("service") or "") == LOCAL_OCR_SERVICE for value in values)
    incomplete_pages = "本地 RapidOCR 未完成" in str(failure or "")
    return {
        "working_result": working_result, "pages": pages, "ocr_text": ocr_text,
        "service_labels": service_labels, "local_only": local_only,
        "failure": failure, "incomplete_pages": incomplete_pages,
    }, None


def _ocr_summarize_prompt(app, payload: dict, rule: dict, document: dict) -> str:
    prompt = storage.render_prompt_template(
        app, "evaluate_all_ocr_user",
        rule=json.dumps(_visual_rule_packet(rule), ensure_ascii=False, separators=(",", ":")),
        document_name=document.get("original_name") or "投标文件",
        bidder_name=document.get("bidder_name") or document.get("original_name") or "投标人",
        text_result=json.dumps(payload["working_result"], ensure_ascii=False, separators=(",", ":")),
        ocr_service=payload["service_labels"],
        ocr_pages="、".join(f"P{page}" for page in payload["pages"]),
        ocr_text=payload["ocr_text"],
    )
    prompt += "\n\n【系统输出与证据协议】\n" + storage.render_prompt_template(app, "evaluate_all_ocr_contract")
    if payload["incomplete_pages"]:
        prompt += (
            "\n\n【OCR执行边界】部分候选页未能完成本地 OCR：" + payload["failure"][:240]
            + "。只能基于已识别页给出部分补充，不得声称本次 OCR 已完整覆盖整条规则。"
        )
    return prompt


def _apply_ocr_summary(component: str, rule: dict, working_result: dict, parsed: dict, payload: dict) -> dict:
    """把一次 OCR 归纳结果合并进单条规则；与 _run_ocr_supplement 的合并语义逐字段一致。"""
    pages = payload["pages"]
    service_labels = payload["service_labels"]
    local_only = payload["local_only"]
    failure = payload["failure"]
    incomplete_pages = payload["incomplete_pages"]
    scope = _ocr_response_scope(parsed)
    content_covered = str(parsed.get("content_coverage") or "").lower() == "covered"
    can_apply = not incomplete_pages and (scope == "full" or content_covered)
    if incomplete_pages:
        scope = "partial"
    evidence_pages: list[int] = []
    for value in parsed.get("evidence_pages") or []:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            page = int(value)
            if page in pages and page not in evidence_pages:
                evidence_pages.append(page)
    if not evidence_pages:
        for value in (parsed.get("evidence"), parsed.get("reason")):
            for page in _explicit_page_references(value):
                if page in pages and page not in evidence_pages:
                    evidence_pages.append(page)
    if not evidence_pages and len(pages) == 1:
        evidence_pages = list(pages)
    display_pages = evidence_pages or pages
    prefix_name = "本地OCR" if local_only else "OCR"
    prefix = f"【{prefix_name}·{service_labels}·" + "、".join(f"P{page}" for page in display_pages) + "】"
    evidence_limit = 500 if component == "objective" else 1600
    reason_limit = 400 if component == "objective" else 1200
    merge_limit = 1_000 if component == "objective" else 2_000
    evidence = _clean_model_text(parsed.get("evidence"))[:evidence_limit]
    reason = _clean_model_text(parsed.get("reason"))[:reason_limit]
    status = "ocr_applied" if scope == "full" else "ocr_applied_partial"
    reconciled_evidence = _reconcile_stale_pending_text(
        working_result.get("evidence"), f"{evidence}\n{reason}", rule, full=scope == "full",
    )
    reconciled_reason = _reconcile_stale_pending_text(
        working_result.get("reason"), f"{evidence}\n{reason}", rule, full=scope == "full",
    )
    if component == "review":
        selected_status = str(parsed.get("status") or working_result.get("status") or "manual") if can_apply else str(working_result.get("status") or "manual")
        if selected_status not in {"satisfied", "not_satisfied", "partial", "not_found", "manual"}:
            selected_status = "manual"
        merged = _review_result_from_model({
            "evidence": _merge_supplement_text(reconciled_evidence, f"{prefix}{evidence}" if evidence else "", limit=merge_limit),
            "page_hint": "P" + "、P".join(str(page) for page in (evidence_pages or pages)),
            "reason": _merge_reason_text(reconciled_reason, f"{prefix}{reason}" if reason else "", limit=merge_limit),
            "risk_level": parsed.get("risk_level") if can_apply else working_result.get("risk_level"),
            "confidence": parsed.get("confidence") if can_apply else working_result.get("confidence"),
            "evidence_quality": "sufficient" if can_apply and evidence else working_result.get("evidence_quality"),
        }, rule["rule_id"], selected_status)
        merged = {
            **merged,
            "visual_page_candidates": list(working_result.get("visual_page_candidates") or []),
            "ocr_candidate_pages": pages,
            "ocr_evidence_pages": evidence_pages,
        }
    else:
        max_score = float(_rule_scoring(rule).get("max_score") or working_result.get("max_score") or 0)
        suggested = _bounded_model_score(parsed.get("suggested_score"), max_score) if can_apply and max_score > 0 else working_result.get("suggested_score")
        merged = {**working_result, "suggested_score": suggested,
                  "evidence": _merge_supplement_text(reconciled_evidence, f"{prefix}{evidence}" if evidence else "", limit=merge_limit),
                  "reason": _merge_reason_text(reconciled_reason, f"{prefix}{reason}" if reason else "", limit=merge_limit),
                  "confidence": _enum_text(parsed.get("confidence"), {"high", "medium", "low"}, working_result.get("confidence")) if can_apply else working_result.get("confidence"),
                  "ocr_candidate_pages": pages, "ocr_evidence_pages": evidence_pages,
                  "requires_review": True, "automation_status": "needs_review", "review_reason": f"{service_labels} 结果已补充，需人工复核。"}
    merged = _append_evidence_layer(
        merged, source="local_ocr" if local_only else "tencent_ocr", summary=evidence or reason, checked_pages=pages,
        evidence_pages=evidence_pages, service=service_labels,
    )
    return _with_vision_execution(
        _set_result_coverage(merged, "covered" if can_apply else "partial"), status, pages, {"display_name": service_labels},
        f"{service_labels} 已识别候选页并{'采纳到规则结论' if can_apply else '补充部分文字事实'}"
        + (f"；{failure}" if incomplete_pages else "。"),
        evidence_pages=evidence_pages,
    )


def _run_ocr_summarize(app, task, document, component, rule, payload, profile) -> dict:
    """对已提取的 OCR 载荷执行单条归纳模型调用；失败语义与 _run_ocr_supplement 一致。"""
    prompt = _ocr_summarize_prompt(app, payload, rule, document)
    try:
        parsed = _request_task_json(
            app, task, profile, f"evaluate_all_{component}_ocr", _system_prompt(app, "evaluate_all"), prompt,
            document_id=document["document_id"], context_mode="local_ocr" if payload["local_only"] else "tencent_ocr",
            max_tokens=_output_token_budget(profile, 1300), thinking_mode="disabled",
        )
    except ValueError:
        fallback = _with_ocr_fallback_evidence(payload["working_result"], component, payload["service_labels"], payload["pages"], payload["ocr_text"])
        message = (
            f"{payload['service_labels']} 已完成文字识别；OCR结论规范化失败，客观分仅保留简短摘要和原建议。"
            if component == "objective"
            else f"{payload['service_labels']} 已识别文字并附入证据；OCR结论规范化失败，已保留原结论。"
        )
        return _set_result_coverage(_with_vision_execution(fallback, "ocr_applied_partial", payload["pages"], {}, message), "partial")
    if not isinstance(parsed, dict) or _ocr_response_coverage(parsed) != "covered":
        fallback = _with_ocr_fallback_evidence(
            payload["working_result"], component, payload["service_labels"], payload["pages"], payload["ocr_text"], uncovered=True,
        )
        return _set_result_coverage(_with_vision_execution(fallback, "ocr_uncovered", payload["pages"], {}, f"{payload['service_labels']} 已识别候选页，但未覆盖可形成结论的关键文字，已保留原结论。"), "uncovered")
    return _apply_ocr_summary(component, rule, payload["working_result"], parsed, payload)


def _run_ocr_batch_supplement(app, task, document, component, entries, profile, *, locator_profile=None) -> dict[str, dict]:
    """同组件多条规则合并 OCR 归纳调用，降低模型调用次数。

    只对 2-6 条规则且合并 OCR 文本不超过阈值的组件启用；模型必须按输入顺序覆盖
    每个 rule_id 且每条 coverage=covered 才采纳整批结果，否则整批回退为逐条原路径
    （_run_ocr_supplement），保证证据链与覆盖率语义与旧实现完全一致。
    """
    results: dict[str, dict] = {}
    batch_items: list[tuple[dict, dict]] = []
    for rule, result, allow_tencent in entries:
        trigger, configured_level = _rule_vision_policy(rule)
        level = configured_level if configured_level in {"low", "standard", "high"} else "standard"
        payload, early = _ocr_supplement_extract(
            app, task, document, rule, result, level, locator_profile=locator_profile, allow_tencent=allow_tencent,
        )
        if payload is None:
            results[rule["rule_id"]] = early
        else:
            batch_items.append((rule, payload))
    if not batch_items:
        return results
    if len(batch_items) < 2 or len(batch_items) > _OCR_BATCH_MAX_RULES or sum(len(p["ocr_text"]) for _, p in batch_items) > _OCR_BATCH_MAX_CHARS:
        for rule, payload in batch_items:
            results[rule["rule_id"]] = _run_ocr_summarize(app, task, document, component, rule, payload, profile)
        return results
    items = [{
        "rule_id": rule["rule_id"],
        "rule": _visual_rule_packet(rule),
        "text_result": payload["working_result"],
        "ocr_service": payload["service_labels"],
        "ocr_pages": payload["pages"],
        "ocr_text": payload["ocr_text"],
    } for rule, payload in batch_items]
    prompt = storage.render_prompt_template(
        app, "evaluate_all_ocr_batch_user",
        items=json.dumps(items, ensure_ascii=False, separators=(",", ":")),
        document_name=document.get("original_name") or "投标文件",
        bidder_name=document.get("bidder_name") or document.get("original_name") or "投标人",
    )
    prompt += "\n\n【系统输出与证据协议】\n" + storage.render_prompt_template(app, "evaluate_all_ocr_contract")
    incomplete = [(rule, payload) for rule, payload in batch_items if payload["incomplete_pages"]]
    if incomplete:
        note = "；".join(f"规则{rule['rule_id'][:8]}：{payload['failure'][:120]}" for rule, payload in incomplete)
        prompt += "\n\n【OCR执行边界】以下规则存在未完成的本地 OCR 页：" + note[:400] + "。只能基于已识别页给出部分补充，不得声称整条规则 OCR 已完整覆盖。"
    parsed = None
    try:
        value = _request_task_json(
            app, task, profile, f"evaluate_all_{component}_ocr_batch", _system_prompt(app, "evaluate_all"), prompt,
            document_id=document["document_id"], context_mode="ocr_batch",
            max_tokens=_output_token_budget(profile, 800 + len(batch_items) * 900), thinking_mode="disabled",
        )
        if isinstance(value, dict) and isinstance(value.get("results"), list):
            parsed = value
    except ValueError:
        parsed = None
    if parsed is not None:
        by_id = {str(item.get("rule_id")): item for item in parsed["results"] if isinstance(item, dict) and str(item.get("rule_id"))}
        if all(rule["rule_id"] in by_id for rule, _ in batch_items) and all(
            _ocr_response_coverage(by_id[rule["rule_id"]]) == "covered" for rule, _ in batch_items
        ):
            for rule, payload in batch_items:
                results[rule["rule_id"]] = _apply_ocr_summary(component, rule, payload["working_result"], by_id[rule["rule_id"]], payload)
            return results
    # 整批回退：逐条走与旧实现完全一致的路径（含重新提取），保证行为一致。
    for rule, result, allow_tencent in entries:
        if rule["rule_id"] in results:
            continue
        results[rule["rule_id"]] = _run_ocr_supplement(
            app, task, document, component, rule, result, profile,
            locator_profile=locator_profile, baseline=True, allow_tencent=allow_tencent,
        )
    return results


def _run_ocr_supplement(app, task: dict, document: dict, component: str, rule: dict, result: dict,
                        profile: dict, locator_profile: dict | None = None, *,
                        baseline: bool = False, allow_tencent: bool = True) -> dict:
    """OCR先补足可读文字；失败/额度不足只记录原因，后续仍可进入原多模态路径。"""
    trigger, configured_level = _rule_vision_policy(rule)
    level = configured_level if configured_level in {"low", "standard", "high"} else "standard"
    if not baseline and (trigger == "off" or configured_level == "off"):
        return result
    if not baseline and trigger == "text_fallback" and not _needs_visual_fallback(component, result):
        return _with_vision_execution(result, "ocr_skipped_text_sufficient", [], {}, "文字证据已足够，本次无需调用 OCR。")
    working_result = result
    if level == "high" and locator_profile and not _vision_page_candidates(document, rule, result):
        located = _locate_visual_pages(app, task, document, rule, locator_profile)
        if located:
            # 一次低清找页结果同时交给 OCR 与后续高清图片复核；OCR失败时也要
            # 保留候选，避免图片阶段再次调用找页模型。
            working_result = {**result, "visual_page_candidates": located}
    candidate_pages = _ocr_candidate_pages(document, rule, working_result, level)
    discovery_count = _ocr_discovery_page_count(rule, level, candidate_pages)
    discovery_pages = candidate_pages[:discovery_count]
    values, failure = _ocr_page_texts(
        app, task, document, rule, working_result, level, pages=discovery_pages,
        allow_tencent=allow_tencent,
    )
    # 两阶段 OCR：先小范围确认是否真正触及材料。计分、表单和分段规则始终完整
    # 覆盖；普通单一材料仅在首轮已明确命中时早停。这样不会用“未命中”推导缺失，
    # 只减少与规则无关页的外部 OCR 调用。
    remaining_pages = candidate_pages[discovery_count:]
    if remaining_pages and not _ocr_discovery_is_sufficient(rule, values):
        followup_values, followup_failure = _ocr_page_texts(
            app, task, document, rule, working_result, level, pages=remaining_pages,
            allow_tencent=allow_tencent,
        )
        values.extend(followup_values)
        if followup_failure:
            failure = "；".join(value for value in (failure, followup_failure) if value)
    if not values:
        status = "ocr_quota_exhausted" if "额度" in failure else "ocr_not_located" if "定位" in failure else "ocr_failed"
        return _set_result_coverage(_with_vision_execution(working_result, status, [], {}, failure or "OCR 未获得可用文字，已保留原文字结论。"), "uncovered")
    pages = [int(value["page"]) for value in values]
    ocr_text = _pack_ocr_page_texts(values, rule=rule)
    if not ocr_text.strip():
        return _set_result_coverage(_with_vision_execution(working_result, "ocr_failed", pages, {}, "OCR 未识别到可用文字，已保留原文字结论。"), "uncovered")
    service_labels = "、".join(dict.fromkeys(_ocr_service_label(str(value.get("service") or "")) for value in values))
    local_only = bool(values) and all(str(value.get("service") or "") == LOCAL_OCR_SERVICE for value in values)
    prompt = storage.render_prompt_template(
        app, "evaluate_all_ocr_user", rule=json.dumps(_visual_rule_packet(rule), ensure_ascii=False, separators=(",", ":")),
        document_name=document.get("original_name") or "投标文件", bidder_name=document.get("bidder_name") or document.get("original_name") or "投标人",
        text_result=json.dumps(working_result, ensure_ascii=False, separators=(",", ":")), ocr_service=service_labels,
        ocr_pages="、".join(f"P{page}" for page in pages), ocr_text=ocr_text,
    )
    prompt += "\n\n【系统输出与证据协议】\n" + storage.render_prompt_template(app, "evaluate_all_ocr_contract")
    # 即使同一规则已有腾讯 OCR 成功页，本地回退页失败也不能被掩盖；只有明确的
    # “本地页未完成”才强制降为部分结论，腾讯正常空白页仍由既有覆盖协议处理。
    incomplete_pages = "本地 RapidOCR 未完成" in str(failure or "")
    if incomplete_pages:
        prompt += (
            "\n\n【OCR执行边界】部分候选页未能完成本地 OCR：" + failure[:240]
            + "。只能基于已识别页给出部分补充，不得声称本次 OCR 已完整覆盖整条规则。"
        )
    try:
        parsed = _request_task_json(
            app, task, profile, f"evaluate_all_{component}_ocr", _system_prompt(app, "evaluate_all"), prompt,
            document_id=document["document_id"], context_mode="local_ocr" if local_only else "tencent_ocr",
            max_tokens=_output_token_budget(profile, 1300), thinking_mode="disabled",
        )
    except ValueError:
        fallback = _with_ocr_fallback_evidence(working_result, component, service_labels, pages, ocr_text)
        message = (
            f"{service_labels} 已完成文字识别；OCR结论规范化失败，客观分仅保留简短摘要和原建议。"
            if component == "objective"
            else f"{service_labels} 已识别文字并附入证据；OCR结论规范化失败，已保留原结论。"
        )
        return _set_result_coverage(_with_vision_execution(fallback, "ocr_applied_partial", pages, {}, message), "partial")
    if not isinstance(parsed, dict) or _ocr_response_coverage(parsed) != "covered":
        fallback = _with_ocr_fallback_evidence(
            working_result, component, service_labels, pages, ocr_text, uncovered=True,
        )
        return _set_result_coverage(_with_vision_execution(fallback, "ocr_uncovered", pages, {}, f"{service_labels} 已识别候选页，但未覆盖可形成结论的关键文字，已保留原结论。"), "uncovered")
    scope = _ocr_response_scope(parsed)
    # 新协议把“文字事实已覆盖”与“仍需核验签章/外观”拆开：OCR 可以为文字性
    # 条件提供明确建议，但绝不能冒充完成图片真实性或签章核验。旧自定义模板没有
    # content_coverage 时维持原有仅 full 才采纳的严格行为。
    content_covered = str(parsed.get("content_coverage") or "").lower() == "covered"
    can_apply_text_conclusion = not incomplete_pages and (scope == "full" or content_covered)
    if incomplete_pages:
        # 单页本地推理异常不能被其他页的正面文字“掩盖”；视觉模型仍可在下一步补看。
        scope = "partial"
    evidence_pages: list[int] = []
    for value in parsed.get("evidence_pages") or []:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            page = int(value)
            if page in pages and page not in evidence_pages:
                evidence_pages.append(page)
    if not evidence_pages:
        for value in (parsed.get("evidence"), parsed.get("reason")):
            for page in _explicit_page_references(value):
                if page in pages and page not in evidence_pages:
                    evidence_pages.append(page)
    if not evidence_pages and len(pages) == 1:
        evidence_pages = list(pages)
    display_pages = evidence_pages or pages
    prefix_name = "本地OCR" if local_only else "OCR"
    prefix = f"【{prefix_name}·{service_labels}·" + "、".join(f"P{page}" for page in display_pages) + "】"
    # 客观分已有文字层证据与计分过程，OCR只补充简短结论，避免重复展示整页识别字段。
    evidence_limit = 500 if component == "objective" else 1600
    reason_limit = 400 if component == "objective" else 1200
    merge_limit = 1_000 if component == "objective" else 2_000
    evidence = _clean_model_text(parsed.get("evidence"))[:evidence_limit]
    reason = _clean_model_text(parsed.get("reason"))[:reason_limit]
    status = "ocr_applied" if scope == "full" else "ocr_applied_partial"
    reconciled_evidence = _reconcile_stale_pending_text(
        working_result.get("evidence"), f"{evidence}\n{reason}", rule, full=scope == "full",
    )
    reconciled_reason = _reconcile_stale_pending_text(
        working_result.get("reason"), f"{evidence}\n{reason}", rule, full=scope == "full",
    )
    if component == "review":
        selected_status = str(parsed.get("status") or working_result.get("status") or "manual") if can_apply_text_conclusion else str(working_result.get("status") or "manual")
        if selected_status not in {"satisfied", "not_satisfied", "partial", "not_found", "manual"}:
            selected_status = "manual"
        merged = _review_result_from_model({
            "evidence": _merge_supplement_text(reconciled_evidence, f"{prefix}{evidence}" if evidence else "", limit=merge_limit),
            "page_hint": "P" + "、P".join(str(page) for page in (evidence_pages or pages)),
            "reason": _merge_reason_text(reconciled_reason, f"{prefix}{reason}" if reason else "", limit=merge_limit),
            "risk_level": parsed.get("risk_level") if can_apply_text_conclusion else working_result.get("risk_level"),
            "confidence": parsed.get("confidence") if can_apply_text_conclusion else working_result.get("confidence"),
            "evidence_quality": "sufficient" if can_apply_text_conclusion and evidence else working_result.get("evidence_quality"),
        }, rule["rule_id"], selected_status)
        # 标准化审查结果只保留业务字段，这些内部路由元数据需显式带到下一层图片识别。
        merged = {
            **merged,
            "visual_page_candidates": list(working_result.get("visual_page_candidates") or []),
            "ocr_candidate_pages": pages,
            "ocr_evidence_pages": evidence_pages,
        }
    else:
        max_score = float(_rule_scoring(rule).get("max_score") or working_result.get("max_score") or 0)
        suggested = _bounded_model_score(parsed.get("suggested_score"), max_score) if can_apply_text_conclusion and max_score > 0 else working_result.get("suggested_score")
        merged = {**working_result, "suggested_score": suggested,
                  "evidence": _merge_supplement_text(reconciled_evidence, f"{prefix}{evidence}" if evidence else "", limit=merge_limit),
                  "reason": _merge_reason_text(reconciled_reason, f"{prefix}{reason}" if reason else "", limit=merge_limit),
                  "confidence": _enum_text(parsed.get("confidence"), {"high", "medium", "low"}, working_result.get("confidence")) if can_apply_text_conclusion else working_result.get("confidence"),
                  "ocr_candidate_pages": pages, "ocr_evidence_pages": evidence_pages,
                  "requires_review": True, "automation_status": "needs_review", "review_reason": f"{service_labels} 结果已补充，需人工复核。"}
    merged = _append_evidence_layer(
        merged, source="local_ocr" if local_only else "tencent_ocr", summary=evidence or reason, checked_pages=pages,
        evidence_pages=evidence_pages, service=service_labels,
    )
    return _with_vision_execution(
        _set_result_coverage(merged, "covered" if can_apply_text_conclusion else "partial"), status, pages, {"display_name": service_labels},
        f"{service_labels} 已识别候选页并{'采纳到规则结论' if can_apply_text_conclusion else '补充部分文字事实'}"
        + (f"；{failure}" if incomplete_pages else "。"),
        evidence_pages=evidence_pages,
    )


def _rule_vision_policy(rule: dict) -> tuple[str, str]:
    """读取可向后兼容的图片识别执行策略。"""
    try:
        meta = storage.rule_execution_meta(rule)
    except (TypeError, ValueError):
        meta = {}
    trigger = str(rule.get("vision_trigger") or meta.get("vision_trigger") or "off")
    level = str(rule.get("vision_level") or meta.get("vision_level") or "off")
    return (trigger if trigger in {"off", "text_fallback", "required"} else "off",
            level if level in _VISION_LEVEL_SETTINGS else "off")


def _needs_visual_fallback(component: str, result: dict) -> bool:
    if component == "review":
        return result.get("status") in {"ocr_required", "manual", "not_found", "partial"} or result.get("evidence_quality") != "sufficient"
    return result.get("suggested_score") is None or result.get("confidence") != "high" or "OCR" in str(result.get("reason") or "")


_SPARSE_TEXT_CHARS_PER_PAGE = 80


def _document_text_coverage_status(document: dict) -> str:
    """判断电子文件的机器可读文本是否足以单独支撑结论。

    这是文件质量门槛，不涉及项目、行业、材料名称或模型。PDF 大量为扫描页时，
    仅靠文本层“未命中”既不能推导材料缺失，也不能推导规则满足。
    """
    try:
        pages = int(document.get("page_count") or 0)
        text_length = int(document.get("text_length") or 0)
    except (TypeError, ValueError):
        return "covered"
    if pages >= 3 and text_length / max(1, pages) < _SPARSE_TEXT_CHARS_PER_PAGE:
        return "uncovered"
    return "covered"


def _set_result_coverage(result: dict, status: str) -> dict:
    """统一保存规则级证据覆盖状态，避免 OCR/图片层各自解释同一语义。"""
    if status not in {"covered", "partial", "uncovered"}:
        status = "partial"
    return {**result, "coverage_status": status}


def _apply_document_evidence_guard(document: dict, component: str, rule: dict, result: dict) -> dict:
    """对扫描型文件禁止用“未覆盖”替代“材料缺失”或“规则满足”。

    OCR/图片层已明确完整覆盖该规则时允许正常结论；否则审查项回落为待 OCR，
    评分项不保留任何暂定分。该守卫在文字初评、OCR 失败及图片补充后均可重复调用。
    """
    if _document_text_coverage_status(document) != "uncovered":
        return _set_result_coverage(result, str(result.get("coverage_status") or "covered"))
    if str(result.get("coverage_status") or "") == "covered":
        return result
    title = str(rule.get("title") or rule.get("check_rule") or "该规则")[:80]
    note = f"文件机器可读文本覆盖不足，尚未通过 OCR 或图片识别完整核验“{title}”；未覆盖不等同于材料缺失或规则满足。"
    if component == "review":
        return {
            **_set_result_coverage(result, "uncovered"),
            "status": "ocr_required", "risk_level": "low", "confidence": "low", "evidence_quality": "missing",
            "requires_review": True, "automation_status": "needs_review",
            "review_reason": "扫描型文件尚未形成该规则的完整证据覆盖，需 OCR 或图片识别后复核。",
            "reason": note,
        }
    return {
        **_set_result_coverage(result, "uncovered"),
        "suggested_score": None, "effective_score": None, "confidence": "low",
        "requires_review": True, "automation_status": "needs_review",
        "review_reason": "扫描型文件尚未形成该评分规则的完整证据覆盖，暂不建议计分。",
        "reason": note,
    }


def _should_run_multimodal_after_ocr(strategy: str, result: dict) -> bool:
    """纯视觉/混合规则必看图；纯文字OCR只有完整覆盖时才省去多模态。"""
    if strategy in {"vision", "hybrid"}:
        return True
    return str(result.get("vision_status") or "") not in {"ocr_applied", "ocr_skipped_text_sufficient"}


def _rule_has_visual_only_terms(rule: dict) -> bool:
    """规则文本明确提及签章、外观、截图样式等纯视觉核验目标。"""
    text = f"{rule.get('title') or ''}\n{rule.get('check_rule') or ''}\n{rule.get('source_text') or ''}"
    return any(term in text for term in _VISION_FACT_TERMS)


def _multimodal_skip_note(strategy: str, result: dict, rule: dict, trigger: str) -> str:
    """混合规则无纯视觉核验目标且 OCR 已完整覆盖时，跳过本次多模态调用并说明原因。

    仅对人工选择的 text_fallback 触发生效；required 规则尊重人工显式要求，
    始终执行图片识别，不因 OCR 结果省略。
    """
    if strategy != "hybrid" or trigger != "text_fallback":
        return ""
    if str(result.get("vision_status") or "") != "ocr_applied":
        return ""
    if _rule_has_visual_only_terms(rule):
        return ""
    return "规则无签章、外观等纯视觉核验目标，OCR 已完整覆盖文字事实，本次未再调用图片模型。"


def _append_unique_pages(target: list[int], values: object, page_count: int) -> None:
    """将明确页码安全并入候选列表，绝不接受裸数字。"""
    if isinstance(values, (int, float)) and not isinstance(values, bool):
        values = f"P{int(values)}"
    for page in _explicit_page_references(values, page_count):
        if page not in target:
            target.append(page)


def _complete_repeated_page_sequence(pages: list[int], page_count: int) -> list[int]:
    """补齐密集等间隔附件序列中的缺页，不把稀疏目录范围扩成大量图片调用。"""
    ordered = sorted(set(page for page in pages if 1 <= page <= page_count))
    if len(ordered) < 3 or ordered[-1] - ordered[0] > 20:
        return []
    differences = [right - left for left, right in zip(ordered, ordered[1:])]
    frequencies = {step: differences.count(step) for step in set(differences) if 1 <= step <= 3}
    if not frequencies:
        return []
    step, count = max(frequencies.items(), key=lambda item: (item[1], -item[0]))
    # 至少两段呈现同一间隔，才认为是连续证书/附件序列，避免根据偶然两页猜测。
    if count < 2 or any(value > step * 2 for value in differences):
        return []
    return [page for page in range(ordered[0], ordered[-1] + 1, step) if page not in ordered]


# 按 PDF 页序缓存解析文本拆分结果；worker 为单进程按需运行，缓存只覆盖当前
# 评审批次涉及的少量文件，任务结束随进程释放，不常驻内存。键含 mtime/size，
# 重新解析同一文档后不会命中旧内容；按字符预算逐出最旧条目，避免多文档项目
# 整体清空后反复重读整份解析文本。综合评审并行处理多份投标文件，访问需加锁。
_PAGE_TEXTS_CACHE: dict[tuple[str, str, int, int], dict[int, str]] = {}
_PAGE_TEXTS_CACHE_TOTAL_CHARS = 0
_PAGE_TEXTS_CACHE_MAX_CHARS = 64 * 1024 * 1024
_PAGE_TEXTS_CACHE_LOCK = threading.Lock()
_SCAN_PAGE_TEXT_LIMIT = 80
# 页眉/页脚中独立成行的数字或“第N页”视为印刷页码；行内还有其他内容的数字不算。
_PRINTED_FOOTER_PATTERN = re.compile(r"^[-—–_\s]*(\d{1,4})[-—–_\s]*$")
_PRINTED_PAGE_LABEL_PATTERN = re.compile(r"^第\s*(\d{1,4})\s*页(?:\s*[/共]\s*\d+\s*页?)?$")


def _document_page_texts(document: dict) -> dict[int, str]:
    """按 PDF 页序拆分本地解析文本；只读解析缓存文件，不调用任何外部服务。"""
    path = str(document.get("parsed_path") or "")
    if not path or not Path(path).is_file():
        return {}
    try:
        stat = Path(path).stat()
    except OSError:
        return {}
    key = (str(document.get("document_id") or ""), path, stat.st_mtime_ns, stat.st_size)
    global _PAGE_TEXTS_CACHE_TOTAL_CHARS
    with _PAGE_TEXTS_CACHE_LOCK:
        cached = _PAGE_TEXTS_CACHE.get(key)
        if cached is not None:
            return cached
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    parts = re.split(r"\[第(\d+)页\]\n", text)
    pages: dict[int, str] = {}
    for index in range(1, len(parts) - 1, 2):
        pages[int(parts[index])] = parts[index + 1]
    with _PAGE_TEXTS_CACHE_LOCK:
        while _PAGE_TEXTS_CACHE and _PAGE_TEXTS_CACHE_TOTAL_CHARS + len(text) > _PAGE_TEXTS_CACHE_MAX_CHARS:
            oldest_key = next(iter(_PAGE_TEXTS_CACHE))
            removed = _PAGE_TEXTS_CACHE.pop(oldest_key)
            _PAGE_TEXTS_CACHE_TOTAL_CHARS -= sum(len(value) for value in removed.values())
        _PAGE_TEXTS_CACHE[key] = pages
        _PAGE_TEXTS_CACHE_TOTAL_CHARS += len(text)
    return pages


def _estimate_printed_page_offset(page_texts: dict[int, str], page_count: int) -> int:
    """用页眉/页脚的独立印刷页码估计“印刷页码→PDF页序”的固定偏移；证据不足不纠偏。"""
    offsets: list[int] = []
    for pdf_page in sorted(page_texts):
        lines = [line.strip() for line in str(page_texts[pdf_page]).splitlines() if line.strip()]
        if not lines:
            continue
        printed: int | None = None
        # 页脚比页眉更常见，后检查页脚让其优先生效。
        for line in [*lines[:2], *lines[-2:]]:
            match = _PRINTED_PAGE_LABEL_PATTERN.match(line) or _PRINTED_FOOTER_PATTERN.match(line)
            if match:
                value = int(match.group(1))
                if 1 <= value <= page_count:
                    printed = value
        if printed is not None:
            offsets.append(pdf_page - printed)
    if len(offsets) < 8:
        return 0
    frequencies: dict[int, int] = {}
    for offset in offsets:
        frequencies[offset] = frequencies.get(offset, 0) + 1
    offset, count = max(frequencies.items(), key=lambda item: item[1])
    # 报价表等数字密集页会产生零散伪命中；要求足够样本且明显多数一致才采信。
    if offset == 0 or count < max(8, int(len(offsets) * 0.6)):
        return 0
    return offset


def _scan_like_pages(page_texts: dict[int, str]) -> set[int]:
    """解析文本几乎为空的页通常是纯扫描/图片页，正是图片识别的主要目标。"""
    return {
        page for page, text in page_texts.items()
        if len(re.sub(r"\s+", "", str(text))) < _SCAN_PAGE_TEXT_LIMIT
    }


def _form_continuation_pages(page_texts: dict[int, str], rule: dict, pages: list[int], page_count: int) -> list[int]:
    """为固定表单补齐紧随其后的连续页。

    长声明函、偏离表、报价表等经常在第二页继续列货物或填写项。若只看命中首页，
    会把后续的真实填写内容错误写成“未提供”。这里只在规则本身需要固定表单时，
    为命中页保守补充最多两个相邻续页；新章节、目录或其他材料会立即停止。
    """
    if "response_form" not in _rule_material_roles(rule) or not page_texts:
        return []
    expanded: list[int] = []
    for page in pages:
        if _page_material_role(page_texts.get(page)) != "response_form":
            continue
        prior_was_continuation = False
        for candidate in range(page + 1, min(page_count, page + 2) + 1):
            text = str(page_texts.get(candidate) or "")
            role = _page_material_role(text)
            compact = re.sub(r"\s+", "", text)
            # 同类表单页直接保留；文本抽取没有重复标题时，长表格续页通常仍含
            # 多个下划线、序号或填写字段。目录/合同/证书等新材料则不跨入。
            likely_continuation = (
                role == "response_form"
                or (
                    role in {"other", "scanned"}
                    and len(compact) >= 30
                    and ("_" in text or "（" in text or bool(re.search(r"(?:^|\n)\s*\d+[.、]", text)))
                )
                or (
                    prior_was_continuation
                    and role in {"other", "scanned"}
                    and any(term in text for term in ("盖章", "签字", "日期", "法定代表人"))
                )
            )
            if not likely_continuation:
                break
            if candidate not in expanded:
                expanded.append(candidate)
            prior_was_continuation = True
    return expanded


def _rule_text_anchor_pages(page_texts: dict[int, str], rule: dict, page_count: int) -> list[int]:
    """从本地解析文本直接召回规则材料页，作为目录/模型页码的补充。

    这是零外部调用的精确词锚：只使用规则中四字以上的材料名，按命中词长度和数量
    排序。它尤其适合承诺函、型号响应表等文字可见但全文扫描未恰好返回页码的材料，
    不会替代既有 OCR、目录或模型候选来源。
    """
    terms = [term for term in _rule_material_terms(rule) if len(term) >= 4][:80]
    if not terms:
        return []
    ranked: list[tuple[int, int]] = []
    for page, raw_text in page_texts.items():
        if not (1 <= int(page) <= page_count):
            continue
        text = re.sub(r"[\s\W_]+", "", str(raw_text or ""))
        hits = [term for term in terms if term in text]
        if hits:
            ranked.append((max(map(len, hits)) * 10 + len(hits), int(page)))
    return [page for _, page in sorted(ranked, key=lambda item: (-item[0], item[1]))[:8]]


def _vision_page_candidates(document: dict, rule: dict, result: dict) -> list[int]:
    """OCR已命中页、图片缺口语境、裸结构化页码按可靠度生成候选。"""
    if document.get("extension") != ".pdf" or not document.get("page_count"):
        return []
    page_count = int(document["page_count"])
    # 本地解析文本提供两项零成本定位能力：估计“印刷页码→PDF页序”的固定偏移，
    # 以及识别纯扫描/图片页。两者都只读解析缓存文件，不增加任何外部调用。
    page_texts = _document_page_texts(document)
    printed_offset = _estimate_printed_page_offset(page_texts, page_count) if page_texts else 0
    # 目录中“材料名称…页码”的明示定位，比模型从正文猜出的页码更可靠。
    # 例如目录列出“节能产品认证证书复印件……563/564”时，应先看证书本体，
    # 而不是先把目录、承诺函和技术应答表耗尽 OCR/识图名额。
    directory_pages = _directory_material_candidates(page_texts, rule, page_count, printed_offset) if page_texts else []
    # “扫描件待核验”“签章不可读”等语句直接描述当前图片缺口，且目录语境引用会
    # 按印刷页码偏移换算为 PDF 页序，是可靠性最高的候选来源，排在最前。
    # 先剥掉【腾讯OCR·…·P…】【本地OCR·…·P…】【图片识别·…·P…】等系统前缀：其中的页码是“已处理页清单”，
    # 不是“材料所在页”，混入候选会把 OCR 实际命中页（如正文“证书在P144明确”）挤出预算。
    # 已完成过直接 OCR/图片取证的页是稳定事实来源，重跑时可优先复用为候选；
    # 它只来自相同文件哈希和相同材料键，仍会与目录、文本锚点一同保留。
    pages: list[int] = []
    for page in _normalise_result_pages(result.get("evidence_pack_candidate_pages")):
        if page <= page_count and page not in pages:
            pages.append(page)
    for page in directory_pages:
        if page not in pages:
            pages.append(page)
    # 目录没有列页码、或材料位于响应表/承诺函时，直接文本锚可避免把图片预算先
    # 花在正文开头。目录显式页仍保持第一优先级。
    for page in _rule_text_anchor_pages(page_texts, rule, page_count) if page_texts else []:
        if page not in pages:
            pages.append(page)
    # 腾讯 OCR 归纳模型明确标出的命中页，是后续核对签章、勾选、版式和图片字段的
    # 最可靠入口；与仅表示“处理过哪些页”的 ocr_candidate_pages 严格分开。
    for source in result.get("ocr_evidence_pages") or []:
        _append_unique_pages(pages, source, page_count)
    # 本地 OCR 已实际处理过的候选页是稳定的“可读文字入口”。它的可靠度低于直接
    # 形成证据的页，故只作为后备，不挤占目录、证据语境或 OCR 明确命中页。
    local_candidate_pages: list[int] = []
    for source in result.get("ocr_candidate_pages") or []:
        _append_unique_pages(local_candidate_pages, source, page_count)
    ordinary: list[int] = []
    for source in (result.get("evidence"), result.get("reason")):
        clean_source = re.sub(r"【(?:腾讯OCR|本地OCR|OCR|图片识别)[^】]*】", " ", str(source or ""))
        priority_values, ordinary_values = _visual_context_candidates(clean_source, page_count, printed_offset)
        for page in priority_values:
            if page not in pages:
                pages.append(page)
        for page in ordinary_values:
            if page not in ordinary:
                ordinary.append(page)
    # 评分 evidence_items / 扫描台账保留的裸页码同属模型引用，但缺少语句语境，
    # 无法做目录纠偏，可靠性低于上面的证据语句，作为第二梯队候选。
    structured_pages: list[int] = []
    for source in result.get("visual_page_candidates") or []:
        _append_unique_pages(structured_pages, source, page_count)
    if not str(result.get("vision_status") or "").startswith("ocr_"):
        # OCR 合并后 page_hint 记录的是“OCR 已处理页清单”而非材料所在页，不作为候选。
        _append_unique_pages(structured_pages, result.get("page_hint"), page_count)
    for page in structured_pages:
        if page not in pages:
            pages.append(page)
    for page in local_candidate_pages:
        if page not in pages:
            pages.append(page)
    # 全文扫描可能命中同组附件的首尾页而漏掉中间纯扫描页；密集、稳定间隔序列可安全补齐。
    for page in _complete_repeated_page_sequence(structured_pages, page_count):
        if page not in pages:
            pages.append(page)
    # 文字证据页（证书汇总表、签章说明页等）的相邻纯扫描页往往就是证书/签章附件
    # 本体。每个锚点最多补一个相邻扫描页，并紧跟其锚点，不打乱既有优先级。
    anchors = _prioritise_material_pages(document, rule, pages + [page for page in ordinary if page not in pages],
                                         protected=result.get("ocr_evidence_pages"))
    if page_texts:
        form_continuations = _form_continuation_pages(page_texts, rule, anchors, page_count)
        scan_pages = _scan_like_pages(page_texts)
        expanded: list[int] = []
        for anchor in anchors:
            expanded.append(anchor)
            # 连续固定表单页紧随命中页插入，优先于泛化的扫描件邻页扩展。
            for continuation in form_continuations:
                if continuation > anchor and continuation - anchor <= 2 and continuation not in expanded:
                    expanded.append(continuation)
            if anchor in scan_pages:
                continue
            for neighbor in (anchor + 1, anchor - 1, anchor + 2, anchor - 2):
                if neighbor in scan_pages and 1 <= neighbor <= page_count and neighbor not in anchors and neighbor not in expanded:
                    expanded.append(neighbor)
                    break
        return expanded
    return anchors


def _scan_visual_page_candidates(scan_index: dict | None, rule_id: str, page_count: int) -> list[int]:
    """从全文扫描台账补充候选页，供没有评分 evidence_items 的规则使用。
    优先只取精确 page_hint；若整条规则一个精确页都没有，才采样粗页块兜底，
    避免粗区间稀释预算，也避免完全丢失没有精确页码的规则。"""
    if not isinstance(scan_index, dict):
        return []
    pages: list[int] = []
    fallback_pages: list[int] = []
    for finding in scan_index.get("findings") or []:
        if not isinstance(finding, dict) or finding.get("rule_id") != rule_id:
            continue
        _append_unique_pages(pages, finding.get("page_hint"), page_count)
        _append_unique_pages(fallback_pages, finding.get("page_range"), page_count)
    return pages or fallback_pages


def _with_scan_visual_candidates(results: list[dict], scan_index: dict | None, document: dict) -> list[dict]:
    page_count = int(document.get("page_count") or 0)
    if page_count <= 0:
        return results
    values = []
    for result in results:
        current = list(result.get("visual_page_candidates") or [])
        for page in _scan_visual_page_candidates(scan_index, str(result.get("rule_id") or ""), page_count):
            if page not in current:
                current.append(page)
        values.append({**result, "visual_page_candidates": current})
    return values


def _visual_followup_pages(document: dict, primary: list[int], all_candidates: list[int], parsed: dict, level: str) -> list[int]:
    """仅当首轮未覆盖时，优先满足模型点名的相邻补页，再用既有候选补满名额。"""
    setting = _VISION_LEVEL_SETTINGS[level]
    remaining = setting["followup_pages"]
    if remaining <= 0:
        return []
    page_count = int(document.get("page_count") or 0)
    pages: list[int] = []
    if _visual_response_needs_more(parsed):
        # 模型已看过首轮图片，它明确请求的页码比静态候选更接近目标材料；
        # 仍限制在已发页±2以内，避免模型凭空要求远处页面造成无边界调用。
        requested = parsed.get("requested_pages") if isinstance(parsed, dict) else []
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    page = int(item)
                    if 1 <= page <= page_count and page not in primary and any(abs(page - sent) <= 2 for sent in primary):
                        if page not in pages:
                            pages.append(page)
        # 其次扩展首轮已发页的相邻页；若只是系统主动覆盖剩余明确候选，不额外猜页。
        for offset in (1, -1):
            for page in primary:
                adjacent = page + offset
                if 1 <= adjacent <= page_count and adjacent not in primary and adjacent not in pages:
                    pages.append(adjacent)
    # 候选列表已按“图片缺口 + 跨页段取样”排好优先级；最后用它补满剩余名额。
    for page in all_candidates:
        if page not in primary and page not in pages:
            pages.append(page)
    return pages[:remaining]


def _visual_response_needs_more(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    if parsed.get("needs_more_image") is True:
        return True
    return str(parsed.get("coverage") or "").lower() in {"not_covered", "uncertain"}


def _visual_response_coverage(parsed: dict) -> str:
    coverage = str(parsed.get("coverage") or "").lower()
    if coverage in {"covered", "not_covered", "uncertain"}:
        return coverage
    # 兼容旧的自定义提示词：有可引用图片事实时视为已覆盖，否则仍需补页。
    return "covered" if _clean_model_text(parsed.get("evidence")) or _clean_model_text(parsed.get("reason")) else "uncertain"


def _visual_response_scope(parsed: dict) -> str:
    if _visual_response_coverage(parsed) != "covered":
        return "none"
    scope = str(parsed.get("conclusion_scope") or "").lower()
    if scope in {"full", "partial"}:
        return scope
    return "partial" if parsed.get("needs_more_image") is True else "full"


def _is_field_value_conflict(item: object) -> bool:
    """只有文字值与图片值都实际可见且明确不同，才算字段级冲突。
    图片中未出现或看不清的字段属于覆盖不足（由 coverage/needs_more_image 表达），
    模型习惯把它们标成 "no"/"mismatch" 或留空值，不能据此升级为冲突。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("match") or "").lower() not in {"conflict", "mismatch"}:
        return False
    return bool(_clean_model_text(item.get("text_value")) and _clean_model_text(item.get("image_value")))


def _visual_response_conflict_level(parsed: dict) -> str:
    level = str(parsed.get("conflict_level") or "").lower()
    checks = parsed.get("field_checks")
    has_concrete_conflict = isinstance(checks, list) and any(_is_field_value_conflict(item) for item in checks)
    # material 会冻结原结论和建议分，必须有“文字值/图片值均可见且明确不同”的字段证据；
    # 模型只给一个枚举但没有对照值时降为 possible，防止无依据地阻断已形成的结论。
    if level == "material":
        return "material" if has_concrete_conflict else "possible"
    if level == "possible" or has_concrete_conflict:
        return "possible"
    return "none"


def _visual_field_conflict_text(parsed: dict) -> str:
    values: list[str] = []
    for item in parsed.get("field_checks") or []:
        if not _is_field_value_conflict(item):
            continue
        field = _clean_model_text(item.get("field")) or "关键字段"
        text_value = _clean_model_text(item.get("text_value"))
        image_value = _clean_model_text(item.get("image_value"))
        values.append(f"{field}：文字层“{text_value}”/ 图片“{image_value}”")
    return "；".join(values)[:800]


def _reported_irrelevant_pages(parsed: dict, sent_pages: list[int]) -> list[int]:
    """只接受模型明确标记且确为本批发送页的无关页，避免无关页污染证据页。"""
    if not isinstance(parsed, dict):
        return []
    return [page for page in _normalise_result_pages(parsed.get("irrelevant_pages")) if page in sent_pages]


_STALE_PENDING_PATTERN = re.compile(r"(?:未见|未提供|未检索到|尚未见|(?:仍)?需(?:OCR|图片|识图)?核验|待(?:OCR|图片|识图)?核验)")
_CONFIRMED_FACT_PATTERN = re.compile(r"(?:可见|已确认|已核验|已提供|齐全|有效期内|清晰可读)")


def _reconcile_stale_pending_text(base: object, supplement: object, rule: dict, *, full: bool = False) -> str:
    """移除已被 OCR/图片直接证实的旧“未见/待核验”短句，避免同一结果自相矛盾。"""
    original = _clean_model_text(base)
    facts = _clean_model_text(supplement)
    if not original or not facts or not _CONFIRMED_FACT_PATTERN.search(facts):
        return original
    role_terms = [term for role in _rule_material_roles(rule) for term in _MATERIAL_ROLE_TERMS.get(role, ())]
    role_terms = [term for term in role_terms if term in facts]
    if not role_terms:
        return original
    pieces = re.split(r"(?<=[。；;])\s*|\n+", original)
    retained: list[str] = []
    for piece in pieces:
        if _STALE_PENDING_PATTERN.search(piece) and any(term in piece for term in role_terms):
            continue
        # 仅在整条规则已完整覆盖时，才移除不带材料名的通用 OCR 尾注。
        if full and "部分关键证据建议通过 OCR" in piece:
            continue
        retained.append(piece)
    return "\n".join(value for value in retained if value).strip()


def _requires_discrete_document_evidence(rule: dict) -> bool:
    """判断客观分是否必须按一份份材料的完整要件计分。

    业绩、证书、许可等累计型评分不能仅凭目录或案例列表把未覆盖的材料一并计入。
    该判断只基于评分规则的通用计分语义，不依赖某个项目、行业或固定页码。
    """
    if str(rule.get("category") or "") != "objective":
        return False
    text = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    has_increment = bool(re.search(r"每(?:提供|有|具备)?[^。；;]{0,18}(?:份|项|个).{0,18}得\s*\d", text))
    has_material = any(term in text for term in (
        "合同", "业绩", "证书", "许可证", "资质", "证明材料", "验收", "检测报告",
    ))
    return has_increment and has_material


def _confirmed_partial_score(rule: dict, parsed: dict, previous: object, max_score: float,
                             checked_pages: object = None, *, evidence_gated: bool = False) -> float | None:
    """部分图片覆盖可上调已完整证实的客观评分叶子项，绝不凭零散描述重算整条规则。"""
    prior = _bounded_model_score(previous, max_score) if max_score > 0 else None
    scoring = _rule_scoring(rule)
    items = scoring.get("items") if isinstance(scoring.get("items"), list) else []
    caps: dict[str, float] = {}
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        value = _bounded_model_score(item.get("max_score"), max_score)
        if value is not None:
            caps[str(item.get("item_id") or f"SI-{index}")] = value
    score_items = parsed.get("score_items") if isinstance(parsed, dict) else None
    if not caps or not isinstance(score_items, list):
        return prior
    allowed_pages = set(_normalise_result_pages(checked_pages))
    confirmed: dict[str, float] = {}
    for item in score_items:
        if not isinstance(item, dict) or str(item.get("status") or "").lower() != "confirmed":
            continue
        item_id = str(item.get("item_id") or "")
        evidence_pages = _normalise_result_pages(item.get("evidence_pages"))
        if item_id not in caps or not evidence_pages:
            continue
        if allowed_pages and not any(page in allowed_pages for page in evidence_pages):
            continue
        score = _bounded_model_score(item.get("suggested_score"), caps[item_id])
        if score is not None:
            confirmed[item_id] = score
    # 部分客观分（尤其是“近三年每提供一项业绩得分、最多 N 分”）在规则库中只有
    # 一个封顶叶子项，但图片模型会按每一份合同/证书返回自己的临时 item_id。
    # 旧实现要求 ID 完全相等，导致“图片已清楚确认 1 份有效业绩”的事实无法进入
    # 建议分。这里仅对 *单一封顶叶子* 开放保守回退：必须有实际证据页，且每个
    # 模型建议分都在该封顶内。多叶子规则仍严格按 ID 汇总，避免把不同子项串分。
    if not confirmed and len(caps) == 1:
        cap = next(iter(caps.values()))
        fallback_scores: list[float] = []
        for item in score_items:
            if not isinstance(item, dict) or str(item.get("status") or "").lower() != "confirmed":
                continue
            evidence_pages = _normalise_result_pages(item.get("evidence_pages"))
            if not evidence_pages or (allowed_pages and not any(page in allowed_pages for page in evidence_pages)):
                continue
            score = _bounded_model_score(item.get("suggested_score"), cap)
            if score is not None:
                fallback_scores.append(score)
        if fallback_scores:
            # 单一封顶项不能把多份材料逐条累加为超额得分；采用模型已受规则上限
            # 约束的最高可确认值，并在最终分数处再次封顶。
            confirmed["__single_cap__"] = min(cap, max(fallback_scores))
    if not confirmed:
        return prior
    value = min(max_score, sum(confirmed.values()))
    # 合同、证书等“每提供一份/项得分”的客观分必须由对应材料的完整要件支撑。
    # 当图片阶段已经明确只确认其中部分材料时，沿用文字阶段较高的暂定分会造成
    # “理由写 3 分、页面却保存 6 分”的自相矛盾。只对这类证据闸门评分允许下调；
    # 主观分和普通分档规则仍沿用原有“只上调已证实事实”的稳定策略。
    if evidence_gated:
        return value
    return max(prior or 0.0, value)


def _merge_usable_visual_responses(responses: list[tuple[list[int], dict]]) -> tuple[dict | None, str, list[int], list[int]]:
    """合并多批图片事实，并严格区分已检查页和模型实际形成证据的页。"""
    usable = [(pages, parsed) for pages, parsed in responses if _visual_response_scope(parsed) in {"full", "partial"}]
    if not usable:
        return None, "none", [], []
    full = [(pages, parsed) for pages, parsed in usable if _visual_response_scope(parsed) == "full"]
    selected_pages, selected = (full or usable)[-1]
    merged = dict(selected)
    evidence_values: list[str] = []
    merged_reason = ""
    checked_pages: list[int] = []
    evidence_pages: list[int] = []
    field_checks: list[dict] = []
    conflict_level = "none"
    conflict_rank = {"none": 0, "possible": 1, "material": 2}
    for pages, parsed in usable:
        for page in pages:
            if page not in checked_pages:
                checked_pages.append(page)
        # evidence_pages 必须是本批实际发送页的子集。模型没给出时不能把全批检查页伪装成证据页。
        irrelevant_pages = set(_reported_irrelevant_pages(parsed, pages))
        reported_evidence = _normalise_result_pages(parsed.get("evidence_pages"))
        for page in reported_evidence:
            if page in pages and page not in irrelevant_pages and page not in evidence_pages:
                evidence_pages.append(page)
        evidence = _clean_model_text(parsed.get("evidence"))
        reason = _clean_model_text(parsed.get("reason"))
        if evidence and evidence not in evidence_values:
            evidence_values.append(evidence)
        if reason:
            merged_reason = _merge_reason_text(merged_reason, reason)
        for item in parsed.get("field_checks") or []:
            if isinstance(item, dict) and item not in field_checks:
                field_checks.append(item)
        current_conflict = _visual_response_conflict_level(parsed)
        if conflict_rank[current_conflict] > conflict_rank[conflict_level]:
            conflict_level = current_conflict
    merged["evidence"] = "\n".join(evidence_values)
    merged["reason"] = merged_reason
    merged["field_checks"] = field_checks
    merged["conflict_level"] = conflict_level
    conclusion_scope = "full" if full else "partial"
    if not evidence_pages:
        # 多页图片没有可追溯证据页时，不允许模型以笼统描述直接完成整条判断或改分。
        # 单页请求则可在明确 covered 时安全回填唯一页，兼容少数未输出 evidence_pages 的模型。
        if conclusion_scope == "full" and len(checked_pages) == 1:
            evidence_pages = list(checked_pages)
        elif conclusion_scope == "full":
            conclusion_scope = "partial"
    return merged, conclusion_scope, checked_pages or selected_pages, evidence_pages


def _vision_render_setting(rule: dict, level: str) -> dict:
    """纯视觉规则（签章/外观类）即使用户选择快速档，也保证足以辨认签章的清晰度。

    页数预算仍由人工选择的档位决定；这里只提高单页渲染质量，不增加页数。
    """
    setting = dict(_VISION_LEVEL_SETTINGS[level])
    if _rule_image_strategy(rule) == "vision" and float(setting["scale"]) < 1.8:
        setting["scale"] = 1.8
        setting["quality"] = max(int(setting["quality"]), 80)
        if setting["detail"] == "low":
            setting["detail"] = "standard"
    elif level == "standard":
        # 材料/字段/文字类规则不涉及签章外观，标准档适度降采样，减小图片体积与
        # 单次视觉调用延迟；OCR 文字层已先完成，识别所需关键字段仍可读。
        setting["scale"] = min(float(setting["scale"]), 1.2)
        setting["quality"] = min(int(setting["quality"]), 76)
        if setting["detail"] == "standard":
            setting["detail"] = "low"
    return setting


def _safe_vision_render_scale(page, requested_scale: float) -> float:
    """在生成 Pixmap 前限制像素数，避免超大图纸造成瞬时内存峰值。"""
    try:
        area = max(1.0, float(page.rect.width) * float(page.rect.height))
        max_scale = (_VISION_MAX_PIXELS_PER_PAGE / area) ** 0.5
    except (AttributeError, TypeError, ValueError):
        max_scale = requested_scale
    # `max_scale` 已按页面面积计算。不要把它反向抬到最小比例，否则超大页面
    # 会重新突破像素上限；0.01 只是异常页面的 API 安全下限。
    return max(0.01, min(float(requested_scale), max_scale))


def _task_vision_render_cache(task: dict | None) -> tuple[dict, threading.Lock] | None:
    """取得任务级、小容量的 JPEG 缓存；任务结束后随 worker 内存自然释放。"""
    if not isinstance(task, dict):
        return None
    lock = task.get("_vision_render_cache_lock")
    if not hasattr(lock, "acquire") or not hasattr(lock, "release"):
        lock = threading.Lock()
        task["_vision_render_cache_lock"] = lock
    cache = task.get("_vision_render_cache")
    if not isinstance(cache, dict):
        cache = {"items": {}, "bytes": 0}
        task["_vision_render_cache"] = cache
    return cache, lock


def _render_vision_images(app, document: dict, pages: list[int], level: str, setting: dict | None = None,
                          *, task: dict | None = None) -> list[dict]:
    setting = dict(setting or _VISION_LEVEL_SETTINGS[level])
    source = storage.document_path(app, document)
    images: list[dict] = []
    task_cache = _task_vision_render_cache(task)
    with fitz.open(source) as pdf:
        for page_number in pages[:setting["max_pages"]]:
            page = pdf[page_number - 1]
            # 必须为每一页复制设置。旧代码在一张大图降采样后会修改外层 setting，
            # 使同批后续证书页也被无故降清晰度。
            page_setting = dict(setting)
            page_setting["scale"] = _safe_vision_render_scale(page, float(page_setting["scale"]))
            cache_key = (
                str(document.get("document_id") or ""), int(page_number),
                round(float(page_setting["scale"]), 3), int(page_setting["quality"]),
            )
            content = None
            if task_cache:
                cache, lock = task_cache
                with lock:
                    content = cache["items"].get(cache_key)
            if content is None:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(page_setting["scale"], page_setting["scale"]), alpha=False)
                content = pixmap.tobytes("jpeg", jpg_quality=page_setting["quality"])
                # 一张图片控制在约 4MB 内，既低于接口上限，也避免 2GB 小服务器出现大块内存峰值。
                while len(content) > 4 * 1024 * 1024 and page_setting["scale"] > _VISION_MIN_RENDER_SCALE:
                    page_setting = {
                        **page_setting,
                        "scale": max(_VISION_MIN_RENDER_SCALE, page_setting["scale"] * 0.75),
                        "quality": max(60, page_setting["quality"] - 8),
                    }
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(page_setting["scale"], page_setting["scale"]), alpha=False)
                    content = pixmap.tobytes("jpeg", jpg_quality=page_setting["quality"])
                if task_cache and len(content) <= 4 * 1024 * 1024:
                    cache, lock = task_cache
                    with lock:
                        # FIFO 淘汰，且只缓存 JPEG 字节，不持有 PDF/Pixmap 对象。
                        while cache["items"] and cache["bytes"] + len(content) > _VISION_TASK_RENDER_CACHE_BYTES:
                            oldest_key = next(iter(cache["items"]))
                            removed = cache["items"].pop(oldest_key)
                            cache["bytes"] = max(0, int(cache["bytes"]) - len(removed))
                        if cache["bytes"] + len(content) <= _VISION_TASK_RENDER_CACHE_BYTES:
                            cache["items"][cache_key] = content
                            cache["bytes"] = int(cache["bytes"]) + len(content)
            images.append({
                "page": page_number,
                "mime_type": "image/jpeg",
                "image_bytes": content,
                "detail": page_setting["detail"],
            })
    return images


def _vision_content(prompt: str, images: list[dict], profile: dict) -> list[dict]:
    """委托网关构造图片协议；worker 只负责页号、候选和证据业务语义。"""
    return build_vision_user_content(profile, prompt, images)


def _vision_locator_groups(page_count: int) -> list[list[int]]:
    """高强度且无文字页码时，按连续页做低清缩略图索引；不常驻、不落盘。"""
    return [
        list(range(start, min(page_count, start + _VISION_LOCATOR_THUMBNAILS_PER_SHEET - 1) + 1))
        for start in range(1, page_count + 1, _VISION_LOCATOR_THUMBNAILS_PER_SHEET)
    ]


def _render_vision_locator_sheets(app, document: dict, groups: list[list[int]]) -> list[dict]:
    """将一组连续页压成带页码标记的低清联系表，用于纯扫描件找页。"""
    source = storage.document_path(app, document)
    sheets: list[dict] = []
    with fitz.open(source) as pdf:
        for group in groups:
            contact = fitz.open()
            try:
                page = contact.new_page(width=900, height=1180)
                columns = 3
                cell_width, cell_height = 280, 280
                for index, page_number in enumerate(group):
                    row, column = divmod(index, columns)
                    left, top = 10 + column * 295, 12 + row * 292
                    source_page = pdf[page_number - 1]
                    thumbnail = source_page.get_pixmap(matrix=fitz.Matrix(0.32, 0.32), alpha=False)
                    rect = fitz.Rect(left, top + 18, left + cell_width, top + cell_height)
                    page.insert_text((left, top + 12), f"P{page_number}", fontsize=11, color=(0, 0, 0))
                    page.insert_image(rect, stream=thumbnail.tobytes("jpeg", jpg_quality=62), keep_proportion=True)
                output = b""
                for scale, quality in ((0.9, 68), (0.65, 55), (0.45, 45)):
                    output = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("jpeg", jpg_quality=quality)
                    if len(output) <= 4 * 1024 * 1024:
                        break
            finally:
                contact.close()
            sheets.append({"pages": group, "mime_type": "image/jpeg", "image_bytes": output, "detail": "low"})
    return sheets


def _vision_locator_content(prompt: str, sheets: list[dict], profile: dict) -> list[dict]:
    images: list[dict] = []
    for sheet in sheets:
        # 联系表不是单一 PDF 页，但仍以稳定文字标签告诉模型其覆盖范围。
        images.append({
            "type": "text", "text": "以下联系表含投标文件页面：" + "、".join(f"P{page}" for page in sheet["pages"]),
        })
        images.append({key: value for key, value in sheet.items() if key != "pages"})
    # 联系表的页范围标签不同于单页图片标识，因此直接走网关的内容适配，但保留标签。
    content = [{"type": "text", "text": prompt}]
    for item in images:
        if item.get("type") == "image_url" or isinstance(item.get("image_bytes"), bytes):
            content.extend(build_vision_user_content(profile, "", [item])[1:])
        else:
            content.append(item)
    return content


def _locate_visual_pages(app, task: dict, document: dict, rule: dict, vision_profile: dict) -> list[int]:
    """仅在精细模式、且文字流程完全未定位到页码时，按低清联系表找页后再精识别。"""
    if document.get("extension") != ".pdf" or not document.get("page_count"):
        return []
    groups = _vision_locator_groups(int(document["page_count"]))
    for batch_number, offset in enumerate(range(0, len(groups), _VISION_LOCATOR_SHEETS_PER_REQUEST), start=1):
        batch = groups[offset:offset + _VISION_LOCATOR_SHEETS_PER_REQUEST]
        sheets = _render_vision_locator_sheets(app, document, batch)
        if not sheets:
            continue
        prompt = storage.render_prompt_template(
            app, "evaluate_all_visual_locator_user", rule=json.dumps(_visual_rule_packet(rule), ensure_ascii=False, separators=(",", ":")),
            document_name=document.get("original_name") or "投标文件", bidder_name=document.get("bidder_name") or document.get("original_name") or "投标人",
            candidate_pages=json.dumps([sheet["pages"] for sheet in sheets], ensure_ascii=False),
        )
        try:
            with _VISION_REQUEST_GATE:
                parsed = _request_task_json(
                    app, task, vision_profile, "evaluate_all_visual_locator", _system_prompt(app, "evaluate_all"),
                    _vision_locator_content(prompt, sheets, vision_profile), document_id=document["document_id"],
                    context_mode=f"vision_locator_{batch_number}", max_tokens=_output_token_budget(vision_profile, 360),
                    thinking_mode="disabled",
                )
        except ValueError:
            return []
        available = {page for group in batch for page in group}
        requested = parsed.get("requested_pages") if isinstance(parsed, dict) else []
        selected = [
            int(page) for page in requested
            if isinstance(page, (int, float)) and not isinstance(page, bool) and int(page) in available
        ] if isinstance(requested, list) else []
        if selected:
            return list(dict.fromkeys(selected))[:_VISION_LEVEL_SETTINGS["high"]["max_pages"]]
    return []


def _visual_rule_packet(rule: dict) -> dict:
    scoring = _rule_scoring(rule)
    return {
        "rule_id": rule["rule_id"], "category": rule.get("category"), "title": rule.get("title"),
        "check_rule": rule.get("check_rule") or rule.get("title"), "source_text": rule.get("source_text"),
        "evidence_items": _rule_evidence_items(rule),
        "scoring": scoring if scoring else None,
    }


def _normalise_result_pages(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    unique_pages: list[int] = []
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 0:
            page = int(value)
            if page not in unique_pages:
                unique_pages.append(page)
    return unique_pages


def _append_evidence_layer(result: dict, *, source: str, summary: object, checked_pages: object,
                           evidence_pages: object, service: str = "", model: str = "") -> dict:
    """以结构化方式保留 OCR/图片证据，旧 evidence/reason 字段仍完整兼容。"""
    current = result.get("evidence_layers")
    layers = [dict(value) for value in current if isinstance(value, dict)] if isinstance(current, list) else []
    layer = {
        "source": source,
        "summary": _clean_model_text(summary)[:1600],
        "checked_pages": _normalise_result_pages(checked_pages),
        "evidence_pages": _normalise_result_pages(evidence_pages),
        "service": str(service or "")[:160],
        "model": str(model or "")[:160],
    }
    # 同一来源的本次补充替换旧层，避免重试或二次合并反复堆叠。
    layers = [value for value in layers if value.get("source") != source]
    layers.append(layer)
    return {**result, "evidence_layers": layers}


def _with_vision_execution(result: dict, status: str, pages: list[int], profile: dict, message: str,
                           *, evidence_pages: object | None = None) -> dict:
    """记录图片流程结果，并为新结果拆分 OCR 与多模态状态。

    vision_status 继续保留为历史 API 字段；新增字段不会改变旧页面和外部调用的
    取值，却能让界面准确说明本次到底执行了哪一种取证能力。
    """
    unique_pages = _normalise_result_pages(pages)
    if evidence_pages is None:
        unique_evidence_pages = _normalise_result_pages(result.get("vision_evidence_pages"))
    else:
        unique_evidence_pages = _normalise_result_pages(evidence_pages)
    ocr_status = str(result.get("ocr_status") or "not_requested")
    multimodal_status = str(result.get("multimodal_status") or "not_requested")
    if status.startswith("ocr_"):
        ocr_status = status
    else:
        multimodal_status = status
    return {
        **result,
        "vision_status": status,
        "ocr_status": ocr_status,
        "multimodal_status": multimodal_status,
        "vision_pages": unique_pages,
        "vision_evidence_pages": unique_evidence_pages,
        "vision_model": str(profile.get("display_name") or profile.get("model_name") or ""),
        "vision_message": message,
    }


def _run_visual_supplement(app, task: dict, document: dict, component: str, rule: dict, result: dict,
                           vision_profile: dict) -> dict:
    trigger, level = _rule_vision_policy(rule)
    if trigger == "off" or level == "off":
        return result
    if trigger == "text_fallback" and not _needs_visual_fallback(component, result):
        if str(result.get("vision_status") or "").startswith("ocr_"):
            # OCR 已把文字性关键事实补足时，无需再覆盖其状态为“未调用图片”。
            return result
        return _with_vision_execution(
            result, "skipped_text_sufficient", [], vision_profile, "文字证据已足够，本次无需调用图片模型。",
        )
    all_pages = _prioritise_material_pages(
        document, rule, _acquisition_candidate_pages(document, rule, result),
        protected=result.get("ocr_evidence_pages"),
    )
    # 纯扫描件可能没有可供文字流程定位的页码。仅精细模式进入低清联系表找页，
    # 找到候选后才发送高清目标页；快速/标准模式仍严格保持零额外图片调用。
    if not all_pages and level == "high":
        all_pages = _locate_visual_pages(app, task, document, rule, vision_profile)
    page_limit = _form_bundle_page_limit(
        rule, level, all_pages,
        _compound_acquisition_page_limit(rule, level, "vision", _VISION_LEVEL_SETTINGS[level]["max_pages"]),
    )
    pages = all_pages[:page_limit]
    if not pages:
        return _with_vision_execution(
            result, "not_located", [], vision_profile, "未定位到可靠候选页，未发送图片，保留文字结论。",
        )
    attempted_pages: list[int] = []
    responses: list[tuple[list[int], dict]] = []

    def request_pages(selected_pages: list[int], *, attempt: int) -> dict | None:
        images = _render_vision_images(
            app, document, selected_pages, level, setting=_vision_render_setting(rule, level), task=task,
        )
        if not images:
            return None
        for image in images:
            page = int(image["page"])
            if page not in attempted_pages:
                attempted_pages.append(page)
        # 第二批必须看到第一批已识别事实，否则两批页面永远无法形成一个完整结论。
        # 复用既有 text_result 占位符，兼容用户已保存的自定义提示词。
        prior_context = dict(result)
        if responses:
            prior_context["prior_image_batches"] = [
                {"pages": batch_pages, "result": parsed_value}
                for batch_pages, parsed_value in responses
            ]
        prompt = storage.render_prompt_template(
            app, "evaluate_all_visual_user",
            rule=json.dumps(_visual_rule_packet(rule), ensure_ascii=False, separators=(",", ":")),
            document_name=document.get("original_name") or "投标文件",
            bidder_name=document.get("bidder_name") or document.get("original_name") or "投标人",
            vision_trigger=trigger, vision_level=level,
            text_result=json.dumps(prior_context, ensure_ascii=False, separators=(",", ":")),
        )
        prompt += "\n\n【系统输出与证据协议】\n" + storage.render_prompt_template(app, "evaluate_all_visual_contract")
        try:
            with _VISION_REQUEST_GATE:
                parsed_value = _request_task_json(
                    app, task, vision_profile, f"evaluate_all_{component}_vision_{attempt}", _system_prompt(app, "evaluate_all"),
                    _vision_content(prompt, images, vision_profile), document_id=document["document_id"],
                    context_mode=f"vision_{level}_{attempt}", max_tokens=_output_token_budget(vision_profile, 1_800),
                    thinking_mode="disabled",
                )
        except ValueError:
            # 图片补充失败绝不覆盖原始文字结论，也不让整份综合评审失败。
            return None
        return parsed_value if isinstance(parsed_value, dict) else None

    parsed = request_pages(pages, attempt=1)
    if not parsed:
        return _with_vision_execution(
            result, "failed", attempted_pages, vision_profile, "图片模型调用或结果解析失败，已保留文字结论。",
        )
    responses.append((list(pages), parsed))
    # 首轮已完整覆盖且无冲突时提前结束，不再为剩余候选页消耗多模态额度；
    # 结论不完整（partial/none）或模型主动报告未覆盖时，仍主动覆盖下一批。
    # 每批保持较少图片，避免把标准档的全部预算一次压给多模态服务造成超载。
    first_batch_conclusive = (
        _visual_response_coverage(parsed) == "covered"
        and _visual_response_scope(parsed) == "full"
        and _visual_response_conflict_level(parsed) == "none"
    )
    irrelevant_after_first = set(_reported_irrelevant_pages(parsed, pages))
    remaining_candidates = [page for page in all_pages if page not in irrelevant_after_first]
    if _visual_response_needs_more(parsed) or (
        any(page not in pages for page in remaining_candidates) and not first_batch_conclusive
    ):
        followup = _visual_followup_pages(document, pages, remaining_candidates, parsed, level)
        if followup:
            retry = request_pages(followup, attempt=2)
            if retry:
                pages = followup
                parsed = retry
                responses.append((list(followup), retry))
    parsed, conclusion_scope, checked_pages, visual_evidence_pages = _merge_usable_visual_responses(responses)
    # 若所有图片都未覆盖目标材料，不能把“未看到”当成规则的负面证据，也不要把无效提示
    # 混入最终理由。原有文字结论和“需 OCR”提示会完整保留。
    if not parsed:
        page_text = "、".join(f"P{page}" for page in attempted_pages)
        return _set_result_coverage(_with_vision_execution(
            result, "uncovered", attempted_pages, vision_profile,
            f"已检查{page_text or '候选页'}，但尚未覆盖可形成结论的关键材料，已保留文字结论。",
        ), "uncovered")
    conflict_level = _visual_response_conflict_level(parsed)
    # 仅 material（影响合规/计分的实质字段冲突）才升级为 conflict 状态并冻结结论；
    # possible 是一般疑似线索，保留图片补充成果，仅以文字形式提示人工留意，
    # 避免“正面确认”或“覆盖不足”被误标成冲突而丢弃图片层的工作。
    has_conflict = conflict_level == "material"
    if has_conflict:
        # 图片层与文字层字段不一致时，图片结果只能作为待复核线索，不能覆盖原结论或建议分。
        conclusion_scope = "partial"
    # 页面和报告分别展示检查页/证据页；业务页码提示优先使用真正形成证据的页。
    all_evidence_pages = _normalise_result_pages(result.get("vision_evidence_pages"))
    for page in _normalise_result_pages(result.get("ocr_evidence_pages")):
        if page not in all_evidence_pages:
            all_evidence_pages.append(page)
    for page in visual_evidence_pages:
        if page not in all_evidence_pages:
            all_evidence_pages.append(page)
    page_hint = "P" + "、P".join(str(page) for page in (visual_evidence_pages or checked_pages))
    # 客观分页面只需要“材料类别、数量/有效性、计分结论”。原始图片/OCR字段仍
    # 记录在 evidence_layers，避免把一页证书或合同逐字复述到最终评分表中。
    visual_evidence_limit = 520 if component == "objective" else 1600
    visual_reason_limit = 420 if component == "objective" else 1200
    visual_evidence = _clean_model_text(parsed.get("evidence"))[:visual_evidence_limit]
    visual_reason = _clean_model_text(parsed.get("reason"))[:visual_reason_limit]
    conflict_text = _visual_field_conflict_text(parsed)
    if conflict_text:
        visual_reason = "\n".join(value for value in (visual_reason, f"图片与文字候选字段疑似不一致：{conflict_text}") if value)[:1200]
    prefix = f"【图片识别·{level}·{page_hint}】"
    reconciled_evidence = _reconcile_stale_pending_text(
        result.get("evidence"), f"{visual_evidence}\n{visual_reason}", rule,
        full=conclusion_scope == "full",
    )
    reconciled_reason = _reconcile_stale_pending_text(
        result.get("reason"), f"{visual_evidence}\n{visual_reason}", rule,
        full=conclusion_scope == "full",
    )
    ocr_preceded = str(result.get("vision_status") or "") in {
        "ocr_applied", "ocr_applied_partial", "ocr_uncovered",
    }
    visual_status = "conflict" if has_conflict else ("applied" if conclusion_scope == "full" else "applied_partial")
    vision_status = f"ocr_vision_{visual_status}" if ocr_preceded else visual_status
    vision_message = (
        "图片检查发现文字层与图片层关键字段疑似不一致，已保留原结论并标记人工重点复核。"
        if has_conflict else (
            "图片识别已形成完整补充并写入本条结论。" if conclusion_scope == "full"
            else (
                "图片识别已补充可见事实；已检查"
                f"{'、'.join(f'P{page}' for page in attempted_pages) or '候选页'}，"
                "但材料覆盖不完整，原文字结论和建议分保持不变。"
            )
        )
    )
    if conflict_level == "possible":
        vision_message += "另有一般疑似字段差异，已并列写入理由供人工留意。"
    if ocr_preceded:
        vision_message = f"已先完成 OCR 文字核验；{vision_message}"
    if component == "review":
        status = str(parsed.get("status") or "manual") if conclusion_scope == "full" else str(result.get("status") or "manual")
        if status not in {"satisfied", "not_satisfied", "partial", "not_found", "manual"}:
            status = "manual"
        merged = _review_result_from_model({
            "evidence": _merge_supplement_text(reconciled_evidence, f"{prefix}{visual_evidence}" if visual_evidence else ""),
            "page_hint": page_hint,
            "reason": _merge_reason_text(
                reconciled_reason, f"{prefix}{visual_reason}" if visual_reason else "",
                supplement_first=has_conflict,
            ),
            "risk_level": parsed.get("risk_level") if conclusion_scope == "full" else result.get("risk_level"),
            "confidence": "low" if has_conflict else (parsed.get("confidence") if conclusion_scope == "full" else result.get("confidence")),
            "evidence_quality": "sufficient" if conclusion_scope == "full" and visual_evidence else result.get("evidence_quality"),
        }, rule["rule_id"], status)
        # 仅在本次任务内供 OCR 影子核对与 EvidencePack 使用；存储层会忽略该临时字段，
        # 因此不会改变现有对外结果 API 或直接改写结论。
        merged["visual_field_checks"] = [
            dict(item) for item in parsed.get("field_checks") or [] if isinstance(item, dict)
        ][:12]
        merged = _append_evidence_layer(
            merged, source="vision", summary=visual_evidence or visual_reason, checked_pages=checked_pages,
            evidence_pages=visual_evidence_pages, model=str(vision_profile.get("display_name") or vision_profile.get("model_name") or ""),
        )
        return _with_vision_execution(
            _set_result_coverage(merged, "covered" if conclusion_scope == "full" else "partial"), vision_status, attempted_pages, vision_profile, vision_message,
            evidence_pages=all_evidence_pages,
        )
    max_score = float(_rule_scoring(rule).get("max_score") or result.get("max_score") or 0)
    suggested = _bounded_model_score(parsed.get("suggested_score"), max_score) if max_score > 0 and conclusion_scope == "full" else None
    if suggested is None:
        suggested = _confirmed_partial_score(
            rule, parsed, result.get("suggested_score"), max_score, checked_pages,
            evidence_gated=component == "objective" and _requires_discrete_document_evidence(rule),
        )
    merge_limit = 1_000 if component == "objective" else 2_000
    merged = {
        **result,
        "suggested_score": suggested,
        "evidence": _merge_supplement_text(
            reconciled_evidence, f"{prefix}{visual_evidence}" if visual_evidence else "", limit=merge_limit,
        ),
        "reason": _merge_reason_text(
            reconciled_reason, f"{prefix}{visual_reason}" if visual_reason else "",
            limit=merge_limit, supplement_first=has_conflict,
        ),
        "confidence": "low" if has_conflict else (
            _enum_text(parsed.get("confidence"), {"high", "medium", "low"}, result.get("confidence"))
            if conclusion_scope == "full" else result.get("confidence")
        ),
        "requires_review": True,
        "automation_status": "needs_review",
        "review_reason": "图片与文字候选字段疑似冲突，需人工重点复核。" if has_conflict else "图片识别结果已补充，需人工复核。",
        # 与审查项保持一致：字段核对仅用于后续 OCR 影子验证，不能自行抬高分数。
        "visual_field_checks": [dict(item) for item in parsed.get("field_checks") or [] if isinstance(item, dict)][:12],
    }
    previous_suggested = _bounded_model_score(result.get("suggested_score"), max_score) if max_score > 0 else None
    merged["reason"] = _reconcile_score_reason(
        merged.get("reason"), suggested,
        adjusted=previous_suggested is not None and suggested is not None and abs(previous_suggested - suggested) > 1e-6,
        source="图片/OCR",
    )
    merged = _append_evidence_layer(
        merged, source="vision", summary=visual_evidence or visual_reason, checked_pages=checked_pages,
        evidence_pages=visual_evidence_pages, model=str(vision_profile.get("display_name") or vision_profile.get("model_name") or ""),
    )
    return _with_vision_execution(
        _set_result_coverage(merged, "covered" if conclusion_scope == "full" else "partial"), vision_status, attempted_pages, vision_profile, vision_message,
        evidence_pages=all_evidence_pages,
    )


def _ordered_combined_results(rules: list[dict], values: list[dict]) -> list[dict]:
    by_id = {item.get("rule_id"): item for item in values}
    return [by_id[item["rule_id"]] for item in rules if item["rule_id"] in by_id]


def _compound_score_rule_halves(rule: dict) -> tuple[dict, dict] | None:
    """仅在叶子项具有明确可加分值时拆单条复合评分规则。"""
    scoring = _rule_scoring(rule)
    items = scoring.get("items") if isinstance(scoring.get("items"), list) else []
    if len(items) <= 1:
        return None
    item_scores: list[float] = []
    for item in items:
        try:
            value = float(item.get("max_score")) if isinstance(item, dict) else 0.0
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        item_scores.append(value)
    try:
        parent_max = float(scoring.get("max_score") or 0)
    except (TypeError, ValueError):
        return None
    if parent_max <= 0 or abs(sum(item_scores) - parent_max) > 1e-6:
        return None
    midpoint = len(items) // 2
    halves = []
    for subset in (items[:midpoint], items[midpoint:]):
        subset_scoring = {**scoring, "max_score": sum(float(item["max_score"]) for item in subset), "items": subset}
        halves.append({**rule, "scoring_json": json.dumps(subset_scoring, ensure_ascii=False)})
    return halves[0], halves[1]


def _merge_compound_score_results(rule: dict, left: dict, right: dict) -> dict:
    scoring = _rule_scoring(rule)
    try:
        max_score = float(scoring.get("max_score") or 0)
    except (TypeError, ValueError):
        max_score = 0.0
    scores = (left.get("suggested_score"), right.get("suggested_score"))
    suggested = min(max_score, sum(float(value) for value in scores)) if all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in scores
    ) and max_score > 0 else None
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    confidence = min(
        (left.get("confidence") or "medium", right.get("confidence") or "medium"),
        key=lambda value: confidence_order.get(value, 1),
    )
    evidence = "\n".join(value for value in (
        f"【子项组1】{left.get('evidence', '')}" if left.get("evidence") else "",
        f"【子项组2】{right.get('evidence', '')}" if right.get("evidence") else "",
    ) if value)
    reason = "\n".join(value for value in (
        f"【子项组1】{left.get('reason', '')}" if left.get("reason") else "",
        f"【子项组2】{right.get('reason', '')}" if right.get("reason") else "",
    ) if value)
    requires_review = bool(left.get("requires_review", True) or right.get("requires_review", True) or suggested is None)
    return {
        "rule_id": rule["rule_id"], "suggested_score": suggested, "final_score": None,
        "effective_score": suggested if not requires_review else None, "max_score": max_score or None,
        "evidence": evidence[:2000], "reason": reason[:2000], "confidence": confidence,
        "automation_status": "needs_review" if requires_review else "ready_for_batch_confirmation",
        "requires_review": requires_review,
        "review_reason": "复合评分规则已按明确叶子分值分组核验，请复核汇总。" if requires_review else "",
    }


def _normalise_partial_combined_results(component: str, output: list[dict], rules: list[dict],
                                        tender_baseline: object = "") -> tuple[list[dict], list[dict]]:
    returned_ids = {item.get("rule_id") for item in output if isinstance(item, dict)}
    present_rules = [item for item in rules if item["rule_id"] in returned_ids]
    missing_rules = [item for item in rules if item["rule_id"] not in returned_ids]
    payload = _combined_batch_payload(component, present_rules)
    return _combined_batch_results(component, output, present_rules, payload, tender_baseline), missing_rules


def _run_combined_batch(app, task: dict, profile: dict, document: dict, component: str, rules: list[dict],
                        system_prompt: str, char_limit: int, label: str, depth: int = 0,
                        scan_index: dict | None = None, allow_missing_retry: bool = True,
                        targeted_retry: bool = False, allow_item_split: bool = True) -> tuple[list[dict], int, int, int, str]:
    """运行一个可独立保存的综合评审规则组；异常时仅拆分当前组。"""
    payload = _combined_batch_payload(component, rules)
    strategy = _scan_strategy(rules)
    context_limit = min(char_limit, EVALUATION_BATCH_CONTEXT_CHARS,
                        EVALUATION_STRATEGY_CONTEXT_CHARS.get(strategy, EVALUATION_BATCH_CONTEXT_CHARS))
    if targeted_retry:
        # 仅补评漏回规则，不携带无关全文；复合评分和数量累计仍保留跨章节证据容量。
        retry_cap = 18_000 if component == "review" else (
            42_000 if any(_rule_execution_strategy(rule) in {"counting", "section"} for rule in rules) else 24_000
        )
        context_limit = min(context_limit, retry_cap)
    if scan_index:
        context = _full_scan_review_context(scan_index, rules, context_limit, targeted=targeted_retry)
    else:
        full_text = Path(document["parsed_path"]).read_text(encoding="utf-8", errors="ignore")
        if len(full_text) <= FULL_SCAN_THRESHOLD_CHARS:
            context = {"text": full_text[:context_limit], "mode": "full_document", "pages": [], "unmatched_rule_ids": []}
        else:
            # 兼容异常元数据或旧解析记录；正常长文件会在调用前建立全文扫描索引。
            context = build_rule_context(document["parsed_path"], rules, context_limit, allow_partial=True)
    unmatched_rule_ids = set(context.get("unmatched_rule_ids") or [])
    if unmatched_rule_ids:
        payload = [{**item, "context_unmatched": item["rule_id"] in unmatched_rule_ids} for item in payload]
    if context["mode"] == "unmatched_rules":
        reason = "本地页级检索未定位到该规则的直接证据，未发送无关全文；请结合投标文件人工核验。"
        return _combined_manual_results(component, rules, payload, reason), 0, 0, len(rules), context["mode"]
    # MiniMax M3 的结构化审查在 adaptive 下会把大量预算用于思考并偶发破坏 JSON；
    # 审查/客观分禁用思考更稳定，主观评分仍保留 adaptive 以维持方案判断质量。
    thinking_mode = "disabled" if component == "objective" or (
        component == "review" and _is_minimax_m3_profile(profile)
    ) else "adaptive"

    def finish(parsed: object, retry_count: int, result_mode: str) -> tuple[list[dict], int, int, int, str]:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise ValueError("模型返回格式不符合综合评审要求")
        tender_baseline = str((scan_index or {}).get("tender_technical_baseline") or "")
        results, missing_rules = _normalise_partial_combined_results(
            component, parsed["results"], rules, tender_baseline,
        )
        if missing_rules and results and allow_missing_retry:
            storage.update_task(app, task["task_id"], message=f"{label} 有 {len(missing_rules)} 条未返回，正在仅补评缺失规则")
            missing = _run_combined_batch(
                app, task, profile, document, component, missing_rules, system_prompt, char_limit,
                f"{label}/缺失补评", depth, scan_index, allow_missing_retry=False,
                targeted_retry=True,
            )
            combined = _ordered_combined_results(rules, results + missing[0])
            return combined, retry_count + missing[1], missing[2], missing[3], f"{result_mode}+missing_retry"
        if missing_rules:
            missing_payload = _combined_batch_payload(component, missing_rules)
            results.extend(_combined_manual_results(
                component, missing_rules, missing_payload,
                "模型未返回该规则，已保留为空并提示人工复核。",
            ))
            return _ordered_combined_results(rules, results), retry_count, 0, len(missing_rules), "missing_manual"
        return _ordered_combined_results(rules, results), retry_count, 0, 0, result_mode

    format_error: ValueError | None = None
    try:
        parsed = _request_task_json(
            app, task, profile, f"evaluate_all_{component}_batch", system_prompt,
            _combined_batch_prompt(app, component, document, payload, context["text"], compact=False),
            document_id=document["document_id"], context_mode=f"{label}:{context['mode']}",
            max_tokens=_output_token_budget(profile, _combined_batch_output_budget(component, rules)), thinking_mode=thinking_mode,
        )
        return finish(parsed, 0, context["mode"])
    except InvalidJsonResponse as exc:
        format_error = exc
        if exc.finish_reason.lower() not in {"length", "max_tokens"}:
            storage.update_task(app, task["task_id"], message=f"{label} 模型结果正在规范化")
            try:
                repaired = _repair_invalid_json(
                    app, task, profile, f"evaluate_all_{component}_json_repair", exc, "results",
                    document_id=document["document_id"],
                )
                return finish(repaired, 1, "response_only_json_repair")
            except ValueError as repair_exc:
                if not _is_model_format_error(repair_exc):
                    raise
                format_error = repair_exc
    except ValueError as exc:
        if not _is_model_format_error(exc):
            raise
        format_error = exc

    # 单条复合评分规则也可能因逐叶子项证据过长而截断。仅当每个叶子项都有明确、
    # 可加的分值时，才安全拆成两个子项组并按父项满分汇总；其他评分口径不擅自拆算。
    compound_halves = _compound_score_rule_halves(rules[0]) if (
        allow_item_split and len(rules) == 1 and component in {"objective", "subjective"}
    ) else None
    if isinstance(format_error, InvalidJsonResponse) and format_error.finish_reason.lower() in {"length", "max_tokens"} and compound_halves:
        storage.update_task(app, task["task_id"], message=f"{label} 单条复合评分输出达到上限，正在按明确叶子评分项拆分")
        left = _run_combined_batch(
            app, task, profile, document, component, [compound_halves[0]], system_prompt, char_limit,
            f"{label}/子项组1", depth + 1, scan_index, allow_missing_retry, targeted_retry, False,
        )
        right = _run_combined_batch(
            app, task, profile, document, component, [compound_halves[1]], system_prompt, char_limit,
            f"{label}/子项组2", depth + 1, scan_index, allow_missing_retry, targeted_retry, False,
        )
        if left[0] and right[0]:
            merged = _merge_compound_score_results(rules[0], left[0][0], right[0][0])
            return [merged], left[1] + right[1], left[2] + right[2] + 1, left[3] + right[3], "split_score_items"

    # 截断响应不能可靠补尾；直接拆小规则组，避免把同一大上下文完整重发一遍。
    if isinstance(format_error, InvalidJsonResponse) and format_error.finish_reason.lower() in {"length", "max_tokens"} and len(rules) > 1 and depth < 3:
        storage.update_task(app, task["task_id"], message=f"{label} 输出达到上限，正在仅拆分该规则组")
        midpoint = max(1, len(rules) // 2)
        left = _run_combined_batch(app, task, profile, document, component, rules[:midpoint], system_prompt, char_limit,
                                   f"{label}/拆分1", depth + 1, scan_index, allow_missing_retry)
        right = _run_combined_batch(app, task, profile, document, component, rules[midpoint:], system_prompt, char_limit,
                                    f"{label}/拆分2", depth + 1, scan_index, allow_missing_retry)
        return left[0] + right[0], left[1] + right[1], left[2] + right[2] + 1, left[3] + right[3], "split_after_length"

    if format_error is not None:
        # 非截断且无法做响应级修复时，保留一次禁用思考的紧凑重试作为兼容兜底。
        storage.update_task(app, task["task_id"], message=f"{label} 模型结果正在按紧凑结构继续")
        try:
            parsed = _request_task_json(
                app, task, profile, f"evaluate_all_{component}_compact_retry", system_prompt,
                _combined_batch_prompt(app, component, document, payload, context["text"], compact=True),
                document_id=document["document_id"], context_mode=f"{label}_compact:{context['mode']}",
                max_tokens=_output_token_budget(profile, _combined_batch_output_budget(component, rules)), thinking_mode="disabled",
            )
            return finish(parsed, 1, context["mode"])
        except ValueError as retry_exc:
            if not _is_model_format_error(retry_exc):
                raise
            if len(rules) > 1 and depth < 3:
                storage.update_task(app, task["task_id"], message=f"{label} 严格重试仍异常，正在仅拆分该规则组")
                midpoint = max(1, len(rules) // 2)
                left = _run_combined_batch(app, task, profile, document, component, rules[:midpoint], system_prompt, char_limit, f"{label}/拆分1", depth + 1, scan_index, allow_missing_retry)
                right = _run_combined_batch(app, task, profile, document, component, rules[midpoint:], system_prompt, char_limit, f"{label}/拆分2", depth + 1, scan_index, allow_missing_retry)
                return left[0] + right[0], left[1] + right[1] + 1, left[2] + right[2] + 1, left[3] + right[3], "split"
            if compound_halves:
                storage.update_task(app, task["task_id"], message=f"{label} 严格重试仍异常，正在按明确叶子评分项拆分")
                left = _run_combined_batch(
                    app, task, profile, document, component, [compound_halves[0]], system_prompt, char_limit,
                    f"{label}/子项组1", depth + 1, scan_index, allow_missing_retry, targeted_retry, False,
                )
                right = _run_combined_batch(
                    app, task, profile, document, component, [compound_halves[1]], system_prompt, char_limit,
                    f"{label}/子项组2", depth + 1, scan_index, allow_missing_retry, targeted_retry, False,
                )
                if left[0] and right[0]:
                    merged = _merge_compound_score_results(rules[0], left[0][0], right[0][0])
                    return [merged], left[1] + right[1] + 1, left[2] + right[2] + 1, left[3] + right[3], "split_score_items"
            reason = "模型连续两次返回格式异常，本规则未获得可靠 AI 结论；已保留任务并转为人工核验。"
            storage.update_task(app, task["task_id"], message=f"{label} 格式重试失败，已标记人工核验并继续")
            return _combined_manual_results(component, rules, payload, reason), 1, 0, len(rules), "manual_fallback"


def _cross_bid_price_rules(rules: list[dict]) -> list[dict]:
    """只有必须横向比较投标报价的规则才进入统一价格计算。"""
    pattern = re.compile(r"最低(?:投标)?价|评审价|评标价|基准价|价格分|报价得分|投标报价[^，。；]{0,20}得分")
    return [
        rule for rule in rules
        if str(rule.get("execution_strategy") or "") == "cross_bid"
        or pattern.search(f"{rule.get('title', '')} {rule.get('check_rule', '')} {rule.get('source_text', '')}")
    ]


def _cross_bid_price_context(documents: list[dict], rules: list[dict]) -> str:
    per_document_limit = min(24_000, max(10_000, 80_000 // max(1, len(documents))))
    packets = []
    for document in documents:
        context = build_rule_context(document["parsed_path"], rules, per_document_limit)
        packets.append({
            "document_id": document["document_id"],
            "bidder_name": document.get("bidder_name") or document.get("original_name"),
            "filename": document.get("original_name"),
            "text": context["text"],
        })
    return json.dumps(packets, ensure_ascii=False, separators=(",", ":"))


def _decimal_price(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


_TOTAL_QUOTE_LABEL_PATTERN = re.compile(r"(?:投标|响应)?(?:总)?报价|开标一览表")
_TOTAL_QUOTE_NUMBER_PATTERN = re.compile(
    r"(?:[￥¥]\s*[:：]?\s*|人民币\s*)?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{4,}(?:\.\d+)?)\s*元?"
)


def _total_quote_from_lines(lines: list) -> tuple[Decimal | None, str]:
    """在给定文本行中保守提取唯一总报价；多个候选或口径不明时返回空。"""
    candidates: list[tuple[Decimal, str]] = []
    for index, line in enumerate(lines):
        if not _TOTAL_QUOTE_LABEL_PATTERN.search(str(line or "")):
            continue
        window = " ".join(str(value or "") for value in lines[index:index + 3])
        for match in _TOTAL_QUOTE_NUMBER_PATTERN.finditer(window):
            try:
                value = Decimal(match.group(1).replace(",", ""))
            except (InvalidOperation, AttributeError):
                continue
            if value.is_finite() and value > 0:
                candidates.append((value, window[:260]))
    unique = {value for value, _ in candidates}
    if len(unique) != 1:
        return None, ""
    value = next(iter(unique))
    excerpt = next((source for candidate, source in candidates if candidate == value), "")
    return value, excerpt


def _local_total_quote(document: dict) -> tuple[Decimal | None, str]:
    """从本地已解析文本保守提取唯一总报价，作为横向价格模型漏项的回填兜底。

    仅接受“总报价/投标报价/开标一览表”近邻窗口内唯一的数字金额；出现多个候选
    或无法确认口径时返回空，绝不猜测，更不会替代模型对资格扣除口径的判断。
    """
    path = str(document.get("parsed_path") or "")
    if not path or not Path(path).is_file():
        return None, ""
    lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    return _total_quote_from_lines(lines)


def _local_total_quote_with_ocr(app, document: dict) -> tuple[Decimal | None, str]:
    """文本层找不到唯一总报价时，复用同任务已缓存的 OCR 页文字再试一次。

    只读页级缓存（开标一览表等规则已识别的候选页），不触发任何新的 OCR 调用；
    仍坚持“唯一金额”约束，OCR 文字出现多个不同报价时同样宁可留空。
    """
    value, excerpt = _local_total_quote(document)
    if value is not None:
        return value, excerpt
    cached_pages = storage.list_ocr_cached_page_texts(app, str(document.get("document_id") or ""))
    if not cached_pages:
        return None, ""
    ocr_lines: list = []
    for page in cached_pages:
        ocr_lines.extend(str(page.get("text") or "").splitlines())
    value, excerpt = _total_quote_from_lines(ocr_lines)
    if value is None:
        return None, ""
    return value, f"OCR页文字：{excerpt[:200]}"


_UPPER_PRICE_LABEL_PATTERN = re.compile(r"(?:最高限价|采购预算|预算金额|项目预算|控制价)")


def _local_project_upper_limit(app, project_id: str) -> tuple[Decimal | None, str]:
    """仅从招标文件中标签邻近的唯一金额提取限价，无法确认时宁可留空。"""
    candidates: list[tuple[Decimal, str]] = []
    for tender in storage.list_documents(app, project_id):
        if tender.get("role") not in {"tender", "tender_attachment"}:
            continue
        path = str(tender.get("parsed_path") or "")
        if not path or not Path(path).is_file():
            continue
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines):
            if not _UPPER_PRICE_LABEL_PATTERN.search(str(line or "")):
                continue
            window = " ".join(str(value or "") for value in lines[index:index + 3])
            for match in _TOTAL_QUOTE_NUMBER_PATTERN.finditer(window):
                try:
                    value = Decimal(match.group(1).replace(",", ""))
                except (InvalidOperation, AttributeError):
                    continue
                if value.is_finite() and value > 0:
                    candidates.append((value, window[:260]))
    unique = {value for value, _ in candidates}
    if len(unique) != 1:
        return None, ""
    value = next(iter(unique))
    return value, next((source for candidate, source in candidates if candidate == value), "")


def _document_shared_price_facts(app, project_id: str, document: dict, *,
                                 upper_limit: tuple[Decimal | None, str] | None = None) -> str:
    """生成可被审查和评分共同消费的最小价格事实包，不引用任一模型的自然语言结论。"""
    quote, quote_excerpt = _local_total_quote_with_ocr(app, document)
    limit, limit_excerpt = upper_limit if upper_limit is not None else _local_project_upper_limit(app, project_id)
    values = []
    if quote is not None:
        values.append(f"投标文件唯一总报价：{quote}元（本地定位：{quote_excerpt[:180]}）")
    if limit is not None:
        values.append(f"招标文件唯一最高限价/预算：{limit}元（本地定位：{limit_excerpt[:180]}）")
    if quote is not None and limit is not None:
        relation = "未超过" if quote <= limit else "超过"
        values.append(f"可比口径下：投标报价{relation}该限价；仍需按具体规则核对报价口径。")
    return "\n".join(values)


_SME_DEDICATED_PURCHASE_PATTERN = re.compile(
    r"专门面向.{0,24}中小企业.{0,90}(?:不再执行|不执行).{0,36}价格(?:评审|评价)?优惠",
    re.DOTALL,
)


def _project_price_policy_context(app, project_id: str) -> str:
    """从招标文件读取价格优惠的项目级例外，避免通用评分表覆盖明确政策前提。"""
    try:
        tenders = [item for item in storage.list_documents(app, project_id) if item.get("role") == "tender"]
    except Exception:
        return ""
    for tender in tenders:
        path = str(tender.get("parsed_path") or "")
        if not path or not Path(path).is_file():
            continue
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        compact = re.sub(r"\s+", "", text)
        if _SME_DEDICATED_PURCHASE_PATTERN.search(compact):
            return "本采购包明确专门面向中小企业，价格评分不执行中小微企业价格扣除。"
    return ""


def _strip_inapplicable_price_discount_text(value: object) -> str:
    """移除模型沿用通用政策模板产生的价格扣除提示，保留其他报价核验事实。"""
    text = _clean_model_text(value)
    if not text:
        return ""
    pieces = re.split(r"(?<=[。；;])\s*|\n+", text)
    retained = [piece for piece in pieces if not (
        ("小微" in piece or "小型" in piece or "微型" in piece or "中小企业" in piece)
        and ("扣除" in piece or "优惠" in piece or "声明函" in piece)
    )]
    return " ".join(piece.strip() for piece in retained if piece.strip())


def _price_formula_kind(rule: dict) -> str | None:
    """只识别原文完整写明的通用价格公式，避免把任意“基准价”误算成最低价法。"""
    text = re.sub(r"\s+", "", " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text")))
    # 最低价比例法必须同时有“最低价为基准”和“基准价/本投标报价”的方向证据。
    lowest_base = re.search(r"最低[一-龥]{0,8}?(?:报价|价格|价).{0,40}(?:为|作为|确定为).{0,24}(?:评标|评审)?基准价", text)
    lowest_ratio = re.search(r"(?:评标|评审)?基准价.{0,20}[／/].{0,20}(?:本|投标人)?(?:投标|响应)?报价", text)
    if lowest_ratio and (lowest_base or re.search(r"价格最低|最低[一-龥]{0,8}?(?:报价|价格|价)", text)):
        return "lowest_ratio"
    # 算术平均值乘固定系数、并按高低偏离分别扣分的公式可以稳定复算；只有四个
    # 关键要素都明确出现才接管模型结果，其他价格公式继续由模型给出建议。
    average = "算术平均" in text and bool(re.search(r"(?:评标|评审)?基准价.{0,36}(?:算术平均|平均值)", text))
    factor = re.search(r"(?:算术平均值|平均值).{0,28}(?:[×x*]|的)\s*(0?\.\s*\d+|\d+\s*%)", text)
    high = re.search(r"高于.{0,24}基准价.{0,24}(?:每|每高).{0,12}1%?.{0,16}扣\s*(\d+(?:\.\d+)?)\s*分", text)
    low = re.search(r"低于.{0,24}基准价.{0,24}(?:每|每低).{0,12}1%?.{0,16}扣\s*(\d+(?:\.\d+)?)\s*分", text)
    if average and factor and high and low:
        raw_factor = factor.group(1).replace(" ", "")
        try:
            value = Decimal(raw_factor[:-1]) / Decimal("100") if raw_factor.endswith("%") else Decimal(raw_factor)
        except InvalidOperation:
            value = Decimal("0")
        if Decimal("0") < value <= Decimal("1"):
            return "average_factor_deviation"
    return None


def _uses_lowest_price_ratio(rule: dict) -> bool:
    """兼容既有调用方：只有明确最低价比例法才返回真。"""
    return _price_formula_kind(rule) == "lowest_ratio"


def _deterministic_price_score(rule: dict, quoted_price: object, quoted_prices: list[object], max_score: float) -> tuple[float | None, str]:
    """复算明确的通用价格公式；信息不完整或公式不匹配时保留模型建议。"""
    kind = _price_formula_kind(rule)
    if not kind or max_score <= 0:
        return None, ""
    current = _decimal_price(quoted_price)
    prices = [value for value in (_decimal_price(item) for item in quoted_prices) if value is not None]
    if current is None or len(prices) < 2:
        return None, ""
    maximum = Decimal(str(max_score))
    if kind == "lowest_ratio":
        base = min(prices)
        score = (base / current * maximum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        bounded = min(maximum, max(Decimal("0"), score))
        return float(bounded), f"系统复算：评标基准价{base}；{base}／{current}×{max_score}={bounded}（四舍五入保留两位小数）。"

    text = re.sub(r"\s+", "", " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text")))
    factor_match = re.search(r"(?:算术平均值|平均值).{0,28}(?:[×x*]|的)\s*(0?\.\s*\d+|\d+\s*%)", text)
    high_match = re.search(r"高于.{0,24}基准价.{0,24}(?:每|每高).{0,12}1%?.{0,16}扣\s*(\d+(?:\.\d+)?)\s*分", text)
    low_match = re.search(r"低于.{0,24}基准价.{0,24}(?:每|每低).{0,12}1%?.{0,16}扣\s*(\d+(?:\.\d+)?)\s*分", text)
    if not (factor_match and high_match and low_match):
        return None, ""
    raw_factor = factor_match.group(1).replace(" ", "")
    factor = Decimal(raw_factor[:-1]) / Decimal("100") if raw_factor.endswith("%") else Decimal(raw_factor)
    # 常见规则在投标人不少于五家时去掉 20% 的最高、最低报价；原文未明确该条件时
    # 不擅自剔除，直接使用全部可识别报价。
    averaged = list(prices)
    if len(prices) >= 5 and re.search(r"(?:去掉|剔除).{0,24}(?:最高|最低).{0,24}(?:20%|百分之二十)", text):
        trim = max(1, int(len(prices) * 0.2))
        if len(prices) > trim * 2:
            averaged = sorted(prices)[trim:-trim]
    base = (sum(averaged) / Decimal(len(averaged))) * factor
    delta_percent = abs(current - base) / base * Decimal("100")
    deduction_rate = Decimal(high_match.group(1)) if current >= base else Decimal(low_match.group(1))
    score = (maximum - delta_percent * deduction_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    bounded = min(maximum, max(Decimal("0"), score))
    side = "高于" if current >= base else "低于"
    return float(bounded), (
        f"系统复算：可识别报价算术平均值{sum(averaged) / Decimal(len(averaged))}×{factor}="
        f"评标基准价{base.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}；本报价{side}基准价"
        f"{delta_percent.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%，按每1%扣{deduction_rate}分，建议{bounded}分。"
    )


def _run_cross_bid_price_scoring(app, task: dict, profile: dict, documents: list[dict], rules: list[dict],
                                 score_run_id: str) -> dict:
    """在单文件评审后统一计算最低价/基准价，补足跨文件公式无法单独判断的问题。"""
    if len(documents) < 2 or not rules:
        return {"rule_count": 0, "result_count": 0, "retry_count": 0, "missing_count": 0}
    payload = _score_payload(rules)
    price_policy_context = _project_price_policy_context(app, str(task.get("project_id") or ""))
    document_packet = _cross_bid_price_context(documents, rules)
    prompt = storage.render_prompt_template(
        app, "evaluate_all_cross_bid_price_user",
        rules=json.dumps(payload, ensure_ascii=False, separators=(",", ":")), documents=document_packet,
        price_policy_context=price_policy_context or "未识别到项目级价格优惠例外，请仅按评分规则和已给证据判断。",
    )
    expected = {(document["document_id"], rule["rule_id"]) for document in documents for rule in rules}

    def save_unavailable(keys: set[tuple[str, str]], reason: str) -> None:
        for document_id, rule_id in keys:
            rule_payload = rules_by_id[rule_id]
            try:
                max_score = float(rule_payload.get("scoring", {}).get("max_score") or 0)
            except (TypeError, ValueError):
                max_score = 0.0
            result = _score_result_from_model(
                rule_id, None, max_score,
                {"reason": reason, "confidence": "low", "needs_ocr": bool(rule_payload.get("ocr_required"))},
                force_needs_ocr=bool(rule_payload.get("ocr_required")),
            )
            storage.save_score_results(app, score_run_id, document_id, [result])

    def request(phase: str) -> dict:
        return _request_task_json(
            app, task, profile, phase, _system_prompt(app, "evaluate_all"), prompt,
            context_mode="cross_bid_price", max_tokens=_output_token_budget(
                profile, max(3_000, 1_000 + len(expected) * 450),
            ), thinking_mode="disabled",
        )

    retry_count = 0

    def request_with_repair(phase: str) -> dict | None:
        nonlocal retry_count
        try:
            return request(phase)
        except InvalidJsonResponse as exc:
            retry_count += 1
            storage.update_task(app, task["task_id"], message="跨投标人价格评分结果正在规范化")
            try:
                return _repair_invalid_json(
                    app, task, profile, f"{phase}_json_repair", exc, "results",
                )
            except ValueError as repair_exc:
                if not _is_model_format_error(repair_exc):
                    raise
                return None

    rules_by_id = {rule["rule_id"]: item for rule, item in zip(rules, payload)}
    documents_by_id = {document["document_id"]: document for document in documents}
    parsed = request_with_repair("evaluate_all_cross_bid_price")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        retry_count += 1
        parsed = request_with_repair("evaluate_all_cross_bid_price_retry")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        # 比较型价格规则不能使用单文件暂定分兜底；明确保存“暂无法计算”，同时不让
        # 已完成的其他审查结果整体失败。
        save_unavailable(expected, "跨投标人价格比较未返回可靠结果，当前暂无法计算建议分，请人工核对全部报价后复核。")
        return {"rule_count": len(rules), "result_count": 0, "retry_count": retry_count,
                "missing_count": len(expected), "format_failure": True}

    valid_rows = [dict(raw) for raw in parsed["results"] if isinstance(raw, dict) and
                  (str(raw.get("document_id") or ""), str(raw.get("rule_id") or "")) in expected]
    # 模型偶尔已返回该投标人行却遗漏 quoted_price，或直接漏掉一行。对每个横向
    # 价格规则使用本地解析文本的“唯一总报价”作保守补位，避免一个明确报价被
    # 静默保存为 null。价格扣除、资格有效性和非标准公式仍完全保留给模型/人工。
    rows_by_key = {
        (str(raw.get("document_id") or ""), str(raw.get("rule_id") or "")): raw
        for raw in valid_rows
    }
    for document in documents:
        local_price, excerpt = _local_total_quote_with_ocr(app, document)
        if local_price is None:
            continue
        for rule in rules:
            key = (document["document_id"], rule["rule_id"])
            raw = rows_by_key.get(key)
            if raw is None:
                raw = {"document_id": key[0], "rule_id": key[1], "confidence": "medium"}
                valid_rows.append(raw)
                rows_by_key[key] = raw
            if _decimal_price(raw.get("quoted_price")) is None:
                raw["quoted_price"] = float(local_price)
                # 本地唯一报价回填后，旧模型“未见总价/暂留空”已失效，不能继续混在
                # 页面证据里。保留新的来源说明，使分数、证据和理由使用同一口径。
                raw["_local_quote_recovered"] = True
                raw["evidence"] = f"投标文件本地解析定位唯一总报价：{local_price}元。{excerpt[:220]}"
                raw["reason"] = "模型报价字段缺失，已由投标文件中唯一总报价回填。"
    prices_by_rule: dict[str, list[object]] = {}
    for raw in valid_rows:
        prices_by_rule.setdefault(str(raw.get("rule_id") or ""), []).append(raw.get("quoted_price"))

    received: set[tuple[str, str]] = set()
    for raw in valid_rows:
        key = (str(raw.get("document_id") or ""), str(raw.get("rule_id") or ""))
        rule_payload = rules_by_id[key[1]]
        try:
            max_score = float(rule_payload.get("scoring", {}).get("max_score") or 0)
        except (TypeError, ValueError):
            max_score = 0.0
        deterministic_score, deterministic_calculation = _deterministic_price_score(
            rule_payload, raw.get("quoted_price"), prices_by_rule.get(key[1], []), max_score,
        )
        if deterministic_score is not None:
            previous_reason = _clean_model_text(raw.get("reason"))
            if price_policy_context:
                previous_reason = _strip_inapplicable_price_discount_text(previous_reason)
            raw = {
                **raw,
                "suggested_score": deterministic_score,
                "calculation": deterministic_calculation,
                "reason": " ".join(value for value in (
                    previous_reason,
                    price_policy_context,
                    "当前按全部可识别报价暂算，资格与符合性通过范围仍由人工最终确认。",
                ) if value).strip(),
            }
        elif raw.get("_local_quote_recovered"):
            raw["reason"] = " ".join(value for value in (
                _clean_model_text(raw.get("reason")),
                "本规则价格公式未达到自动复算条件，价格分需人工核对。",
            ) if value).strip()
        suggested = _suggested_score(rule_payload, raw, "objective", max_score)
        result = _score_result_from_model(
            key[1], suggested, max_score, raw,
            force_needs_ocr=bool(rule_payload.get("ocr_required")),
        )
        storage.save_score_results(app, score_run_id, documents_by_id[key[0]]["document_id"], [result])
        received.add(key)
    missing = expected - received
    if missing:
        save_unavailable(missing, "跨投标人价格比较未返回本投标人的可靠结果，当前暂无法计算建议分，请人工复核报价口径和公式。")
    return {"rule_count": len(rules), "result_count": len(received), "retry_count": retry_count,
            "missing_count": len(missing)}


class _EvaluationProgress:
    """汇总并行文件的进度，并在整份文件完成后发布可展示的部分结果。"""

    def __init__(self, app, task: dict, total_units: int, document_count: int):
        self.app = app
        self.task = task
        self.total_units = max(1, total_units)
        self.document_count = document_count
        self.completed_units = 0
        self.completed_documents: list[dict] = []
        self.lock = threading.Lock()

    def _progress(self) -> int:
        return int(self.completed_units * 100 / self.total_units)

    def message(self, message: str) -> None:
        with self.lock:
            storage.update_task(self.app, self.task["task_id"], progress=self._progress(), message=message)

    def advance(self, message: str, units: int = 1) -> None:
        with self.lock:
            self.completed_units = min(self.total_units, self.completed_units + max(0, units))
            storage.update_task(self.app, self.task["task_id"], progress=self._progress(), message=message)

    def document_completed(self, document: dict, *, reused: bool = False) -> None:
        bidder_name = document["bidder_name"] or document["original_name"]
        with self.lock:
            if not any(item["document_id"] == document["document_id"] for item in self.completed_documents):
                self.completed_documents.append({"document_id": document["document_id"], "bidder_name": bidder_name})
            status = "已复用" if reused else "已完成"
            storage.update_task(
                self.app, self.task["task_id"], progress=self._progress(),
                message=f"{status} {bidder_name} 的综合评审（{len(self.completed_documents)}/{self.document_count}）",
                result={"partial": True, "completed_documents": list(self.completed_documents)},
            )


def _evaluate_document(app, task: dict, document: dict, *, rule_set: dict, profile: dict, char_limit: int,
                       expected_rule_ids: dict[str, set[str]], review_rules: list[dict], objective_rules: list[dict],
                       subjective_rules: list[dict], review_run: dict | None, objective_run: dict | None,
                       subjective_run: dict | None, project_scope: dict, system_prompt: str,
                       scan_units: int, groups_per_document: int, vision_profile: dict | None,
                       ocr_features_enabled: bool, visual_units: int, progress: _EvaluationProgress) -> dict:
    """处理一份投标文件；不同投标人可并行，单份文件内仍严格顺序执行。"""
    bidder_name = document["bidder_name"] or document["original_name"]
    progress.message(f"正在综合评审：{bidder_name}")
    # 扫描型文件需要重新应用当前 OCR/图片覆盖策略；不得复用早期仅凭稀疏文本得出的结论。
    reusable = None if (
        task.get("payload", {}).get("force_rerun")
        or task.get("payload", {}).get("retry_failed_task_id")
        or _document_text_coverage_status(document) == "uncovered"
    ) else storage.reusable_evaluation_document_results(
        app, task["project_id"], rule_set["rule_set_id"], profile["profile_id"], document["document_id"], expected_rule_ids,
        task.get("payload", {}).get("input_fingerprint"), PROMPT_VERSION,
    )
    if reusable:
        if review_run:
            storage.save_review_results(app, review_run["review_run_id"], document["document_id"], reusable["review"])
        if objective_run:
            storage.save_score_results(app, objective_run["score_run_id"], document["document_id"], reusable["objective"])
        if subjective_run:
            storage.save_score_results(app, subjective_run["score_run_id"], document["document_id"], reusable["subjective"])
        progress.advance(f"已复用 {bidder_name} 的完整评审结果", scan_units + groups_per_document + visual_units)
        progress.document_completed(document, reused=True)
        return {"reused_document_count": 1}

    price_fact_cache = task.setdefault("_shared_price_fact_packets", {})
    price_fact_lock = task.setdefault("_shared_price_fact_lock", threading.Lock())
    with price_fact_lock:
        # 最高限价只来自项目招标文件，所有投标人完全相同。长招标文件只扫描一次，
        # 避免并行评审每家投标人都重复读取和逐行匹配整份招标文件。
        if "_shared_project_upper_limit" not in task:
            task["_shared_project_upper_limit"] = _local_project_upper_limit(app, task["project_id"])
        project_upper_limit = task["_shared_project_upper_limit"]
        price_facts = price_fact_cache.get(document["document_id"])
        if price_facts is None:
            price_facts = _document_shared_price_facts(
                app, task["project_id"], document, upper_limit=project_upper_limit,
            )
            price_fact_cache[document["document_id"]] = price_facts
    document = {
        **document,
        "_shared_price_facts": price_facts,
    }

    # “仅重跑失败项”不依赖上一任务整体成功：只要输入指纹、规则集和单文件均未变，
    # 已完整结束的规则直接写入本轮 run，剩余规则再走原有全文扫描和图片补充链路。
    resume_failed_only = bool(task.get("payload", {}).get("retry_failed_task_id"))
    checkpoint_results = storage.get_evaluation_unit_checkpoints(
        app, task["project_id"], rule_set["rule_set_id"], document["document_id"],
        str(task.get("payload", {}).get("input_fingerprint") or ""),
    ) if resume_failed_only else {"review": {}, "objective": {}, "subjective": {}}
    completed_results: dict[str, dict[str, dict]] = {"review": {}, "objective": {}, "subjective": {}}
    reused_rule_ids: dict[str, set[str]] = {"review": set(), "objective": set(), "subjective": set()}
    component_specs = (("review", review_rules, review_run), ("objective", objective_rules, objective_run), ("subjective", subjective_rules, subjective_run))
    for component, component_rules, run in component_specs:
        snapshots = checkpoint_results.get(component, {})
        reusable_rows = [snapshots[rule["rule_id"]] for rule in component_rules if rule["rule_id"] in snapshots]
        if not reusable_rows:
            continue
        if component == "review" and run:
            storage.save_review_results(app, run["review_run_id"], document["document_id"], reusable_rows)
        elif run:
            storage.save_score_results(app, run["score_run_id"], document["document_id"], reusable_rows)
        completed_results[component].update({item["rule_id"]: item for item in reusable_rows})
        reused_rule_ids[component].update(item["rule_id"] for item in reusable_rows)
    pending_rules = [
        rule for component, component_rules, _ in component_specs
        for rule in component_rules if rule["rule_id"] not in reused_rule_ids[component]
    ]
    if resume_failed_only and not pending_rules:
        progress.advance(f"已复用 {bidder_name} 的全部成功规则", scan_units + groups_per_document + visual_units)
        progress.document_completed(document, reused=True)
        return {"reused_document_count": 1, "reused_unit_count": sum(len(values) for values in completed_results.values()), "failed_units": []}

    scan_unavailable = False
    try:
        scan_index = _scan_document_fulltext(
            app, task, profile, document, pending_rules, project_scope, system_prompt,
            progress_callback=progress.advance,
        )
    except ValueError as exc:
        if not _is_recoverable_model_error(exc):
            raise
        # 全文扫描的远端服务短暂不可用时，不让整份文件归零：后续规则组仍按本地章节
        # 检索运行，并在任务结果中保留恢复告警。
        scan_index = {}
        scan_unavailable = True
        progress.advance(f"{bidder_name} 全文扫描暂时不可用，正在继续逐规则审查", scan_units)
    if scan_index:
        scan_index["evidence_ledger"] = _build_rule_evidence_ledger(
            scan_index, review_rules + objective_rules + subjective_rules,
        )
        # 仅供本地结果归因使用，绝不随提示词发送给模型；避免模型把招标原文中的
        # 参数写法误判为投标文件自身矛盾。
        scan_index["tender_technical_baseline"] = _project_tender_technical_baseline(app, task["project_id"])
    ledger = scan_index.get("evidence_ledger", {}) if scan_index else {}
    values = {
        "reused_document_count": 0,
        "full_scan_document_count": 1 if scan_index else 0,
        "full_scan_batch_count": scan_index.get("scan_batch_count", 0) if scan_index else 0,
        "full_scan_failed_chunk_count": len(scan_index.get("failed_chunks", [])) if scan_index else 0,
        "compact_retry_count": scan_index.get("compact_retry_count", 0) if scan_index else 0,
        "split_retry_count": scan_index.get("split_retry_count", 0) if scan_index else 0,
        "manual_fallback_rule_count": 0,
        "batch_count": 0,
        "evidence_ledger_rule_count": len(ledger),
        "evidence_ledger_empty_rule_count": sum(
            1 for value in ledger.values() if not value.get("candidates")
        ) if isinstance(ledger, dict) else 0,
        "local_ocr_rule_count": 0,
        "local_ocr_skipped_rule_count": 0,
        "local_ocr_seconds": 0.0,
        "enhancement_rule_count": 0,
        "failed_units": [],
    }
    # 全文扫描临时不可用时，后续逐规则审查已经用本地章节检索继续完成；这是恢复告警
    # 而非可单独重跑的规则失败，不能把所有已完成规则误显示为“部分完成”。
    values["full_scan_recovery_warning"] = bool(scan_unavailable and scan_units)
    components = component_specs
    for component, component_rules, run in components:
        component_rules = [rule for rule in component_rules if rule["rule_id"] not in reused_rule_ids[component]]
        rules_by_id = {str(rule["rule_id"]): rule for rule in component_rules}
        # 长文件已有全文扫描索引时，按重合证据页重组规则，减少不同组重复携带同一页。
        groups = _evaluation_rule_batches(component, component_rules, scan_index=scan_index)
        def run_group(group_index: int, group: list[dict]):
            label = f"{bidder_name}·{component} 第{group_index}组"
            progress.message(f"正在综合评审：{label}")
            try:
                results, compact_count, split_count, fallback_count, _ = _run_combined_batch(
                    app, task, profile, document, component, group, system_prompt, char_limit, label, scan_index=scan_index,
                )
                return group_index, label, results, compact_count, split_count, fallback_count, []
            except ValueError as exc:
                if not _is_recoverable_model_error(exc):
                    raise
                payload = _combined_batch_payload(component, group)
                reason = "模型连接连续恢复失败，本规则组暂未获得可靠 AI 结论；其他规则已继续完成，可仅重跑本组。"
                results = _combined_manual_results(component, group, payload, reason)
                failure = {
                    "document_id": document["document_id"], "bidder_name": bidder_name,
                    "component": component, "rule_ids": [rule["rule_id"] for rule in group],
                    "reason": str(exc)[:240],
                }
                storage.update_task(app, task["task_id"], message=f"{label} 连接恢复失败，已保留为待重跑项并继续其他规则")
                return group_index, label, results, 0, 0, len(group), [failure]

        # 单一投标文件过去会把全部规则组串行执行；现在复用任务级请求闸门，在不超过
        # 当前模型并发上限的前提下并行独立规则组。小服务器只增加短生命周期线程，
        # 不加载模型或维持后台队列。
        gate = task.get("_evaluation_request_gate")
        # 与请求闸门的当前已验证并发位保持一致：单文件首轮仍串行建立服务商稳定性，
        # 连续成功后由 gate 升档，下一类规则组即可并行，避免瞬时并发改变既有容错语义。
        admitted_limit = int(getattr(gate, "limit", 1) or 1)
        worker_count = min(_profile_parallel_limit(profile, len(groups)), admitted_limit, len(groups)) if groups else 0
        def persist_completed(value):
            group_index, label, results, compact_count, split_count, fallback_count, failed_units = value
            # 将全文扫描阶段已定位的页码保留给后续图片识别；此字段只在本次任务内流转，
            # 不改变既有评审结果的存储结构或对外 API。
            results = _with_scan_visual_candidates(results, scan_index, document)
            results = [
                _apply_document_evidence_guard(document, component, rules_by_id[str(item["rule_id"])], item)
                for item in results if str(item.get("rule_id") or "") in rules_by_id
            ]
            # 每个规则组成功后立即持久化；后续组异常时，页面仍能获得已完成部分。
            if component == "review" and run:
                storage.save_review_results(app, run["review_run_id"], document["document_id"], results)
            elif run:
                storage.save_score_results(app, run["score_run_id"], document["document_id"], results)
            completed_results[component].update({item["rule_id"]: item for item in results})
            values["compact_retry_count"] += compact_count
            values["split_retry_count"] += split_count
            values["manual_fallback_rule_count"] += fallback_count
            values["batch_count"] += 1
            values["failed_units"].extend(failed_units)
            progress.advance(f"已完成综合评审：{label}")

        if worker_count > 1:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="evaluation-rule") as executor:
                futures = [executor.submit(run_group, index, group) for index, group in enumerate(groups, start=1)]
                completed_groups = []
                for future in as_completed(futures):
                    completed_groups.append(future.result())
            for value in sorted(completed_groups):
                persist_completed(value)
        else:
            for index, group in enumerate(groups, start=1):
                persist_completed(run_group(index, group))
    visual_components = (
        ("review", review_rules, review_run),
        ("objective", objective_rules, objective_run),
        ("subjective", subjective_rules, subjective_run),
    )
    # OCR 与多模态是独立总开关：本地 OCR 不依赖多模态档案；腾讯关闭或不可用时
    # 仍可完成纯文字 OCR；多模态档案只决定是否额外执行图片外观事实核验。
    ocr_configuration = storage.ocr_configuration(app)
    ocr_enabled = _evaluation_ocr_enabled(ocr_features_enabled, ocr_configuration)
    for component, component_rules, run in visual_components:
        # OCR 归纳批量合并（默认关闭，开启后同组件 2 条以上规则一次模型调用）：
        # 先收集本组件需要 OCR 的规则并批量提取文字，主循环命中批结果时直接使用；
        # 批内任何缺失/异常都会整批回退为逐条原路径，行为与旧实现一致。
        ocr_batch_results: dict[str, dict] = {}
        if _ocr_batch_enabled():
            batch_entries: list[tuple[dict, dict, bool]] = []
            for rule in component_rules:
                if rule["rule_id"] in reused_rule_ids[component]:
                    continue
                base = completed_results[component].get(rule["rule_id"])
                if not base:
                    continue
                base = _with_evidence_pack_candidates(app, document, rule, base)
                if ocr_enabled and _local_ocr_baseline_required(rule, base, component):
                    trigger, level = _rule_vision_policy(rule)
                    enhancement = trigger != "off" and level != "off" and _rule_image_mode(rule) != "off"
                    tencent_upgrade = enhancement and (trigger == "required" or _needs_visual_fallback(component, base))
                    batch_entries.append((rule, base, tencent_upgrade))
            if len(batch_entries) >= 2:
                progress.message(f"正在批量 OCR 识别：{bidder_name} · {component}")
                ocr_started_at = time.monotonic()
                ocr_batch_results = _run_ocr_batch_supplement(
                    app, task, document, component, batch_entries, profile, locator_profile=vision_profile,
                )
                values["local_ocr_seconds"] += max(0.0, time.monotonic() - ocr_started_at)
                values["local_ocr_rule_count"] += len(batch_entries)
                values["batch_count"] += 1
        for rule in component_rules:
            if rule["rule_id"] in reused_rule_ids[component]:
                continue
            trigger, level = _rule_vision_policy(rule)
            image_mode = _rule_image_mode(rule)
            base = completed_results[component].get(rule["rule_id"])
            if not base:
                progress.advance(f"跳过未产生文字结果的图片识别：{bidder_name}")
                continue
            # 只取同文件、同材料事实的已确认页作为候选优先级；不读取旧状态、分数或理由，
            # 所以重新提取/重新评审仍会独立产生本轮结论。
            base = _with_evidence_pack_candidates(app, document, rule, base)
            label = rule.get("title") or rule.get("check_rule")
            merged = base
            image_strategy = _rule_image_strategy(rule)
            baseline_required = ocr_enabled and _local_ocr_baseline_required(rule, merged, component)
            enhancement_requested = trigger != "off" and level != "off" and image_mode != "off"
            # “智能升级”不因规则属于证书/字段类就无条件消耗腾讯额度；只有原文字
            # 证据不足，或人工选择“每次均升级”时才开放高精度复核。
            tencent_upgrade_requested = enhancement_requested and (
                trigger == "required" or _needs_visual_fallback(component, merged)
            )
            if not baseline_required:
                values["local_ocr_skipped_rule_count"] += 1
            if enhancement_requested:
                values["enhancement_rule_count"] += 1
            if not baseline_required and not enhancement_requested:
                continue
            # 固定顺序：本地 OCR 先给出可复用的基础文字，再根据人工策略和规则事实
            # 升级腾讯精确字段核验或多模态外观核验。这样非多模态模型也能稳定运行。
            if baseline_required:
                progress.message(f"正在本地 OCR 识别：{bidder_name} · {label}")
                if rule["rule_id"] in ocr_batch_results:
                    merged = ocr_batch_results[rule["rule_id"]]
                else:
                    ocr_started_at = time.monotonic()
                    merged = _run_ocr_supplement(
                        app, task, document, component, rule, merged, profile,
                        # 高强度纯扫描件在文字锚点失效时，可借助已配置的多模态模型
                        # 做一次低清找页；即使专家模式仅选择腾讯 OCR，也不丢失旧能力。
                        locator_profile=vision_profile, baseline=True, allow_tencent=tencent_upgrade_requested,
                    )
                    values["local_ocr_seconds"] += max(0.0, time.monotonic() - ocr_started_at)
                    values["local_ocr_rule_count"] += 1
                    values["batch_count"] += 1
            allow_vision = enhancement_requested and (image_mode in {"vision_only", "combined"} or image_mode == "auto")
            skip_note = _multimodal_skip_note(image_strategy, merged, rule, trigger) if enhancement_requested else ""
            should_run_vision = image_mode == "combined" or _should_run_multimodal_after_ocr(image_strategy, merged)
            if allow_vision and vision_profile and not skip_note and should_run_vision:
                progress.message(f"正在图片识别：{bidder_name} · {label}")
                merged = _run_visual_supplement(app, task, document, component, rule, merged, vision_profile)
                values["batch_count"] += 1
            elif skip_note:
                # 保留 OCR 已采纳的状态与页码，仅补充说明未调用图片模型的原因。
                merged = _with_vision_execution(
                    merged, str(merged.get("vision_status") or "ocr_applied"),
                    [page for page in merged.get("vision_pages") or []],
                    {"display_name": merged.get("vision_model") or ""}, skip_note,
                )
            elif allow_vision and image_strategy == "vision" and not vision_profile:
                merged = _with_vision_execution(
                    merged, "unavailable", [], {}, "本规则需要图片外观判断，但未获得可用的多模态模型。",
                )
            elif allow_vision and image_strategy == "hybrid" and not vision_profile:
                # 本地/Tencent OCR 已完成的文字事实必须保留；只说明未能继续核验签章、
                # 外观等图片事实，不能把它回退为“完全未执行”。
                existing_status = str(merged.get("vision_status") or "")
                if existing_status.startswith("ocr_"):
                    merged = _with_vision_execution(
                        merged, existing_status, merged.get("vision_pages") or [], {},
                        "OCR 文字核验已完成；未获得可用的多模态模型，图片外观事实未执行。",
                        evidence_pages=merged.get("vision_evidence_pages") or [],
                    )
                else:
                    merged = _with_vision_execution(
                        merged, "unavailable", [], {}, "本规则需要图片外观判断，但未获得可用的多模态模型。",
                    )
            elif image_mode == "ocr_only" and not ocr_enabled:
                # 仅 OCR 是明确的人工选择；不能误报成多模态不可用。
                merged = _with_vision_execution(merged, "ocr_failed", [], {}, "OCR 文字识别未启用或当前运行环境不可用，已保留文字结论。")
            elif allow_vision and not vision_profile and not ocr_enabled:
                # 两条图片能力均不可用时，明确保留文字结论。
                merged = _with_vision_execution(merged, "unavailable", [], {}, "本次未获得可用的 OCR 或多模态模型，未执行图片取证。")
            merged = _apply_document_evidence_guard(document, component, rule, merged)
            completed_results[component][rule["rule_id"]] = merged
            if component == "review" and run:
                storage.save_review_results(app, run["review_run_id"], document["document_id"], [merged])
            elif run:
                storage.save_score_results(app, run["score_run_id"], document["document_id"], [merged])
            progress.advance(f"已完成图片识别：{bidder_name} · {label}")
    # EvidencePack 当前仅作影子记录：它观察现有文字/OCR/图片链路和候选页来源，
    # 不参与任何选页、调用、合并、评分或对外 API，确保现有稳定结果不受影响。
    # 影子记录绝不能影响主流程：构造或保存失败仅记录日志，文档仍正常标记完成。
    try:
        rules_by_component = {
            "review": review_rules, "objective": objective_rules, "subjective": subjective_rules,
        }
        shadow_packs: list[dict] = []
        for component, component_rules in rules_by_component.items():
            rule_map = {str(rule.get("rule_id") or ""): rule for rule in component_rules}
            for rule_id, result in completed_results[component].items():
                rule = rule_map.get(str(rule_id))
                if not rule or not isinstance(result, dict):
                    continue
                try:
                    shadow_packs.append(_build_shadow_evidence_pack(task, document, component, rule, result, scan_index))
                except Exception:
                    # 单条规则的影子构造失败不影响其他规则和文档完成状态。
                    traceback.print_exc()
        if shadow_packs:
            storage.save_evidence_packs(
                app, task["project_id"], task["task_id"], document["document_id"],
                str(document.get("sha256") or ""), shadow_packs,
            )
    except Exception:
        # 影子保存失败（磁盘满、DB 锁定等）仅记录日志，真实评审结果已保存，不影响完成状态。
        traceback.print_exc()
    try:
        failed_rule_ids_by_component: dict[str, set[str]] = {"review": set(), "objective": set(), "subjective": set()}
        for unit in values.get("failed_units", []):
            if not isinstance(unit, dict):
                continue
            component = str(unit.get("component") or "")
            if component not in failed_rule_ids_by_component:
                continue
            failed_rule_ids_by_component[component].update(
                str(rule_id) for rule_id in unit.get("rule_ids", []) if str(rule_id)
            )
        checkpoint_values = {
            component: {
                rule_id: result for rule_id, result in by_rule.items()
                if rule_id not in failed_rule_ids_by_component[component]
            }
            for component, by_rule in completed_results.items()
        }
        storage.delete_evaluation_unit_checkpoints(
            app, task["project_id"], rule_set["rule_set_id"], document["document_id"],
            str(task.get("payload", {}).get("input_fingerprint") or ""), failed_rule_ids_by_component,
        )
        storage.save_evaluation_unit_checkpoints(
            app, task["project_id"], rule_set["rule_set_id"], document["document_id"],
            str(task.get("payload", {}).get("input_fingerprint") or ""), checkpoint_values,
        )
    except Exception:
        # 断点缓存只用于加速重跑；保存失败不影响本轮已落库的正式结果。
        traceback.print_exc()
    progress.document_completed(document)
    return values


def _evaluation_highlight_candidates(app, project_id: str) -> tuple[list[dict], dict[tuple[str, str], dict]]:
    """只向收尾提炼发送已有的重要候选，不重发全文或普通满足项。"""
    _, review_results = storage.latest_review_results(app, project_id)
    _, objective_results = storage.latest_score_results(app, project_id, "objective")
    _, subjective_results = storage.latest_score_results(app, project_id, "subjective")
    grouped: dict[str, dict] = {}
    allowed: dict[tuple[str, str], dict] = {}
    review_rank = {"not_satisfied": 4, "partial": 3, "not_found": 2}
    category_rank = {"rejection": 4, "substantive": 4, "qualification": 3, "compliance": 3, "other": 1}
    risk_rank = {"high": 3, "medium": 2, "low": 1}
    for item in review_results:
        if item.get("status") not in review_rank or item.get("risk_level") not in {"high", "medium"}:
            continue
        document_id = str(item.get("document_id") or "")
        rule_id = str(item.get("rule_id") or "")
        if not document_id or not rule_id:
            continue
        candidate = {
            "type": "review", "rule_id": rule_id, "category": item.get("category"),
            "title": item.get("title"), "check_rule": item.get("check_rule"),
            "status": item.get("status"), "risk_level": item.get("risk_level"),
            "confidence": item.get("confidence"), "evidence_quality": item.get("evidence_quality"),
            "evidence": str(item.get("evidence") or "")[:360],
            "reason": str(item.get("reason") or "")[:260],
        }
        candidate["_rank"] = (
            review_rank.get(str(item.get("status")), 0),
            category_rank.get(str(item.get("category")), 0),
            risk_rank.get(str(item.get("risk_level")), 0),
        )
        candidate["_critical_eligible"] = (
            item.get("status") == "not_satisfied"
            and item.get("risk_level") == "high"
            and item.get("confidence") == "high"
            and item.get("evidence_quality") == "sufficient"
            and item.get("category") in {"qualification", "compliance", "substantive", "rejection"}
            and bool(re.search(r"投标无效|否决|不通过|废标|无效投标", str(item.get("check_rule") or "")))
        )
        group = grouped.setdefault(document_id, {
            "document_id": document_id,
            "bidder_name": item.get("bidder_name") or item.get("original_name") or "未命名投标人",
            "candidates": [],
        })
        group["candidates"].append(candidate)
        allowed[(document_id, rule_id)] = candidate
    for item in [*objective_results, *subjective_results]:
        try:
            suggested_score = float(item.get("suggested_score"))
            max_score = float(item.get("max_score"))
        except (TypeError, ValueError):
            continue
        if max_score <= 0 or suggested_score / max_score > 0.5:
            continue
        document_id = str(item.get("document_id") or "")
        rule_id = str(item.get("rule_id") or "")
        if not document_id or not rule_id:
            continue
        candidate = {
            "type": "score", "rule_id": rule_id, "title": item.get("title"),
            "check_rule": item.get("check_rule"), "suggested_score": suggested_score,
            "max_score": max_score, "confidence": item.get("confidence"),
            "evidence": str(item.get("evidence") or "")[:300],
            "reason": str(item.get("reason") or "")[:220],
            "_rank": (1, 0, 0), "_critical_eligible": False,
        }
        group = grouped.setdefault(document_id, {
            "document_id": document_id,
            "bidder_name": item.get("bidder_name") or item.get("original_name") or "未命名投标人",
            "candidates": [],
        })
        group["candidates"].append(candidate)
        allowed[(document_id, rule_id)] = candidate
    values = []
    for group in grouped.values():
        ordered = sorted(group["candidates"], key=lambda item: item["_rank"], reverse=True)[:12]
        values.append({
            "document_id": group["document_id"], "bidder_name": group["bidder_name"],
            "candidates": [
                _highlight_display_candidate(item)
                for item in ordered
            ],
        })
    return values, allowed


_HIGHLIGHT_STATUS_LABELS = {
    "satisfied": "满足", "not_satisfied": "不满足", "partial": "部分满足",
    "not_found": "未找到证据", "manual": "需人工判断", "ocr_required": "需 OCR 后判定",
}
_HIGHLIGHT_RISK_LABELS = {"high": "高", "medium": "中", "low": "低"}
_HIGHLIGHT_EVIDENCE_LABELS = {"sufficient": "充分", "limited": "有限", "missing": "缺失"}
_HIGHLIGHT_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}


def _highlight_display_candidate(item: dict) -> dict:
    """把内部英文状态字段翻译成中文描述后再交给提炼模型，避免模型照抄
    status=/risk_level=/evidence_quality= 等 JSON 字段名泄漏到用户可见结论。"""
    value: dict[str, object] = {
        "type": item.get("type"),
        "category": item.get("category"),
        "title": item.get("title"),
        "check_rule": item.get("check_rule"),
    }
    if item.get("type") == "score":
        value["suggested_score"] = item.get("suggested_score")
        value["max_score"] = item.get("max_score")
    else:
        value["status_label"] = _HIGHLIGHT_STATUS_LABELS.get(
            str(item.get("status")), str(item.get("status") or ""))
        value["risk_label"] = _HIGHLIGHT_RISK_LABELS.get(
            str(item.get("risk_level")), str(item.get("risk_level") or ""))
        value["confidence_label"] = _HIGHLIGHT_CONFIDENCE_LABELS.get(
            str(item.get("confidence")), str(item.get("confidence") or ""))
        value["evidence_quality_label"] = _HIGHLIGHT_EVIDENCE_LABELS.get(
            str(item.get("evidence_quality")), str(item.get("evidence_quality") or ""))
    value["evidence"] = _clean_model_text(item.get("evidence"))[:300]
    value["reason"] = _clean_model_text(item.get("reason"))[:220]
    return {key: val for key, val in value.items() if val not in (None, "")}


def _normalise_evaluation_highlights(parsed: dict, candidates: list[dict],
                                     allowed: dict[tuple[str, str], dict]) -> list[dict]:
    bidder_names = {item["document_id"]: item["bidder_name"] for item in candidates}
    severity_rank = {"critical": 3, "high": 2, "attention": 1}
    values = []
    seen_documents: set[str] = set()
    raw_summaries = parsed.get("summaries") if isinstance(parsed, dict) else None
    for summary in raw_summaries if isinstance(raw_summaries, list) else []:
        if not isinstance(summary, dict):
            continue
        document_id = str(summary.get("document_id") or "")
        if document_id not in bidder_names or document_id in seen_documents:
            continue
        seen_documents.add(document_id)
        highlights = []
        seen_rule_ids: set[str] = set()
        raw_highlights = summary.get("highlights")
        for item in raw_highlights if isinstance(raw_highlights, list) else []:
            # 每家最多 3 条，避免汇总结论面板信息过载。
            if not isinstance(item, dict) or len(highlights) >= 3:
                continue
            rule_id = str(item.get("rule_id") or "")
            candidate = allowed.get((document_id, rule_id))
            if not candidate or rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(rule_id)
            level = str(item.get("level") or "attention")
            if level not in severity_rank:
                level = "attention"
            if level == "critical" and not candidate.get("_critical_eligible"):
                level = "high"
            # 用户保留“照抄照搬”作为独立复核规则时仍可展示，但技术响应表对
            # 招标参数的逐项复述通常是响应方式本身，不能在重要结论层被放大成
            # 高风险；除非上游规则已经给出其他实质性不响应证据。
            if "照抄" in str(candidate.get("title") or "") and level in {"critical", "high"}:
                level = "attention"
            keyword = re.sub(r"[*_`#]+", "", _clean_model_text(item.get("keyword")))[:16]
            conclusion = re.sub(r"\s+", " ", _clean_model_text(item.get("conclusion")))[:80]
            basis = re.sub(r"\s+", " ", _clean_model_text(item.get("basis")))[:120]
            if not keyword or not conclusion:
                continue
            highlights.append({
                "rule_id": rule_id, "level": level, "keyword": keyword,
                "conclusion": conclusion, "basis": basis,
            })
        highlights.sort(key=lambda item: severity_rank[item["level"]], reverse=True)
        overall_level = highlights[0]["level"] if highlights else "none"
        headline = re.sub(r"\s+", " ", _clean_model_text(summary.get("headline"))).strip()
        if len(headline) > 40:
            headline = f"{headline[:40].rstrip()}…"
        values.append({
            "document_id": document_id, "bidder_name": bidder_names[document_id],
            "overall_level": overall_level, "headline": headline,
            "highlights": highlights,
        })
    return values


def _summarise_evaluation_highlights(app, task: dict, profile: dict) -> list[dict]:
    candidates, allowed = _evaluation_highlight_candidates(app, task["project_id"])
    if not candidates:
        return []
    storage.update_task(app, task["task_id"], message="正在提炼极其重要的评审结论")
    prompt = storage.render_prompt_template(
        app, "evaluate_all_highlights_user",
        candidates=json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
    )
    try:
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_highlights", _system_prompt(app, "evaluate_all_highlights"),
            prompt, context_mode="result_highlights_only",
            max_tokens=_output_token_budget(profile, min(8_000, 1_400 + len(candidates) * 600)),
            thinking_mode="disabled",
        )
    except InvalidJsonResponse as exc:
        # 投标人多时结论 JSON 较长易被截断；与其他阶段一致先修复重试，
        # 修复仍失败才由调用方保留原始结果并标记 highlight_failure_count。
        storage.update_task(app, task["task_id"], message="重要结论正在规范化")
        parsed = _repair_invalid_json(
            app, task, profile, "evaluate_all_highlights_json_repair", exc, "summaries",
        )
    return _normalise_evaluation_highlights(parsed, candidates, allowed)


def _evaluate_all(app, task: dict) -> dict:
    """综合评审按规则小组运行并立即落库，避免单次混合 JSON 过大。"""
    rule_set, all_rules = storage.list_rules(app, task["project_id"])
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始综合评审")
    review_rules = [item for item in all_rules if item["enabled"] and item["category"] in {"qualification", "compliance", "substantive", "rejection", "other"}]
    objective_rules = [item for item in all_rules if item["enabled"] and item["category"] == "objective"]
    subjective_rules = [item for item in all_rules if item["enabled"] and item["category"] == "subjective"]
    cross_bid_price_rules = _cross_bid_price_rules(objective_rules)
    cross_bid_price_rule_ids = {item["rule_id"] for item in cross_bid_price_rules}
    # 最低价、基准价等比较型规则不能在单份文件阶段独立计分。单文件阶段完全跳过，
    # 最终统一比较成功后再写入结果，失败时写入明确的“暂无法计算”。
    local_objective_rules = [item for item in objective_rules if item["rule_id"] not in cross_bid_price_rule_ids]
    if not (review_rules or objective_rules or subjective_rules):
        raise ValueError("综合评审需要至少一条已确认的审查或评分规则")
    all_documents = storage.list_documents(app, task["project_id"])
    documents = [item for item in all_documents if item["role"] == "bid"]
    if not documents or any(item["parse_status"] != "success" or not item["parsed_path"] for item in documents):
        raise ValueError("请先成功解析全部投标文件")
    profile = storage.get_model_profile(app, task.get("payload", {}).get("profile_id"), "deepseek-v4-flash")
    # 两项图片能力独立读取：文字模型可在没有多模态档案时继续完成 OCR 取证。
    vision_features_enabled = bool(storage.vision_configuration(app).get("enabled"))
    ocr_features_enabled = bool(storage.ocr_feature_configuration(app).get("enabled"))
    vision_profile = storage.resolve_vision_model_profile(app, profile)
    char_limit = _prompt_char_limit(profile, 260_000, 600_000)
    review_run = storage.create_review_run(app, task["project_id"], task["task_id"], profile["profile_id"]) if review_rules else None
    objective_run = storage.create_score_run(app, task["project_id"], task["task_id"], "objective", profile["profile_id"]) if objective_rules else None
    subjective_run = storage.create_score_run(app, task["project_id"], task["task_id"], "subjective", profile["profile_id"]) if subjective_rules else None
    expected_rule_ids = {
        "review": {item["rule_id"] for item in review_rules},
        "objective": {item["rule_id"] for item in objective_rules},
        "subjective": {item["rule_id"] for item in subjective_rules},
    }
    system_prompt = _system_prompt(app, "evaluate_all")
    storage.update_task(app, task["task_id"], message="正在根据招标文件建立项目范围画像")
    project_scope = _project_scope_profile(
        app, task, profile, all_documents, review_rules + objective_rules + subjective_rules,
    )
    # 范围画像供模型理解项目；完整招标文本只供本地校验“该对象是否已列入采购范围”，
    # 不进入任何提示词，既避免长清单抽样遗漏，也不增加模型输入 token。
    project_scope["_tender_scope_baseline"] = _tender_scope_baseline(all_documents)
    compact_retry_count = split_retry_count = 0
    manual_fallback_rule_count = 0
    evidence_ledger_rule_count = evidence_ledger_empty_rule_count = 0
    failed_units: list[dict] = []
    reused_document_count = 0
    batch_count = 0
    full_scan_document_count = full_scan_batch_count = full_scan_failed_chunk_count = 0
    full_scan_recovery_warning_count = 0
    local_ocr_rule_count = local_ocr_skipped_rule_count = enhancement_rule_count = 0
    local_ocr_seconds = 0.0
    groups_per_document = sum(
        len(_evaluation_rule_batches(component, component_rules))
        for component, component_rules in (("review", review_rules), ("objective", local_objective_rules), ("subjective", subjective_rules))
    )
    has_local_rules = bool(review_rules or local_objective_rules or subjective_rules)
    visual_rule_count = sum(
        1 for item in (review_rules + local_objective_rules + subjective_rules)
        if _visual_advance_estimated(item)
    ) if (vision_features_enabled or ocr_features_enabled) else 0
    scan_units_by_document = {
        item["document_id"]: _full_scan_chunk_count(item) if has_local_rules else 0
        for item in documents
    }
    cross_bid_units = 1 if objective_run and len(documents) >= 2 and cross_bid_price_rules else 0
    total_work_units = max(1, sum(scan_units_by_document.values()) + len(documents) * (groups_per_document + visual_rule_count) + cross_bid_units)
    # 首次仍以两路保守启动；连续成功后再按模型档案增加并行位。
    # 对 2 核 2GB 服务器而言这主要增加网络等待并行，不常驻加载额外模型。
    parallel_limit = _profile_parallel_limit(profile, len(documents))
    task["_evaluation_request_gate"] = _EvaluationRequestGate(
        # 多投标人两路保守启动；单份文件先串行确认服务商稳定性，连续成功后 gate
        # 自动升档，后续独立规则组才会并行。
        min(2 if len(documents) > 1 else 1, parallel_limit),
        max_limit=parallel_limit,
    )
    progress = _EvaluationProgress(app, task, total_work_units, len(documents))

    def run_document(document: dict) -> dict:
        return _evaluate_document(
            app, task, document, rule_set=rule_set, profile=profile, char_limit=char_limit,
            expected_rule_ids=expected_rule_ids, review_rules=review_rules, objective_rules=local_objective_rules,
            subjective_rules=subjective_rules, review_run=review_run, objective_run=objective_run,
            subjective_run=subjective_run, project_scope=project_scope, system_prompt=system_prompt,
            scan_units=scan_units_by_document[document["document_id"]], groups_per_document=groups_per_document,
            vision_profile=vision_profile, ocr_features_enabled=ocr_features_enabled,
            visual_units=visual_rule_count, progress=progress,
        )

    # 只有投标人之间的文件审查并行；单份文件仍保持页块、规则组的先后顺序。
    # 模型请求默认两路、稳定后按档案动态升档，触发服务商限流后会自动逐级降路重试。
    document_results: list[dict] = []
    if len(documents) == 1:
        document_results.append(run_document(documents[0]))
    else:
        with ThreadPoolExecutor(max_workers=parallel_limit, thread_name_prefix="evaluation-bid") as executor:
            futures = [executor.submit(run_document, document) for document in documents]
            for future in as_completed(futures):
                document_results.append(future.result())
    for value in document_results:
        reused_document_count += value.get("reused_document_count", 0)
        compact_retry_count += value.get("compact_retry_count", 0)
        split_retry_count += value.get("split_retry_count", 0)
        manual_fallback_rule_count += value.get("manual_fallback_rule_count", 0)
        evidence_ledger_rule_count += value.get("evidence_ledger_rule_count", 0)
        evidence_ledger_empty_rule_count += value.get("evidence_ledger_empty_rule_count", 0)
        failed_units.extend(item for item in value.get("failed_units", []) if isinstance(item, dict))
        batch_count += value.get("batch_count", 0)
        full_scan_document_count += value.get("full_scan_document_count", 0)
        full_scan_batch_count += value.get("full_scan_batch_count", 0)
        full_scan_failed_chunk_count += value.get("full_scan_failed_chunk_count", 0)
        full_scan_recovery_warning_count += int(bool(value.get("full_scan_recovery_warning")))
        local_ocr_rule_count += value.get("local_ocr_rule_count", 0)
        local_ocr_skipped_rule_count += value.get("local_ocr_skipped_rule_count", 0)
        local_ocr_seconds += float(value.get("local_ocr_seconds", 0) or 0)
        enhancement_rule_count += value.get("enhancement_rule_count", 0)
    cross_bid_price = {"rule_count": 0, "result_count": 0, "retry_count": 0, "missing_count": 0}
    price_review_reconciled_count = 0
    if cross_bid_units and objective_run:
        progress.message("正在统一比较全部投标人的报价并计算价格分")
        cross_bid_price = _run_cross_bid_price_scoring(
            app, task, profile, documents, cross_bid_price_rules, objective_run["score_run_id"],
        )
        # 单文件审查可能先把“报价未见”写入结果；跨投标人价格核验完成后，以同批
        # 已定位的明确报价事实回收这类过时表述，避免摘要把已找到的报价误报为缺失。
        if review_run and cross_bid_price.get("result_count"):
            price_review_reconciled_count = storage.reconcile_price_review_results(
                app, review_run["review_run_id"], objective_run["score_run_id"],
            )
        progress.advance("已完成全部投标人的报价比较与价格评分")
    highlight_failure_count = 0
    try:
        highlights = _summarise_evaluation_highlights(app, task, profile)
    except Exception as exc:
        # 重要结论只是既有结果的展示层，不得因为该附加调用异常而丢失已落库的评审和评分结果。
        highlights = []
        highlight_failure_count = 1
        storage.update_task(app, task["task_id"], message=f"重要结论提炼未完成，原始评审结果已完整保留：{exc}")
    recovery = storage.task_recovery_summary(app, task["task_id"])
    return {"review_run_id": review_run["review_run_id"] if review_run else None, "objective_run_id": objective_run["score_run_id"] if objective_run else None,
            "subjective_run_id": subjective_run["score_run_id"] if subjective_run else None, "document_count": len(documents),
            "reused_document_count": reused_document_count, "model_document_count": len(documents) - reused_document_count,
            "rule_count": len(all_rules), "profile": profile["display_name"],
            "vision_profile": vision_profile.get("display_name") if vision_profile and visual_rule_count else None,
            "vision_rule_count": visual_rule_count,
            # format_recovery_count 保留旧任务结果的统计语义；其余字段按真实调用路径拆分，
            # 避免把 JSON 修复误显示为“紧凑重试”。
            "format_recovery_count": compact_retry_count,
            "json_repair_count": recovery["json_repair_count"],
            "compact_retry_count": recovery["compact_retry_count"],
            "split_retry_count": split_retry_count, "rule_split_count": split_retry_count,
            "missing_rule_retry_count": recovery["missing_rule_retry_count"],
            "manual_fallback_rule_count": manual_fallback_rule_count,
            "batch_count": batch_count, "full_scan_document_count": full_scan_document_count,
            "full_scan_batch_count": full_scan_batch_count, "full_scan_failed_chunk_count": full_scan_failed_chunk_count,
            "full_scan_recovery_warning_count": full_scan_recovery_warning_count,
            "cross_bid_price": cross_bid_price,
            "price_review_reconciled_count": price_review_reconciled_count,
            "evidence_ledger_rule_count": evidence_ledger_rule_count,
            "evidence_ledger_empty_rule_count": evidence_ledger_empty_rule_count,
            "local_ocr_rule_count": local_ocr_rule_count,
            "local_ocr_skipped_rule_count": local_ocr_skipped_rule_count,
            "local_ocr_seconds": round(local_ocr_seconds, 2),
            "enhancement_rule_count": enhancement_rule_count,
            "completion_state": "partial_success" if failed_units else "complete",
            "failed_units": failed_units,
            "highlights": highlights, "highlight_failure_count": highlight_failure_count,
            "prompt_version": PROMPT_VERSION}


def run_task(app, task: dict) -> None:
    try:
        if task["task_type"] == "parse_documents":
            result = _parse_document(app, task)
        elif task["task_type"] == "compare_documents":
            result = _compare_documents(app, task)
        elif task["task_type"] == "extract_rules":
            result = _extract_rules(app, task)
        elif task["task_type"] == "review_documents":
            result = _review_documents(app, task)
        elif task["task_type"] == "score_objective":
            result = _score_documents(app, task, "objective")
        elif task["task_type"] == "score_subjective":
            result = _score_documents(app, task, "subjective")
        elif task["task_type"] == "evaluate_all":
            result = _evaluate_all(app, task)
        else:
            raise ValueError(f"暂不支持的任务类型：{task['task_type']}")
        storage.update_task(app, task["task_id"], progress=100, message="任务完成", status="success", result=result)
    except (ComparisonLimitError, ValueError) as exc:
        storage.update_task(app, task["task_id"], status="error", error=str(exc), message="任务失败")
    except Exception as exc:
        traceback.print_exc()
        storage.update_task(app, task["task_id"], status="error", error=f"任务执行异常：{exc}", message="任务失败")


def main() -> int:
    app = create_worker_app()
    storage.init_database(app)
    lock = _lock_path(app)
    try:
        lock.write_text(str(os.getpid()), encoding="utf-8")
        storage.interrupt_stale_running_tasks(app)
        while True:
            task = storage.next_queued_task(app)
            if not task:
                break
            run_task(app, task)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
