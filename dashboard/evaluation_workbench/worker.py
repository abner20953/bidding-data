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
                       max_tokens: int | None = None) -> dict:
    """调用模型并只记录用量元数据，不记录正文或提示词。"""
    recorded = False

    def record_usage(usage: dict) -> None:
        nonlocal recorded
        recorded = True
        storage.record_model_call(
            app, task["task_id"], task["project_id"], phase, profile.get("profile_id"),
            document_id=document_id, input_chars=len(system_prompt) + len(user_prompt),
            context_mode=context_mode, usage=usage,
        )

    try:
        return request_json(profile, system_prompt, user_prompt, usage_callback=record_usage, max_tokens=max_tokens)
    finally:
        # 部分兼容接口不返回 usage；仍保留发送字符数以便统计和优化。
        if not recorded:
            storage.record_model_call(
                app, task["task_id"], task["project_id"], phase, profile.get("profile_id"),
                document_id=document_id, input_chars=len(system_prompt) + len(user_prompt),
                context_mode=context_mode,
            )


def _system_prompt(app, template_id: str, fixed_boundary: str) -> str:
    """通用提示词可配置；输出格式、证据边界与法律边界始终由系统追加。"""
    return f"{storage.prompt_template(app, template_id)}\n\n系统固定边界（不可由通用提示词覆盖）：{fixed_boundary}"


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
    """为结构化输出设置保守上限；MiniMax M2 思考不可关闭时不额外截断。"""
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
    system_prompt = _system_prompt(
        app, "compare_ai_assessment",
        "只能评估固定规则提取的证据可靠性；不得认定串通投标、废标或作出法律结论；证据不足时必须保守输出 unassessable。",
    )
    for start in range(0, len(signals), COMPARE_AI_BATCH_SIZE):
        batch = signals[start:start + COMPARE_AI_BATCH_SIZE]
        packets = [_compare_evidence_packet(item) for item in batch]
        user_prompt = f"""请按固定规则复核以下压缩证据包，只返回 JSON：
{{"assessments":[{{"signal_id":"ID","decision":"confirmed_clue|suspected_clue|excluded|unassessable","risk_level":"low|medium|high","confidence":"high|medium|low","reason":"简洁理由","suggested_check":"建议核验事项"}}]}}

判定含义：confirmed_clue 仅表示该异常线索有较充分证据；suspected_clue 表示仍有合理替代解释；excluded 表示现有证据更可能为模板/公共来源；unassessable 表示证据不足。不得输出串标成立、废标或扣分结论。
证据包：{json.dumps(packets, ensure_ascii=False, separators=(',', ':'))}"""
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


_UNREVIEWABLE_RULE_MARKERS = (
    "密封", "递交时间", "递交地点", "提交时间", "提交地点", "投标截止", "开标时间", "开标地点",
    "开标现场", "响应文件份数", "电子版份数", "响应文件电子版", "电子响应文件",
    "正本副本", "正本一份", "纸质正本", "纸质副本", "签收", "送达",
)
_RULE_SECTION_MARKERS = ("评标办法", "评分标准", "资格审查", "符合性审查", "废标", "无效投标", "否决投标", "资格条件")
_SCORE_CLAUSE_PATTERN = re.compile(r"(?:得\s*\d+(?:\.\d+)?\s*分|最高(?:得)?\s*\d+(?:\.\d+)?\s*分|满分(?:为)?\s*\d+(?:\.\d+)?\s*分)")


def _is_unreviewable_rule(item: dict) -> bool:
    """过滤仅能在线下或开标环节核验的规则，避免只依赖模型服从提示词。"""
    value = re.sub(r"\s+", "", f"{item.get('title', '')}{item.get('check_rule', '')}{item.get('source_text', '')}")
    return any(marker in value for marker in _UNREVIEWABLE_RULE_MARKERS)


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
        start, end = max(0, index - 4), min(len(lines), index + 5)
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


def _rule_extraction_prompt(text: str, *, compact: bool, score_packets: list[str]) -> str:
    limits = (
        "这是格式异常后的紧凑重试。最多返回 30 条规则；title 最多 30 字，check_rule 最多 90 字，source_text 最多 120 字。"
        if compact else
        "最多返回 45 条规则；title 最多 40 字，check_rule 最多 140 字，source_text 最多 220 字。"
    )
    score_audit = "\n".join(f"【评分条款 {index}】\n{packet}" for index, packet in enumerate(score_packets, start=1))
    score_requirement = (
        "本地已定位以下疑似评分条款。必须逐项核验并为每个不同的明确计分条款输出一条 objective 或 subjective 规则；"
        "不得遗漏业绩、报价、人员、资质、方案等评分项。"
        if score_audit else "未定位到明确评分条款时，不要臆造评分规则。"
    )
    return f"""请从以下招标文件原文提取可由 AI 审查的评标规则，只返回一个合法 JSON 对象：
{{"rules":[{{"category":"qualification|compliance|substantive|rejection|objective|subjective","title":"简明规则名称","check_rule":"面向投标文件的明确检查指令","source_text":"招标原文短摘录","ocr_required":false,"scoring":{{"max_score":数字,"kind":"boolean|manual"}} }}]}}

严格要求：不得使用 Markdown 或在 JSON 前后添加说明；不得逐条复述招标原文；同类要求合并为一条规则。{limits}
{score_requirement}
只保留能通过已上传投标文件中可检索的文字、表格或元数据核验的事项，规则必须明确且能据文件内容判断。不要输出密封与递交要求、响应文件份数或电子版要求、递交地点/时间、开标现场、线下原件、纸质材料、投标文件本身无法体现的签收事项，以及其他无法仅凭投标文件核验的事项。若规则的关键证据只能依赖扫描图片、证照图片、签章或手写内容识别，请将 ocr_required 设为 true；其余为 false。

分类说明：qualification 为资格性；compliance 为符合性；substantive 为实质性；rejection 为无效投标/废标；objective 为客观分；subjective 为主观分。objective 和 subjective 必须填写 scoring.max_score，且只能填写招标原文明确规定的满分；没有明确满分时不要输出为评分项。objective 仅“满足即满分”时 kind 为 boolean，分档、数量、累计或人工判断评分时 kind 为 manual；subjective 的 kind 为 manual。非评分项省略 scoring。

评分条款覆盖清单：
{score_audit or '无'}

招标文件原文：
{text}"""


def _score_rule_supplement_prompt(score_packets: list[str], existing_rules: list[dict]) -> str:
    existing = [
        {"category": item.get("category"), "title": item.get("title"), "check_rule": item.get("check_rule"), "max_score": (item.get("scoring") or {}).get("max_score")}
        for item in existing_rules if item.get("category") in {"objective", "subjective"}
    ]
    packet_text = "\n".join(f"【评分条款 {index}】\n{packet}" for index, packet in enumerate(score_packets, start=1))
    return f"""仅根据以下评分条款，补充主规则提取遗漏的明确评分项。只返回合法 JSON：
{{"rules":[{{"category":"objective|subjective","title":"简明规则名称","check_rule":"面向投标文件的明确检查指令","source_text":"原文短摘录","ocr_required":false,"scoring":{{"max_score":数字,"kind":"boolean|manual"}}}}]}}

已有评分规则：{json.dumps(existing, ensure_ascii=False, separators=(',', ':'))}
必须仅返回缺失项，不得重复已有项；每个明确评分条款都要覆盖。objective 和 subjective 都必须有 scoring.max_score，且仅能填写原文明确的满分。不得使用 Markdown 或添加说明；title 最多 30 字，check_rule 最多 90 字，source_text 最多 120 字。

评分条款：
{packet_text}"""


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
    storage.update_task(app, task["task_id"], progress=15, message="正在调用模型提取评审规则")
    system_prompt = _system_prompt(app, "extract_rules", "只能依据给出的招标文件原文，不得编造；必须遵守输出 JSON 结构。")
    user_prompt = _rule_extraction_prompt(text, compact=False, score_packets=score_packets)
    compact_retry_count = 0
    try:
        parsed = _request_task_json(app, task, profile, "extract_rules", system_prompt, user_prompt,
                                    document_id=tender["document_id"], context_mode="full_prefix",
                                    max_tokens=_output_token_budget(profile, 12_000))
    except ValueError as exc:
        if not _is_invalid_json_model_response(exc):
            raise
        compact_retry_count = 1
        storage.update_task(app, task["task_id"], progress=40, message="模型输出过长，正在按紧凑格式重试")
        retry_prompt = _rule_extraction_prompt(text, compact=True, score_packets=score_packets)
        parsed = _request_task_json(app, task, profile, "extract_rules_compact_retry", system_prompt, retry_prompt,
                                    document_id=tender["document_id"], context_mode="full_prefix_compact_retry",
                                    max_tokens=_output_token_budget(profile, 8_000))
    raw_rules = parsed.get("rules") if isinstance(parsed, dict) else None
    if not isinstance(raw_rules, list):
        raise ValueError("模型返回格式不符合规则提取要求")
    primary_score_rules = [item for item in raw_rules if isinstance(item, dict) and item.get("category") in {"objective", "subjective"}]
    scoring_supplement_count = 0
    if score_packets and len(primary_score_rules) < len(score_packets):
        storage.update_task(app, task["task_id"], progress=60, message="正在核验评分条款覆盖并补充遗漏项")
        try:
            supplement = _request_task_json(
                app, task, profile, "extract_rules_scoring_supplement", system_prompt,
                _score_rule_supplement_prompt(score_packets, primary_score_rules), document_id=tender["document_id"],
                context_mode="score_clause_packets_only", max_tokens=_output_token_budget(profile, 4_000),
            )
            supplement_rules = supplement.get("rules") if isinstance(supplement, dict) else None
            if isinstance(supplement_rules, list):
                raw_rules.extend(item for item in supplement_rules if isinstance(item, dict))
                scoring_supplement_count = len(supplement_rules)
        except ValueError as exc:
            # 主规则已提取成功时，补充调用异常不应丢弃已得到的规则集。
            storage.update_task(app, task["task_id"], message=f"评分条款补充未完成：{exc}")
    candidates = [item for item in raw_rules if isinstance(item, dict) and str(item.get("title", "")).strip() and item.get("category") in {"qualification", "compliance", "substantive", "rejection", "objective", "subjective"}]
    rules = [item for item in candidates if not _is_unreviewable_rule(item)]
    excluded_rule_count = len(candidates) - len(rules)
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
            "scoring_supplement_count": scoring_supplement_count}


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
        system_prompt = _system_prompt(app, "review_documents", "只能基于规则与投标文件可见原文，不能推断图片、签字、盖章或线下材料。")
        user_prompt = f"""请逐条审查投标文件。返回 JSON：
{{"results":[{{"rule_id":"规则ID","status":"satisfied|not_satisfied|partial|not_found|manual|ocr_required","evidence":"投标文件原文摘录","page_hint":null,"reason":"简洁判断理由","risk_level":"low|medium|high","confidence":"high|medium|low","evidence_quality":"sufficient|limited|missing"}}]}}

对于 ocr_required=true 的规则，当前系统未执行 OCR；若关键证据仅存在于图片、签章、证照或手写内容，必须返回 ocr_required，不能据此返回不满足。
规则：
{rule_prompt}

投标文件：{document['original_name']}；投标人：{document['bidder_name'] or '未填写'}
原文：
{text[:char_limit]}"""
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
        if score_type == "objective":
            instruction = """返回 JSON：{\"results\":[{\"rule_id\":\"规则ID\",\"met\":true|false|null,\"needs_ocr\":true|false,\"evidence\":\"原文摘录\",\"reason\":\"判断理由\",\"confidence\":\"high|medium|low\"}]}。只判断证据是否满足，不自行计算分数。ocr_required=true 且关键证据仅在图片中时，needs_ocr 必须为 true，met 返回 null。"""
        else:
            instruction = """返回 JSON：{\"results\":[{\"rule_id\":\"规则ID\",\"suggested_score\":数字,\"needs_ocr\":true|false,\"evidence\":\"原文摘录\",\"reason\":\"得扣分理由\",\"confidence\":\"high|medium|low\"}]}。分数不得超出规则 scoring.max_score。ocr_required=true 且关键证据仅在图片中时，needs_ocr 必须为 true，suggested_score 返回 null。"""
        system_prompt = _system_prompt(app, f"score_{score_type}", "只能依据评分规则与投标文件原文，不得编造材料或超出评分上限。")
        user_prompt = f"{instruction}\n评分规则：{json.dumps(rule_payload, ensure_ascii=False, separators=(',', ':'))}\n投标文件：{document['original_name']}\n原文：\n{text}"
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


def _combined_evaluation_prompt(document: dict, review_payload: list[dict], objective_payload: list[dict],
                                 subjective_payload: list[dict], text: str, *, compact: bool) -> str:
    limits = (
        "每条 evidence 最多 60 个字符、reason 最多 50 个字符；证据不足时返回空字符串，不得解释输出格式。"
        if compact else
        "每条 evidence 最多 140 个字符、reason 最多 100 个字符；证据不足时简要说明，不得复述整段原文。"
    )
    retry_note = "这是针对格式异常的紧凑重试，" if compact else ""
    return f"""{retry_note}对同一份投标文件完成下列三类工作，并只返回一个合法 JSON 对象：
{{"review_results":[{{"rule_id":"规则ID","status":"satisfied|not_satisfied|partial|not_found|manual|ocr_required","evidence":"原文摘录","page_hint":null,"reason":"简洁理由","risk_level":"low|medium|high","confidence":"high|medium|low","evidence_quality":"sufficient|limited|missing"}}],"objective_scores":[{{"rule_id":"规则ID","met":true|false|null,"needs_ocr":true|false,"evidence":"原文摘录","reason":"判断理由","confidence":"high|medium|low"}}],"subjective_scores":[{{"rule_id":"规则ID","suggested_score":数字,"needs_ocr":true|false,"evidence":"原文摘录","reason":"得扣分理由","confidence":"high|medium|low"}}]}}

严格要求：不得使用 Markdown 代码块、不得在 JSON 前后添加说明、所有字符串必须使用标准 JSON 双引号和转义。{limits}
审查规则：{json.dumps(review_payload, ensure_ascii=False, separators=(',', ':'))}
客观评分规则：{json.dumps(objective_payload, ensure_ascii=False, separators=(',', ':'))}
主观评分规则：{json.dumps(subjective_payload, ensure_ascii=False, separators=(',', ':'))}
对标记 ocr_required=true 的规则，当前系统未执行 OCR；若关键证据仅在图片、签章、证照或手写内容中，审查结果必须为 ocr_required，评分结果 needs_ocr=true 且不给分。客观分只判断证据是否满足，不自行计算分数。主观分不得超出规则 scoring.max_score。
投标文件：{document['original_name']}；投标人：{document['bidder_name'] or '未填写'}
原文：
{text}"""


def _is_invalid_json_model_response(exc: ValueError) -> bool:
    return str(exc).startswith("模型未返回有效 JSON")


def _evaluate_all(app, task: dict) -> dict:
    """可选的综合评审：每份投标文件仅发送一次正文，原有结果表分别落库。"""
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
    review_payload = [{"rule_id": item["rule_id"], "category": item["category"], "title": item["title"],
                       "check_rule": item.get("check_rule") or item["title"], "source_text": item["source_text"],
                       "ocr_required": item.get("check_mode") == "ocr"} for item in review_rules]
    objective_payload = _score_payload(objective_rules)
    subjective_payload = _score_payload(subjective_rules)
    expected_rule_ids = {
        "review": {item["rule_id"] for item in review_rules},
        "objective": {item["rule_id"] for item in objective_rules},
        "subjective": {item["rule_id"] for item in subjective_rules},
    }
    system_prompt = _system_prompt(app, "evaluate_all", "只能依据规则与投标文件可见原文，不得编造、推断签字盖章或线下材料。")
    compact_retry_count = 0
    reused_document_count = 0
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
        context = build_rule_context(document["parsed_path"], all_rules, char_limit)
        text = context["text"]
        user_prompt = _combined_evaluation_prompt(document, review_payload, objective_payload, subjective_payload, text, compact=False)
        try:
            parsed = _request_task_json(app, task, profile, "evaluate_all", system_prompt, user_prompt,
                                        document_id=document["document_id"], context_mode=context["mode"],
                                        max_tokens=_output_token_budget(profile, 900 + len(all_rules) * 240))
        except ValueError as exc:
            if not _is_invalid_json_model_response(exc):
                raise
            compact_retry_count += 1
            storage.update_task(app, task["task_id"], message=f"{document['bidder_name'] or document['original_name']} 返回格式异常，正在紧凑重试")
            retry_context = build_rule_context(document["parsed_path"], all_rules, min(char_limit, 160_000))
            retry_prompt = _combined_evaluation_prompt(
                document, review_payload, objective_payload, subjective_payload, retry_context["text"], compact=True,
            )
            parsed = _request_task_json(app, task, profile, "evaluate_all_compact_retry", system_prompt, retry_prompt,
                                        document_id=document["document_id"], context_mode=retry_context["mode"],
                                        max_tokens=_output_token_budget(profile, 700 + len(all_rules) * 180))
        if not isinstance(parsed, dict):
            raise ValueError("模型返回格式不符合综合评审要求")
        if review_run:
            storage.save_review_results(app, review_run["review_run_id"], document["document_id"], _normalise_review_results(parsed.get("review_results"), review_rules))
        if objective_run:
            storage.save_score_results(app, objective_run["score_run_id"], document["document_id"], _normalise_score_results(parsed.get("objective_scores"), objective_payload, "objective"))
        if subjective_run:
            storage.save_score_results(app, subjective_run["score_run_id"], document["document_id"], _normalise_score_results(parsed.get("subjective_scores"), subjective_payload, "subjective"))
    return {"review_run_id": review_run["review_run_id"] if review_run else None, "objective_run_id": objective_run["score_run_id"] if objective_run else None,
            "subjective_run_id": subjective_run["score_run_id"] if subjective_run else None, "document_count": len(documents),
            "reused_document_count": reused_document_count, "model_document_count": len(documents) - reused_document_count,
            "rule_count": len(all_rules), "profile": profile["display_name"], "compact_retry_count": compact_retry_count,
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
