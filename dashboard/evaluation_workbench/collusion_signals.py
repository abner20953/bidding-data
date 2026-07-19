"""把两两查重证据整理为审慎、可复核的横向异常线索。

本模块只消费现有 ``CollusionDetector`` 的结果，不自行认定串通投标，也不改变
原有 ``/bijiao`` 比对算法和接口。所有线索均须人工核验。
"""

from __future__ import annotations

import re
import uuid


ANALYSIS_VERSION = "cross-bid-signals-v1"
DECISION_BOUNDARY = (
    "本结果仅表示投标文件之间存在需要复核的横向异常线索，不构成串通投标认定、"
    "法定情形认定、废标依据或自动扣分依据。最终结论须由评标委员会结合原件、"
    "招标规则及调查核验结果作出。"
)

DIMENSION_LABELS = {
    "text_similarity": "正文雷同",
    "text_error": "共同异常或错误",
    "contact": "相同联系方式",
    "person_identity": "相同人员身份信息",
    "tender_common_edit": "招标原文共同改动",
    "metadata": "相同文档属性",
}

NOT_EXECUTED_DIMENSIONS = [
    {"dimension": "price_pattern", "label": "报价规律", "reason": "尚未取得经确认的结构化报价数据，不从正文数字推断"},
    {"dimension": "payment_source", "label": "缴费来源", "reason": "尚未接入可核验的缴费流水或支付来源数据"},
    {"dimension": "performance_reference", "label": "业绩引用关系", "reason": "尚未建立经人工确认的业绩主体与合同结构"},
    {"dimension": "foreign_entity_leak", "label": "他方主体信息残留", "reason": "当前仅比较共同敏感实体，未自动推断实体归属"},
    {"dimension": "interpretation_error", "label": "共同理解偏差", "reason": "需要结合具体招标条款和人工语义判断"},
    {"dimension": "address", "label": "地址关联", "reason": "尚未取得经确认的结构化地址信息"},
]


def _clip(value, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _entity_dimension(value: str) -> str | None:
    normalized = re.sub(r"[\s\-—_]", "", str(value or "")).upper()
    if re.fullmatch(r"(?:\d{15}|\d{17}[0-9X])", normalized):
        return "person_identity"
    if re.fullmatch(r"1[3-9]\d{9}", normalized) or "@" in normalized:
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
    if item.get("shared_edits"):
        evidence["shared_edits"] = item["shared_edits"]
    if item.get("error_kind"):
        evidence["error_kind"] = item["error_kind"]
    return evidence


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
        "human_disposition": "pending",
        "human_note": "",
        "basis": basis,
        "evidence": evidence[:5],
        "counter_evidence": counter_evidence or [],
    }


def analyze_pair(task_id: str, left: dict, right: dict, result: dict, *, tender_loaded: bool = False) -> list[dict]:
    paragraphs = result.get("paragraphs") or []
    signals = []

    text_items = [item for item in paragraphs if item.get("type") in {"text", "fuzzy"}]
    if text_items:
        exact_count = sum(item.get("type") == "text" for item in text_items)
        signals.append(_signal(
            task_id, left, right, "text_similarity", "C3" if exact_count else "C2",
            f"发现 {exact_count} 处完全雷同、{len(text_items) - exact_count} 处近似雷同；"
            + ("已由底层算法排除招标原文直接复制内容。" if tender_loaded else "未提供招标文件，尚未完成招标原文排除。"),
            [_page_evidence(item) for item in text_items],
            ["常见行业表述、法定格式或未提供的公共模板仍可能形成相似文本，需结合完整上下文复核。"],
        ))

    error_items = [item for item in paragraphs if item.get("type") in {"shared_error", "rare_word"}]
    if error_items:
        signals.append(_signal(
            task_id, left, right, "text_error", "C3" if any(item.get("type") == "shared_error" for item in error_items) else "C2",
            f"发现 {len(error_items)} 处共同的高置信异常、错误或罕见表述。",
            [_page_evidence(item) for item in error_items],
            ["同一资料来源、行业惯用文本或共同第三方模板也可能产生相同错误。"],
        ))

    entity_groups = {"contact": [], "person_identity": []}
    for item in paragraphs:
        if item.get("type") != "entity":
            continue
        dimension = _entity_dimension(item.get("text_a", ""))
        if dimension:
            entity_groups[dimension].append(item)
    for dimension, items in entity_groups.items():
        if items:
            signals.append(_signal(
                task_id, left, right, dimension, "C3",
                f"发现 {len(items)} 项两份文件共有且未在招标文件中出现的敏感实体。",
                [_page_evidence(item) for item in items],
                ["须核对该实体是否属于依法共享的联系人、联合体成员、公共服务机构或同一授权主体。"],
            ))

    edit_items = [item for item in paragraphs if item.get("type") == "tender_related" and item.get("shared_edits")]
    if edit_items:
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
        signals.append(_signal(
            task_id, left, right, "metadata", "C2" if strong else "C1",
            f"发现 {len(metadata_matches)} 项相同文档属性；该维度仅作为辅助排查，不参与相似度分数。",
            [{"field": item.get("label") or item.get("field"), "value": _clip(item.get("value"), 100), "strength": item.get("strength")} for item in metadata_matches],
            ["相同办公软件、默认作者、批量转换工具或文件模板均可能产生相同属性。"],
        ))
    return signals


def build_cross_bid_analysis(task_id: str, pairs: list[tuple[dict, dict, dict]], *, tender_loaded: bool) -> dict:
    signals = []
    pair_summaries = []
    for left, right, result in pairs:
        pair_signals = analyze_pair(task_id, left, right, result, tender_loaded=tender_loaded)
        signals.extend(pair_signals)
        dimensions = sorted({item["dimension"] for item in pair_signals})
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
        })
    pair_summaries.sort(key=lambda item: (-item["independent_dimension_count"], item["bidder_a"] or "", item["bidder_b"] or ""))
    return {
        "analysis_version": ANALYSIS_VERSION,
        "decision_boundary": DECISION_BOUNDARY,
        "assessment_scope": "collusion_signal_only",
        "statutory_collusion_condition": "not_assessed",
        "methodology": {
            "pairwise": True,
            "tender_source_excluded": bool(tender_loaded),
            "public_template_removed": False,
            "template_filter_note": (
                "已加载招标文件并排除其直接复制内容；尚未配置经人工确认的其他公共模板库"
                if tender_loaded else
                "未提供招标文件且尚未配置经人工确认的公共模板库；文本线索需提高复核谨慎度"
            ),
            "severity_rule": "全部线索固定为 S3（人工核验），多维命中只提高复核优先级，不提高法律定性。",
        },
        "executed_dimensions": [{"dimension": key, "label": label} for key, label in DIMENSION_LABELS.items()],
        "not_executed_dimensions": NOT_EXECUTED_DIMENSIONS,
        "pair_summaries": pair_summaries,
        "signals": signals,
        "signal_count": len(signals),
    }
