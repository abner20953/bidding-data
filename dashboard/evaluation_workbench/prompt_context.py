"""低资源的页级上下文选择。

不依赖常驻索引、向量库或本地模型。检索把握不足时一律回退旧的全文前缀，
避免为了节省 Token 而把缺证据误判成不满足。
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
    raw = f"{rule.get('title', '')} {rule.get('source_text', '')}"
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


def build_rule_context(path: str | Path, rules: list[dict], char_limit: int) -> dict:
    """返回省 Token 的相关页上下文，或严格回退到旧的前缀行为。"""
    full_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    fallback = {"text": full_text[:char_limit], "mode": "full_prefix", "pages": []}
    pages = _pages_from_text(full_text)
    if not pages or not rules:
        return fallback

    selected: set[int] = set()
    for rule in rules:
        candidates = _best_pages(pages, rule)
        # 每条规则都必须至少找到两字符以上的本地线索；否则保留旧行为。
        if not candidates:
            return fallback
        for page in candidates:
            selected.update(candidate for candidate in (page - 1, page, page + 1) if candidate in pages)

    selected_pages = sorted(selected)
    context = "\n\n".join(f"[第{page}页]\n{pages[page]}" for page in selected_pages)
    # 只有比旧上下文至少缩小 30% 才启用，既节省 Token 也避免过度裁剪。
    if not context or len(context) > char_limit * 0.7:
        return fallback
    return {"text": context, "mode": "retrieved_pages", "pages": selected_pages}
