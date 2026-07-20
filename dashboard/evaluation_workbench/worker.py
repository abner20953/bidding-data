"""按需启动的评标工作台任务进程。"""

from __future__ import annotations

import itertools
import hashlib
import json
import os
import re
import sys
import traceback
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import fitz

from dashboard.evaluation_workbench import storage
from dashboard.evaluation_workbench.ai_gateway import InvalidJsonResponse, request_json
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.prompt_context import build_rule_context, select_rule_chunks, split_full_text_chunks
from dashboard.blueprints.evaluation_workbench import create_worker_app
from dashboard.utils.comparator import CollusionDetector, ComparisonLimitError


MAX_PARSE_PAGES = 2000
MAX_PARSED_CHARS = 2_000_000
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024
PROMPT_VERSION = "fulltext-coverage-v3"
COMPARE_AI_PROMPT_VERSION = "compare-evidence-ai-v2"
COMPARE_AI_BATCH_SIZE = 24


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

    try:
        effective_profile = {**profile, "thinking_mode": thinking_mode} if thinking_mode else profile
        return request_json(
            effective_profile, system_prompt, user_prompt, usage_callback=record_usage,
            response_metadata_callback=record_response_metadata, max_tokens=max_tokens,
        )
    finally:
        # 部分兼容接口不返回 usage；仍保留发送字符数与截断元数据以便统计和优化。
        storage.record_model_call(
            app, task["task_id"], task["project_id"], phase, profile.get("profile_id"),
            document_id=document_id, input_chars=len(system_prompt) + len(user_prompt),
            context_mode=context_mode, usage=usage, response_metadata=response_metadata,
        )


def _system_prompt(app, template_id: str) -> str:
    return storage.render_prompt_template(app, template_id)


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
    completed, failures = 0, []
    system_prompt = _system_prompt(app, "compare_ai_assessment")
    for start in range(0, len(signals), COMPARE_AI_BATCH_SIZE):
        batch = signals[start:start + COMPARE_AI_BATCH_SIZE]
        packets = [_compare_evidence_packet(item) for item in batch]
        user_prompt = storage.render_prompt_template(app, "compare_ai_assessment_user", packets=json.dumps(packets, ensure_ascii=False, separators=(",", ":")))
        try:
            parsed = _request_task_json(app, task, profile, "compare_ai_assessment", system_prompt, user_prompt,
                                        context_mode="evidence_batch",
                                        max_tokens=_output_token_budget(profile, 700 + len(batch) * 120))
            values = parsed.get("assessments") if isinstance(parsed, dict) else []
            for value in values if isinstance(values, list) else []:
                if not isinstance(value, dict) or value.get("signal_id") not in by_id:
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
                completed += 1
        except Exception as exc:  # 保留确定性查重结果，不能因 AI 暂不可用而丢失证据。
            message = str(exc)[:180]
            failures.append(message)
            if "鉴权失败" in message or "尚未配置 API Key" in message or "HTTP 4" in message:
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
        "status": "partial" if failures else "success", "assessed_count": completed, "signal_count": len(signals),
        "failure_count": len(failures), "reason": "；".join(failures), "profile": profile["display_name"],
        "prompt_version": COMPARE_AI_PROMPT_VERSION, "input_mode": "fixed_rule_evidence_packets_only",
    }


_RULE_SECTION_MARKERS = ("评标办法", "评分标准", "资格审查", "符合性审查", "废标", "无效投标", "否决投标", "资格条件")
_SCORE_CLAUSE_PATTERN = re.compile(r"(?:得\s*\d+(?:\.\d+)?\s*分|最高(?:得)?\s*\d+(?:\.\d+)?\s*分|满分(?:为)?\s*\d+(?:\.\d+)?\s*分)")
_SCORE_COVERAGE_IGNORED_TERMS = {"项目", "评分", "标准", "要求", "供应", "服务", "能力", "部分", "内容", "提供", "文件", "采购", "投标", "技术", "商务"}


def _rule_source_excerpt(text: str, budget: int) -> str:
    """优先保留评标相关章节，避免长招标文件的无关前缀挤掉评分附件。"""
    if len(text) <= budget:
        return text
    windows: list[tuple[int, int]] = []
    for marker in _RULE_SECTION_MARKERS:
        start = 0
        while len(windows) < 12:
            found = text.find(marker, start)
            if found < 0:
                break
            windows.append((max(0, found - 1_200), min(len(text), found + 8_000)))
            start = found + len(marker)
    if not windows:
        return text[:budget]
    windows.sort()
    merged: list[tuple[int, int]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    selected = "\n\n".join(text[start:end] for start, end in merged)
    return selected[:budget] if selected else text[:budget]


def _score_clause_packets(text: str, limit: int = 24) -> list[str]:
    """从评分表行中构造短片段，作为 AI 规则提取的覆盖清单，而非本地直接判分。"""
    lines = [line.strip() for line in text.splitlines()]
    windows: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if not _SCORE_CLAUSE_PATTERN.search(re.sub(r"\s+", "", line)):
            continue
        # 评分表的项目名称通常在得分行之前；不带后续行，避免把下一条评分项误判为当前条款已覆盖。
        start, end = max(0, index - 6), min(len(lines), index + 1)
        if windows and start <= windows[-1][1] + 2:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
        if len(windows) >= limit:
            break
    packets = []
    for start, end in windows:
        value = "\n".join(line for line in lines[start:end] if line)[:900]
        if value:
            packets.append(value)
    return packets


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


def _score_packet_is_covered(packet: str, score_rules: list[dict]) -> bool:
    """仅在评分项目名称有明确交集时视为已覆盖，不能用评分规则总数代替逐条核验。"""
    compact_packet = re.sub(r"\s+", "", packet)
    return any(
        any(term in compact_packet for term in _score_rule_title_terms(rule))
        for rule in score_rules
    )


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
            for start in range(0, len(part), max_chars):
                piece = part[start:start + max_chars].strip()
                if piece:
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


def _rule_extraction_prompt(app, text: str, *, compact: bool, score_packets: list[str], max_rules: int = 45) -> str:
    limits = (
        f"这是格式异常后的紧凑重试。最多返回 {max_rules} 条规则；title 最多 30 字，check_rule 最多 90 字，source_text 最多 120 字。"
        if compact else
        f"最多返回 {max_rules} 条规则；title 最多 40 字，check_rule 最多 140 字，source_text 最多 220 字。"
    )
    score_audit = "\n".join(f"【评分条款 {index}】\n{packet}" for index, packet in enumerate(score_packets, start=1))
    score_requirement = (
        "本地已定位以下疑似评分条款。必须逐项核验并为每个不同的明确计分条款输出一条 objective 或 subjective 规则；"
        "不得遗漏业绩、报价、人员、资质、方案等评分项。"
        if score_audit else "未定位到明确评分条款时，不要臆造评分规则。"
    )
    return storage.render_prompt_template(app, "extract_rules_user", limits=limits, score_requirement=score_requirement,
                                          score_audit=score_audit or "无", text=text)


def _score_rule_supplement_prompt(app, score_packets: list[str], existing_rules: list[dict]) -> str:
    existing = [
        {"category": item.get("category"), "title": item.get("title"), "check_rule": item.get("check_rule"), "max_score": (item.get("scoring") or {}).get("max_score")}
        for item in existing_rules if item.get("category") in {"objective", "subjective"}
    ]
    packet_text = "\n".join(f"【评分条款 {index}】\n{packet}" for index, packet in enumerate(score_packets, start=1))
    return storage.render_prompt_template(app, "extract_rules_supplement_user",
                                          existing_rules=json.dumps(existing, ensure_ascii=False, separators=(",", ":")), packet_text=packet_text)


def _rule_batch_output_tokens(text: str, compact: bool = False) -> int:
    """小批次按内容量分配输出；紧凑重试绝不降低输出上限。"""
    target = max(2_500, min(6_000, 1_400 + len(text) // 3))
    return max(target, 3_500) if compact else target


def _extract_rule_batch(app, task: dict, profile: dict, system_prompt: str, text: str,
                        *, document_id: str, batch_label: str, depth: int = 0) -> tuple[list[dict], int, int]:
    """提取一个小批次；截断时只二分当前批次，最小批次才紧凑重试。"""
    packets = _score_clause_packets(text)
    max_rules = 16 if depth == 0 else 10
    user_prompt = _rule_extraction_prompt(app, text, compact=False, score_packets=packets, max_rules=max_rules)
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
                        batch_label=f"{batch_label}/拆分{index}", depth=depth + 1,
                    )
                    rules.extend(value)
                    compact_retries += compact_count
                    split_retries += split_count
                return rules, compact_retries, split_retries + 1
        storage.update_task(app, task["task_id"], message=f"{batch_label} 格式异常，正在以紧凑 JSON 重试")
        retry_prompt = _rule_extraction_prompt(app, text, compact=True, score_packets=packets, max_rules=max(8, max_rules))
        parsed = _request_task_json(
            app, task, profile, "extract_rules_compact_retry", system_prompt, retry_prompt,
            document_id=document_id, context_mode=f"{batch_label}_compact_retry",
            max_tokens=_output_token_budget(profile, _rule_batch_output_tokens(text, compact=True)), thinking_mode="disabled",
        )
        rules = parsed.get("rules") if isinstance(parsed, dict) else None
        if not isinstance(rules, list):
            raise ValueError("模型返回格式不符合规则提取要求")
        return [item for item in rules if isinstance(item, dict)], 1, 0


def _rule_signature(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("category", "")).strip(),
        re.sub(r"\s+", "", str(item.get("title", ""))).casefold(),
        re.sub(r"\s+", "", str(item.get("check_rule", "") or item.get("title", ""))).casefold(),
    )


def _dedupe_rule_candidates(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result = []
    for item in items:
        signature = _rule_signature(item)
        if not all(signature) or signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


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
    if len(source_documents) == 1:
        source_parts = [f"【{source_documents[0][0]}】\n{_rule_source_excerpt(source_documents[0][1], char_limit)}"]
    else:
        attachment_count = len(source_documents) - 1
        minimum_attachment_budget = min(12_000, max(2_000, char_limit // (attachment_count + 2)))
        main_budget = max(minimum_attachment_budget, min(int(char_limit * 0.6), char_limit - attachment_count * minimum_attachment_budget))
        attachment_budget = max(1_000, int((char_limit - main_budget) / attachment_count))
        source_parts = [
            f"【{label}】\n{_rule_source_excerpt(value, main_budget if index == 0 else attachment_budget)}"
            for index, (label, value) in enumerate(source_documents)
        ]
    text = "\n\n".join(source_parts)[:char_limit]
    score_packets = _score_clause_packets(text)
    batches = _split_rule_extraction_text(text, RULE_EXTRACTION_BATCH_CHARS)
    if not batches:
        raise ValueError("招标文件未提取到可供规则识别的正文")
    storage.update_task(app, task["task_id"], progress=15, message=f"正在分段提取评审规则（共 {len(batches)} 批）")
    system_prompt = _system_prompt(app, "extract_rules")
    raw_rules: list[dict] = []
    compact_retry_count = split_retry_count = 0
    for index, batch in enumerate(batches, start=1):
        progress = 15 + int((index - 1) * 45 / len(batches))
        storage.update_task(app, task["task_id"], progress=progress, message=f"正在提取规则第 {index}/{len(batches)} 批")
        extracted, compact_count, split_count = _extract_rule_batch(
            app, task, profile, system_prompt, batch, document_id=tender["document_id"],
            batch_label=f"rule_batch_{index}_of_{len(batches)}",
        )
        raw_rules.extend(extracted)
        compact_retry_count += compact_count
        split_retry_count += split_count
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
            packet_batch = uncovered_score_packets[index:index + 6]
            try:
                supplement = _request_task_json(
                    app, task, profile, "extract_rules_scoring_supplement", system_prompt,
                    _score_rule_supplement_prompt(app, packet_batch, primary_score_rules), document_id=tender["document_id"],
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
    candidates = _dedupe_rule_candidates([item for item in raw_rules if isinstance(item, dict) and str(item.get("title", "")).strip() and item.get("category") in {"qualification", "compliance", "substantive", "rejection", "objective", "subjective"}])
    # 是否可由投标文件核验交给完整提示词与人工确认判断；不以词表硬过滤，避免误删业绩有效期等规则。
    rules = candidates
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
                scoring["kind"] = "boolean" if scoring.get("kind") == "boolean" else "manual"
            else:
                scoring["kind"] = "manual"
            item["scoring"] = scoring
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
            "split_retry_count": split_retry_count}


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
                    "ocr_required": item.get("check_mode") == "ocr"} for item in rules]
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
        by_id = {item["rule_id"]: item for item in rules}
        normalized = []
        for item in output:
            rule_id = item.get("rule_id") if isinstance(item, dict) else None
            if rule_id not in by_id:
                continue
            status = item.get("status")
            if status not in {"satisfied", "not_satisfied", "partial", "not_found", "manual", "ocr_required"}:
                status = "manual"
            normalized.append(_review_result_from_model(item, rule_id, status))
        returned_ids = {item["rule_id"] for item in normalized}
        normalized.extend(_review_result_from_model({"reason": "模型未返回该规则的可验证结论，请人工复核。"}, rule["rule_id"], "manual") for rule in rules if rule["rule_id"] not in returned_ids)
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
                             "ocr_required": rule.get("check_mode") == "ocr", "scoring": scoring})
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
        normalized.append(_review_result_from_model(item, rule_id, status))
    returned_ids = {item["rule_id"] for item in normalized}
    normalized.extend(_review_result_from_model({"reason": "模型未返回该规则的可验证结论，请人工复核。"}, rule["rule_id"], "manual")
                      for rule in rules if rule["rule_id"] not in returned_ids)
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
        payload.append({"rule_id": rule["rule_id"], "title": rule["title"], "check_rule": rule.get("check_rule") or rule["title"], "source_text": rule["source_text"],
                        "ocr_required": rule.get("check_mode") == "ocr", "scoring": scoring})
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
        results.append(_score_result_from_model(item["rule_id"], suggested, max_score, raw))
    return results


def _score_result_from_model(rule_id: str, suggested: float | None, max_score: float, raw: dict) -> dict:
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


def _combined_batch_output_budget(component: str, rule_count: int) -> int:
    """为思考模型留足结构化输出空间，同时避免客观分不必要地放大输出。"""
    count = max(1, rule_count)
    if component == "review":
        return max(4_000, 1_600 + count * 650)
    if component == "subjective":
        return max(4_500, 1_800 + count * 700)
    return max(2_000, 800 + count * 300)


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
                 "ocr_required": item.get("check_mode") == "ocr"} for item in rules]
    return _score_payload(rules)


def _full_scan_catalog(rules: list[dict]) -> list[dict]:
    """生成首轮扫描专用的精简规则目录，详细评分规则留给最终汇总阶段。"""
    catalog = []
    for rule in rules:
        query = re.sub(r"\s+", " ", f"{rule.get('title') or ''}；{rule.get('check_rule') or rule.get('title') or ''}").strip()
        item = {
            "id": rule["rule_id"],
            # 保留旧字段，避免用户在提示词配置中保留了旧版 findings 模板时无法对应规则。
            "rule_id": rule["rule_id"],
            "q": query[:FULL_SCAN_CATALOG_RULE_CHARS],
            "type": rule["category"],
        }
        if rule.get("check_mode") == "ocr":
            item["ocr"] = 1
        # 对业绩等数量/累计评分项保留极短的计分线索，避免首轮遗漏每一项材料；
        # 不在此阶段给分或做有效性裁断。
        if rule["category"] == "objective":
            try:
                scoring = json.loads(rule.get("scoring_json") or "{}")
            except json.JSONDecodeError:
                scoring = {}
            if scoring:
                item["score_hint"] = json.dumps(scoring, ensure_ascii=False, separators=(",", ":"))[:160]
        catalog.append(item)
    return catalog


def _full_scan_chunk_label(chunk: dict) -> str:
    start_page, end_page = chunk.get("start_page"), chunk.get("end_page")
    if start_page and end_page:
        return f"第{start_page}-{end_page}页" if start_page != end_page else f"第{start_page}页"
    return str(chunk.get("chunk_id") or "连续文本块")


def _full_scan_prompt(app, document: dict, catalog: list[dict], chunk: dict, *, compact: bool) -> str:
    retry_note = (
        "这是格式异常后的严格 JSON 重试：只输出一个 JSON 对象；matches 最多 16 条，每段摘录最多 90 字；"
        "不得使用 Markdown、注释或前后说明。\n"
        if compact else ""
    )
    return storage.render_prompt_template(
        app, "evaluate_all_full_scan_user", retry_note=retry_note,
        rules=json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写",
        chunk_label=_full_scan_chunk_label(chunk), text=chunk["text"],
    )


def _normalise_scan_findings(output: object, allowed_ids: set[str], chunk: dict) -> list[dict]:
    """兼容新版紧凑数组及用户遗留模板的 findings 对象。"""
    findings = []
    for raw in output if isinstance(output, list) else []:
        if isinstance(raw, list) and len(raw) >= 4:
            rule_id, page_hint, evidence, status = raw[:4]
            observation, needs_ocr, confidence = "", False, "medium"
        elif isinstance(raw, dict):
            rule_id = raw.get("rule_id") or raw.get("id")
            page_hint = raw.get("page_hint") or raw.get("page")
            evidence = raw.get("evidence") or raw.get("quote")
            status = raw.get("tentative_status") or raw.get("polarity") or raw.get("status")
            observation, needs_ocr = raw.get("observation"), raw.get("needs_ocr") is True
            confidence = raw.get("confidence")
        else:
            continue
        if rule_id not in allowed_ids:
            continue
        if status not in {"supports", "contradicts", "partial", "suspected"}:
            status = {"support": "supports", "contradict": "contradicts", "suspect": "suspected"}.get(str(status).lower(), "suspected")
        confidence = confidence if confidence in {"high", "medium", "low"} else "medium"
        findings.append({
            "rule_id": rule_id,
            "chunk_id": chunk["chunk_id"],
            "page_range": _full_scan_chunk_label(chunk),
            "page_hint": _clean_model_text(page_hint)[:80],
            "evidence": _clean_model_text(evidence)[:360],
            "observation": _clean_model_text(observation)[:240],
            "tentative_status": status,
            "matched_count": None,
            "suggested_score": None,
            "needs_ocr": needs_ocr,
            "confidence": confidence,
        })
    return findings


REGION_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,12}?(?:特别行政区|自治区|自治州|地区|省|市|盟|区|县|旗)")
REGION_LEADING_NOISE = (
    "以及", "并按", "按照", "根据", "符合", "执行", "遵守", "涉及", "位于", "地址", "地点",
    "要求", "项目", "采购", "招标", "投标", "服务", "作业", "本地", "当地", "国家法规",
)
REGION_GENERIC_VALUES = {
    "项目区", "服务区", "作业区", "办公区", "生活区", "管理区", "行政区", "城区", "市区", "辖区",
}


def _clean_region_value(value: str) -> str:
    value = value.strip("，。；：、（）()【】[] ")
    changed = True
    while changed:
        changed = False
        for prefix in REGION_LEADING_NOISE:
            if value.startswith(prefix) and len(value) - len(prefix) >= 3:
                value, changed = value[len(prefix):], True
                break
    return value


def _region_mentions(line: str) -> list[str]:
    """提取轻量行政区名称；只用于召回候选，不据此作业务结论。"""
    values = []
    for match in REGION_PATTERN.finditer(line):
        value = _clean_region_value(match.group(0))
        if value in REGION_GENERIC_VALUES or not 3 <= len(value) <= 10:
            continue
        if value not in values:
            values.append(value)
    return values


def _iter_chunk_lines(chunks: list[dict]):
    page_marker = re.compile(r"\[第(\d+)页\]")
    for chunk in chunks:
        current_page = ""
        for raw_line in str(chunk.get("text") or "").splitlines():
            marker = page_marker.search(raw_line)
            if marker:
                current_page = marker.group(1)
                continue
            line = re.sub(r"\s+", "", raw_line.strip())
            if len(line) >= 3:
                yield str(chunk.get("chunk_id") or ""), current_page, line


def _local_entity_candidates(chunks: list[dict], limit: int = 300) -> list[dict]:
    """本地全文实体校对层；按类型独立限额，避免后半册地域被前页实体挤掉。"""
    patterns = (
        ("公司名称", re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,50}(?:有限责任公司|股份有限公司|有限公司|公司)")),
        ("项目名称", re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·\-]{4,70}(?:项目|工程)")),
    )
    per_kind_limit = max(40, limit // 3)
    buckets: dict[str, list[dict]] = {"公司名称": [], "项目名称": [], "地区名称": []}
    seen: dict[str, set[str]] = {key: set() for key in buckets}
    for chunk_id, current_page, line in _iter_chunk_lines(chunks):
        for kind, pattern in patterns:
            if len(buckets[kind]) >= per_kind_limit:
                continue
            for match in pattern.finditer(line):
                value = match.group(0)[-80:]
                if value in seen[kind]:
                    continue
                seen[kind].add(value)
                buckets[kind].append({"kind": kind, "value": value, "page_hint": current_page,
                                      "chunk_id": chunk_id, "context": line[:220]})
                if len(buckets[kind]) >= per_kind_limit:
                    break
        for value in _region_mentions(line):
            kind = "地区名称"
            if value in seen[kind]:
                continue
            seen[kind].add(value)
            if len(buckets[kind]) < max(600, per_kind_limit * 4):
                buckets[kind].append({"kind": kind, "value": value, "page_hint": current_page,
                                      "chunk_id": chunk_id, "context": line[:220]})
    # 轮转输出保证地区、公司和项目三类都能进入有限上下文；高风险异地政策另由
    # geography_candidates 独立保留，不会因这里的均衡限额丢失。
    ordered: list[dict] = []
    kinds = ("地区名称", "公司名称", "项目名称")
    for index in range(max(len(buckets[kind]) for kind in kinds)):
        for kind in kinds:
            if index < len(buckets[kind]):
                ordered.append(buckets[kind][index])
                if len(ordered) >= limit:
                    return ordered
    return ordered


def _reference_regions(documents: list[dict]) -> list[str]:
    """从招标文件及附件提取项目合法地域；不依赖常驻模型或外部地名库。"""
    counts: dict[str, int] = {}
    first_seen: list[str] = []
    early_regions: set[str] = set()
    for document in documents:
        if document.get("role") not in {"tender", "tender_attachment"} or not document.get("parsed_path"):
            continue
        path = Path(document["parsed_path"])
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        char_offset = 0
        for line in text.splitlines():
            for value in _region_mentions(line):
                if char_offset < 30_000:
                    early_regions.add(value)
                counts[value] = counts.get(value, 0) + 1
                if value not in first_seen:
                    first_seen.append(value)
            char_offset += len(line) + 1
    # 封面/项目概况中的地域和正文反复出现的地域作为基准；招标文件中偶然出现的
    # 业绩示例或联系地址不应把异地复制线索“洗白”。
    values = [item for item in first_seen if item in early_regions or counts[item] >= 2]
    return sorted(values, key=lambda item: (-counts[item], first_seen.index(item)))[:40]


def _geography_consistency_candidates(chunks: list[dict], reference_regions: list[str], limit: int = 80) -> list[dict]:
    """召回异地政策/项目残留；地址和业绩场景降权，最终仍由 AI 结合原页判断。"""
    references = set(reference_regions)
    strong_terms = ("本地", "当地", "管控要求", "飞行管控", "作业区域", "作业区", "实施地点", "服务地点", "项目所在地")
    exclusion_terms = ("注册地址", "住所", "住址", "通讯地址", "类似项目", "业绩", "合同名称", "项目名称", "发包人")
    by_value: dict[str, dict] = {}
    for chunk_id, page_hint, line in _iter_chunk_lines(chunks):
        for value in _region_mentions(line):
            if value in references:
                continue
            signal_score = sum(1 for term in strong_terms if term in line)
            strong = signal_score > 0
            excluded = any(term in line for term in exclusion_terms)
            priority = 3 if strong and not excluded else 2 if not excluded else 1
            candidate = {
                "kind": "异地行政区候选", "value": value, "page_hint": page_hint,
                "chunk_id": chunk_id, "context": line[:260],
                "candidate_priority": "high" if priority == 3 else "medium" if priority == 2 else "low",
                "likely_address_or_performance": excluded,
                "signal_score": signal_score,
            }
            previous = by_value.get(value)
            previous_rank = (
                {"high": 3, "medium": 2, "low": 1}.get(previous.get("candidate_priority"), 0),
                int(previous.get("signal_score") or 0),
            ) if previous else (0, 0)
            if (priority, signal_score) > previous_rank:
                by_value[value] = candidate
    candidates = list(by_value.values())
    candidates.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}[item["candidate_priority"]],
                                      -int(item.get("signal_score") or 0),
                                      int(item["page_hint"]) if str(item["page_hint"]).isdigit() else 10**9))
    return candidates[:limit]


def _run_full_scan_piece(app, task: dict, profile: dict, document: dict, catalog: list[dict], chunk: dict,
                          system_prompt: str, depth: int = 0) -> tuple[list[dict], int, int, list[dict]]:
    """扫描一个连续页块；只在输出异常时拆分规则目录，绝不递归重发全文。"""
    allowed_ids = {item["id"] for item in catalog}
    # 首轮只是候选索引而非最终结论；固定且较小的输出预算能抑制模型逐条长篇解释。
    max_tokens = _output_token_budget(profile, min(2_800, max(1_400, 800 + len(catalog) * 28)))

    def findings_from(parsed: object) -> list[dict]:
        values = parsed.get("matches") if isinstance(parsed, dict) else None
        if values is None and isinstance(parsed, dict):
            values = parsed.get("findings")  # 兼容用户尚未重置的旧自定义模板。
        if not isinstance(values, list):
            raise ValueError("模型返回格式不符合全文扫描要求")
        return _normalise_scan_findings(values, allowed_ids, chunk)

    format_error: ValueError | None = None
    try:
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_full_scan", system_prompt,
            _full_scan_prompt(app, document, catalog, chunk, compact=False),
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
        left = _run_full_scan_piece(app, task, profile, document, catalog[:midpoint], chunk, system_prompt, depth + 1)
        right = _run_full_scan_piece(app, task, profile, document, catalog[midpoint:], chunk, system_prompt, depth + 1)
        return left[0] + right[0], left[1] + right[1], left[2] + right[2] + 1, left[3] + right[3]

    storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} {_full_scan_chunk_label(chunk)} 全文扫描格式异常，正在严格重试")
    try:
        parsed = _request_task_json(
            app, task, profile, "evaluate_all_full_scan_compact_retry", system_prompt,
            _full_scan_prompt(app, document, catalog, chunk, compact=True),
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
            left = _run_full_scan_piece(app, task, profile, document, catalog[:midpoint], chunk, system_prompt, depth + 1)
            right = _run_full_scan_piece(app, task, profile, document, catalog[midpoint:], chunk, system_prompt, depth + 1)
            return left[0] + right[0], left[1] + right[1] + 1, left[2] + right[2] + 1, left[3] + right[3]
        # 不能再拆分时不静默丢页：最终复核会把此页块原文发送给相关规则组。
        return [], 1, 0, [{**chunk, "scan_error": str(retry_exc)[:300]}]


def _scan_document_fulltext(app, task: dict, profile: dict, document: dict, rules: list[dict],
                            system_prompt: str, *, reference_regions: list[str] | None = None,
                            progress_offset: int = 0, progress_total: int = 1) -> dict | None:
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
        "version": PROMPT_VERSION, "profile": profile.get("profile_id"), "model": profile.get("model_name"),
        "catalog": catalog, "template": storage.prompt_template(app, "evaluate_all_full_scan_user"),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    findings: list[dict] = []
    failed_chunks: list[dict] = []
    compact_retry_count = split_retry_count = 0
    total = max(1, len(chunks))
    completed = 0
    for chunk in chunks:
        completed += 1
        chunk_hash = hashlib.sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest()
        progress = int((progress_offset + completed - 1) * 100 / max(1, progress_total))
        storage.update_task(
            app, task["task_id"], progress=progress,
            message=f"正在全文证据扫描 {document['bidder_name'] or document['original_name']}：{_full_scan_chunk_label(chunk)}（{completed}/{total}）",
        )
        checkpoint = storage.get_evaluation_scan_checkpoint(app, document["document_id"], scan_key, chunk["chunk_id"], chunk_hash)
        if checkpoint is not None:
            findings.extend(checkpoint)
            continue
        result = _run_full_scan_piece(app, task, profile, document, catalog, chunk, system_prompt)
        findings.extend(result[0])
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
        "entity_candidates": _local_entity_candidates(chunks),
        "reference_regions": list(reference_regions or []),
        "geography_candidates": _geography_consistency_candidates(chunks, list(reference_regions or [])),
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
    categories = {item.get("category") for item in rules}
    raw = " ".join(f"{item.get('title', '')} {item.get('check_rule', '')}" for item in rules)
    if "objective" in categories or any(term in raw for term in ("业绩", "数量", "累计", "每个", "项目数", "得分")):
        return "counting"
    if "subjective" in categories or any(term in raw for term in ("技术方案", "实施方案", "服务方案", "组织方案")):
        return "section"
    if any(term in raw for term in ("公司名称", "项目名称", "前后", "一致", "无关公司", "无关项目", "全文")):
        return "consistency"
    return "point"


def _full_scan_review_context(scan: dict, rules: list[dict], char_limit: int) -> dict:
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
    # 首轮 AI 候选优先，其次用本地章节词加强召回；失败页块始终进入复核。
    selected_ids: list[str] = []
    for finding in findings:
        chunk_id = str(finding.get("chunk_id") or "")
        root_id = chunk_id.split(".", 1)[0]
        if root_id and root_id not in selected_ids:
            selected_ids.append(root_id)
    per_rule = 6 if strategy in {"counting", "section"} else 4
    for chunk_id in select_rule_chunks(scan.get("chunks", []), rules, per_rule=per_rule):
        if chunk_id not in selected_ids:
            selected_ids.append(chunk_id)
    rule_text = " ".join(f"{item.get('title', '')} {item.get('check_rule', '')} {item.get('source_text', '')}" for item in rules)
    consistency_terms = ("公司名称", "项目名称", "地名", "地区", "无关", "矛盾", "技术方案", "项目一致", "全文")
    if any(term in rule_text for term in consistency_terms):
        # 高优先级异地政策候选对应原页必须进入最终判断，不能只给模型看正则摘录。
        geo_ids = [str(item.get("chunk_id") or "").split(".", 1)[0]
                   for item in scan.get("geography_candidates", [])
                   if item.get("candidate_priority") == "high"]
        for chunk_id in reversed([item for item in geo_ids if item]):
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
    findings_limit = 240 if strategy == "counting" else 160
    findings_packet = json.dumps(findings[:findings_limit], ensure_ascii=False, separators=(",", ":"))
    entity_packet = ""
    if any(term in rule_text for term in consistency_terms):
        geography = scan.get("geography_candidates", [])
        entity_packet = (
            "\n\n【项目地域基准（来自招标文件，仅用于一致性比对）】\n"
            + json.dumps(scan.get("reference_regions", []), ensure_ascii=False, separators=(",", ":"))
            + "\n\n【异地行政区/当地政策一致性候选（须结合原页判断，地址与业绩已降权）】\n"
            + json.dumps(geography[:80], ensure_ascii=False, separators=(",", ":"))
            + "\n\n【本地全文公司/项目/地区实体校对候选】\n"
            + json.dumps(scan.get("entity_candidates", [])[:180], ensure_ascii=False, separators=(",", ":"))
        )
    header = (
        f"【全文覆盖说明】已按连续页块扫描全文，共 {scan.get('chunk_count', 0)} 个页块；本规则组采用{strategy}汇总策略；"
        f"首轮为当前规则组报告 {len(findings)} 条候选证据。"
        "首轮未报告候选不等于技术失败，应结合规则给出‘全文扫描未发现’或其他最可能建议。"
    )
    if failed_root_ids:
        header += f"有 {len(failed_root_ids)} 个首轮格式异常页块，下面已附原文供本轮直接复核。"
    prefix = f"{header}{entity_packet}\n\n【首轮 AI 候选证据】\n{findings_packet}\n\n【重点原文】\n"
    if len(prefix) >= char_limit:
        prefix = prefix[:char_limit]
    chunks_by_id = {str(item.get("chunk_id")): item for item in scan.get("chunks", [])}
    parts = [prefix]
    size = len(prefix)
    included = []
    for chunk_id in selected_ids:
        chunk = chunks_by_id.get(chunk_id)
        if not chunk:
            continue
        piece = f"\n\n【{_full_scan_chunk_label(chunk)}】\n{chunk.get('text', '')}"
        remaining = char_limit - size
        if remaining <= 0:
            break
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
                rule["rule_id"], "manual",
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


def _normalise_partial_combined_results(component: str, output: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict]]:
    returned_ids = {item.get("rule_id") for item in output if isinstance(item, dict)}
    present_rules = [item for item in rules if item["rule_id"] in returned_ids]
    missing_rules = [item for item in rules if item["rule_id"] not in returned_ids]
    payload = _combined_batch_payload(component, present_rules)
    return _combined_batch_results(component, output, present_rules, payload), missing_rules


def _run_combined_batch(app, task: dict, profile: dict, document: dict, component: str, rules: list[dict],
                        system_prompt: str, char_limit: int, label: str, depth: int = 0,
                        scan_index: dict | None = None, allow_missing_retry: bool = True) -> tuple[list[dict], int, int, int, str]:
    """运行一个可独立保存的综合评审规则组；异常时仅拆分当前组。"""
    payload = _combined_batch_payload(component, rules)
    strategy = _scan_strategy(rules)
    context_limit = min(char_limit, EVALUATION_BATCH_CONTEXT_CHARS,
                        EVALUATION_STRATEGY_CONTEXT_CHARS.get(strategy, EVALUATION_BATCH_CONTEXT_CHARS))
    if scan_index:
        context = _full_scan_review_context(scan_index, rules, context_limit)
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
            max_tokens=_output_token_budget(profile, _combined_batch_output_budget(component, len(rules))), thinking_mode=thinking_mode,
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
                max_tokens=_output_token_budget(profile, _combined_batch_output_budget(component, len(rules))), thinking_mode="disabled",
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

    parsed = request_with_repair("evaluate_all_cross_bid_price")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        retry_count += 1
        parsed = request_with_repair("evaluate_all_cross_bid_price_retry")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        # 格式故障不能让已经完成的数十个审查结果整体失败；保留单文件阶段结果并明确统计缺失。
        return {"rule_count": len(rules), "result_count": 0, "retry_count": retry_count,
                "missing_count": len(expected), "format_failure": True}

    rules_by_id = {rule["rule_id"]: item for rule, item in zip(rules, payload)}
    documents_by_id = {document["document_id"]: document for document in documents}
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
        result = _score_result_from_model(key[1], suggested, max_score, raw)
        storage.save_score_results(app, score_run_id, documents_by_id[key[0]]["document_id"], [result])
        received.add(key)
    return {"rule_count": len(rules), "result_count": len(received), "retry_count": retry_count,
            "missing_count": len(expected - received)}


def _evaluate_all(app, task: dict) -> dict:
    """综合评审按规则小组运行并立即落库，避免单次混合 JSON 过大。"""
    rule_set, all_rules = storage.list_rules(app, task["project_id"])
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始综合评审")
    review_rules = [item for item in all_rules if item["enabled"] and item["category"] in {"qualification", "compliance", "substantive", "rejection", "other"}]
    objective_rules = [item for item in all_rules if item["enabled"] and item["category"] == "objective"]
    subjective_rules = [item for item in all_rules if item["enabled"] and item["category"] == "subjective"]
    cross_bid_price_rules = _cross_bid_price_rules(objective_rules)
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
    reference_regions = _reference_regions(all_documents)
    compact_retry_count = split_retry_count = 0
    manual_fallback_rule_count = 0
    reused_document_count = 0
    batch_count = 0
    full_scan_document_count = full_scan_batch_count = full_scan_failed_chunk_count = 0
    groups_per_document = sum(
        len(_rule_batches(component_rules, EVALUATION_BATCH_SIZES[component]))
        for component, component_rules in (("review", review_rules), ("objective", objective_rules), ("subjective", subjective_rules))
    )
    scan_units_by_document = {item["document_id"]: _full_scan_chunk_count(item) for item in documents}
    cross_bid_units = 1 if objective_run and len(documents) >= 2 and cross_bid_price_rules else 0
    total_work_units = max(1, sum(scan_units_by_document.values()) + len(documents) * groups_per_document + cross_bid_units)
    completed_work_units = 0
    for index, document in enumerate(documents, start=1):
        storage.update_task(app, task["task_id"], progress=int(completed_work_units * 100 / total_work_units), message=f"正在综合评审 {index}/{len(documents)}：{document['bidder_name'] or document['original_name']}")
        reusable = storage.reusable_evaluation_document_results(
            app, task["project_id"], rule_set["rule_set_id"], profile["profile_id"], document["document_id"], expected_rule_ids,
            PROMPT_VERSION,
        )
        if reusable:
            if review_run:
                storage.save_review_results(app, review_run["review_run_id"], document["document_id"], reusable["review"])
            if objective_run:
                storage.save_score_results(app, objective_run["score_run_id"], document["document_id"], reusable["objective"])
            if subjective_run:
                storage.save_score_results(app, subjective_run["score_run_id"], document["document_id"], reusable["subjective"])
            reused_document_count += 1
            completed_work_units += scan_units_by_document[document["document_id"]] + groups_per_document
            storage.update_task(app, task["task_id"], progress=int(completed_work_units * 100 / total_work_units), message=f"已复用 {document['bidder_name'] or document['original_name']} 的完整评审结果")
            continue
        scan_index = _scan_document_fulltext(
            app, task, profile, document, review_rules + objective_rules + subjective_rules, system_prompt,
            reference_regions=reference_regions,
            progress_offset=completed_work_units, progress_total=total_work_units,
        )
        completed_work_units += scan_units_by_document[document["document_id"]]
        if scan_index:
            full_scan_document_count += 1
            full_scan_batch_count += scan_index.get("scan_batch_count", 0)
            full_scan_failed_chunk_count += len(scan_index.get("failed_chunks", []))
            compact_retry_count += scan_index.get("compact_retry_count", 0)
            split_retry_count += scan_index.get("split_retry_count", 0)
        components = (("review", review_rules, review_run), ("objective", objective_rules, objective_run), ("subjective", subjective_rules, subjective_run))
        for component, component_rules, run in components:
            for group_index, group in enumerate(_rule_batches(component_rules, EVALUATION_BATCH_SIZES[component]), start=1):
                label = f"{document['bidder_name'] or document['original_name']}·{component} 第{group_index}组"
                storage.update_task(app, task["task_id"], message=f"正在综合评审：{label}")
                results, compact_count, split_count, fallback_count, _ = _run_combined_batch(
                    app, task, profile, document, component, group, system_prompt, char_limit, label,
                    scan_index=scan_index,
                )
                # 每个规则组成功后立即持久化；后续组失败也不会丢失已完成组。
                if component == "review" and run:
                    storage.save_review_results(app, run["review_run_id"], document["document_id"], results)
                elif run:
                    storage.save_score_results(app, run["score_run_id"], document["document_id"], results)
                compact_retry_count += compact_count
                split_retry_count += split_count
                manual_fallback_rule_count += fallback_count
                batch_count += 1
                completed_work_units += 1
                storage.update_task(app, task["task_id"], progress=int(completed_work_units * 100 / total_work_units), message=f"已完成综合评审：{label}")
    cross_bid_price = {"rule_count": 0, "result_count": 0, "retry_count": 0, "missing_count": 0}
    if cross_bid_units and objective_run:
        storage.update_task(app, task["task_id"], progress=int(completed_work_units * 100 / total_work_units),
                            message="正在统一比较全部投标人的报价并计算价格分")
        cross_bid_price = _run_cross_bid_price_scoring(
            app, task, profile, documents, cross_bid_price_rules, objective_run["score_run_id"],
        )
        completed_work_units += 1
    return {"review_run_id": review_run["review_run_id"] if review_run else None, "objective_run_id": objective_run["score_run_id"] if objective_run else None,
            "subjective_run_id": subjective_run["score_run_id"] if subjective_run else None, "document_count": len(documents),
            "reused_document_count": reused_document_count, "model_document_count": len(documents) - reused_document_count,
            "rule_count": len(all_rules), "profile": profile["display_name"], "compact_retry_count": compact_retry_count,
            "split_retry_count": split_retry_count, "manual_fallback_rule_count": manual_fallback_rule_count,
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
