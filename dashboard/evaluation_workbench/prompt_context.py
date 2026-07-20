"""低资源的页级上下文与全文覆盖分块。

不依赖常驻索引、向量库或本地模型。兼容单项任务仍可使用页级检索；综合评审
先让 AI 顺序扫描全文分块，再用这里的轻量关键词定位加强二次复核。关键词命中
不再决定某一页是否会被 AI 看到。
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

# 这些词只用于提高二次复核的召回率，不用于作出业务结论。把核心短词放在长句
# 之前，避免旧实现按长度截断后丢失“业绩”“技术方案”等真正出现在投标文件中
# 的章节名称。
DOMAIN_TERM_GROUPS = (
    (("业绩", "类似项目", "项目经验"), ("业绩", "类似项目", "近年的类似项目", "项目情况表", "发包人", "合同价格")),
    (("技术方案", "响应方案", "实施方案", "服务方案"), ("技术方案", "响应方案", "实施方案", "服务方案", "整体实施方案")),
    (("公司名称", "项目名称", "无关公司", "无关项目"), ("公司名称", "项目名称", "供应商", "项目")),
    (("报价", "评审价格", "评标价格"), ("报价", "总报价", "评审价格", "报价表")),
    (("人员", "项目负责人", "团队"), ("人员", "项目负责人", "主要人员", "人员汇总表")),
    (("资质", "证书", "许可证"), ("资质", "证书", "许可证", "营业执照")),
)


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
    priority: list[str] = []
    for triggers, terms in DOMAIN_TERM_GROUPS:
        if any(trigger in raw for trigger in triggers):
            priority.extend(terms)
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
    result: list[str] = []
    for term in priority:
        if term not in GENERIC_TERMS and term not in result:
            result.append(term)
    # 长句有区分度，但不能再挤掉所有短章节词；保留更宽的轻量集合只增加少量
    # 字符串匹配开销，不产生常驻资源。
    for term in sorted(values, key=lambda item: (-len(item), item)):
        if term not in result:
            result.append(term)
        if len(result) >= 32:
            break
    return result


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
    return [page for _, page in scored[:4]]


def split_full_text_chunks(path: str | Path, target_chars: int = 11_000, overlap_pages: int = 1) -> list[dict]:
    """按页顺序覆盖全文；无页码的 DOCX 使用带重叠的字符分块。"""
    full_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    pages = _pages_from_text(full_text)
    chunks: list[dict] = []
    if pages:
        page_items = sorted(pages.items())
        start_index = 0
        while start_index < len(page_items):
            end_index = start_index
            size = 0
            while end_index < len(page_items):
                page_number, page_text = page_items[end_index]
                piece_size = len(page_text) + len(str(page_number)) + 12
                if end_index > start_index and size + piece_size > target_chars:
                    break
                size += piece_size
                end_index += 1
            selected = page_items[start_index:end_index]
            text = "\n\n".join(f"[第{page}页]\n{value}" for page, value in selected)
            chunks.append({
                "chunk_id": f"chunk_{len(chunks) + 1}",
                "start_page": selected[0][0],
                "end_page": selected[-1][0],
                "text": text,
            })
            if end_index >= len(page_items):
                break
            start_index = max(start_index + 1, end_index - max(0, overlap_pages))
        return chunks

    value = full_text.strip()
    if not value:
        return []
    overlap_chars = min(800, max(0, target_chars // 10))
    start = 0
    while start < len(value):
        end = min(len(value), start + target_chars)
        chunks.append({
            "chunk_id": f"chunk_{len(chunks) + 1}",
            "start_page": None,
            "end_page": None,
            "text": value[start:end],
        })
        if end >= len(value):
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def select_rule_chunks(chunks: list[dict], rules: list[dict], per_rule: int = 4) -> list[str]:
    """为全文扫描后的二次复核补充确定性候选块，不作为首轮过滤器。"""
    selected: list[str] = []
    for rule in rules:
        anchors = _anchors(rule)
        scored: list[tuple[int, int, str]] = []
        for index, chunk in enumerate(chunks):
            text = str(chunk.get("text") or "")
            score = sum(min(3, text.count(term)) * max(2, len(term)) ** 2 for term in anchors if term in text)
            if score:
                scored.append((score, index, str(chunk.get("chunk_id"))))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for _, _, chunk_id in scored[:per_rule]:
            if chunk_id and chunk_id not in selected:
                selected.append(chunk_id)
    return selected


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
