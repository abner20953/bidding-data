import gzip
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

import fitz  # PyMuPDF


ALGORITHM_VERSION = 5
MIN_EXACT_LENGTH = 9
MAX_EXACT_BLOCK_LENGTH = 1200
MIN_FUZZY_LENGTH = 20
MIN_FUZZY_DISPLAY_LENGTH = 30
MAX_UNIT_LENGTH = 220
UNIT_OVERLAP = 40
SHINGLE_SIZE = 5
MAX_POSTINGS_PER_SHINGLE = 80
MAX_CANDIDATES_PER_UNIT = 8
MAX_FUZZY_RESULTS = 200
MAX_SHARED_ERROR_RESULTS = 50
MAX_PDF_PAGES = 2000
MAX_COMPARISON_PAGES = 4000
MAX_EXTRACTED_CHARS = 8_000_000
MAX_COMPARISON_CHARS = 12_000_000
MAX_EXACT_UNITS_PER_FILE = 200_000
MAX_FUZZY_UNITS_PER_FILE = 50_000
MIN_SHARED_EDIT_COVERAGE = 0.6
TENDER_DERIVED_RATIO = 0.78
TENDER_EXACT_SHINGLE_COVERAGE = 0.65
TENDER_FRAGMENT_SHINGLE_COVERAGE = 0.40
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "bijiao_cache")
)

class ComparisonLimitError(ValueError):
    pass


def _validate_total_page_budget(page_counts):
    total_pages = sum(page_counts)
    if total_pages > MAX_COMPARISON_PAGES:
        raise ComparisonLimitError(
            f"本次比对共 {total_pages} 页，超过 {MAX_COMPARISON_PAGES} 页总限制"
        )


def _validate_total_character_budget(character_counts):
    total_characters = sum(character_counts)
    if total_characters > MAX_COMPARISON_CHARS:
        raise ComparisonLimitError(
            f"本次比对共提取 {total_characters:,} 个字符，"
            f"超过 {MAX_COMPARISON_CHARS:,} 个字符总限制"
        )


def _preflight_page_budget(pdf_paths):
    """Reject oversized multi-file comparisons before extracting any page text."""
    page_counts = []
    for pdf_path in pdf_paths:
        if not pdf_path:
            continue
        try:
            with fitz.open(pdf_path) as document:
                page_count = document.page_count
        except Exception:
            # The extractor provides a more useful error for invalid PDFs.
            return
        if page_count > MAX_PDF_PAGES:
            raise ComparisonLimitError(
                f"PDF 页数为 {page_count}，超过单文件 {MAX_PDF_PAGES} 页限制"
            )
        page_counts.append(page_count)
    _validate_total_page_budget(page_counts)


class CollusionDetector:
    def __init__(self, tender_path=None, build_text_index=True):
        self.tender_path = tender_path
        self.build_text_index = build_text_index
        self.tender_exact_texts = set()
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
            "extracted_chars": extracted_chars,
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

        if self.build_text_index:
            tender_exact_units = self.get_exact_units(pages)
            self.tender_exact_texts = {unit["text"] for unit in tender_exact_units}
            self.tender_skeletons = {
                skeleton
                for text in self.tender_exact_texts
                if len((skeleton := self.get_skeleton(text))) > 1
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

    def get_exact_units(self, pages):
        """Return ordered, page-aware segments used to merge exact matching blocks."""
        repeated_lines = self._repeated_page_lines(pages)
        units = []
        order = 0

        for page_number, raw_text, _ in pages:
            for line in raw_text.splitlines():
                normalized_line = self.normalize(line)
                if not normalized_line or normalized_line in repeated_lines:
                    continue
                for part in re.split(r"[。.!！?？;；,，]", line):
                    normalized = self.normalize(part.strip())
                    if len(normalized) < MIN_EXACT_LENGTH:
                        continue
                    units.append(
                        {"text": normalized, "page": page_number, "order": order}
                    )
                    if len(units) > MAX_EXACT_UNITS_PER_FILE:
                        raise ComparisonLimitError(
                            "PDF 可比对短段过多，超过单文件 "
                            f"{MAX_EXACT_UNITS_PER_FILE:,} 段限制"
                        )
                    order += 1
        return units

    def _is_tender_copy(self, text):
        if not self.tender_full_text:
            return False
        return text in self.tender_exact_texts or text in self.tender_full_text

    def _tender_shingle_coverage(self, text):
        if not self.tender_unit_index:
            return 0.0
        signature = self._shingles(text)
        if not signature:
            return 0.0
        tender_postings = self.tender_unit_index["postings"]
        return sum(shingle in tender_postings for shingle in signature) / len(signature)

    @staticmethod
    def _edit_operations(source, target):
        operations = {}
        matcher = SequenceMatcher(None, source, target, autojunk=False)
        for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            replacement = target[target_start:target_end]
            signature = (source_start, source_end, replacement)
            operations[signature] = {
                "source_start": source_start,
                "source_end": source_end,
                "target_start": target_start,
                "target_end": target_end,
                "original": source[source_start:source_end],
                "modified": replacement,
                "weight": max(source_end - source_start, len(replacement), 1),
            }
        return operations

    @staticmethod
    def _looks_like_table_extraction(text):
        """Identify table-like text where PDF cell ordering creates false edits."""
        if len(text) < 50:
            return False
        numeric_groups = re.findall(r"\d+(?:\.\d+)?", text)
        measurement_markers = re.findall(
            r"(?:mm|cm|kg|m3|m2|\*|×|≥|≤|±|张|把|台|套|件)", text
        )
        return len(numeric_groups) >= 5 and len(measurement_markers) >= 2

    def _shared_tender_edit_evidence(self, tender_text, text_a, text_b):
        """Prove that A and B made substantially the same edits to tender text."""
        if not tender_text or tender_text == text_a or tender_text == text_b:
            return None
        if any(
            self._looks_like_table_extraction(text)
            for text in (tender_text, text_a, text_b)
        ):
            return None

        edits_a = self._edit_operations(tender_text, text_a)
        edits_b = self._edit_operations(tender_text, text_b)
        shared_signatures = set(edits_a) & set(edits_b)
        if not shared_signatures:
            return None

        total_a = sum(edit["weight"] for edit in edits_a.values())
        total_b = sum(edit["weight"] for edit in edits_b.values())
        shared_weight = sum(edits_a[key]["weight"] for key in shared_signatures)
        coverage = shared_weight / max(total_a, total_b, 1)
        if coverage < MIN_SHARED_EDIT_COVERAGE:
            return None

        evidence = []
        for signature in sorted(shared_signatures, key=lambda key: (key[0], key[1]))[:3]:
            edit = edits_a[signature]
            public_change = {
                "original": edit["original"] or "（此处新增）",
                "modified": edit["modified"] or "（删除）",
            }
            evidence.append(public_change)
        return {
            "changes": evidence,
            "coverage": round(coverage * 100, 1),
        }

    def _add_error_issue(self, issues, kind, label, detail, text, page):
        normalized = self.normalize(text)
        if len(re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalized)) < 4:
            return
        fingerprint = f"{kind}|{normalized}"
        issues.setdefault(
            fingerprint,
            {
                "kind": kind,
                "label": label,
                "detail": detail,
                "text": text.strip(),
                "page": page,
                "fingerprint": fingerprint,
            },
        )

    def _collect_high_confidence_errors(self, pages):
        issues = {}
        punctuation_pattern = re.compile(r"[,，。.;；:：、]{2,}")
        arithmetic_pattern = re.compile(
            r"(?<![\d.])([\d,]+(?:\.\d+)?)\s*[×*xX]\s*"
            r"([\d,]+(?:\.\d+)?)\s*[=＝]\s*([\d,]+(?:\.\d+)?)(?![\d.])"
        )
        number_pattern = re.compile(r"^\s*(\d{1,3})\s*([.．、)）])\s*(.+)$")
        malformed_number_pattern = re.compile(
            r"^\s*(\d{1,3})\s*([.．、)）])\s*([.．、)）])"
        )
        bracket_pairs = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}"}
        closing_brackets = {value: key for key, value in bracket_pairs.items()}

        for page_number, raw_text, _ in pages:
            raw_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            numbered_lines = []

            for line_index, line in enumerate(raw_lines):
                for punctuation_match in punctuation_pattern.finditer(line):
                    cluster = punctuation_match.group()
                    is_ellipsis = len(cluster) >= 3 and set(cluster) <= {".", "。"}
                    if is_ellipsis:
                        continue
                    self._add_error_issue(
                        issues,
                        "punctuation",
                        "共同标点错误",
                        f"连续出现异常标点“{cluster}”",
                        line,
                        page_number,
                    )
                    break

                repeated_character = re.search(r"([\u4e00-\u9fff])\1{2,}", line)
                if repeated_character:
                    repeated = repeated_character.group()
                    self._add_error_issue(
                        issues,
                        "text",
                        "共同文字错误",
                        f"同一汉字异常连续出现“{repeated}”",
                        line,
                        page_number,
                    )

                malformed_number = malformed_number_pattern.search(line)
                if malformed_number:
                    self._add_error_issue(
                        issues,
                        "numbering",
                        "共同编号错误",
                        f"编号 {malformed_number.group(1)} 后连续使用了两个分隔符",
                        line,
                        page_number,
                    )

                number_match = number_pattern.match(line)
                if number_match:
                    numbered_lines.append(
                        {
                            "number": int(number_match.group(1)),
                            "style": unicodedata.normalize("NFKC", number_match.group(2)),
                            "line_index": line_index,
                            "line": line,
                        }
                    )

                for arithmetic_match in arithmetic_pattern.finditer(line):
                    try:
                        left = Decimal(arithmetic_match.group(1).replace(",", ""))
                        right = Decimal(arithmetic_match.group(2).replace(",", ""))
                        stated = Decimal(arithmetic_match.group(3).replace(",", ""))
                    except InvalidOperation:
                        continue
                    expected = left * right
                    if abs(expected - stated) <= Decimal("0.01"):
                        continue
                    expression = arithmetic_match.group()
                    expected_text = format(expected, "f")
                    self._add_error_issue(
                        issues,
                        "calculation",
                        "共同计算错误",
                        f"算式“{expression}”的正确结果应为 {expected_text}",
                        line,
                        page_number,
                    )

            for index in range(1, len(numbered_lines)):
                previous = numbered_lines[index - 1]
                current = numbered_lines[index]
                if previous["number"] != current["number"]:
                    continue
                if current["line_index"] - previous["line_index"] > 3:
                    continue
                has_previous_context = (
                    index >= 2
                    and numbered_lines[index - 2]["number"] == current["number"] - 1
                )
                has_next_context = (
                    index + 1 < len(numbered_lines)
                    and numbered_lines[index + 1]["number"] == current["number"] + 1
                )
                if not (has_previous_context and has_next_context):
                    continue
                combined_text = f"{previous['line']}\n{current['line']}"
                self._add_error_issue(
                    issues,
                    "numbering",
                    "共同编号错误",
                    f"同一编号 {current['number']} 在连续列表中重复出现",
                    combined_text,
                    page_number,
                )

            run = []
            for entry in numbered_lines + [None]:
                if entry and (
                    not run
                    or (
                        entry["number"] == run[-1]["number"] + 1
                        and entry["line_index"] - run[-1]["line_index"] <= 3
                    )
                ):
                    run.append(entry)
                    continue
                if len(run) >= 4:
                    style_counts = Counter(item["style"] for item in run)
                    dominant_style, dominant_count = style_counts.most_common(1)[0]
                    if dominant_count >= len(run) - 1 and len(style_counts) == 2:
                        outliers = [item for item in run if item["style"] != dominant_style]
                        if len(outliers) == 1:
                            outlier = outliers[0]
                            self._add_error_issue(
                                issues,
                                "numbering",
                                "共同编号错误",
                                f"连续编号组主要使用“{dominant_style}”，但该项使用“{outlier['style']}”",
                                outlier["line"],
                                page_number,
                            )
                run = [entry] if entry else []

        bracket_stack = []
        unmatched_brackets = []
        for page_number, raw_text, _ in pages:
            for position, character in enumerate(raw_text):
                if character in bracket_pairs:
                    bracket_stack.append((character, page_number, raw_text, position))
                    continue
                if character not in closing_brackets:
                    continue

                line_start = raw_text.rfind("\n", 0, position) + 1
                line_prefix = raw_text[line_start:position]
                if character in {")", "）"} and re.fullmatch(
                    r"\s*\d{1,3}\s*", line_prefix
                ):
                    continue
                if bracket_stack and bracket_stack[-1][0] == closing_brackets[character]:
                    bracket_stack.pop()
                else:
                    unmatched_brackets.append(
                        (character, page_number, raw_text, position)
                    )

        unmatched_brackets.extend(bracket_stack)
        for character, page_number, raw_text, position in unmatched_brackets[:20]:
            line_start = raw_text.rfind("\n", 0, position) + 1
            line_end = raw_text.find("\n", position)
            if line_end < 0:
                line_end = len(raw_text)
            line = raw_text[line_start:line_end].strip()
            self._add_error_issue(
                issues,
                "punctuation",
                "共同标点错误",
                f"括号“{character}”在全文中没有配对",
                line,
                page_number,
            )

        return issues

    def _find_shared_high_confidence_errors(self, pages_a, pages_b):
        issues_a = self._collect_high_confidence_errors(pages_a)
        issues_b = self._collect_high_confidence_errors(pages_b)
        tender_fingerprints = (
            set(self._collect_high_confidence_errors(self.tender_pages))
            if self.tender_pages
            else set()
        )
        shared_fingerprints = (set(issues_a) & set(issues_b)) - tender_fingerprints
        matches = []

        for fingerprint in shared_fingerprints:
            issue_a = issues_a[fingerprint]
            issue_b = issues_b[fingerprint]
            matches.append(
                {
                    "type": "shared_error",
                    "error_kind": issue_a["kind"],
                    "text_a": issue_a["text"],
                    "text_b": issue_b["text"],
                    "page_a": issue_a["page"],
                    "page_b": issue_b["page"],
                    "similarity": 100.0,
                    "tender_text": "",
                    "shared_edits": [],
                    "badges": [issue_a["label"]],
                    "desc": f"两份文件在相同内容中{issue_a['detail']}",
                }
            )

        error_priority = {
            "numbering": 0,
            "calculation": 1,
            "text": 2,
            "punctuation": 3,
        }
        deduplicated = {}
        for item in matches:
            key = (
                self.normalize(item["text_a"]),
                item["page_a"],
                item["page_b"],
            )
            existing = deduplicated.get(key)
            if existing is None or (
                error_priority[item["error_kind"]]
                < error_priority[existing["error_kind"]]
            ):
                deduplicated[key] = item

        results = list(deduplicated.values())
        results.sort(key=lambda item: (item["page_a"], item["page_b"], item["desc"]))
        return results[:MAX_SHARED_ERROR_RESULTS]

    def _remove_exact_matches_covered_by_errors(self, collisions, shared_errors):
        if not shared_errors:
            return collisions

        normalized_errors = [
            {
                "page_a": item["page_a"],
                "page_b": item["page_b"],
                "text_a": self.normalize(item["text_a"]),
                "text_b": self.normalize(item["text_b"]),
            }
            for item in shared_errors
        ]
        filtered = []
        for item in collisions:
            if item["type"] != "text":
                filtered.append(item)
                continue
            components_a = [part for part in item["text_a"].split("；") if part]
            components_b = [part for part in item["text_b"].split("；") if part]
            is_covered = any(
                item.get("page_a") == error["page_a"]
                and item.get("page_b") == error["page_b"]
                and all(part in error["text_a"] for part in components_a)
                and all(part in error["text_b"] for part in components_b)
                for error in normalized_errors
            )
            if not is_covered:
                filtered.append(item)
        return filtered

    @staticmethod
    def _split_exact_pairs(pairs):
        groups = []
        current = []
        current_length = 0

        for unit_a, unit_b in pairs:
            is_contiguous = not current or (
                unit_a["order"] == current[-1][0]["order"] + 1
                and unit_b["order"] == current[-1][1]["order"] + 1
            )
            added_length = len(unit_a["text"]) + (1 if current else 0)
            if current and (
                not is_contiguous
                or current_length + added_length > MAX_EXACT_BLOCK_LENGTH
            ):
                groups.append(current)
                current = []
                current_length = 0
                added_length = len(unit_a["text"])
            current.append((unit_a, unit_b))
            current_length += added_length

        if current:
            groups.append(current)
        return groups

    def _find_exact_collisions(self, units_a, units_b):
        filtered_a = [unit for unit in units_a if not self._is_tender_copy(unit["text"])]
        filtered_b = [unit for unit in units_b if not self._is_tender_copy(unit["text"])]
        sequence_a = [unit["text"] for unit in filtered_a]
        sequence_b = [unit["text"] for unit in filtered_b]
        matcher = SequenceMatcher(None, sequence_a, sequence_b)
        collisions = []
        matched_texts = set()
        chosen_pairs = {}

        for block in matcher.get_matching_blocks():
            if not block.size:
                continue
            for offset in range(block.size):
                unit_a = filtered_a[block.a + offset]
                unit_b = filtered_b[block.b + offset]
                chosen_pairs.setdefault(unit_a["text"], (unit_a, unit_b))

        # Sequence alignment gives the best contiguous blocks. Supplement it with
        # exact text found in a different section order so legacy matches are not lost.
        units_by_text_a = {}
        units_by_text_b = {}
        for unit in filtered_a:
            units_by_text_a.setdefault(unit["text"], unit)
        for unit in filtered_b:
            units_by_text_b.setdefault(unit["text"], unit)
        for text in set(units_by_text_a) & set(units_by_text_b):
            chosen_pairs.setdefault(
                text, (units_by_text_a[text], units_by_text_b[text])
            )

        ordered_pairs = sorted(
            chosen_pairs.values(),
            key=lambda pair: (pair[0]["order"], pair[1]["order"]),
        )
        for group in self._split_exact_pairs(ordered_pairs):
            text = "；".join(unit_a["text"] for unit_a, _ in group)
            matched_texts.update(unit_a["text"] for unit_a, _ in group)
            tender_references = []
            shared_edits = []
            tender_similarities = []
            tender_derived_segments = 0
            tender_skeleton_segments = 0
            tender_shingle_segments = 0

            for unit_a, _ in group:
                skeleton = self.get_skeleton(unit_a["text"])
                if len(skeleton) > 1 and skeleton in self.tender_skeletons:
                    tender_skeleton_segments += 1
                shingle_coverage = self._tender_shingle_coverage(unit_a["text"])
                numeric_groups = re.findall(r"\d+", unit_a["text"])
                if shingle_coverage >= TENDER_EXACT_SHINGLE_COVERAGE or (
                    shingle_coverage >= TENDER_FRAGMENT_SHINGLE_COVERAGE
                    and len(numeric_groups) >= 2
                ):
                    tender_shingle_segments += 1
                tender_match = self._best_tender_match(
                    unit_a["text"], minimum_ratio=0.78
                )
                if not tender_match:
                    continue
                tender_text = tender_match["unit"]["text"]
                edit_evidence = self._shared_tender_edit_evidence(
                    tender_text, unit_a["text"], unit_a["text"]
                )
                if not edit_evidence:
                    if tender_match["ratio"] >= TENDER_DERIVED_RATIO:
                        tender_derived_segments += 1
                    continue
                if tender_text not in tender_references:
                    tender_references.append(tender_text)
                tender_similarities.append(tender_match["ratio"])
                for change in edit_evidence["changes"]:
                    if change not in shared_edits and len(shared_edits) < 3:
                        shared_edits.append(change)

            if not shared_edits and (
                tender_derived_segments == len(group)
                or tender_skeleton_segments == len(group)
                or tender_shingle_segments == len(group)
            ):
                continue

            if shared_edits:
                result_type = "tender_related"
                badges = ["完全匹配", "已验证共同修改"]
                desc = f"两份文件相对招标原文存在 {len(shared_edits)} 处相同改动"
            else:
                result_type = "text"
                badges = ["完全匹配"]
                desc = (
                    f"发现连续 {len(group)} 段非招标文件雷同内容"
                    if len(group) > 1
                    else "发现非招标文件雷同内容"
                )

            collisions.append(
                {
                    "type": result_type,
                    "text_a": text,
                    "text_b": text,
                    "page_a": group[0][0]["page"],
                    "page_a_end": group[-1][0]["page"],
                    "page_b": group[0][1]["page"],
                    "page_b_end": group[-1][1]["page"],
                    "segment_count": len(group),
                    "similarity": 100.0,
                    "tender_similarity": (
                        round(max(tender_similarities) * 100, 1)
                        if tender_similarities
                        else 0
                    ),
                    "tender_text": "；".join(tender_references[:3]),
                    "shared_edits": shared_edits,
                    "error_kind": "",
                    "badges": badges,
                    "desc": desc,
                }
            )

        return collisions, matched_texts

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
                        if len(units) > MAX_FUZZY_UNITS_PER_FILE:
                            raise ComparisonLimitError(
                                "PDF 可比对长段过多，超过单文件 "
                                f"{MAX_FUZZY_UNITS_PER_FILE:,} 段限制"
                            )
                        seen.add(key)
        return units

    @staticmethod
    def _shingles(text):
        if len(text) < SHINGLE_SIZE:
            return {hash(text)} if text else set()
        return {
            hash(text[index : index + SHINGLE_SIZE])
            for index in range(len(text) - SHINGLE_SIZE + 1)
        }

    def _build_unit_index(self, units):
        signature_sizes = []
        postings = {}
        missing = object()
        for index, unit in enumerate(units):
            signature = self._shingles(unit["text"])
            signature_sizes.append(len(signature))
            for shingle in signature:
                matches = postings.get(shingle, missing)
                if matches is missing:
                    postings[shingle] = index
                elif isinstance(matches, int):
                    postings[shingle] = [matches, index]
                elif len(matches) <= MAX_POSTINGS_PER_SHINGLE:
                    # The 81st posting marks this shingle as too common. Further
                    # entries would never be queried and only consume memory.
                    matches.append(index)
        return {
            "units": units,
            "signature_sizes": signature_sizes,
            "postings": postings,
        }

    def _best_candidates(
        self, text, unit_index, minimum_ratio=0.0, minimum_jaccard=0.28
    ):
        if not unit_index or not unit_index["units"]:
            return []
        signature = self._shingles(text)
        overlap_counts = Counter()
        for shingle in signature:
            matches = unit_index["postings"].get(shingle)
            if isinstance(matches, int):
                overlap_counts[matches] += 1
            elif matches is not None and len(matches) <= MAX_POSTINGS_PER_SHINGLE:
                overlap_counts.update(matches)

        candidates = []
        for index, overlap in overlap_counts.most_common(MAX_CANDIDATES_PER_UNIT * 3):
            candidate = unit_index["units"][index]
            candidate_text = candidate["text"]
            length_ratio = min(len(text), len(candidate_text)) / max(len(text), len(candidate_text))
            if length_ratio < 0.55:
                continue
            union_size = (
                len(signature)
                + unit_index["signature_sizes"][index]
                - overlap
            )
            jaccard = overlap / union_size if union_size else 0
            if jaccard < minimum_jaccard:
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

    def _best_tender_match(
        self, text, minimum_ratio=0.72, minimum_jaccard=0.28
    ):
        candidates = self._best_candidates(
            text,
            self.tender_unit_index,
            minimum_ratio,
            minimum_jaccard=minimum_jaccard,
        )
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

        normalized_text = unicodedata.normalize("NFKC", text)
        separator_pattern = r"[\s\-—_]*"
        identities = re.findall(
            rf"(?<!\d)(?:(?:\d{separator_pattern}){{17}}[0-9Xx]|"
            rf"(?:\d{separator_pattern}){{14}}\d)(?!\d)",
            normalized_text,
        )
        for identity in identities:
            identity = re.sub(r"[\s\-—_]+", "", identity).upper()
            if self._is_valid_cn_id(identity):
                entities.add(identity)

        phones = re.findall(
            rf"(?<!\d)1{separator_pattern}[3-9](?:{separator_pattern}\d){{9}}(?!\d)",
            normalized_text,
        )
        entities.update(re.sub(r"[\s\-—_]+", "", phone) for phone in phones)

        clean_email = normalized_text
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
                if min(len(text_a), len(text_b)) < MIN_FUZZY_DISPLAY_LENGTH:
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
            shared_edits = []
            error_kind = ""
            verified_tender_edit = False

            if tender_a and tender_b and tender_a["index"] == tender_b["index"]:
                candidate_tender_text = tender_a["unit"]["text"]
                edit_evidence = self._shared_tender_edit_evidence(
                    candidate_tender_text, text_a, text_b
                )
                if (
                    tender_a["ratio"] >= 0.9
                    and tender_b["ratio"] >= 0.9
                    and edit_evidence
                ):
                    tender_text = candidate_tender_text
                    shared_edits = edit_evidence["changes"]
                    verified_tender_edit = True
                    result_type = "tender_related"
                    badges = ["招标原文关联", "已验证共同修改"]
                    desc = (
                        f"A/B 相似度 {proposal['similarity']:.1f}%，相对招标原文存在 "
                        f"{len(shared_edits)} 处相同改动"
                    )

            if not verified_tender_edit and self.tender_unit_index:
                derived_a = tender_a or self._best_tender_match(
                    text_a, minimum_ratio=0.55, minimum_jaccard=0.12
                )
                derived_b = tender_b or self._best_tender_match(
                    text_b, minimum_ratio=0.55, minimum_jaccard=0.12
                )
                table_a = self._looks_like_table_extraction(text_a)
                table_b = self._looks_like_table_extraction(text_b)
                is_tender_derived = False

                if derived_a and derived_b:
                    weaker_ratio = min(derived_a["ratio"], derived_b["ratio"])
                    same_tender_unit = derived_a["index"] == derived_b["index"]
                    is_tender_derived = (
                        weaker_ratio >= 0.72
                        or (
                            proposal["similarity"] >= 90
                            and weaker_ratio >= 0.65
                            and (same_tender_unit or table_a or table_b)
                        )
                        or (
                            proposal["similarity"] >= 85
                            and weaker_ratio >= 0.58
                            and table_a
                            and table_b
                        )
                    )
                elif derived_a or derived_b:
                    derived_ratio = (derived_a or derived_b)["ratio"]
                    is_tender_derived = (
                        (
                            proposal["similarity"] >= 92
                            and derived_ratio >= 0.70
                            and (table_a or table_b)
                        )
                        or (
                            proposal["similarity"] >= 85
                            and derived_ratio >= 0.60
                            and table_a
                            and table_b
                        )
                    )

                if is_tender_derived:
                    continue

            proposal.pop("index_a")
            proposal.pop("index_b")
            proposal.update(
                {
                    "type": result_type,
                    "tender_text": tender_text,
                    "shared_edits": shared_edits,
                    "error_kind": error_kind,
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
            if item["type"] in {"text", "fuzzy", "tender_related", "shared_error"}
        }
        matched_b = {
            item["text_b"]
            for item in collisions
            if item["type"] in {"text", "fuzzy", "tender_related", "shared_error"}
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
            "shared_error": counts["shared_error"],
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
        _validate_total_character_budget(
            (
                stats_a.get("extracted_chars", len(raw_a)),
                stats_b.get("extracted_chars", len(raw_b)),
                self.tender_stats.get("extracted_chars", len(self.tender_full_text)),
            )
        )
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

        if check_text:
            exact_collisions, exact_matched_texts = self._find_exact_collisions(
                self.get_exact_units(pages_a),
                self.get_exact_units(pages_b),
            )

            units_a = self.get_comparison_units(pages_a)
            units_b = self.get_comparison_units(pages_b)
            text_collisions = exact_collisions + self._find_fuzzy_collisions(
                units_a,
                units_b,
                exact_matched_texts,
            )
            collisions.extend(text_collisions)

        if check_spelling:
            shared_errors = self._find_shared_high_confidence_errors(
                pages_a, pages_b
            )
            collisions = self._remove_exact_matches_covered_by_errors(
                collisions, shared_errors
            )
            collisions.extend(shared_errors)

        warnings = []
        warning_a = self._scan_warning("A", stats_a)
        warning_b = self._scan_warning("B", stats_b)
        if warning_a:
            warnings.append(warning_a)
        if warning_b:
            warnings.append(warning_b)

        type_priority = {
            "entity": 0,
            "shared_error": 1,
            "tender_related": 2,
            "text": 3,
            "fuzzy": 4,
            "rare_word": 5,
        }
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
    _preflight_page_budget((path_a, path_b, path_tender))
    detector = CollusionDetector(path_tender, build_text_index=check_text)
    return detector.find_collisions(
        path_a,
        path_b,
        check_entity=check_entity,
        check_text=check_text,
        check_spelling=check_spelling,
    )
