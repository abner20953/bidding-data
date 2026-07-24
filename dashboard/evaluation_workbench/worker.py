"""按需启动的工作台任务进程。"""

from __future__ import annotations

import itertools
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree

import fitz

from dashboard.evaluation_workbench import storage
from dashboard.evaluation_workbench.ai_gateway import (
    InvalidJsonResponse, ModelResponseEnvelopeError, _recover_complete_json_array, request_json,
)
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.prompt_context import (
    build_rule_context, select_rule_chunk_map, select_rule_chunks, split_full_text_chunks,
)
from dashboard.evaluation_workbench.prompt_templates import EVALUATION_PROMPT_VERSION
from dashboard.blueprints.evaluation_workbench import create_worker_app
from dashboard.utils.comparator import CollusionDetector, ComparisonLimitError


MAX_PARSE_PAGES = 2000
MAX_PARSED_CHARS = 2_000_000
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024
PROMPT_VERSION = EVALUATION_PROMPT_VERSION
COMPARE_AI_PROMPT_VERSION = "compare-evidence-ai-v2"
# 单条线索的证据包虽小，但查重往往同时命中多种维度；以较小批次起步，并在
# 截断时继续局部拆分，避免某一批过长导致整批线索都只能降级为人工核验。
COMPARE_AI_BATCH_SIZE = 8


class _EvaluationRequestGate:
    """规则提取/综合评审的模型请求闸门；稳定时升至三路，限流时逐级回退。"""

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
        "http 429", "http 529", "http 502", "http 503", "http 504",
        "rate limit", "too many requests", "overloaded", "temporarily unavailable", "timeout", "timed out",
    )) or "限流" in str(error) or "接口繁忙" in str(error)


def _prompt_char_limit(profile: dict, default: int, ceiling: int) -> int:
    """以保守字符数近似上下文，给提示和输出预留空间。"""
    try:
        context_limit = int(profile.get("context_limit") or 0)
    except (TypeError, ValueError):
        context_limit = 0
    return min(ceiling, max(8_000, int(context_limit * 0.7))) if context_limit else default


def _lock_path(app) -> Path:
    return storage.data_dir(app) / "worker.lock"


def _request_task_json(app, task: dict, profile: dict, phase: str, system_prompt: str, user_prompt: str,
                       *, document_id: str | None = None, context_mode: str = "full_prefix",
                       max_tokens: int | None = None, thinking_mode: str | None = None) -> dict:
    """调用模型并只记录用量元数据，不记录正文或提示词。"""
    usage: dict = {}
    response_metadata: dict = {"requested_max_tokens": max_tokens}

    def record_usage(value: dict) -> None:
        usage.update(value if isinstance(value, dict) else {})

    def record_response_metadata(value: dict) -> None:
        response_metadata.update(value if isinstance(value, dict) else {})

    gate = task.get("_evaluation_request_gate")
    try:
        effective_profile = {**profile, "thinking_mode": thinking_mode} if thinking_mode else profile
        for attempt in range(2):
            retry_after_rate_limit = False
            if gate:
                gate.acquire()
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
                # HTTP 成功但缺少 choices/message/content 同样属于服务商瞬时空包，
                # 不能把已完成的全文扫描和其他投标人结果一并判为失败。
                should_retry = attempt == 0 and (
                    _is_rate_limit_error(exc) or isinstance(exc, ModelResponseEnvelopeError)
                )
                if should_retry:
                    if gate and gate.reduce_after_rate_limit():
                        message = "模型接口返回不完整响应，已自动降低并行度后重试当前分组" if isinstance(exc, ModelResponseEnvelopeError) else "模型接口限流或暂时繁忙，已自动降低并行度后继续"
                        storage.update_task(app, task["task_id"], message=message)
                    elif isinstance(exc, ModelResponseEnvelopeError):
                        storage.update_task(app, task["task_id"], message="模型接口返回不完整响应，正在重试当前分组")
                    retry_after_rate_limit = True
                else:
                    raise
            finally:
                if gate:
                    gate.release()
            if retry_after_rate_limit:
                # 必须先释放并发位；否则失败请求在退避期间会无谓阻塞另一家投标人的收尾。
                time.sleep(2)
                continue
    finally:
        # 部分兼容接口不返回 usage；仍保留发送字符数与截断元数据以便统计和优化。
        storage.record_model_call(
            app, task["task_id"], task["project_id"], phase, profile.get("profile_id"),
            document_id=document_id, input_chars=len(system_prompt) + len(user_prompt),
            context_mode=context_mode, usage=usage, response_metadata=response_metadata,
        )


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
                         if key in {"page_a", "page_b", "text_a", "text_b", "similarity", "shared_edits", "error_kind", "entity_kind", "field", "value", "strength"}})
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
            if not isinstance(value, dict) or value.get("signal_id") not in allowed_ids:
                continue
            decision = value.get("decision")
            if decision not in {"confirmed_clue", "suspected_clue", "excluded", "unassessable"}:
                decision = "unassessable"
            signal = by_id[value["signal_id"]]
            signal["ai_assessment"] = {
                "decision": decision,
                "risk_level": value.get("risk_level") if value.get("risk_level") in {"low", "medium", "high"} else "medium",
                "confidence": value.get("confidence") if value.get("confidence") in {"high", "medium", "low"} else "medium",
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
        decisions = [
            signal["ai_assessment"]["decision"] for signal in signals
            if {signal.get("document_a_id"), signal.get("document_b_id")} == pair_ids
        ]
        if "confirmed_clue" in decisions:
            summary["assessment_result"] = "confirmed_clue"
        elif "suspected_clue" in decisions:
            summary["assessment_result"] = "suspected_clue"
        elif decisions and all(decision == "excluded" for decision in decisions):
            summary["assessment_result"] = "excluded"
        elif decisions:
            summary["assessment_result"] = "unassessable"
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
_QUALIFICATION_CLAUSE_ID_PATTERN = re.compile(r"(?m)^\s*(1\.[1-9])(?:\s|$)")

def _score_clause_packets(text: str, limit: int = 240) -> list[dict]:
    """为每个明确计分行构造独立、稳定的覆盖条款，不合并相邻评分项。"""
    lines = [line.strip() for line in text.splitlines()]
    packets: list[dict] = []
    for index, line in enumerate(lines):
        if not _SCORE_CLAUSE_PATTERN.search(re.sub(r"\s+", "", line)):
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
    """从正式资格证明材料表中构造逐项核验包，补上评分覆盖之外的资格覆盖口径。

    仅依据表格的通用结构（1.x 编号与“证明材料”），不依赖业绩、社保或财务等业务词。
    PDF 将表格跨页拆开时，同一连续区域会一并保留，避免遗漏后一页的具体条件。
    """
    pages = [value.strip() for value in _PARSED_PAGE_MARKER.split(text) if value.strip()]
    selected: list[str] = []
    for page in pages:
        clause_ids = _QUALIFICATION_CLAUSE_ID_PATTERN.findall(page)
        if "证明材料" in page and len(set(clause_ids)) >= 2:
            selected.append(page)
    if not selected:
        return []
    source = "\n\n".join(selected)
    matches = list(_QUALIFICATION_CLAUSE_ID_PATTERN.finditer(source))
    packets: list[dict] = []
    for index, match in enumerate(matches):
        clause_id = match.group(1)
        start = max(0, match.start() - 180)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        value = source[start:end].strip()
        if not value:
            continue
        # 使用完整条件而非条款号生成稳定 ID，避免不同文件同为 1.1 时误作同一条款。
        digest = re.sub(r"\s+", "", value)
        packets.append({
            "clause_id": f"QF-{hashlib.sha1(digest.encode('utf-8')).hexdigest()[:10]}",
            "label": clause_id,
            "text": value[:2_400],
        })
        if len(packets) >= limit:
            break
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
        values.append(f"【评分条款 {clause_id}】\n{_score_packet_text(packet)}")
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
            "ocr_required": item.get("ocr_required"), "scoring": item.get("scoring"),
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
                message=f"{batch_label} 格式异常，已在本地回收 {len(recovered_rules)} 条完整规则，正在补充遗漏项",
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
        storage.update_task(app, task["task_id"], message=f"{batch_label} 格式异常，正在以紧凑 JSON 重试")
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
        if not _is_invalid_json_model_response(exc):
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
        storage.update_task(app, task["task_id"], message=f"{batch_label} 格式异常，正在以紧凑 JSON 重试")
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
    # 初始闸门仍是两路；第三个工作位只在任务已启用动态闸门时才会创建并获得请求许可。
    gate = task.get("_evaluation_request_gate")
    workers = min(3 if gate and gate.max_limit >= 3 else 2, total)
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
                message=f"正在分段提取评审规则（已完成 {completed}/{total} 批，动态至多三路并发）",
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


def _dedupe_rule_candidates(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    result = []
    for item in items:
        signature = _rule_signature(item)
        if not all(signature) or signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


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
            storage.update_task(app, task["task_id"], message=f"正在分组编译评审规则（{len(groups)} 组，动态至多三路并发）")
            group_results: list[tuple[list[dict], list[dict], bool] | None] = [None] * len(groups)
            with ThreadPoolExecutor(max_workers=min(3, len(groups))) as executor:
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
        if item.get("enabled")
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
            if not isinstance(operation, dict) or operation.get("reason") not in {"partial_boundary", "umbrella"}:
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
            if any(rules[index].get("category") in {"objective", "subjective"} for index in indexes):
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
            if not isinstance(operation, dict) or operation.get("reason") not in allowed_drop_reasons:
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
    documents = storage.list_documents(app, task["project_id"])
    tender = next((item for item in documents if item["role"] == "tender"), None)
    if not tender or tender.get("parse_status") != "success" or not tender.get("parsed_path"):
        raise ValueError("请先上传并成功解析主招标文件")
    main_text = Path(tender["parsed_path"]).read_text(encoding="utf-8", errors="ignore").strip()
    if not main_text:
        raise ValueError("主招标文件未提取到可用文本，扫描件需要先提供可检索版本")
    profile = storage.get_model_profile(app, task.get("payload", {}).get("profile_id"), "deepseek-v4-flash")
    char_limit = _prompt_char_limit(profile, 180_000, 400_000)
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
    score_packets = _score_clause_packets(text, limit=400)
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
    system_prompt = _system_prompt(app, "extract_rules")
    # 规则映射和顶层规则组编译共用同一限流闸门：默认两路，连续成功后可升至三路，
    # 接口繁忙时自动回落。只限制远端请求，不额外增加本地解析并行度。
    # 这只限制远端请求，并不创建常驻线程或后台进程。
    task["_evaluation_request_gate"] = _EvaluationRequestGate(limit=2, max_limit=min(3, len(batches)))
    raw_rules, compact_retry_count, split_retry_count = _extract_rule_batches(
        app, task, profile, system_prompt, batches, document_id=tender["document_id"],
        review_anchor_catalog=review_anchor_catalog,
    )
    raw_rules = _dedupe_rule_candidates(raw_rules)
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
                    raw_rules.extend(item for item in supplement_rules if isinstance(item, dict))
                    scoring_supplement_count += len(supplement_rules)
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
                raw_rules.extend(item for item in supplement_rules if isinstance(item, dict))
                qualification_supplement_count = len(supplement_rules)
        except ValueError as exc:
            # 正式资格覆盖是增强路径；异常时保留已映射规则，并在任务结果中透明记录。
            qualification_supplement_failures = 1
            storage.update_task(app, task["task_id"], message=f"部分资格条款补充未完成：{exc}")
    mapped_candidates = [
        item for item in raw_rules if isinstance(item, dict) and str(item.get("title", "")).strip()
        and item.get("category") in {"qualification", "compliance", "substantive", "rejection", "objective", "subjective"}
    ]
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
    rules = candidates
    for item in rules:
        if _rule_requires_visual_verification(item):
            item["ocr_required"] = True
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
    rules, quality_gate = _final_rule_quality_gate(
        app, task, profile, system_prompt, rules, score_packets,
    )
    rules, finalisation = _finalise_rule_operations(
        app, task, profile, system_prompt, rules,
    )
    for item in rules:
        if _rule_requires_visual_verification(item):
            item["ocr_required"] = True
    if not rules:
        raise ValueError("模型未提取到可确认的有效规则，请检查招标文件文本或更换模型")
    storage.update_task(app, task["task_id"], progress=80, message="正在保存待确认规则")
    rule_set = storage.replace_rules_from_extraction(app, task["project_id"], task["task_id"], rules)
    global_rule_count = rule_set.get("global_rule_count", 0)
    return {"rule_set_id": rule_set["rule_set_id"], "version": rule_set["version"], "rule_count": len(rules) + global_rule_count,
            "ai_rule_count": len(rules), "global_rule_count": global_rule_count,
            "excluded_rule_count": excluded_rule_count, "profile": profile["display_name"],
            "compact_retry_count": compact_retry_count, "score_clause_count": len(score_packets),
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


def _normalise_review_results(output: object, rules: list[dict]) -> list[dict]:
    by_id = {item["rule_id"]: item for item in rules}
    normalized = []
    for item in output if isinstance(output, list) else []:
        rule_id = item.get("rule_id") if isinstance(item, dict) else None
        if rule_id not in by_id:
            continue
        status = item.get("status")
        if status not in {"satisfied", "not_satisfied", "partial", "not_found", "manual", "ocr_required"}:
            status = "manual"
        original_status = status
        visual_rule = _rule_requires_visual_verification(by_id[rule_id])
        # OCR 规则在当前流程尚未真正识别图像时，不能因文本层未命中就输出高风险
        # 不满足；模型若在理由中明确提出 OCR 缺口，也统一回落到待 OCR。
        if visual_rule or (
            status != "satisfied" and _is_explicit_ocr_gap(item, by_id[rule_id])
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
    confidence = item.get("confidence") if item.get("confidence") in {"high", "medium", "low"} else "medium"
    evidence_quality = item.get("evidence_quality") if item.get("evidence_quality") in {"sufficient", "limited", "missing"} else ("sufficient" if str(item.get("evidence", "")).strip() else "missing")
    risk = item.get("risk_level") if item.get("risk_level") in {"low", "medium", "high"} else "medium"
    # 仅对正向、低风险、证据充分的结论自动进入批量确认；否定/废标类风险不自动放行。
    if status == "ocr_required":
        # OCR 缺失仅说明当前无法读取图像证据，并非投标文件本身存在风险。
        confidence, evidence_quality, risk = "low", "missing", "low"
        if not str(item.get("reason", "")).strip():
            item = {**item, "reason": "该规则的关键证据需要 OCR 识别后才能判定。"}
    auto_ready = status == "satisfied" and risk == "low" and confidence == "high" and evidence_quality == "sufficient"
    return {"rule_id": rule_id, "status": status, "evidence": _clean_model_text(item.get("evidence"))[:2000],
            "page_hint": _clean_model_text(item.get("page_hint"))[:80] or None, "reason": _clean_model_text(item.get("reason"))[:2000],
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
                        "ocr_required": _rule_requires_visual_verification(rule), "scoring": scoring})
    return payload


def _clean_model_text(value: object) -> str:
    """移除只供内部编排使用的标记，保留模型的业务判断。"""
    text = str(value or "")
    text = re.sub(r"\bcontext_unmatched\s*=\s*true\b[，,。；;：:\s]*", "", text, flags=re.IGNORECASE)
    return text.strip()


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
        return None
    return None


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
    parts = []
    calculation = _clean_model_text(raw.get("calculation"))
    reason = _clean_model_text(raw.get("reason"))
    if calculation:
        parts.append(f"计分过程：{calculation}")
    if reason:
        parts.append(reason)
    if raw.get("needs_ocr") is True:
        parts.append("部分关键证据建议通过 OCR 进一步核验；以上为基于可见材料的暂定建议。")
    if not parts:
        parts.append("AI未返回完整理由。" if suggested is not None else "模型未返回可用建议分。")
    return "\n".join(parts)


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
    confidence = raw.get("confidence") if raw.get("confidence") in {"high", "medium", "low"} else "medium"
    needs_ocr = raw.get("needs_ocr") is True
    evidence = _score_evidence_text(raw)
    has_evidence = bool(evidence)
    auto_ready = suggested is not None and confidence == "high" and has_evidence and not needs_ocr
    return {"rule_id": rule_id, "suggested_score": suggested, "final_score": None,
            "effective_score": suggested if auto_ready else None, "max_score": max_score or None,
            "evidence": evidence[:2000],
            "reason": _score_reason_text(raw, suggested)[:2000],
            "confidence": confidence, "automation_status": "ready_for_batch_confirmation" if auto_ready else "needs_review",
            "requires_review": not auto_ready,
            "review_reason": "" if auto_ready else "未得到高置信、可引用的建议分，需人工复核。"}


def _is_invalid_json_model_response(exc: ValueError) -> bool:
    return str(exc).startswith("模型未返回有效 JSON")


def _is_model_format_error(exc: ValueError) -> bool:
    message = str(exc)
    return _is_invalid_json_model_response(exc) or message.startswith("模型返回格式不符合综合评审要求")


EVALUATION_BATCH_SIZES = {"review": 8, "objective": 8, "subjective": 6}
# 超过阈值的文件先按连续页块做全文覆盖扫描；阈值以下的短文件直接随最终规则
# 组发送全文，同样满足全文覆盖。所有索引都只存在于当前工作进程内。
FULL_SCAN_THRESHOLD_CHARS = 24_000
FULL_SCAN_CHUNK_CHARS = 11_000
# 首轮只建立候选证据索引：每个页块携带一次完整的精简规则目录。正常情况下
# 不再形成“页块 × 规则批次”的矩阵；若模型确实输出超长，才仅拆规则目录一次。
FULL_SCAN_CATALOG_RULE_CHARS = 220
# 二次复核上下文上限。全文首轮已覆盖所有页面，此处只装入候选证据和重点原文。
EVALUATION_BATCH_CONTEXT_CHARS = 64_000
EVALUATION_STRATEGY_CONTEXT_CHARS = {
    "point": 42_000,
    "consistency": 55_000,
    "counting": 64_000,
    "section": 64_000,
}


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
    return storage.render_prompt_template(
        app, template_id, rules=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写", text=text,
        retry_note=retry_note,
    )


def _combined_batch_payload(component: str, rules: list[dict]) -> list[dict]:
    if component == "review":
        return [{"rule_id": item["rule_id"], "category": item["category"], "title": item["title"],
                 "check_rule": item.get("check_rule") or item["title"], "source_text": item["source_text"],
                 "ocr_required": _rule_requires_visual_verification(item)} for item in rules]
    return _score_payload(rules)


def _full_scan_catalog(rules: list[dict]) -> list[dict]:
    """生成首轮扫描专用的精简规则目录，详细评分规则留给最终汇总阶段。"""
    catalog = []
    for rule in rules:
        query = re.sub(r"\s+", " ", f"{rule.get('title') or ''}；{rule.get('check_rule') or rule.get('title') or ''}").strip()
        # 主观评分表常把多个有独立分值的子项写在一条规则中。首轮只截取通用长度
        # 会丢掉末尾的子项，导致后续评分只能看到“总分”而不能看到完整评分维度。
        query_limit = 420 if rule["category"] == "subjective" else FULL_SCAN_CATALOG_RULE_CHARS
        item = {
            "id": rule["rule_id"],
            # 保留旧字段，避免用户在提示词配置中保留了旧版 findings 模板时无法对应规则。
            "rule_id": rule["rule_id"],
            "q": query[:query_limit],
            "type": rule["category"],
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
        "本次正常扫描 matches 最多 36 条、scope_anomalies 最多 8 条；复合评分规则的不同叶子项可分别返回，"
        "同一规则最多 8 条；若后文的通用限制与本段冲突，以本段为准。\n"
    )
    prompt = storage.render_prompt_template(
        app, "evaluate_all_full_scan_user", retry_note=retry_note,
        project_scope=json.dumps(project_scope, ensure_ascii=False, separators=(",", ":")),
        rules=json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写",
        chunk_label=_full_scan_chunk_label(chunk), text=chunk["text"],
    )
    # 同步兼容云端尚未恢复默认的旧自定义模板，避免严格重试同时出现 16/4 与 36/8
    # 两套上限。这里只收紧格式数量，不改任何业务判断指令。
    if compact:
        prompt = prompt.replace("最多36条", "最多16条").replace("最多 36 条", "最多 16 条")
        prompt = prompt.replace("最多8条", "最多4条").replace("最多 8 条", "最多 4 条")
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
        if rule_id not in allowed_ids:
            continue
        if status not in {"supports", "contradicts", "partial", "suspected"}:
            status = {"support": "supports", "contradict": "contradicts", "suspect": "suspected"}.get(str(status).lower(), "suspected")
        confidence = confidence if confidence in {"high", "medium", "low"} else "medium"
        evidence_priority = evidence_priority if evidence_priority in {"high", "medium", "low"} else "medium"
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
        candidates.append(candidate)
    return candidates


SCOPE_PROFILE_FIELDS = (
    "project_identity", "scope_summary", "service_targets", "core_tasks", "technical_topics",
    "equipment_or_materials", "deliverables", "standards_or_rules", "regions", "keywords",
)


def _scope_source(documents: list[dict], char_limit: int) -> str:
    """构造项目范围画像依据；长招标文件均衡保留前、中、后段。"""
    sources = []
    tender_documents = [item for item in documents if item.get("role") in {"tender", "tender_attachment"}]
    # 平均分配预算，避免多个招标附件时前几份文件挤占全部上下文。
    per_document = max(1, char_limit // max(1, len(tender_documents)))
    for document in tender_documents:
        path = Path(str(document.get("parsed_path") or ""))
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if len(text) > per_document:
            # 项目范围、技术要求和评分附件常位于正文中段；只保留首尾会让范围画像产生
            # 结构性盲区。三段等额抽样只用于建立范围基准，不替代后续投标文件全文扫描。
            segment = max(1, (per_document - 100) // 3)
            middle_start = max(0, len(text) // 2 - segment // 2)
            text = (
                f"【文件前段】\n{text[:segment]}\n\n"
                f"【文件中段】\n{text[middle_start:middle_start + segment]}\n\n"
                f"【文件后段】\n{text[-segment:]}"
            )
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
        rules=json.dumps(rule_packet, ensure_ascii=False, separators=(",", ":")), tender_text=tender_text,
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


def _run_full_scan_piece(app, task: dict, profile: dict, document: dict, catalog: list[dict], chunk: dict,
                          project_scope: dict, system_prompt: str, depth: int = 0) -> tuple[dict, int, int, list[dict]]:
    """扫描一个连续页块；只在输出异常时拆分规则目录，绝不递归重发全文。"""
    allowed_ids = {item["id"] for item in catalog}
    # 首轮只是候选索引而非最终结论，但 36 条规则证据和 8 条范围候选需要足够的
    # JSON 闭合空间；预算仍受全局 12k 上限和模型档案约束。
    max_tokens = _output_token_budget(profile, min(4_200, max(1_800, 1_000 + len(catalog) * 45)))

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
            document_id=document["document_id"], context_mode=f"full_scan:{chunk['chunk_id']}",
            max_tokens=max_tokens, thinking_mode="disabled",
        )
        return findings_from(parsed), 0, 0, []
    except InvalidJsonResponse as exc:
        format_error = exc
        if exc.finish_reason.lower() not in {"length", "max_tokens"}:
            storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 扫描 JSON 异常，正在仅修复响应")
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

    storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 全文扫描格式异常，正在严格重试")
    try:
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_full_scan_compact_retry", system_prompt,
            _full_scan_prompt(app, document, catalog, chunk, project_scope, compact=True),
            document_id=document["document_id"], context_mode=f"full_scan_compact:{chunk['chunk_id']}",
            max_tokens=max_tokens, thinking_mode="disabled",
        )
        return findings_from(parsed), 1, 0, []
    except ValueError as retry_exc:
        if not _is_model_format_error(retry_exc) and not str(retry_exc).startswith("模型返回格式不符合全文扫描要求"):
            raise
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
    chunks = split_full_text_chunks(document["parsed_path"], FULL_SCAN_CHUNK_CHARS, overlap_pages=1)
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
    findings: list[dict] = []
    scope_anomalies: list[dict] = []
    failed_chunks: list[dict] = []
    compact_retry_count = split_retry_count = 0
    total = max(1, len(chunks))
    completed = 0
    for chunk in chunks:
        completed += 1
        chunk_hash = hashlib.sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest()
        message = f"正在全文证据扫描 {document['bidder_name'] or document['original_name']}：{_full_scan_chunk_label(chunk)}（{completed}/{total}）"
        if progress_callback:
            progress_callback(message)
        else:
            progress = int((progress_offset + completed - 1) * 100 / max(1, progress_total))
            storage.update_task(app, task["task_id"], progress=progress, message=message)
        checkpoint = storage.get_evaluation_scan_checkpoint(app, document["document_id"], scan_key, chunk["chunk_id"], chunk_hash)
        if checkpoint is not None:
            # 兼容 v3 已落库的纯 findings 检查点，避免升级时浪费一次扫描。
            if isinstance(checkpoint, list):
                findings.extend(checkpoint)
            elif isinstance(checkpoint, dict):
                findings.extend(checkpoint.get("findings") or [])
                scope_anomalies.extend(checkpoint.get("scope_anomalies") or [])
            continue
        result = _run_full_scan_piece(app, task, profile, document, catalog, chunk, project_scope, system_prompt)
        findings.extend(result[0]["findings"])
        scope_anomalies.extend(result[0]["scope_anomalies"])
        compact_retry_count += result[1]
        split_retry_count += result[2]
        failed_chunks.extend(result[3])
        if not result[3]:
            storage.save_evaluation_scan_checkpoint(
                app, task["project_id"], document["document_id"], scan_key, chunk["chunk_id"], chunk_hash, result[0],
            )
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
    ranked_findings = sorted(
        findings,
        key=lambda item: (
            priority_rank.get(item.get("evidence_priority"), 1),
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
    scope_anomalies.sort(key=lambda item: priority_rank.get(item.get("candidate_priority"), 1))
    if is_review_group:
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
    # 最终组不需要重发首轮记录的所有内部字段。压缩为可追溯的证据目录后，优先给原页
    # 留出空间，避免“候选 JSON 很完整、重点原文却被截断”的反向退化。
    compact_findings = [
        {
            "rule_id": item.get("rule_id"), "page": item.get("page_hint") or item.get("page_range"),
            "evidence": item.get("evidence"), "status": item.get("tentative_status"),
            "priority": item.get("evidence_priority"), "evidence_origin": item.get("observation"),
        }
        for item in ranked_findings
    ]
    scope_packet = ""
    if is_review_group:
        scope_packet = (
            "\n\n【项目范围画像（来自招标文件和已确认规则）】\n"
            + json.dumps(scan.get("project_scope", {}), ensure_ascii=False, separators=(",", ":"))
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
    fallback_by_rule = select_rule_chunk_map(scan.get("chunks", []), rules, per_rule=1)
    for rule in rules:
        rule_id = str(rule.get("rule_id") or "")
        finding = best_by_rule.get(rule_id)
        chunk_id = str((finding or {}).get("chunk_id") or "").split(".", 1)[0]
        if not chunk_id:
            chunk_id = next(iter(fallback_by_rule.get(rule_id, [])), "")
        if not chunk_id:
            continue
        if finding and finding.get("evidence"):
            rule_evidence.setdefault(chunk_id, []).append(str(finding.get("evidence")))
        if chunk_id not in required_ids:
            required_ids.append(chunk_id)
    ordered_ids = required_ids + [item for item in selected_ids if item not in required_ids]

    def source_excerpt(chunk: dict, evidence_values: list[str], budget: int) -> str:
        source = str(chunk.get("text") or "")
        if len(source) <= budget:
            return source
        offset = 0
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
        body = source_excerpt(chunk, rule_evidence.get(chunk_id, []), body_budget)
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


def _combined_batch_results(component: str, output: object, rules: list[dict], payload: list[dict]) -> list[dict]:
    if component == "review":
        return _normalise_review_results(output, rules)
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
    return (
        "api.minimaxi.com" in str(profile.get("base_url") or "").lower()
        and str(profile.get("model_name") or "").lower() == "minimax-m3"
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


def _normalise_partial_combined_results(component: str, output: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict]]:
    returned_ids = {item.get("rule_id") for item in output if isinstance(item, dict)}
    present_rules = [item for item in rules if item["rule_id"] in returned_ids]
    missing_rules = [item for item in rules if item["rule_id"] not in returned_ids]
    payload = _combined_batch_payload(component, present_rules)
    return _combined_batch_results(component, output, present_rules, payload), missing_rules


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
        results, missing_rules = _normalise_partial_combined_results(component, parsed["results"], rules)
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
            storage.update_task(app, task["task_id"], message=f"{label} 返回 JSON 语法异常，正在仅修复响应")
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
        storage.update_task(app, task["task_id"], message=f"{label} 返回格式异常，正在严格 JSON 重试")
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
    return [rule for rule in rules if pattern.search(
        f"{rule.get('title', '')} {rule.get('check_rule', '')} {rule.get('source_text', '')}"
    )]


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


def _run_cross_bid_price_scoring(app, task: dict, profile: dict, documents: list[dict], rules: list[dict],
                                 score_run_id: str) -> dict:
    """在单文件评审后统一计算最低价/基准价，补足跨文件公式无法单独判断的问题。"""
    if len(documents) < 2 or not rules:
        return {"rule_count": 0, "result_count": 0, "retry_count": 0, "missing_count": 0}
    payload = _score_payload(rules)
    document_packet = _cross_bid_price_context(documents, rules)
    prompt = storage.render_prompt_template(
        app, "evaluate_all_cross_bid_price_user",
        rules=json.dumps(payload, ensure_ascii=False, separators=(",", ":")), documents=document_packet,
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
            storage.update_task(app, task["task_id"], message="跨投标人价格评分返回格式异常，正在仅修复响应")
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

    received: set[tuple[str, str]] = set()
    for raw in parsed["results"]:
        if not isinstance(raw, dict):
            continue
        key = (str(raw.get("document_id") or ""), str(raw.get("rule_id") or ""))
        if key not in expected:
            continue
        rule_payload = rules_by_id[key[1]]
        try:
            max_score = float(rule_payload.get("scoring", {}).get("max_score") or 0)
        except (TypeError, ValueError):
            max_score = 0.0
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
                       scan_units: int, groups_per_document: int, progress: _EvaluationProgress) -> dict:
    """处理一份投标文件；不同投标人可并行，单份文件内仍严格顺序执行。"""
    bidder_name = document["bidder_name"] or document["original_name"]
    progress.message(f"正在综合评审：{bidder_name}")
    reusable = None if task.get("payload", {}).get("force_rerun") else storage.reusable_evaluation_document_results(
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
        progress.advance(f"已复用 {bidder_name} 的完整评审结果", scan_units + groups_per_document)
        progress.document_completed(document, reused=True)
        return {"reused_document_count": 1}

    scan_index = _scan_document_fulltext(
        app, task, profile, document, review_rules + objective_rules + subjective_rules, project_scope, system_prompt,
        progress_callback=progress.advance,
    )
    values = {
        "reused_document_count": 0,
        "full_scan_document_count": 1 if scan_index else 0,
        "full_scan_batch_count": scan_index.get("scan_batch_count", 0) if scan_index else 0,
        "full_scan_failed_chunk_count": len(scan_index.get("failed_chunks", [])) if scan_index else 0,
        "compact_retry_count": scan_index.get("compact_retry_count", 0) if scan_index else 0,
        "split_retry_count": scan_index.get("split_retry_count", 0) if scan_index else 0,
        "manual_fallback_rule_count": 0,
        "batch_count": 0,
    }
    components = (("review", review_rules, review_run), ("objective", objective_rules, objective_run), ("subjective", subjective_rules, subjective_run))
    for component, component_rules, run in components:
        # 长文件已有全文扫描索引时，按重合证据页重组规则，减少不同组重复携带同一页。
        groups = _evaluation_rule_batches(component, component_rules, scan_index=scan_index)
        for group_index, group in enumerate(groups, start=1):
            label = f"{bidder_name}·{component} 第{group_index}组"
            progress.message(f"正在综合评审：{label}")
            results, compact_count, split_count, fallback_count, _ = _run_combined_batch(
                app, task, profile, document, component, group, system_prompt, char_limit, label, scan_index=scan_index,
            )
            # 每个规则组成功后立即持久化；只有完成该投标人的全部规则后才对页面公开展示。
            if component == "review" and run:
                storage.save_review_results(app, run["review_run_id"], document["document_id"], results)
            elif run:
                storage.save_score_results(app, run["score_run_id"], document["document_id"], results)
            values["compact_retry_count"] += compact_count
            values["split_retry_count"] += split_count
            values["manual_fallback_rule_count"] += fallback_count
            values["batch_count"] += 1
            progress.advance(f"已完成综合评审：{label}")
    progress.document_completed(document)
    return values


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
    compact_retry_count = split_retry_count = 0
    manual_fallback_rule_count = 0
    reused_document_count = 0
    batch_count = 0
    full_scan_document_count = full_scan_batch_count = full_scan_failed_chunk_count = 0
    groups_per_document = sum(
        len(_evaluation_rule_batches(component, component_rules))
        for component, component_rules in (("review", review_rules), ("objective", local_objective_rules), ("subjective", subjective_rules))
    )
    has_local_rules = bool(review_rules or local_objective_rules or subjective_rules)
    scan_units_by_document = {
        item["document_id"]: _full_scan_chunk_count(item) if has_local_rules else 0
        for item in documents
    }
    cross_bid_units = 1 if objective_run and len(documents) >= 2 and cross_bid_price_rules else 0
    total_work_units = max(1, sum(scan_units_by_document.values()) + len(documents) * groups_per_document + cross_bid_units)
    # 首次仍以两路保守启动；连续成功后才让第三家投标文件进入第三条模型通道。
    # 对 2 核 2GB 服务器而言这主要增加网络等待并行，不常驻加载额外模型。
    task["_evaluation_request_gate"] = _EvaluationRequestGate(
        2 if len(documents) > 1 else 1,
        max_limit=min(3, len(documents)),
    )
    progress = _EvaluationProgress(app, task, total_work_units, len(documents))

    def run_document(document: dict) -> dict:
        return _evaluate_document(
            app, task, document, rule_set=rule_set, profile=profile, char_limit=char_limit,
            expected_rule_ids=expected_rule_ids, review_rules=review_rules, objective_rules=local_objective_rules,
            subjective_rules=subjective_rules, review_run=review_run, objective_run=objective_run,
            subjective_run=subjective_run, project_scope=project_scope, system_prompt=system_prompt,
            scan_units=scan_units_by_document[document["document_id"]], groups_per_document=groups_per_document,
            progress=progress,
        )

    # 只有投标人之间的文件审查并行；单份文件仍保持页块、规则组的先后顺序。
    # 模型请求默认两路、稳定后动态至多三路，触发服务商限流后会自动逐级降路重试。
    document_results: list[dict] = []
    if len(documents) == 1:
        document_results.append(run_document(documents[0]))
    else:
        with ThreadPoolExecutor(max_workers=min(3, len(documents)), thread_name_prefix="evaluation-bid") as executor:
            futures = [executor.submit(run_document, document) for document in documents]
            for future in as_completed(futures):
                document_results.append(future.result())
    for value in document_results:
        reused_document_count += value.get("reused_document_count", 0)
        compact_retry_count += value.get("compact_retry_count", 0)
        split_retry_count += value.get("split_retry_count", 0)
        manual_fallback_rule_count += value.get("manual_fallback_rule_count", 0)
        batch_count += value.get("batch_count", 0)
        full_scan_document_count += value.get("full_scan_document_count", 0)
        full_scan_batch_count += value.get("full_scan_batch_count", 0)
        full_scan_failed_chunk_count += value.get("full_scan_failed_chunk_count", 0)
    cross_bid_price = {"rule_count": 0, "result_count": 0, "retry_count": 0, "missing_count": 0}
    if cross_bid_units and objective_run:
        progress.message("正在统一比较全部投标人的报价并计算价格分")
        cross_bid_price = _run_cross_bid_price_scoring(
            app, task, profile, documents, cross_bid_price_rules, objective_run["score_run_id"],
        )
        progress.advance("已完成全部投标人的报价比较与价格评分")
    recovery = storage.task_recovery_summary(app, task["task_id"])
    return {"review_run_id": review_run["review_run_id"] if review_run else None, "objective_run_id": objective_run["score_run_id"] if objective_run else None,
            "subjective_run_id": subjective_run["score_run_id"] if subjective_run else None, "document_count": len(documents),
            "reused_document_count": reused_document_count, "model_document_count": len(documents) - reused_document_count,
            "rule_count": len(all_rules), "profile": profile["display_name"],
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
            "cross_bid_price": cross_bid_price,
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
