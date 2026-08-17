"""项目级报价与价格分试算。

本模块只读取已解析文字和既有 OCR 缓存，不启动任务、不调用模型，也不读写
综合评审或评分结果。它是文件中心的独立人工试算层。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from dashboard.evaluation_workbench import storage


PRICE_SHEET_VERSION = "price-sheet-v2"

_PRICE_RULE_PATTERN = re.compile(
    r"最低(?:投标)?价|评审价|评标价|基准价|价格分|报价得分|投标报价[^，。；]{0,20}得分"
)
_QUOTE_FIELD_PATTERN = re.compile(r"(?:投标|响应|磋商)?(?:总)?报价(?!\s*(?:表|栏|一览))")
_TOTAL_QUOTE_LABEL_PATTERN = re.compile(r"(?:投标|响应|磋商)?(?:总)?报价|开标一览表")
_AMOUNT_WITH_UNIT_PATTERN = re.compile(
    r"(?:￥|¥|人民币)?\s*[:：]?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>万元|元)"
)
_CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(?:￥|¥|人民币)\s*[:：]?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{4,}(?:\.\d+)?)"
)
_PLAIN_AMOUNT_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?P<amount>\d{5,}(?:\.\d+)?)(?![0-9A-Za-z])")
_IDENTIFIER_PREFIX_PATTERN = re.compile(r"(?:统一社会信用代码|信用代码|项目编号|采购编号|招标编号|合同编号|编号|税号)\s*[:：]?\s*$")
_COMPACT_DATE_PATTERN = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
_YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}$")


def _decimal(value: object, *, allow_zero: bool = False) -> Decimal | None:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        return None
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _plain_amount_is_safe(tail: str, match: re.Match) -> bool:
    """无单位金额仅作保守兜底，排除常见编号和紧凑日期。"""
    raw = match.group("amount")
    if _COMPACT_DATE_PATTERN.fullmatch(raw) or _YEAR_PATTERN.fullmatch(raw):
        return False
    prefix = tail[max(0, match.start() - 36):match.start()]
    return not _IDENTIFIER_PREFIX_PATTERN.search(prefix)


def _amounts_after_quote_label(window: str) -> list[Decimal]:
    quote_field = _QUOTE_FIELD_PATTERN.search(window)
    label = quote_field or _TOTAL_QUOTE_LABEL_PATTERN.search(window)
    if not label:
        return []
    # 只读取标签后的短邻域，避免同一页的预算、日期、税率或分项价混入总报价。
    tail = window[label.end():label.end() + 180]
    values: list[Decimal] = []
    occupied: list[tuple[int, int]] = []
    for match in _AMOUNT_WITH_UNIT_PATTERN.finditer(tail):
        value = _decimal(match.group("amount"))
        if value is None:
            continue
        if match.group("unit") == "万元":
            value *= Decimal("10000")
        values.append(value)
        occupied.append(match.span())
    if values:
        return values
    patterns = [_CURRENCY_AMOUNT_PATTERN]
    # 无单位、无币种符号的长数字只有紧随明确“报价”字段时才采纳；单凭
    # “开标一览表”标题不足以区分报价、项目编号、日期或其他表格数字。
    if quote_field:
        patterns.append(_PLAIN_AMOUNT_PATTERN)
    for pattern in patterns:
        for match in pattern.finditer(tail):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            if pattern is _PLAIN_AMOUNT_PATTERN and not _plain_amount_is_safe(tail, match):
                continue
            value = _decimal(match.group("amount"))
            if value is not None:
                values.append(value)
        if values:
            break
    return values


def _quote_from_lines(lines) -> tuple[Decimal | None, str, str]:
    candidates: list[tuple[Decimal, str]] = []
    buffer: deque[str] = deque(maxlen=3)

    def inspect(values: list[str]) -> None:
        if not values or not _TOTAL_QUOTE_LABEL_PATTERN.search(values[0]):
            return
        window = " ".join(values)
        candidates.extend((amount, window[:260]) for amount in _amounts_after_quote_label(window))

    for raw in lines:
        buffer.append(str(raw or "").strip())
        if len(buffer) == 3:
            inspect(list(buffer))
    while buffer:
        inspect(list(buffer))
        buffer.popleft()
    unique = {value for value, _ in candidates}
    if len(unique) == 1:
        value = next(iter(unique))
        excerpt = next(source for candidate, source in candidates if candidate == value)
        return value, excerpt, "found"
    if len(unique) > 1:
        return None, "识别到多个不同报价金额，请人工核对报价口径。", "ambiguous"
    return None, "未从明确总报价字段附近识别到唯一金额。", "missing"


def _quote_from_document(app, entry: dict) -> tuple[Decimal | None, str, str, str]:
    parsed_path = str(entry.get("parsed_path") or "")
    if entry.get("parse_status") != "success" or not parsed_path or not Path(parsed_path).is_file():
        return None, "文件尚未成功解析。", "unavailable", ""
    with Path(parsed_path).open("r", encoding="utf-8", errors="ignore") as handle:
        value, excerpt, status = _quote_from_lines(handle)
    if value is not None:
        return value, excerpt, status, "parsed_text"
    # 只复用已经存在的 OCR 页缓存；这里绝不触发 RapidOCR、腾讯 OCR 或图片模型。
    cached_pages = storage.list_ocr_cached_page_texts(app, str(entry.get("document_id") or ""))
    if cached_pages:
        cached_lines = (
            line
            for page in cached_pages
            for line in str(page.get("text") or "").splitlines()
        )
        ocr_value, ocr_excerpt, ocr_status = _quote_from_lines(cached_lines)
        if ocr_value is not None:
            return ocr_value, ocr_excerpt, ocr_status, "ocr_cache"
        if status == "missing" and ocr_status == "ambiguous":
            return None, ocr_excerpt, ocr_status, "ocr_cache"
    return None, excerpt, status, "parsed_text"


def _extraction_fingerprint(entry: dict) -> str:
    path = Path(str(entry.get("parsed_path") or ""))
    stat_value = ""
    try:
        stat = path.stat()
        stat_value = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        pass
    raw = f"{PRICE_SHEET_VERSION}|{entry.get('document_sha256') or ''}|{entry.get('parse_status') or ''}|{stat_value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rule_scoring(rule: dict) -> dict:
    try:
        value = json.loads(rule.get("scoring_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _price_formula_kind(rule: dict) -> str | None:
    text = re.sub(r"\s+", "", " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text")))
    lowest_base = re.search(r"最低[一-龥]{0,8}?(?:报价|价格|价).{0,40}(?:为|作为|确定为).{0,24}(?:评标|评审)?基准价", text)
    lowest_ratio = re.search(r"(?:评标|评审)?基准价.{0,20}[／/].{0,20}(?:本|投标人)?(?:投标|响应)?报价", text)
    if lowest_ratio and (lowest_base or re.search(r"价格最低|最低[一-龥]{0,8}?(?:报价|价格|价)", text)):
        return "lowest_ratio"
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


def _price_rules(app, project_id: str) -> tuple[dict | None, list[dict]]:
    rule_set, rules = storage.list_rules(app, project_id)
    values = []
    for rule in rules:
        if not rule.get("enabled") or rule.get("category") != "objective":
            continue
        text = f"{rule.get('title', '')} {rule.get('check_rule', '')} {rule.get('source_text', '')}"
        if str(rule.get("execution_strategy") or "") != "cross_bid" and not _PRICE_RULE_PATTERN.search(text):
            continue
        scoring = _rule_scoring(rule)
        max_score = _decimal(scoring.get("max_score"))
        values.append({
            "rule_id": rule["rule_id"], "title": str(rule.get("title") or "价格评分"),
            "check_rule": str(rule.get("check_rule") or ""),
            "source_text": str(rule.get("source_text") or ""),
            "max_score": float(max_score) if max_score is not None else None,
            "formula_kind": _price_formula_kind(rule), "_rule": rule,
        })
    return rule_set, values


def _formula_label(kind: str | None) -> str:
    return {
        "lowest_ratio": "最低价比例法",
        "average_factor_deviation": "算术平均值偏差扣分法",
    }.get(kind, "暂不支持自动复算的公式")


def _calculate_rule(rule: dict, entries: list[dict]) -> dict:
    kind = rule.get("formula_kind")
    max_score = _decimal(rule.get("max_score"))
    priced = [
        (entry, _decimal(entry.get("calculation_price")))
        for entry in entries if entry.get("included")
    ]
    priced = [(entry, price) for entry, price in priced if price is not None]
    scores: dict[str, dict] = {}
    benchmark: Decimal | None = None
    if kind and max_score is not None and len(priced) >= 2:
        prices = [price for _, price in priced]
        if kind == "lowest_ratio":
            benchmark = min(prices)
            for entry, price in priced:
                score = min(max_score, max(Decimal("0"), (benchmark / price * max_score).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
                scores[entry["price_entry_id"]] = {
                    "score": float(score), "source": "system",
                    "calculation": f"{benchmark}／{price}×{max_score}={score}",
                }
        else:
            text = re.sub(r"\s+", "", " ".join(str(rule["_rule"].get(key) or "") for key in ("title", "check_rule", "source_text")))
            factor_match = re.search(r"(?:算术平均值|平均值).{0,28}(?:[×x*]|的)\s*(0?\.\s*\d+|\d+\s*%)", text)
            high_match = re.search(r"高于.{0,24}基准价.{0,24}(?:每|每高).{0,12}1%?.{0,16}扣\s*(\d+(?:\.\d+)?)\s*分", text)
            low_match = re.search(r"低于.{0,24}基准价.{0,24}(?:每|每低).{0,12}1%?.{0,16}扣\s*(\d+(?:\.\d+)?)\s*分", text)
            raw_factor = factor_match.group(1).replace(" ", "")
            factor = Decimal(raw_factor[:-1]) / Decimal("100") if raw_factor.endswith("%") else Decimal(raw_factor)
            averaged = list(prices)
            if len(prices) >= 5 and re.search(r"(?:去掉|剔除).{0,24}(?:最高|最低).{0,24}(?:20%|百分之二十)", text):
                trim = max(1, int(len(prices) * 0.2))
                if len(prices) > trim * 2:
                    averaged = sorted(prices)[trim:-trim]
            benchmark = sum(averaged) / Decimal(len(averaged)) * factor
            for entry, price in priced:
                delta = abs(price - benchmark) / benchmark * Decimal("100")
                rate = Decimal(high_match.group(1)) if price >= benchmark else Decimal(low_match.group(1))
                score = min(max_score, max(Decimal("0"), (max_score - delta * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
                scores[entry["price_entry_id"]] = {
                    "score": float(score), "source": "system",
                    "calculation": f"计分价{price}，偏离基准价{delta.quantize(Decimal('0.01'))}%，建议{score}分",
                }
    for entry in entries:
        if entry["price_entry_id"] in scores or not entry.get("included"):
            continue
        manual = _decimal((entry.get("manual_scores") or {}).get(rule["rule_id"]), allow_zero=True)
        if manual is not None and max_score is not None and manual <= max_score:
            scores[entry["price_entry_id"]] = {
                "score": float(manual), "source": "manual",
                "calculation": "手工填写的价格分；不参与评标基准价推导。",
            }
    return {
        **{key: value for key, value in rule.items() if key != "_rule"},
        "formula_label": _formula_label(kind), "automatic": bool(kind and max_score is not None),
        "benchmark_price": _decimal_text(benchmark), "priced_participant_count": len(priced),
        "scores": scores,
    }


def _public_entry(entry: dict) -> dict:
    try:
        manual_scores = json.loads(entry.get("manual_scores_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        manual_scores = {}
    if not isinstance(manual_scores, dict):
        manual_scores = {}
    try:
        adjustment = json.loads(entry.get("adjustment_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        adjustment = {}
    if not isinstance(adjustment, dict):
        adjustment = {}
    extracted = _decimal(entry.get("extracted_quote"))
    manual = _decimal(entry.get("manual_quote"))
    effective = manual if manual is not None else extracted
    evaluation = _decimal(entry.get("evaluation_price"))
    return {
        "price_entry_id": entry["price_entry_id"], "document_id": entry.get("document_id"),
        "bidder_name": entry.get("bidder_name") or "未命名投标人", "source_type": entry.get("source_type"),
        "extracted_quote": _decimal_text(extracted), "manual_quote": _decimal_text(manual),
        "effective_quote": _decimal_text(effective), "evaluation_price": _decimal_text(evaluation),
        "calculation_price": _decimal_text(evaluation if evaluation is not None else effective),
        "included": bool(entry.get("included")), "exclusion_reason": entry.get("exclusion_reason") or "",
        "quote_source": entry.get("quote_source") or "", "quote_excerpt": entry.get("quote_excerpt") or "",
        "extraction_status": entry.get("extraction_status") or "pending", "manual_scores": manual_scores,
        "adjustment": adjustment,
    }


def _task_active(app, project_id: str) -> bool:
    return any(item.get("status") in {"queued", "running"} for item in storage.list_tasks(app, project_id))


def _refresh_needed(app, project_id: str, entries: list[dict]) -> bool:
    document_entries = {entry.get("document_id"): entry for entry in entries if entry.get("source_type") == "document"}
    for document in storage.list_documents(app, project_id):
        if document.get("role") != "bid":
            continue
        entry = document_entries.get(document.get("document_id"))
        if not entry or entry.get("extraction_fingerprint") != _extraction_fingerprint({**entry, **document}):
            return True
    return False


def build_price_sheet(app, project_id: str) -> dict:
    """纯读取价格工作表；GET 调用不创建条目、不提取报价、不更新时间戳。"""
    entries = storage.list_price_entries(app, project_id)
    task_active = _task_active(app, project_id)
    needs_refresh = not task_active and _refresh_needed(app, project_id, entries)
    public_entries = [_public_entry(item) for item in entries]
    rule_set, rules = _price_rules(app, project_id)
    calculated_rules = [_calculate_rule(rule, public_entries) for rule in rules]
    for entry in public_entries:
        entry["scores"] = {
            rule["rule_id"]: rule["scores"].get(entry["price_entry_id"])
            for rule in calculated_rules
        }
    return {
        "version": PRICE_SHEET_VERSION,
        "rule_set": {key: rule_set.get(key) for key in ("rule_set_id", "status", "version")}
        if rule_set else None,
        "rules": [{key: value for key, value in rule.items() if key != "scores"} for rule in calculated_rules],
        "entries": public_entries,
        "deferred": task_active,
        "needs_refresh": needs_refresh,
        "notice": (
            "项目任务运行中，报价自动识别已暂缓；当前仅显示缓存和人工数据。"
            if task_active else "价格工作表为独立试算，不覆盖综合评审的 AI 建议得分。"
        ),
    }


def refresh_price_sheet(app, project_id: str, *, force_refresh: bool = False) -> dict:
    """明确刷新时才允许写入台账及报价缓存。"""
    if _task_active(app, project_id):
        return build_price_sheet(app, project_id)
    entries = storage.sync_price_document_entries(app, project_id)
    # 报价试算不属于任务主链。主任务运行时只读已经缓存的价格，不扫描大文件，
    # 避免在 2 核 2 GB 服务器上与规则提取或综合评审争用 CPU、磁盘和 SQLite。
    if _task_active(app, project_id):
        return build_price_sheet(app, project_id)
    for entry in entries:
        if entry.get("source_type") != "document":
            continue
        fingerprint = _extraction_fingerprint(entry)
        if not force_refresh and entry.get("extraction_fingerprint") == fingerprint:
            continue
        value, excerpt, status, source = _quote_from_document(app, entry)
        storage.update_price_entry(app, project_id, entry["price_entry_id"], {
            "extracted_quote": _decimal_text(value), "quote_source": source,
            "quote_excerpt": excerpt[:500], "extraction_status": status,
            "extraction_fingerprint": fingerprint,
        })
    return build_price_sheet(app, project_id)


def _validated_optional_price(value: object, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = _decimal(value)
    if parsed is None:
        raise ValueError(f"{label}必须是大于 0 的数字")
    return _decimal_text(parsed)


def _validated_rate(value: object, label: str) -> Decimal:
    rate = _decimal(value, allow_zero=True)
    if rate is None or rate > Decimal("100"):
        raise ValueError(f"{label}必须是 0 到 100 之间的数字")
    return rate


def _normalise_adjustment(value: object, quote: Decimal | None, evaluation_value: object) -> tuple[str | None, str]:
    """将人工选择的价格调整确定性地换算为最终计分价。"""
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or "none")
    if mode not in {"none", "discount", "tax_excluded", "manual"}:
        raise ValueError("不支持的价格调整方式")
    note = str(raw.get("note") or "").strip()[:300]
    if mode == "none":
        return None, json.dumps({"mode": "none", "note": note}, ensure_ascii=False, separators=(",", ":"))
    if quote is None:
        raise ValueError("使用价格调整前必须先填写确认投标报价")
    if mode == "manual":
        evaluation = _decimal(evaluation_value)
        if evaluation is None:
            raise ValueError("手工计分价必须是大于 0 的数字")
        return _decimal_text(evaluation), json.dumps({"mode": mode, "note": note}, ensure_ascii=False, separators=(",", ":"))
    base = _decimal(raw.get("base_amount")) or quote
    if base > quote:
        raise ValueError("调整基数不能高于确认投标报价")
    rate = _validated_rate(raw.get("rate_percent"), "调整比例或税率")
    if mode == "discount":
        evaluation = quote - base * rate / Decimal("100")
    else:
        if rate == 0:
            raise ValueError("去税换算的税率必须大于 0")
        evaluation = quote - base + base / (Decimal("1") + rate / Decimal("100"))
    if evaluation <= 0:
        raise ValueError("调整后的计分价必须大于 0")
    return _decimal_text(evaluation.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), json.dumps({
        "mode": mode, "base_amount": _decimal_text(base), "rate_percent": _decimal_text(rate), "note": note,
    }, ensure_ascii=False, separators=(",", ":"))


def _updated_manual_scores(app, project_id: str, entry: dict, payload: dict) -> str:
    scores = dict(entry.get("manual_scores") or {})
    rule_id = str(payload.get("manual_score_rule_id") or "").strip()
    if not rule_id:
        return json.dumps(scores, ensure_ascii=False, separators=(",", ":"))
    _, rules = _price_rules(app, project_id)
    rule = next((item for item in rules if item["rule_id"] == rule_id), None)
    maximum = _decimal(rule.get("max_score")) if rule else None
    raw_score = payload.get("manual_score")
    if raw_score is None or str(raw_score).strip() == "":
        scores.pop(rule_id, None)
    else:
        score = _decimal(raw_score, allow_zero=True)
        if score is None or not rule or maximum is None or score > maximum:
            raise ValueError("手工价格分必须在当前规则满分范围内")
        scores[rule_id] = _decimal_text(score)
    return json.dumps(scores, ensure_ascii=False, separators=(",", ":"))


def _batch_entry(app, project_id: str, entry: dict, payload: dict) -> dict:
    manual_quote = _validated_optional_price(payload.get("manual_quote"), "确认投标报价")
    quote = _decimal(manual_quote)
    if quote is None:
        # 清空人工报价即恢复自动报价；不能继续使用保存前的 effective_quote，
        # 否则存在人工报价时会把旧值误作为优惠或去税的计算基数。
        quote = _decimal(entry.get("extracted_quote")) or _decimal(entry.get("effective_quote"))
    evaluation, adjustment_json = _normalise_adjustment(payload.get("adjustment"), quote, payload.get("evaluation_price"))
    return {
        "price_entry_id": entry.get("price_entry_id"),
        "bidder_name": str(payload.get("bidder_name") or entry.get("bidder_name") or "").strip(),
        "manual_quote": manual_quote,
        "evaluation_price": evaluation,
        "included": payload.get("included") is True,
        "exclusion_reason": str(payload.get("exclusion_reason") or "").strip()[:300],
        "manual_scores_json": _updated_manual_scores(app, project_id, entry, payload),
        "adjustment_json": adjustment_json,
    }


def apply_batch(app, project_id: str, payload: dict) -> dict:
    """批量保存人工价格调整并统一重算，避免逐行请求和逐行计算。"""
    if _task_active(app, project_id):
        raise ValueError("项目任务运行中，请等待完成后再保存价格调整")
    current_entries = {item["price_entry_id"]: _public_entry(item) for item in storage.list_price_entries(app, project_id)}
    raw_updates = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    raw_new = payload.get("new_entries") if isinstance(payload.get("new_entries"), list) else []
    deleted = payload.get("delete_manual_entry_ids") if isinstance(payload.get("delete_manual_entry_ids"), list) else []
    updates = []
    for raw in raw_updates:
        if not isinstance(raw, dict):
            raise ValueError("价格修改格式不正确")
        entry = current_entries.get(str(raw.get("price_entry_id") or ""))
        if not entry:
            raise ValueError("价格工作表中的投标人不存在")
        updates.append(_batch_entry(app, project_id, entry, raw))
    new_entries = []
    for raw in raw_new:
        if not isinstance(raw, dict):
            raise ValueError("新增投标人格式不正确")
        new_entries.append(_batch_entry(app, project_id, {"manual_scores": {}}, raw))
    storage.apply_price_entry_batch(
        app, project_id, updates=updates, new_entries=new_entries,
        delete_manual_entry_ids=[str(item) for item in deleted],
    )
    return build_price_sheet(app, project_id)


def update_entry(app, project_id: str, price_entry_id: str, payload: dict) -> dict:
    fields: dict = {}
    if "bidder_name" in payload:
        fields["bidder_name"] = payload.get("bidder_name")
    if "manual_quote" in payload:
        fields["manual_quote"] = _validated_optional_price(payload.get("manual_quote"), "投标报价")
    if "evaluation_price" in payload:
        fields["evaluation_price"] = _validated_optional_price(payload.get("evaluation_price"), "计分价")
    if "included" in payload:
        fields["included"] = 1 if payload.get("included") is True else 0
    if "exclusion_reason" in payload:
        fields["exclusion_reason"] = str(payload.get("exclusion_reason") or "").strip()[:300]
    if "manual_score" in payload or "rule_id" in payload:
        rule_id = str(payload.get("rule_id") or "").strip()
        if not rule_id:
            raise ValueError("填写手工价格分时必须指定价格评分规则")
        entry = storage.get_price_entry(app, project_id, price_entry_id)
        if not entry:
            raise ValueError("价格工作表中的投标人不存在")
        try:
            scores = json.loads(entry.get("manual_scores_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            scores = {}
        if not isinstance(scores, dict):
            scores = {}
        raw_score = payload.get("manual_score")
        if raw_score is None or str(raw_score).strip() == "":
            scores.pop(rule_id, None)
        else:
            score = _decimal(raw_score, allow_zero=True)
            if score is None:
                raise ValueError("手工价格分必须是大于或等于 0 的数字")
            _, rules = _price_rules(app, project_id)
            rule = next((item for item in rules if item["rule_id"] == rule_id), None)
            maximum = _decimal(rule.get("max_score")) if rule else None
            if not rule or maximum is None or score > maximum:
                raise ValueError("手工价格分不能超过当前规则满分")
            scores[rule_id] = _decimal_text(score)
        fields["manual_scores_json"] = json.dumps(scores, ensure_ascii=False, separators=(",", ":"))
    return storage.update_price_entry(app, project_id, price_entry_id, fields)


def add_manual_entry(app, project_id: str, payload: dict) -> dict:
    # 先校验金额，避免创建空白行后才发现输入无效。
    quote = _validated_optional_price(payload.get("manual_quote"), "投标报价")
    evaluation = _validated_optional_price(payload.get("evaluation_price"), "计分价")
    entry = storage.create_manual_price_entry(app, project_id, str(payload.get("bidder_name") or ""))
    fields = {"manual_quote": quote, "evaluation_price": evaluation}
    if "included" in payload:
        fields["included"] = payload.get("included") is True
    if payload.get("manual_score") not in (None, ""):
        fields.update({"manual_score": payload.get("manual_score"), "rule_id": payload.get("rule_id")})
    try:
        return update_entry(app, project_id, entry["price_entry_id"], fields)
    except Exception:
        storage.delete_manual_price_entry(app, project_id, entry["price_entry_id"])
        raise
