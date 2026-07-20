"""按需启动的评标工作台任务进程。"""

from __future__ import annotations

import itertools
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
from dashboard.evaluation_workbench.ai_gateway import request_json
from dashboard.evaluation_workbench.collusion_signals import build_cross_bid_analysis
from dashboard.evaluation_workbench.prompt_context import build_rule_context
from dashboard.blueprints.evaluation_workbench import create_worker_app
from dashboard.utils.comparator import CollusionDetector, ComparisonLimitError


MAX_PARSE_PAGES = 2000
MAX_PARSED_CHARS = 2_000_000
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024
PROMPT_VERSION = "token-optimized-v2"
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
    seen: set[tuple[str, str, str]] = set()
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
            needs_ocr = raw.get("needs_ocr") is True
            if score_type == "objective":
                # 第一版只自动计算已确认的“满足即满分”客观规则；其他规则保留人工分。
                kind = item["scoring"].get("kind", "boolean")
                met = raw.get("met")
                suggested = None if needs_ocr else (max_score if kind == "boolean" and met is True else (0.0 if kind == "boolean" and met is False else None))
            else:
                value = raw.get("suggested_score")
                suggested = None if needs_ocr else (min(max_score, max(0.0, float(value))) if isinstance(value, (int, float)) and not isinstance(value, bool) and max_score > 0 else None)
            if needs_ocr and not str(raw.get("reason", "")).strip():
                raw = {**raw, "reason": "该评分项的关键证据需要 OCR 识别后才能评分。"}
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
    return {"rule_id": rule_id, "status": status, "evidence": str(item.get("evidence", ""))[:2000],
            "page_hint": str(item.get("page_hint", ""))[:80] or None, "reason": str(item.get("reason", ""))[:2000],
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
        needs_ocr = raw.get("needs_ocr") is True
        if score_type == "objective":
            kind = item["scoring"].get("kind", "boolean")
            met = raw.get("met")
            suggested = None if needs_ocr else (max_score if kind == "boolean" and met is True else (0.0 if kind == "boolean" and met is False else None))
        else:
            value = raw.get("suggested_score")
            suggested = None if needs_ocr else (min(max_score, max(0.0, float(value))) if isinstance(value, (int, float)) and not isinstance(value, bool) and max_score > 0 else None)
        if needs_ocr and not str(raw.get("reason", "")).strip():
            raw = {**raw, "reason": "该评分项的关键证据需要 OCR 识别后才能评分。"}
        results.append(_score_result_from_model(item["rule_id"], suggested, max_score, raw))
    return results


def _score_result_from_model(rule_id: str, suggested: float | None, max_score: float, raw: dict) -> dict:
    confidence = raw.get("confidence") if raw.get("confidence") in {"high", "medium", "low"} else "medium"
    needs_ocr = raw.get("needs_ocr") is True
    has_evidence = bool(str(raw.get("evidence", "")).strip())
    auto_ready = suggested is not None and confidence == "high" and has_evidence
    return {"rule_id": rule_id, "suggested_score": suggested, "final_score": None,
            "effective_score": suggested if auto_ready else None, "max_score": max_score or None,
            "evidence": str(raw.get("evidence", ""))[:2000],
            "reason": str(raw.get("reason", "该评分项需要 OCR 识别后才能评分。" if needs_ocr else "模型未返回可确认结论，请人工评分。"))[:2000],
            "confidence": confidence, "automation_status": "ready_for_batch_confirmation" if auto_ready else "needs_review",
            "requires_review": not auto_ready,
            "review_reason": "" if auto_ready else "未得到高置信、可引用的建议分，需人工复核。"}


def _is_invalid_json_model_response(exc: ValueError) -> bool:
    return str(exc).startswith("模型未返回有效 JSON")


EVALUATION_BATCH_SIZES = {"review": 8, "objective": 8, "subjective": 6}


def _rule_batches(rules: list[dict], size: int) -> list[list[dict]]:
    return [rules[index:index + size] for index in range(0, len(rules), size)]


def _combined_batch_prompt(app, component: str, document: dict, payload: list[dict], text: str, *, compact: bool) -> str:
    template_id = f"evaluate_all_{component}_user"
    return storage.render_prompt_template(
        app, template_id, rules=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        document_name=document["original_name"], bidder_name=document["bidder_name"] or "未填写", text=text,
        retry_note="这是针对格式异常的紧凑重试，" if compact else "",
    )


def _combined_batch_payload(component: str, rules: list[dict]) -> list[dict]:
    if component == "review":
        return [{"rule_id": item["rule_id"], "category": item["category"], "title": item["title"],
                 "check_rule": item.get("check_rule") or item["title"], "source_text": item["source_text"],
                 "ocr_required": item.get("check_mode") == "ocr"} for item in rules]
    return _score_payload(rules)


def _combined_batch_results(component: str, output: object, rules: list[dict], payload: list[dict]) -> list[dict]:
    if component == "review":
        return _normalise_review_results(output, rules)
    return _normalise_score_results(output, payload, component)


def _run_combined_batch(app, task: dict, profile: dict, document: dict, component: str, rules: list[dict],
                        system_prompt: str, char_limit: int, label: str, depth: int = 0) -> tuple[list[dict], int, int, str]:
    """运行一个可独立保存的综合评审规则组；异常时仅拆分当前组。"""
    payload = _combined_batch_payload(component, rules)
    context = build_rule_context(document["parsed_path"], rules, char_limit)
    thinking_mode = "disabled" if component == "objective" else "adaptive"
    try:
        parsed = _request_task_json(
            app, task, profile, f"evaluate_all_{component}_batch", system_prompt,
            _combined_batch_prompt(app, component, document, payload, context["text"], compact=False),
            document_id=document["document_id"], context_mode=f"{label}:{context['mode']}",
            max_tokens=_output_token_budget(profile, max(1_200, 500 + len(rules) * 260)), thinking_mode=thinking_mode,
        )
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise ValueError("模型返回格式不符合综合评审要求")
        return _combined_batch_results(component, parsed["results"], rules, payload), 0, 0, context["mode"]
    except ValueError as exc:
        if not _is_invalid_json_model_response(exc):
            raise
        if len(rules) > 1 and depth < 3:
            storage.update_task(app, task["task_id"], message=f"{label} 返回格式异常，正在仅拆分该规则组重试")
            midpoint = max(1, len(rules) // 2)
            left = _run_combined_batch(app, task, profile, document, component, rules[:midpoint], system_prompt, char_limit, f"{label}/拆分1", depth + 1)
            right = _run_combined_batch(app, task, profile, document, component, rules[midpoint:], system_prompt, char_limit, f"{label}/拆分2", depth + 1)
            return left[0] + right[0], left[1] + right[1], left[2] + right[2] + 1, "split"
        storage.update_task(app, task["task_id"], message=f"{label} 返回格式异常，正在紧凑 JSON 重试")
        retry_context = build_rule_context(document["parsed_path"], rules, min(char_limit, 120_000))
        parsed = _request_task_json(
            app, task, profile, f"evaluate_all_{component}_compact_retry", system_prompt,
            _combined_batch_prompt(app, component, document, payload, retry_context["text"], compact=True),
            document_id=document["document_id"], context_mode=f"{label}_compact:{retry_context['mode']}",
            max_tokens=_output_token_budget(profile, max(1_500, 600 + len(rules) * 280)), thinking_mode=thinking_mode,
        )
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise ValueError("模型返回格式不符合综合评审要求")
        return _combined_batch_results(component, parsed["results"], rules, payload), 1, 0, retry_context["mode"]


def _evaluate_all(app, task: dict) -> dict:
    """综合评审按规则小组运行并立即落库，避免单次混合 JSON 过大。"""
    rule_set, all_rules = storage.list_rules(app, task["project_id"])
    if not rule_set or rule_set["status"] != "confirmed":
        raise ValueError("请先确认当前评审规则集，再开始综合评审")
    review_rules = [item for item in all_rules if item["enabled"] and item["category"] in {"qualification", "compliance", "substantive", "rejection", "other"}]
    objective_rules = [item for item in all_rules if item["enabled"] and item["category"] == "objective"]
    subjective_rules = [item for item in all_rules if item["enabled"] and item["category"] == "subjective"]
    if not (review_rules or objective_rules or subjective_rules):
        raise ValueError("综合评审需要至少一条已确认的审查或评分规则")
    documents = [item for item in storage.list_documents(app, task["project_id"]) if item["role"] == "bid"]
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
    compact_retry_count = split_retry_count = 0
    reused_document_count = 0
    batch_count = 0
    for index, document in enumerate(documents, start=1):
        storage.update_task(app, task["task_id"], progress=int((index - 1) * 100 / len(documents)), message=f"正在综合评审 {index}/{len(documents)}：{document['bidder_name'] or document['original_name']}")
        reusable = storage.reusable_evaluation_document_results(
            app, task["project_id"], rule_set["rule_set_id"], profile["profile_id"], document["document_id"], expected_rule_ids,
        )
        if reusable:
            if review_run:
                storage.save_review_results(app, review_run["review_run_id"], document["document_id"], reusable["review"])
            if objective_run:
                storage.save_score_results(app, objective_run["score_run_id"], document["document_id"], reusable["objective"])
            if subjective_run:
                storage.save_score_results(app, subjective_run["score_run_id"], document["document_id"], reusable["subjective"])
            reused_document_count += 1
            continue
        components = (("review", review_rules, review_run), ("objective", objective_rules, objective_run), ("subjective", subjective_rules, subjective_run))
        for component, component_rules, run in components:
            for group_index, group in enumerate(_rule_batches(component_rules, EVALUATION_BATCH_SIZES[component]), start=1):
                label = f"{document['bidder_name'] or document['original_name']}·{component} 第{group_index}组"
                storage.update_task(app, task["task_id"], message=f"正在综合评审：{label}")
                results, compact_count, split_count, _ = _run_combined_batch(
                    app, task, profile, document, component, group, system_prompt, char_limit, label,
                )
                # 每个规则组成功后立即持久化；后续组失败也不会丢失已完成组。
                if component == "review" and run:
                    storage.save_review_results(app, run["review_run_id"], document["document_id"], results)
                elif run:
                    storage.save_score_results(app, run["score_run_id"], document["document_id"], results)
                compact_retry_count += compact_count
                split_retry_count += split_count
                batch_count += 1
    return {"review_run_id": review_run["review_run_id"] if review_run else None, "objective_run_id": objective_run["score_run_id"] if objective_run else None,
            "subjective_run_id": subjective_run["score_run_id"] if subjective_run else None, "document_count": len(documents),
            "reused_document_count": reused_document_count, "model_document_count": len(documents) - reused_document_count,
            "rule_count": len(all_rules), "profile": profile["display_name"], "compact_retry_count": compact_retry_count,
            "split_retry_count": split_retry_count, "batch_count": batch_count, "prompt_version": PROMPT_VERSION}


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
