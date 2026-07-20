"""低资源的页级上下文选择。

不依赖常驻索引、向量库或本地模型。普通单项任务在检索把握不足时仍可回退
全文前缀；综合评审的规则组则允许对未命中的规则单独标记为待人工核验，避免
一条规则没有关键词就把整份投标文件重复发送给模型。
"""

from __future__ import annotations

import re
from pathlib import Path


PAGE_MARKER = re.compile(r"\[第(\d+)页\]\s*")
CHINESE_BLOCK = re.compile(r"[\u4e00-\u9fff]{2,}")
ASCII_BLOCK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,}")
GENERIC_TERMS = {
    "投标", "投标人", "投标文件", "招标", "招标文件", "采购人", "供应商", "项目",
    "是否", "符合", "要求", "规定", "审查", "评分", "有效", "相关", "内容", "文件",
}


def _pages_from_text(text: str) -> dict[int, str]:
    markers = list(PAGE_MARKER.finditer(text))
    if len(markers) < 2:
        return {}
    return {
        int(marker.group(1)): text[marker.end(): markers[index + 1].start() if index + 1 < len(markers) else len(text)]
        for index, marker in enumerate(markers)
    }


def _anchors(rule: dict) -> list[str]:
    raw = f"{rule.get('title', '')} {rule.get('check_rule', '')} {rule.get('source_text', '')}"
    values: set[str] = set()
    for block in [*CHINESE_BLOCK.findall(raw), *ASCII_BLOCK.findall(raw)]:
        block = block.strip()
        if len(block) < 2 or block in GENERIC_TERMS:
            continue
        values.add(block[:12])
        # 中文规则在投标文件中经常只出现其中的关键短语，保留有限子串提高召回。
        if any("\u4e00" <= char <= "\u9fff" for char in block):
            for width in (6, 5, 4, 3, 2):
                for start in range(0, min(len(block) - width + 1, 10)):
                    term = block[start:start + width]
                    if term not in GENERIC_TERMS:
                        values.add(term)
    return sorted(values, key=lambda item: (-len(item), item))[:16]


def _best_pages(pages: dict[int, str], rule: dict) -> list[int]:
    anchors = _anchors(rule)
    if not anchors:
        return []
    scored: list[tuple[int, int]] = []
    for page_number, page_text in pages.items():
        score = sum(min(2, page_text.count(term)) * len(term) ** 2 for term in anchors if term in page_text)
        if score:
            scored.append((score, page_number))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [page for _, page in scored[:2]]


def build_rule_context(path: str | Path, rules: list[dict], char_limit: int, *, allow_partial: bool = False) -> dict:
    """返回相关页上下文。

    ``allow_partial`` 仅供规则组调用：无法定位的规则会列入
    ``unmatched_rule_ids``，其余规则继续使用已命中的页面。调用方应把这些规则
    交给模型返回待人工核验，不能据缺失片段作出不满足结论。
    """
    full_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    fallback = {"text": full_text[:char_limit], "mode": "full_prefix", "pages": [], "unmatched_rule_ids": []}
    pages = _pages_from_text(full_text)
    if not pages or not rules:
        return fallback

    selected: set[int] = set()
    unmatched_rule_ids: list[str] = []
    for rule in rules:
        candidates = _best_pages(pages, rule)
        # 普通单项任务保持保守的旧回退；规则组可只让这一条规则人工核验。
        if not candidates:
            if not allow_partial:
                return fallback
            rule_id = str(rule.get("rule_id") or "")
            if rule_id:
                unmatched_rule_ids.append(rule_id)
            continue
        for page in candidates:
            selected.update(candidate for candidate in (page - 1, page, page + 1) if candidate in pages)

    selected_pages = sorted(selected)
    context = "\n\n".join(f"[第{page}页]\n{pages[page]}" for page in selected_pages)
    if allow_partial:
        # 不为未命中的规则回退全文。若全部未命中，留空上下文并由调用方明确要求
        # 返回人工核验，避免把 10 万字符以上的投标文件反复发给模型。
        if not context:
            return {"text": "", "mode": "unmatched_rules", "pages": [], "unmatched_rule_ids": unmatched_rule_ids}
        # 规则组可接受接近上限的相关页，但绝不再换成无关的全文前缀。
        return {"text": context[:char_limit], "mode": "retrieved_pages_partial" if unmatched_rule_ids else "retrieved_pages", "pages": selected_pages, "unmatched_rule_ids": unmatched_rule_ids}
    # 只有比旧上下文至少缩小 30% 才启用，既节省 Token 也避免过度裁剪。
    if not context or len(context) > char_limit * 0.7:
        return fallback
    return {"text": context, "mode": "retrieved_pages", "pages": selected_pages, "unmatched_rule_ids": []}
