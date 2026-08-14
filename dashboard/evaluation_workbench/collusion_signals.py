"""把两两查重证据整理为审慎、可复核的横向异常线索。

本模块只消费现有 ``CollusionDetector`` 的结果，不自行认定串通投标，也不改变
原有 ``/bijiao`` 比对算法和接口。所有线索均须人工核验。
"""

from __future__ import annotations

import re
import uuid


ANALYSIS_VERSION = "cross-bid-signals-v5"
DECISION_BOUNDARY = (
    "本结果仅表示投标文件之间存在需要复核的横向异常线索，不构成串通投标认定、"
    "法定情形认定、废标依据或自动扣分依据。最终结论须由评标委员会结合原件、"
    "招标规则及调查核验结果作出。"
)

DIMENSION_LABELS = {
    "text_similarity": "正文雷同",
    "text_error": "共同异常或错误",
    "contact": "共同联系电话",
    "email": "共同邮箱",
    "person_name": "共同姓名",
    "person_identity": "相同人员身份信息",
    "address": "共同地址",
    "tender_common_edit": "招标原文共同改动",
    "metadata": "相同文档属性",
}

NOT_EXECUTED_DIMENSIONS = [
    {"dimension": "price_pattern", "label": "报价规律", "reason": "尚未取得经确认的结构化报价数据，不从正文数字推断"},
    {"dimension": "payment_source", "label": "缴费来源", "reason": "尚未接入可核验的缴费流水或支付来源数据"},
    {"dimension": "performance_reference", "label": "业绩引用关系", "reason": "尚未建立经人工确认的业绩主体与合同结构"},
    {"dimension": "foreign_entity_leak", "label": "他方主体信息残留", "reason": "当前仅比较共同敏感实体，未自动推断实体归属"},
    {"dimension": "interpretation_error", "label": "共同理解偏差", "reason": "需要结合具体招标条款和人工语义判断"},
]


def _clip(value, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _entity_dimension(value: str) -> str | None:
    normalized = re.sub(r"[\s\-—_]", "", str(value or "")).upper()
    # 仅 18 位二代身份证；15 位纯数字串多为报价等长数字拼接，易误判。
    if re.fullmatch(r"\d{17}[0-9X]", normalized):
        return "person_identity"
    if "@" in normalized:
        return "email"
    if re.fullmatch(r"1[3-9]\d{9}", normalized):
        return "contact"
    return None


def _page_evidence(item: dict) -> dict:
    left = item.get("text_a", "")
    right = item.get("text_b", "")
    evidence = {
        "page_a": item.get("page_a") or None,
        "page_b": item.get("page_b") or None,
        "text_a": _clip(left),
        "text_b": _clip(right),
    }
    if item.get("similarity") is not None:
        evidence["similarity"] = item["similarity"]
    if item.get("tender_similarity") is not None:
        evidence["tender_similarity"] = item["tender_similarity"]
    if item.get("tender_coverage_a") is not None:
        evidence["tender_coverage_a"] = item["tender_coverage_a"]
    if item.get("tender_coverage_b") is not None:
        evidence["tender_coverage_b"] = item["tender_coverage_b"]
    if item.get("segment_count") is not None:
        evidence["segment_count"] = item["segment_count"]
    if item.get("shared_edits"):
        evidence["shared_edits"] = item["shared_edits"]
    if item.get("error_kind"):
        evidence["error_kind"] = item["error_kind"]
    if item.get("entity_kind"):
        evidence["entity_kind"] = item["entity_kind"]
    if item.get("context_a"):
        evidence["context_a"] = _clip(item["context_a"])
    if item.get("context_b"):
        evidence["context_b"] = _clip(item["context_b"])
    return evidence


def _text_coverage(stats: dict | None) -> dict:
    """仅说明纯文字查重的可读范围，不把扫描比例当作异常证据。"""
    stats = stats if isinstance(stats, dict) else {}
    total = int(stats.get("total_pages") or 0)
    scan_pages = int(stats.get("suspected_scan_pages") or 0)
    chars = stats.get("chinese_chars")
    raw_ratio = stats.get("scan_ratio")
    if not total and not isinstance(raw_ratio, (int, float)):
        return {"status": "unknown", "scan_ratio": None, "message": "未取得页级文字覆盖统计"}
    ratio = float(raw_ratio or 0)
    if (isinstance(chars, (int, float)) and chars < 100) or ratio >= 0.75:
        status = "severely_limited"
        message = f"可读文字覆盖严重不足（疑似扫描页 {scan_pages}/{total}）" if total else f"可读文字覆盖严重不足（疑似扫描页比例 {ratio:.1%}）"
    elif ratio >= 0.25:
        status = "limited"
        message = f"可读文字覆盖有限（疑似扫描页 {scan_pages}/{total}）" if total else f"可读文字覆盖有限（疑似扫描页比例 {ratio:.1%}）"
    else:
        status = "complete"
        message = f"可读文字覆盖基本充分（疑似扫描页 {scan_pages}/{total}）" if total else f"可读文字覆盖基本充分（疑似扫描页比例 {ratio:.1%}）"
    return {
        "status": status,
        "scan_ratio": round(ratio, 4),
        "total_pages": total,
        "suspected_scan_pages": scan_pages,
        "message": message,
    }


def _pair_text_coverage(result: dict) -> dict:
    text_stats = ((result.get("metadata") or {}).get("text_stats") or {})
    left = _text_coverage(text_stats.get("file_a"))
    right = _text_coverage(text_stats.get("file_b"))
    rank = {"complete": 0, "limited": 1, "severely_limited": 2, "unknown": 3}
    status = max((left.get("status"), right.get("status")), key=lambda value: rank.get(value, 3))
    return {"status": status, "documents": [left, right]}


_FORM_TEMPLATE_MARKERS = (
    "项目名称", "项目编号", "投标函", "授权委托书", "法定代表人身份证明",
    "开标一览表", "投标有效期", "投标人名称", "采购人", "招标人",
)


def _collision_value(item: dict) -> int:
    """给横向碰撞排序并屏蔽明显的公共格式残留。

    底层比较器已经按招标原文做了主过滤；这里是面向“是否值得送 AI/展示”的第二道
    通用门槛。它不依赖某个项目名称：只排除可预期的格式字段、封面和法定表单，
    对技术参数、专有表述、金额/型号组合和正文异常仍优先保留。
    """
    text = re.sub(r"\s+", "", f"{item.get('text_a') or ''}{item.get('text_b') or ''}")
    if not text:
        return 0
    try:
        tender_coverage = min(
            float(item.get("tender_coverage_a") or 0),
            float(item.get("tender_coverage_b") or 0),
        )
    except (TypeError, ValueError):
        tender_coverage = 0
    if tender_coverage >= 0.20 and not item.get("shared_edits"):
        return 0
    marker_count = sum(marker in text for marker in _FORM_TEMPLATE_MARKERS)
    if marker_count >= 2:
        return 0
    if "投标文件" in text and any(marker in text for marker in ("正本", "副本", "封面")):
        return 0
    if "遵守本投标文件" in text and "投标有效期" in text:
        return 0
    # 同一采购人、项目号或日期本身不构成独立线索；只有同时带有较长的非表单正文
    # 时才保留，且分值较低，让真正的技术/方案证据优先进入 AI 证据包。
    value = min(180, len(re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)))
    if marker_count:
        value -= 80
    if re.search(r"(?:\d+(?:\.\d+)?\s*(?:mm|cm|kg|台|套|项|元|%|㎡|m²)|[A-Za-z]{2,}[-－]?\d+)", text):
        value += 45
    if item.get("type") == "fuzzy":
        try:
            value += max(0, int(float(item.get("similarity") or 0)) - 85)
        except (TypeError, ValueError):
            pass
    return max(0, value)


def _rank_reportable_items(items: list[dict], *, minimum_value: int = 16) -> tuple[list[dict], int]:
    values = [(item, _collision_value(item)) for item in items]
    # 非模板的短独有技术表述也可能是有价值线索；格式字段已在上层严格剔除，
    # 此处仅过滤几乎不含信息的残片。
    kept = [item for item, value in values if value >= minimum_value]
    kept.sort(key=lambda item: (-_collision_value(item), int(item.get("page_a") or 0), int(item.get("page_b") or 0)))
    return kept, len(items) - len(kept)


def _signal(task_id: str, left: dict, right: dict, dimension: str, confidence: str,
            basis: str, evidence: list[dict], counter_evidence: list[str] | None = None) -> dict:
    return {
        "signal_id": str(uuid.uuid4()),
        "task_id": task_id,
        "document_a_id": left["document_id"],
        "document_b_id": right["document_id"],
        "bidder_a": left.get("bidder_name") or left.get("original_name") or "文件A",
        "bidder_b": right.get("bidder_name") or right.get("original_name") or "文件B",
        "signal_type": "collusion_signal",
        "dimension": dimension,
        "dimension_label": DIMENSION_LABELS[dimension],
        "severity": "S3",
        "confidence": confidence,
        "evidence_status": "human_verification_required",
        "assessment_result": "pending_human_review",
        "basis": basis,
        "evidence": evidence[:5],
        "counter_evidence": counter_evidence or [],
    }


def analyze_pair(task_id: str, left: dict, right: dict, result: dict, *, tender_loaded: bool = False) -> list[dict]:
    paragraphs = result.get("paragraphs") or []
    signals = []

    text_items, filtered_text_count = _rank_reportable_items([
        item for item in paragraphs if item.get("type") in {"text", "fuzzy"}
    ])
    if text_items:
        exact_count = sum(item.get("type") == "text" for item in text_items)
        signals.append(_signal(
            task_id, left, right, "text_similarity", "C3" if exact_count else "C2",
            f"发现 {exact_count} 处完全雷同、{len(text_items) - exact_count} 处近似雷同；"
            + ("已由底层算法排除招标原文直接复制内容。" if tender_loaded else "未提供招标文件，尚未完成招标原文排除。"),
            [_page_evidence(item) for item in text_items],
            [
                "已在信号层剔除封面、投标函、授权等常见格式字段；仍需结合完整上下文复核。",
                *( [f"另有 {filtered_text_count} 条低信息格式/公共来源碰撞未进入 AI 判定。"] if filtered_text_count else [] ),
            ],
        ))

    error_items, filtered_error_count = _rank_reportable_items([
        item for item in paragraphs if item.get("type") in {"shared_error", "rare_word"}
    ], minimum_value=8)
    if error_items:
        signals.append(_signal(
            task_id, left, right, "text_error", "C3" if any(item.get("type") == "shared_error" for item in error_items) else "C2",
            f"发现 {len(error_items)} 处共同的高置信异常、错误或罕见表述。",
            [_page_evidence(item) for item in error_items],
            [
                "同一资料来源、行业惯用文本或共同第三方模板也可能产生相同错误。",
                *( [f"另有 {filtered_error_count} 条低信息排版/格式异常未作为线索展示。"] if filtered_error_count else [] ),
            ],
        ))

    entity_groups = {"contact": [], "email": [], "person_name": [], "person_identity": [], "address": []}
    for item in paragraphs:
        if item.get("type") != "entity":
            continue
        dimension = {
            "phone": "contact", "email": "email", "person_name": "person_name",
            "person_identity": "person_identity", "address": "address",
        }.get(item.get("entity_kind")) or _entity_dimension(item.get("text_a", ""))
        if dimension:
            entity_groups[dimension].append(item)
    for dimension, items in entity_groups.items():
        if items:
            signals.append(_signal(
                task_id, left, right, dimension, "C3",
                f"发现 {len(items)} 项两份文件共有且未在招标文件中出现的{DIMENSION_LABELS[dimension]}信息。",
                [_page_evidence(item) for item in items],
                ["须核对该信息是否属于依法共享的联系人、联合体成员、公共服务机构、同一授权主体或注册地址。"],
            ))

    edit_items = [item for item in paragraphs if item.get("type") == "tender_related" and item.get("shared_edits")]
    if edit_items:
        # 义务主体第一人称改写（如“实施方须”改为“我方会”）是每个投标人的必然
        # 响应方式：全部为这类改动时仅降权为 C1，混入任何其他实质改动时维持 C2。
        # 新版比较器给每个碰撞提供基于“全部共同编辑”的摘要；旧结果没有该字段
        # 时仍按已展示证据兼容读取，避免破坏历史 API 结果。
        voice_only = bool(edit_items) and all(
            bool(item.get("voice_adaptation_only"))
            if "voice_adaptation_only" in item
            else bool(item.get("shared_edits")) and all(change.get("voice_adaptation") for change in item.get("shared_edits", []) if isinstance(change, dict))
            for item in edit_items
        )
        if voice_only:
            signal = _signal(
                task_id, left, right, "tender_common_edit", "C1",
                f"发现 {len(edit_items)} 处相对于招标原文的共同改动，"
                "但均为义务主体第一人称改写（如“实施方须”改为“我方会”），嫌疑度较低。",
                [_page_evidence(item) for item in edit_items],
                [
                    "第一人称改写是所有投标人响应招标要求的常规表述方式，不能单独作为异常线索。",
                    "同一澄清文件、统一答疑或公开模板可能导致一致改动，须先核对招标补充材料。",
                ],
            )
            # 降权标记：保留信号供人工查看，但不计入配对维度数与复核优先级。
            signal["voice_adaptation_only"] = True
            signals.append(signal)
        else:
            signals.append(_signal(
                task_id, left, right, "tender_common_edit", "C2",
                f"发现 {len(edit_items)} 处相对于招标原文的共同实质改动。",
                [_page_evidence(item) for item in edit_items],
                ["同一澄清文件、统一答疑或公开模板可能导致一致改动，须先核对招标补充材料。"],
            ))

    auxiliary = ((result.get("metadata") or {}).get("auxiliary") or {})
    metadata_matches = [item for item in auxiliary.get("matches") or [] if not item.get("also_in_tender")]
    if metadata_matches:
        strong = any(item.get("strength") == "reference" for item in metadata_matches)
        text_stats = (result.get("metadata") or {}).get("text_stats") or {}
        file_a_stats = text_stats.get("file_a") or {}
        file_b_stats = text_stats.get("file_b") or {}
        scan_ratio_a = file_a_stats.get("scan_ratio")
        scan_ratio_b = file_b_stats.get("scan_ratio")
        scan_evidence = []
        scan_note = ""
        if isinstance(scan_ratio_a, (int, float)) and isinstance(scan_ratio_b, (int, float)):
            scan_evidence.append({
                "field": "疑似扫描页比例",
                "value": f"{left.get('bidder_name') or '文件A'} {scan_ratio_a:.1%} / "
                         f"{right.get('bidder_name') or '文件B'} {scan_ratio_b:.1%}",
                "strength": "coverage",
            })
            if scan_ratio_a >= 0.25 or scan_ratio_b >= 0.25:
                scan_note = "；部分页面可读文字较少，正文查重覆盖有限"
        signals.append(_signal(
            task_id, left, right, "metadata", "C2" if strong else "C1",
            f"发现 {len(metadata_matches)} 项相同文档属性{scan_note}；"
            "该维度仅作为辅助排查，不参与相似度分数。",
            [
                {"field": item.get("label") or item.get("field"),
                 "value": _clip(item.get("value"), 100), "strength": item.get("strength")}
                for item in metadata_matches
            ] + scan_evidence,
            [
                "相同办公软件、默认作者、批量转换工具或文件模板均可能产生相同属性。",
                "扫描页比例只反映文本比对覆盖程度，不能单独证明文件存在异常关系。",
            ],
        ))
    return signals


def build_cross_bid_analysis(task_id: str, pairs: list[tuple[dict, dict, dict]], *, tender_loaded: bool) -> dict:
    signals = []
    pair_summaries = []
    document_coverages: dict[str, dict] = {}
    for left, right, result in pairs:
        pair_signals = analyze_pair(task_id, left, right, result, tender_loaded=tender_loaded)
        signals.extend(pair_signals)
        pair_coverage = _pair_text_coverage(result)
        for document, coverage in zip((left, right), pair_coverage["documents"]):
            document_coverages.setdefault(document["document_id"], {
                "document_id": document["document_id"],
                "bidder_name": document.get("bidder_name") or document.get("original_name") or "投标文件",
                **coverage,
            })
        # 纯第一人称改写的共同改动信号仅作展示，不抬高配对复核优先级；
        # 其他维度（含混合实质改动的 tender_common_edit）计数逻辑不变。
        dimensions = sorted({
            item["dimension"] for item in pair_signals
            if not item.get("voice_adaptation_only")
        })
        if len(dimensions) >= 3:
            priority = "high"
        elif len(dimensions) == 2:
            priority = "medium"
        elif dimensions:
            priority = "normal"
        else:
            priority = "none"
        pair_summaries.append({
            "document_a_id": left["document_id"],
            "document_b_id": right["document_id"],
            "bidder_a": left.get("bidder_name") or left.get("original_name"),
            "bidder_b": right.get("bidder_name") or right.get("original_name"),
            "independent_dimension_count": len(dimensions),
            "signal_count": len(pair_signals),
            "dimensions": dimensions,
            "dimension_labels": [DIMENSION_LABELS[item] for item in dimensions],
            "review_priority": priority,
            "assessment_result": "pending_human_review" if pair_signals else "no_signal_detected",
            "text_coverage": pair_coverage,
        })
    pair_summaries.sort(key=lambda item: (-item["independent_dimension_count"], item["bidder_a"] or "", item["bidder_b"] or ""))
    coverage_rank = {"complete": 0, "limited": 1, "severely_limited": 2, "unknown": 3}
    coverage_documents = list(document_coverages.values())
    coverage_status = max(
        (item.get("status") for item in coverage_documents),
        key=lambda value: coverage_rank.get(value, 3),
        default="unknown",
    )
    return {
        "analysis_version": ANALYSIS_VERSION,
        "decision_boundary": DECISION_BOUNDARY,
        "assessment_scope": "collusion_signal_only",
        "statutory_collusion_condition": "not_assessed",
        "methodology": {
            "pairwise": True,
            "text_only": True,
            "tender_source_excluded": bool(tender_loaded),
            "public_template_removed": False,
            "builtin_form_filter_applied": True,
            "template_filter_note": (
                "已排除招标原文直接复制、表格提取伪差异及内置封面、投标函、授权等固定表单；"
                "公开产品彩页或第三方参数库尚无可靠来源库"
                if tender_loaded else
                "未提供招标文件；已应用内置固定表单过滤，但文本线索仍需提高复核谨慎度"
            ),
            "severity_rule": "全部线索固定为 S3（人工核验），多维命中只提高复核优先级，不提高法律定性。",
        },
        "executed_dimensions": [{"dimension": key, "label": label} for key, label in DIMENSION_LABELS.items()],
        "not_executed_dimensions": NOT_EXECUTED_DIMENSIONS,
        "pair_summaries": pair_summaries,
        "text_coverage": {
            "status": coverage_status,
            "documents": coverage_documents,
            "note": "本次仅比较 PDF 可提取文字，不调用 OCR 或图片识别；扫描页中的内容未参与文本查重。",
        },
        "signals": signals,
        "signal_count": len(signals),
    }
