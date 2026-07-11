import gzip
import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import fitz  # PyMuPDF
import jieba


ALGORITHM_VERSION = 2
MIN_EXACT_LENGTH = 9
MIN_FUZZY_LENGTH = 20
MAX_UNIT_LENGTH = 220
UNIT_OVERLAP = 40
SHINGLE_SIZE = 5
MAX_POSTINGS_PER_SHINGLE = 80
MAX_CANDIDATES_PER_UNIT = 8
MAX_FUZZY_RESULTS = 200
MAX_PDF_PAGES = 1200
MAX_EXTRACTED_CHARS = 8_000_000
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "bijiao_cache")
)

_jieba_initialized = False


class ComparisonLimitError(ValueError):
    pass


def _ensure_jieba():
    global _jieba_initialized
    if not _jieba_initialized:
        jieba.initialize()
        _jieba_initialized = True


def _detect_rare_words(text):
    """Return 2-3 character Chinese words missing from jieba's common dictionary."""
    _ensure_jieba()
    if not text or len(text) < 2:
        return []

    rare_words = []
    pos = 0
    for word in jieba.cut(text):
        word_length = len(word)
        if (
            word_length in (2, 3)
            and all("\u4e00" <= char <= "\u9fff" for char in word)
            and word not in jieba.dt.FREQ
        ):
            rare_words.append((word, pos))
        pos += word_length
    return rare_words


class CollusionDetector:
    def __init__(self, tender_path=None):
        self.tender_path = tender_path
        self.tender_sentences = set()
        self.tender_skeletons = set()
        self.tender_full_text = ""
        self.tender_entities = set()
        self.tender_pages = []
        self.tender_metadata = {}
        self.tender_stats = {}
        self.tender_units = []
        self.tender_unit_index = None
        if tender_path and os.path.exists(tender_path):
            self.load_tender()

    @staticmethod
    def normalize(text):
        """Normalize formatting differences without discarding meaningful numbers."""
        if not text:
            return ""
        text = unicodedata.normalize("NFKC", text)
        translations = str.maketrans(
            {
                "，": ",",
                "。": ".",
                "：": ":",
                "；": ";",
                "！": "!",
                "？": "?",
                "“": '"',
                "”": '"',
                "‘": "'",
                "’": "'",
            }
        )
        return re.sub(r"\s+", "", text.translate(translations)).lower()

    @staticmethod
    def get_skeleton(text):
        return re.sub(r"[^\u4e00-\u9fff]", "", text or "")

    @staticmethod
    def _cache_key(pdf_path):
        stat = os.stat(pdf_path)
        identity = f"{os.path.realpath(pdf_path)}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha256(identity.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _prune_cache():
        try:
            files = []
            total_size = 0
            with os.scandir(CACHE_DIR) as entries:
                for entry in entries:
                    if not entry.is_file() or not entry.name.endswith(".json.gz"):
                        continue
                    stat = entry.stat()
                    total_size += stat.st_size
                    files.append((stat.st_atime, stat.st_size, entry.path))
            if total_size <= CACHE_MAX_BYTES:
                return
            files.sort()
            target_size = int(CACHE_MAX_BYTES * 0.8)
            for _, size, path in files:
                if total_size <= target_size:
                    break
                try:
                    os.remove(path)
                    total_size -= size
                except OSError:
                    continue
        except OSError:
            pass

    def _read_cache(self, pdf_path):
        try:
            cache_path = os.path.join(CACHE_DIR, f"{self._cache_key(pdf_path)}.json.gz")
            if not os.path.exists(cache_path):
                return None
            with gzip.open(cache_path, "rt", encoding="utf-8") as cache_file:
                cached = json.load(cache_file)
            if cached.get("version") != ALGORITHM_VERSION:
                return None
            os.utime(cache_path, None)
            pages = [tuple(page) for page in cached["pages"]]
            return cached["full_text"], pages, cached["metadata"], cached["stats"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_cache(self, pdf_path, full_text, pages, metadata, stats):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            cache_path = os.path.join(CACHE_DIR, f"{self._cache_key(pdf_path)}.json.gz")
            temp_path = f"{cache_path}.{os.getpid()}.tmp"
            payload = {
                "version": ALGORITHM_VERSION,
                "full_text": full_text,
                "pages": pages,
                "metadata": metadata,
                "stats": stats,
            }
            with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=5) as cache_file:
                json.dump(payload, cache_file, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, cache_path)
            self._prune_cache()
        except OSError:
            pass

    def extract_text_with_pages(self, pdf_path):
        """Extract text, page mapping, metadata and per-page readability statistics."""
        cached = self._read_cache(pdf_path)
        if cached is not None:
            return cached

        pages = []
        text_parts = []
        metadata = {}
        page_chinese_counts = []
        extracted_chars = 0

        try:
            with fitz.open(pdf_path) as document:
                if document.page_count > MAX_PDF_PAGES:
                    raise ComparisonLimitError(
                        f"PDF 页数为 {document.page_count}，超过单文件 {MAX_PDF_PAGES} 页限制"
                    )
                if not document.is_pdf:
                    raise ValueError("上传文件不是有效的 PDF")
                metadata = dict(document.metadata or {})
                for page in document:
                    raw_text = page.get_text("text", sort=True) or ""
                    extracted_chars += len(raw_text)
                    if extracted_chars > MAX_EXTRACTED_CHARS:
                        raise ComparisonLimitError(
                            f"PDF 可提取文本超过 {MAX_EXTRACTED_CHARS:,} 字符限制"
                        )
                    normalized = self.normalize(raw_text)
                    pages.append((page.number + 1, raw_text, normalized))
                    text_parts.append(raw_text)
                    page_chinese_counts.append(
                        len(re.sub(r"[^\u4e00-\u9fff]", "", raw_text))
                    )
        except ComparisonLimitError:
            raise
        except Exception as exc:
            raise ValueError(f"PDF 文本提取失败: {exc}") from exc

        full_text = "".join(text_parts)
        low_text_pages = [
            index + 1 for index, count in enumerate(page_chinese_counts) if count < 30
        ]
        total_pages = len(pages)
        stats = {
            "total_pages": total_pages,
            "readable_pages": total_pages - len(low_text_pages),
            "suspected_scan_pages": len(low_text_pages),
            "suspected_scan_page_numbers": low_text_pages[:100],
            "scan_ratio": round(len(low_text_pages) / total_pages, 4) if total_pages else 0,
            "chinese_chars": sum(page_chinese_counts),
        }
        self._write_cache(pdf_path, full_text, pages, metadata, stats)
        return full_text, pages, metadata, stats

    def load_tender(self):
        text, pages, metadata, stats = self.extract_text_with_pages(self.tender_path)
        self.tender_full_text = self.normalize(text)
        self.tender_pages = pages
        self.tender_metadata = metadata
        self.tender_stats = stats

        for _, raw_text, _ in pages:
            self.tender_entities.update(self.extract_entities(raw_text))

        self.tender_sentences = set(self.get_sentences(text))
        self.tender_skeletons = {
            skeleton
            for sentence in self.tender_sentences
            if len((skeleton := self.get_skeleton(sentence))) > 1
        }
        self.tender_units = self.get_comparison_units(pages)
        self.tender_unit_index = self._build_unit_index(self.tender_units)

    def get_sentences(self, text):
        """Preserve the existing exact-match segmentation for API compatibility."""
        sentences = []
        for line in (text or "").split("\n"):
            for part in re.split(r"[。.!！?？;；,，]", line):
                part = part.strip()
                if len(part) >= MIN_EXACT_LENGTH:
                    sentences.append(self.normalize(part))
        return sentences

    def _repeated_page_lines(self, pages):
        if len(pages) < 3:
            return set()
        line_pages = Counter()
        for _, raw_text, _ in pages:
            unique_lines = {
                self.normalize(line)
                for line in raw_text.splitlines()
                if 4 <= len(self.normalize(line)) <= 60
            }
            line_pages.update(unique_lines)
        threshold = max(3, int(len(pages) * 0.6 + 0.5))
        return {line for line, count in line_pages.items() if count >= threshold}

    def get_comparison_units(self, pages):
        """Build page-aware units for fuzzy matching while suppressing repeated headers."""
        repeated_lines = self._repeated_page_lines(pages)
        units = []
        seen = set()

        for page_number, raw_text, _ in pages:
            kept_lines = []
            for line in raw_text.splitlines():
                normalized_line = self.normalize(line)
                if normalized_line and normalized_line not in repeated_lines:
                    kept_lines.append(line.strip())
            page_text = "".join(kept_lines)
            for part in re.split(r"[。.!！?？;；]+", page_text):
                normalized = self.normalize(part)
                if len(normalized) < MIN_FUZZY_LENGTH:
                    continue
                if len(normalized) <= MAX_UNIT_LENGTH:
                    chunks = [normalized]
                else:
                    step = MAX_UNIT_LENGTH - UNIT_OVERLAP
                    chunks = [
                        normalized[start : start + MAX_UNIT_LENGTH]
                        for start in range(0, len(normalized), step)
                        if len(normalized[start : start + MAX_UNIT_LENGTH]) >= MIN_FUZZY_LENGTH
                    ]
                for chunk in chunks:
                    key = (page_number, chunk)
                    if key not in seen:
                        units.append({"text": chunk, "page": page_number})
                        seen.add(key)
        return units

    @staticmethod
    def _shingles(text):
        if len(text) < SHINGLE_SIZE:
            return {text} if text else set()
        return {text[index : index + SHINGLE_SIZE] for index in range(len(text) - SHINGLE_SIZE + 1)}

    def _build_unit_index(self, units):
        signatures = []
        postings = defaultdict(list)
        for index, unit in enumerate(units):
            signature = self._shingles(unit["text"])
            signatures.append(signature)
            for shingle in signature:
                postings[shingle].append(index)
        return {"units": units, "signatures": signatures, "postings": postings}

    def _best_candidates(self, text, unit_index, minimum_ratio=0.0):
        if not unit_index or not unit_index["units"]:
            return []
        signature = self._shingles(text)
        overlap_counts = Counter()
        for shingle in signature:
            matches = unit_index["postings"].get(shingle, ())
            if len(matches) <= MAX_POSTINGS_PER_SHINGLE:
                overlap_counts.update(matches)

        candidates = []
        for index, overlap in overlap_counts.most_common(MAX_CANDIDATES_PER_UNIT * 3):
            candidate = unit_index["units"][index]
            candidate_text = candidate["text"]
            length_ratio = min(len(text), len(candidate_text)) / max(len(text), len(candidate_text))
            if length_ratio < 0.55:
                continue
            candidate_signature = unit_index["signatures"][index]
            union_size = len(signature | candidate_signature)
            jaccard = overlap / union_size if union_size else 0
            if jaccard < 0.28:
                continue
            ratio = SequenceMatcher(None, text, candidate_text, autojunk=False).ratio()
            if ratio >= minimum_ratio:
                candidates.append(
                    {
                        "index": index,
                        "unit": candidate,
                        "ratio": ratio,
                        "jaccard": jaccard,
                    }
                )
        candidates.sort(key=lambda item: (item["ratio"], item["jaccard"]), reverse=True)
        return candidates[:MAX_CANDIDATES_PER_UNIT]

    def _best_tender_match(self, text, minimum_ratio=0.72):
        candidates = self._best_candidates(text, self.tender_unit_index, minimum_ratio)
        return candidates[0] if candidates else None

    @staticmethod
    def find_page_for_text(target_text, pages):
        for page_number, _, normalized_text in pages:
            if target_text in normalized_text:
                return page_number
        return 0

    @staticmethod
    def _is_valid_cn_id(identity):
        if len(identity) == 15:
            return identity.isdigit()
        if not re.fullmatch(r"\d{17}[0-9X]", identity):
            return False
        weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
        checks = "10X98765432"
        return checks[sum(int(identity[i]) * weights[i] for i in range(17)) % 11] == identity[-1]

    def extract_entities(self, text):
        entities = set()
        if not text:
            return entities

        clean_digits = re.sub(r"[\s\-—_]+", "", unicodedata.normalize("NFKC", text))
        identities = re.findall(r"(?<!\d)(?:\d{17}[0-9Xx]|\d{15})(?!\d)", clean_digits)
        for identity in identities:
            identity = identity.upper()
            if self._is_valid_cn_id(identity):
                entities.add(identity)

        phones = re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", clean_digits)
        entities.update(phones)

        clean_email = unicodedata.normalize("NFKC", text)
        entities.update(
            email.lower()
            for email in re.findall(
                r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                clean_email,
            )
        )
        return entities

    @staticmethod
    def _scan_warning(label, stats):
        if not stats:
            return None
        total_pages = stats.get("total_pages", 0)
        scan_pages = stats.get("suspected_scan_pages", 0)
        chinese_chars = stats.get("chinese_chars", 0)
        if chinese_chars < 100:
            return f"投标文件 {label} 中可读中文字数仅有 {chinese_chars} 字，可能为扫描件"
        if total_pages and scan_pages:
            ratio = scan_pages / total_pages
            if ratio >= 0.2:
                return (
                    f"投标文件 {label} 有 {scan_pages}/{total_pages} 页可读文字过少，"
                    "这些页面可能无法参与文本比对"
                )
        return None

    def _find_fuzzy_collisions(self, units_a, units_b, exact_sentences):
        index_b = self._build_unit_index(units_b)
        proposals = []
        seen_pairs = set()

        for index_a, unit_a in enumerate(units_a):
            text_a = unit_a["text"]
            for candidate in self._best_candidates(text_a, index_b, minimum_ratio=0.78):
                unit_b = candidate["unit"]
                text_b = unit_b["text"]
                if text_a == text_b or (text_a in exact_sentences and text_b in exact_sentences):
                    continue
                pair_key = (text_a, text_b)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                proposals.append(
                    {
                        "index_a": index_a,
                        "index_b": candidate["index"],
                        "text_a": text_a,
                        "text_b": text_b,
                        "page_a": unit_a["page"],
                        "page_b": unit_b["page"],
                        "similarity": round(candidate["ratio"] * 100, 1),
                    }
                )

        proposals.sort(key=lambda item: item["similarity"], reverse=True)
        matches = []
        used_a = set()
        used_b = set()
        for proposal in proposals:
            if proposal["index_a"] in used_a or proposal["index_b"] in used_b:
                continue
            used_a.add(proposal["index_a"])
            used_b.add(proposal["index_b"])

            text_a = proposal["text_a"]
            text_b = proposal["text_b"]
            result_type = "fuzzy"
            badges = ["近似雷同"]
            desc = f"文本相似度 {proposal['similarity']:.1f}%"
            tender_a = self._best_tender_match(text_a, minimum_ratio=0.72)
            tender_b = self._best_tender_match(text_b, minimum_ratio=0.72)
            tender_text = ""

            if tender_a and tender_b and tender_a["index"] == tender_b["index"]:
                tender_text = tender_a["unit"]["text"]
                if tender_a["ratio"] >= 0.9 and tender_b["ratio"] >= 0.9:
                    result_type = "tender_related"
                    badges = ["招标原文近似", "共同修改线索"]
                    desc = (
                        f"A/B 相似度 {proposal['similarity']:.1f}%，且均与同一招标原文高度相似"
                    )

            proposal.pop("index_a")
            proposal.pop("index_b")
            proposal.update(
                {
                    "type": result_type,
                    "tender_text": tender_text,
                    "badges": badges,
                    "desc": desc,
                }
            )
            matches.append(proposal)
            if len(matches) >= MAX_FUZZY_RESULTS:
                break

        return matches

    @staticmethod
    def _build_summary(collisions, stats_a, stats_b):
        counts = Counter(item["type"] for item in collisions)
        matched_a = {
            item["text_a"]
            for item in collisions
            if item["type"] in {"text", "fuzzy", "tender_related"}
        }
        matched_b = {
            item["text_b"]
            for item in collisions
            if item["type"] in {"text", "fuzzy", "tender_related"}
        }
        matched_chars_a = sum(len(text) for text in matched_a)
        matched_chars_b = sum(len(text) for text in matched_b)
        chinese_a = max(stats_a.get("chinese_chars", 0), 1)
        chinese_b = max(stats_b.get("chinese_chars", 0), 1)
        return {
            "total": len(collisions),
            "exact": counts["text"],
            "fuzzy": counts["fuzzy"],
            "tender_related": counts["tender_related"],
            "entity": counts["entity"],
            "rare_word": counts["rare_word"],
            "matched_chars_a": matched_chars_a,
            "matched_chars_b": matched_chars_b,
            "matched_ratio_a": round(min(matched_chars_a / chinese_a * 100, 100), 1),
            "matched_ratio_b": round(min(matched_chars_b / chinese_b * 100, 100), 1),
        }

    def find_collisions(
        self, path_a, path_b, check_entity=True, check_text=True, check_spelling=False
    ):
        raw_a, pages_a, metadata_a, stats_a = self.extract_text_with_pages(path_a)
        raw_b, pages_b, metadata_b, stats_b = self.extract_text_with_pages(path_b)
        collisions = []

        if check_entity:
            entities_a = set()
            entity_pages_a = {}
            for page_number, raw_text, _ in pages_a:
                page_entities = self.extract_entities(raw_text)
                entities_a.update(page_entities)
                for entity in page_entities:
                    entity_pages_a.setdefault(entity, page_number)

            entities_b = set()
            entity_pages_b = {}
            for page_number, raw_text, _ in pages_b:
                page_entities = self.extract_entities(raw_text)
                entities_b.update(page_entities)
                for entity in page_entities:
                    entity_pages_b.setdefault(entity, page_number)

            for entity in sorted((entities_a & entities_b) - self.tender_entities):
                collisions.append(
                    {
                        "type": "entity",
                        "text_a": entity,
                        "text_b": entity,
                        "page_a": entity_pages_a.get(entity, 0),
                        "page_b": entity_pages_b.get(entity, 0),
                        "badges": ["敏感实体"],
                        "desc": f"发现相同的实体信息: {entity}",
                    }
                )

        filtered_sentences = set()
        if check_text or check_spelling:
            common_sentences = set(self.get_sentences(raw_a)) & set(self.get_sentences(raw_b))
            for sentence in common_sentences:
                is_tender_exact = sentence in self.tender_sentences or sentence in self.tender_full_text
                skeleton = self.get_skeleton(sentence)
                is_tender_skeleton = (
                    len(skeleton) > 1 and skeleton in self.tender_skeletons
                )
                if not is_tender_exact and not is_tender_skeleton:
                    filtered_sentences.add(sentence)

        if check_text:
            for sentence in sorted(filtered_sentences):
                page_a = self.find_page_for_text(sentence, pages_a)
                page_b = self.find_page_for_text(sentence, pages_b)
                tender_match = self._best_tender_match(sentence, minimum_ratio=0.78)
                if tender_match:
                    result_type = "tender_related"
                    badges = ["完全匹配", "招标原文共同修改线索"]
                    desc = (
                        "两份投标文件文本完全一致，且与招标原文高度相似但并非原文完全复制"
                    )
                    tender_text = tender_match["unit"]["text"]
                    similarity = round(tender_match["ratio"] * 100, 1)
                else:
                    result_type = "text"
                    badges = ["完全匹配"]
                    desc = "发现非招标文件雷同语句"
                    tender_text = ""
                    similarity = 100.0
                collisions.append(
                    {
                        "type": result_type,
                        "text_a": sentence,
                        "text_b": sentence,
                        "page_a": page_a,
                        "page_b": page_b,
                        "similarity": similarity,
                        "tender_text": tender_text,
                        "badges": badges,
                        "desc": desc,
                    }
                )

            units_a = self.get_comparison_units(pages_a)
            units_b = self.get_comparison_units(pages_b)
            collisions.extend(self._find_fuzzy_collisions(units_a, units_b, filtered_sentences))

        if check_spelling:
            for sentence in sorted(filtered_sentences):
                if len(sentence) < 6:
                    continue
                try:
                    rare_words = _detect_rare_words(sentence)
                except Exception:
                    continue
                if not rare_words:
                    continue
                words_desc = "、".join(f'"{word}"' for word, _ in rare_words[:3])
                collisions.append(
                    {
                        "type": "rare_word",
                        "text_a": sentence,
                        "text_b": sentence,
                        "page_a": self.find_page_for_text(sentence, pages_a),
                        "page_b": self.find_page_for_text(sentence, pages_b),
                        "badges": ["共同罕见词"],
                        "desc": f"两份文件共同出现词典外词汇: {words_desc}",
                    }
                )

        warnings = []
        warning_a = self._scan_warning("A", stats_a)
        warning_b = self._scan_warning("B", stats_b)
        if warning_a:
            warnings.append(warning_a)
        if warning_b:
            warnings.append(warning_b)

        type_priority = {"entity": 0, "tender_related": 1, "text": 2, "fuzzy": 3, "rare_word": 4}
        collisions.sort(
            key=lambda item: (
                type_priority.get(item["type"], 9),
                item.get("page_a", 0),
                item.get("page_b", 0),
            )
        )

        return {
            "metadata": {
                "file_a": metadata_a,
                "file_b": metadata_b,
                "tender": self.tender_metadata,
                "warnings": warnings,
                "text_stats": {"file_a": stats_a, "file_b": stats_b, "tender": self.tender_stats},
                "algorithm_version": ALGORITHM_VERSION,
            },
            "summary": self._build_summary(collisions, stats_a, stats_b),
            "paragraphs": collisions,
        }


def compare_documents(
    path_a,
    path_b,
    path_tender=None,
    check_entity=True,
    check_text=True,
    check_spelling=False,
):
    detector = CollusionDetector(path_tender)
    return detector.find_collisions(
        path_a,
        path_b,
        check_entity=check_entity,
        check_text=check_text,
        check_spelling=check_spelling,
    )
