"""项目级报价台账与价格分计算输入。

报价识别始终只读取已解析文字和既有 OCR 缓存；价格分由独立后台 AI 任务计算，
本模块仅负责整理输入、校验可确定的算式和展示已验证的任务结果。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from dashboard.evaluation_workbench import storage


# 报价定位算法变更时递增此版本，使已落库的旧识别结果自动进入刷新判定，
# 避免历史项目继续沿用旧定位器留下的空报价或误识别结果。
PRICE_SHEET_VERSION = "price-sheet-v4"

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


def _amounts_near_quote_label(window: str) -> list[Decimal]:
    """从明确报价字段的最近邻域提取金额。

    常见投标函会写成“人民币 X 元的投标总报价”，金额位于字段之前；报价表又常把
    金额放在字段之后。两种版式都只接受字段前后有限邻域中最近的金额，避免把同页
    的年份、税率、编号或分项金额混入。
    """
    quote_field = _QUOTE_FIELD_PATTERN.search(window)
    label = quote_field or _TOTAL_QUOTE_LABEL_PATTERN.search(window)
    if not label:
        return []
    start = max(0, label.start() - (180 if quote_field else 0))
    end = min(len(window), label.end() + 180)
    context = window[start:end]
    label_start, label_end = label.start() - start, label.end() - start
    candidates: list[tuple[int, Decimal]] = []

    def distance(match: re.Match) -> int:
        if match.end() <= label_start:
            return label_start - match.end()
        if match.start() >= label_end:
            return match.start() - label_end
        return 0

    def collect(pattern: re.Pattern, *, allow_plain: bool = False) -> None:
        for match in pattern.finditer(context):
            if allow_plain and not _plain_amount_is_safe(context, match):
                continue
            value = _decimal(match.group("amount"))
            if value is None:
                continue
            if pattern is _AMOUNT_WITH_UNIT_PATTERN and match.group("unit") == "万元":
                value *= Decimal("10000")
            candidates.append((distance(match), value))

    collect(_AMOUNT_WITH_UNIT_PATTERN)
    collect(_CURRENCY_AMOUNT_PATTERN)
    # 无单位数字只能紧邻明确报价字段采纳；“开标一览表”标题本身不足以判定其后的
    # 数字就是总报价。
    if quote_field:
        collect(_PLAIN_AMOUNT_PATTERN, allow_plain=True)
    if not candidates:
        return []
    nearest = min(item[0] for item in candidates)
    return [value for item_distance, value in candidates if item_distance == nearest]


def _quote_from_lines(lines) -> tuple[Decimal | None, str, str]:
    candidates: list[tuple[Decimal, str]] = []
    buffer: deque[str] = deque(maxlen=3)

    def inspect(values: list[str]) -> None:
        if not values or not _TOTAL_QUOTE_LABEL_PATTERN.search(values[0]):
            return
        window = " ".join(values)
        candidates.extend((amount, window[:260]) for amount in _amounts_near_quote_label(window))

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
    direct = rule.get("scoring") if isinstance(rule, dict) else None
    if isinstance(direct, dict):
        return direct
    try:
        value = json.loads(rule.get("scoring_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _formula_text(rule: dict) -> str:
    return re.sub(r"\s+", "", " ".join(
        str(rule.get(key) or "") for key in ("title", "check_rule", "source_text")
    ))


def _decimal_factor(value: str) -> Decimal | None:
    raw = str(value or "").replace(" ", "")
    try:
        factor = Decimal(raw[:-1]) / Decimal("100") if raw.endswith("%") else Decimal(raw)
    except InvalidOperation:
        return None
    return factor if Decimal("0") < factor <= Decimal("1") else None


def _compile_price_formula(rule: dict) -> dict:
    """只编译可由确定性算式完整表达的价格规则。

    价格规则文字通常同时描述基准价、修正范围和扣分方向。任何一个要素不明确时，
    宁可回退为人工计分，也不能把“去高低价后取均值”错误当作最低价比例法。
    """
    text = _formula_text(rule)
    max_score = _decimal(_rule_scoring(rule).get("max_score"))
    if max_score is None:
        return {"kind": None, "reason": "未识别到有效满分"}

    average_terms = bool(re.search(r"(?:算术)?平均(?:值|价)?", text))
    deviation_terms = bool(re.search(r"(?:高于|低于|偏离).{0,28}基准价|基准价.{0,28}(?:高于|低于|偏离|扣)", text))
    lowest_base = re.search(
        r"(?:最低(?:有效)?|价格最低的?)[一-龥0-9（）()、，,]{0,18}(?:投标|响应)?(?:报价|价格|价)"
        r".{0,56}(?:为|作为|确定为).{0,24}(?:评标|评审)?基准价",
        text,
    )
    lowest_ratio = re.search(
        r"(?:评标|评审)?基准价[^。；;]{0,42}[／/]\s*(?:本|投标人)?(?:投标|响应)?报价",
        text,
    )
    # 最低价比例法与平均值偏差法的口径互斥。出现均值、偏离或扣分结构时，
    # 不允许以某个“最低报价”描述误判为最低价比例法。
    if lowest_base and lowest_ratio and not average_terms and not deviation_terms:
        return {"kind": "lowest_ratio", "max_score": max_score}

    average_base = re.search(
        r"(?:算术)?平均(?:值|价)?.{0,48}(?:为|作为|确定为).{0,24}(?:评标|评审)?基准价"
        # “基准价的计算方法”后的去高低价、取整数和适用家数说明常很长，不能用
        # 普通的短邻域误判为缺公式。这里保留明确的“基准价计算方法→算术平均”
        # 结构和单句边界，不以单纯扩大任意关键词距离来放宽识别。
        r"|(?:评标|评审)?基准价(?:的)?计算方法[:：]?[^。；;]{0,220}?(?:算术)?平均(?:值|价)?"
        r"|(?:评标|评审)?基准价.{0,48}(?:算术)?平均(?:值|价)?",
        text,
    )
    factor_match = re.search(r"(?:算术平均值|平均值|平均价).{0,28}(?:[×x*]|的)\s*(0?\.\s*\d+|\d+\s*%)", text)
    factor = _decimal_factor(factor_match.group(1)) if factor_match else Decimal("1")
    # 不跨句寻找高低偏差项，且“每高/每低”必须与对应方向绑定；否则一条规则里
    # 的低于基准价扣分可能被贪婪地误读为高于基准价扣分。
    # 评分表既会写“高于/低于基准价”，也会先写“报价比基准价每增加/减少”。
    # 两者都是明确的价格偏离方向；仍限定在同一句的“基准价 + 每 1% + 扣分”结构，
    # 不把普通数量递增条款误当作价格公式。
    high = re.search(
        r"(?:高于[^。；;]{0,28}?基准价[^。；;]{0,28}?每(?:高于|高)?\s*1%?"
        r"|(?:报价|价格|价)[^。；;]{0,32}?基准价[^。；;]{0,32}?每(?:增加|高于|高)\s*1%?)"
        r".{0,18}?扣\s*(\d+(?:\.\d+)?)\s*分",
        text,
    )
    low = re.search(
        r"(?:低于[^。；;]{0,28}?基准价[^。；;]{0,28}?每(?:低于|低)?\s*1%?"
        r"|(?:报价|价格|价)[^。；;]{0,32}?基准价[^。；;]{0,32}?每(?:减少|低于|低)\s*1%?)"
        r".{0,18}?扣\s*(\d+(?:\.\d+)?)\s*分",
        text,
    )
    if not (average_terms and average_base and factor and high and low):
        return {"kind": None, "reason": "公式要素不完整，需手工计分"}

    try:
        high_rate, low_rate = Decimal(high.group(1)), Decimal(low.group(1))
    except InvalidOperation:
        return {"kind": None, "reason": "偏差扣分比例无法确定，需手工计分"}
    if high_rate < 0 or low_rate < 0:
        return {"kind": None, "reason": "偏差扣分比例无效，需手工计分"}

    trim_mode = "none"
    if re.search(r"(?:去掉|剔除).{0,28}(?:最高|最低).{0,28}(?:20%|百分之二十)", text):
        trim_mode = "percent_20"
    elif re.search(r"(?:去掉|剔除).{0,20}(?:一个|1个)?最高.{0,20}(?:一个|1个)?最低", text):
        trim_mode = "one_each"
    return {
        "kind": "average_factor_deviation", "max_score": max_score, "factor": factor,
        "high_rate": high_rate, "low_rate": low_rate, "trim_mode": trim_mode,
    }


def _price_formula_kind(rule: dict) -> str | None:
    """保留旧内部函数名，避免已有调用方依赖；实际逻辑统一由编译器承担。"""
    return _compile_price_formula(rule).get("kind")


def is_price_scoring_rule(rule: dict) -> bool:
    """判断是否应由独立报价工作表负责的价格评分规则。"""
    if str(rule.get("category") or "") != "objective":
        return False
    text = " ".join(str(rule.get(key) or "") for key in ("title", "check_rule", "source_text"))
    return str(rule.get("execution_strategy") or "") == "cross_bid" or bool(_PRICE_RULE_PATTERN.search(text))


def _price_rules(app, project_id: str) -> tuple[dict | None, list[dict]]:
    rule_set, rules = storage.list_rules(app, project_id)
    dedicated = storage.current_price_rule_set(app, project_id)
    # 用户在价格页主动提取的规则仅服务价格试算；若它晚于完整规则集，就优先采用，
    # 既不会污染综合评审，也不会让完整规则集的旧价格条款覆盖用户的明确操作。
    if dedicated and (not rule_set or str(dedicated.get("updated_at") or "") >= str(rule_set.get("updated_at") or "")):
        rule_set = {
            "rule_set_id": dedicated.get("price_rule_set_id"), "status": "price_only",
            "version": "独立价格规则", "source": "price_only",
        }
        rules = dedicated.get("rules") or []
    values = []
    for rule in rules:
        if rule.get("enabled") is False or not is_price_scoring_rule(rule):
            continue
        scoring = _rule_scoring(rule)
        max_score = _decimal(scoring.get("max_score"))
        formula = _compile_price_formula(rule)
        values.append({
            "rule_id": rule["rule_id"], "title": str(rule.get("title") or "价格评分"),
            "check_rule": str(rule.get("check_rule") or ""),
            "source_text": str(rule.get("source_text") or ""),
            "max_score": float(max_score) if max_score is not None else None,
            "formula_kind": formula.get("kind"), "formula_reason": formula.get("reason") or "",
            "_formula": formula, "_rule": rule,
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
    included_entries = [entry for entry in entries if entry.get("included")]
    priced = [(entry, _decimal(entry.get("calculation_price"))) for entry in included_entries]
    missing_entries = [entry for entry, price in priced if price is None]
    priced = [(entry, price) for entry, price in priced if price is not None]
    scores: dict[str, dict] = {}
    benchmark: Decimal | None = None
    calculation_ready = bool(kind and max_score is not None and not missing_entries and len(priced) >= 2)
    if calculation_ready:
        prices = [price for _, price in priced]
        if kind == "lowest_ratio":
            benchmark = min(prices)
            for entry, price in priced:
                score = min(max_score, max(Decimal("0"), (benchmark / price * max_score).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
                scores[entry["price_entry_id"]] = {
                    "score": float(score), "source": "system",
                    "calculation": f"{benchmark}／{price}×{max_score}={score}",
                }
        elif kind == "average_factor_deviation":
            formula = rule.get("_formula") or {}
            factor = formula["factor"]
            averaged = list(prices)
            if len(prices) >= 5 and formula.get("trim_mode") == "percent_20":
                trim = max(1, int((Decimal(len(prices)) * Decimal("0.2")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
                if len(prices) > trim * 2:
                    averaged = sorted(prices)[trim:-trim]
            elif len(prices) > 2 and formula.get("trim_mode") == "one_each":
                averaged = sorted(prices)[1:-1]
            benchmark = sum(averaged) / Decimal(len(averaged)) * factor
            for entry, price in priced:
                delta = abs(price - benchmark) / benchmark * Decimal("100")
                rate = formula["high_rate"] if price >= benchmark else formula["low_rate"]
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
        **{key: value for key, value in rule.items() if key not in {"_rule", "_formula"}},
        "formula_label": _formula_label(kind), "automatic": bool(kind and max_score is not None),
        "benchmark_price": _decimal_text(benchmark), "priced_participant_count": len(priced),
        "included_participant_count": len(included_entries), "calculation_ready": calculation_ready,
        "missing_calculation_entries": [
            {"price_entry_id": entry["price_entry_id"], "bidder_name": entry["bidder_name"]}
            for entry in missing_entries
        ],
        "calculation_block_reason": (
            "存在参与计算但未填写计分价的投标人，不能使用不完整报价自动计分。"
            if kind and max_score is not None and missing_entries else
            ("至少需要两家参与且计分价完整的投标人，才能自动计分。"
             if kind and max_score is not None and len(priced) < 2 else "")
        ),
        "scores": scores,
    }


def price_calculation_input(app, project_id: str) -> dict:
    """构造稳定、紧凑的 AI 价格计算输入；不含投标文件全文。"""
    rule_set, rules = _price_rules(app, project_id)
    entries = [_public_entry(item) for item in storage.list_price_entries(app, project_id)]
    payload_rules = [{
        "rule_id": rule["rule_id"], "title": rule["title"], "check_rule": rule["check_rule"],
        "source_text": rule["source_text"], "max_score": rule["max_score"],
    } for rule in rules]
    payload_entries = [{
        "price_entry_id": entry["price_entry_id"], "bidder_name": entry["bidder_name"],
        "included": bool(entry["included"]), "calculation_price": entry["calculation_price"],
        "adjustment": entry.get("adjustment") or {}, "exclusion_reason": entry.get("exclusion_reason") or "",
    } for entry in entries]
    stable = {"rule_set_id": (rule_set or {}).get("rule_set_id"), "rules": payload_rules, "entries": payload_entries}
    fingerprint = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**stable, "fingerprint": fingerprint, "public_entries": entries, "internal_rules": rules}


def normalise_ai_price_calculation(raw: object, calculation_input: dict) -> tuple[dict, list[str]]:
    """校验 AI 计算结果的身份、覆盖与自身数值一致性。

    不以本地公式替代或否决模型对复杂取整、分支条件的业务解释；但当输入价格完整时，
    模型必须给出可展示的建议分，并保证其 JSON 中的最终分与自身申明的最终分一致。
    """
    value = raw if isinstance(raw, dict) else {}
    input_rules = {item["rule_id"]: item for item in calculation_input.get("rules", []) if isinstance(item, dict)}
    entries = {item["price_entry_id"]: item for item in calculation_input.get("entries", []) if isinstance(item, dict)}
    expected_entries = {key for key, item in entries.items() if item.get("included")}
    normalised: dict[str, dict] = {}
    errors: list[str] = []
    raw_rules = value.get("rules") if isinstance(value.get("rules"), list) else []
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("rule_id") or "")
        rule = input_rules.get(rule_id)
        if not rule or rule_id in normalised:
            errors.append("返回了未知或重复的价格规则")
            continue
        max_score = _decimal(rule.get("max_score"), allow_zero=True)
        requires_numeric_score = bool(
            expected_entries
            and max_score is not None
            and all(_decimal(entries[entry_id].get("calculation_price")) is not None for entry_id in expected_entries)
        )
        rows: dict[str, dict] = {}
        raw_results = item.get("results") if isinstance(item.get("results"), list) else []
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            entry_id = str(result.get("price_entry_id") or "")
            if entry_id not in expected_entries or entry_id in rows:
                errors.append("返回了未知、未参与或重复的投标人")
                continue
            score = _decimal(result.get("score"), allow_zero=True)
            final_score = _decimal(result.get("final_score"), allow_zero=True)
            if score is not None and max_score is not None and score > max_score:
                errors.append("建议价格分超过规则满分")
                continue
            if requires_numeric_score and score is None:
                errors.append("计分输入完整时不得留空 AI 建议分")
                continue
            if score is not None and final_score is None:
                errors.append("AI 价格分结果缺少最终分校验字段")
                continue
            if score is not None and final_score != score:
                errors.append("AI 建议分与其申明的最终分不一致")
                continue
            if score is None and final_score is not None:
                errors.append("AI 最终分与建议分字段不一致")
                continue
            rows[entry_id] = {
                "score": float(score) if score is not None else None,
                "source": "ai", "calculation": str(result.get("calculation") or "").strip()[:500],
                "reason": str(result.get("reason") or "").strip()[:500],
            }
        if set(rows) != expected_entries:
            errors.append("价格计算未覆盖全部参与计算的投标人")
        normalised[rule_id] = {
            "benchmark_price": _decimal_text(_decimal(item.get("benchmark_price"), allow_zero=True)),
            "status": str(item.get("status") or "needs_review"),
            "reason": str(item.get("reason") or "").strip()[:500], "scores": rows,
        }
    if set(normalised) != set(input_rules):
        errors.append("价格计算未覆盖全部价格评分规则")
    return {"rules": normalised}, list(dict.fromkeys(errors))


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


def _task_active(app, project_id: str, *, ignore_task_id: str | None = None) -> bool:
    return any(
        item.get("status") in {"queued", "running"} and item.get("task_id") != ignore_task_id
        for item in storage.list_tasks(app, project_id)
    )


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
    calculation_input = price_calculation_input(app, project_id)
    public_entries = calculation_input["public_entries"]
    rule_set, rules = _price_rules(app, project_id)
    stored_run = storage.current_price_score_run(app, project_id, calculation_input["fingerprint"])
    ai_rules = (stored_run or {}).get("result", {}).get("rules", {})
    calculated_rules = []
    for rule in rules:
        local = _calculate_rule(rule, public_entries)
        ai = ai_rules.get(rule["rule_id"], {}) if isinstance(ai_rules, dict) else {}
        # 已发布的旧接口仍可能保存人工分；页面不再提供该入口，但历史记录保持可读。
        # 本地可计算公式仅用于校验 AI，不作为新的最终展示分。
        local["scores"] = ai.get("scores", {}) if ai else {
            entry_id: score for entry_id, score in local["scores"].items() if score.get("source") == "manual"
        }
        local["benchmark_price"] = ai.get("benchmark_price") if ai else None
        local["calculation_ready"] = bool(ai) and not task_active
        local["calculation_status"] = ai.get("status", "待 AI 计算") if ai else "待 AI 计算"
        local["calculation_reason"] = ai.get("reason", "") if ai else ""
        calculated_rules.append(local)
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
        "calculation_ready": bool(stored_run),
        "calculation_input_fingerprint": calculation_input["fingerprint"],
        "calculation_profile_id": (stored_run or {}).get("profile_id"),
        "notice": (
            "项目任务运行中，报价自动识别已暂缓；当前仅显示缓存和人工数据。"
            if task_active else "报价由本地文字识别；价格分由所选 AI 模型计算，程序只校验可确定的公式。"
        ),
    }


def refresh_price_sheet(app, project_id: str, *, force_refresh: bool = False,
                        ignore_task_id: str | None = None) -> dict:
    """明确刷新时才允许写入台账及报价缓存。"""
    if _task_active(app, project_id, ignore_task_id=ignore_task_id):
        return build_price_sheet(app, project_id)
    entries = storage.sync_price_document_entries(app, project_id)
    # 报价试算不属于任务主链。主任务运行时只读已经缓存的价格，不扫描大文件，
    # 避免在 2 核 2 GB 服务器上与规则提取或综合评审争用 CPU、磁盘和 SQLite。
    if _task_active(app, project_id, ignore_task_id=ignore_task_id):
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
