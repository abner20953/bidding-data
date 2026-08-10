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


# 共同删改的全文锚点校验与义务主体改写簇识别已改变查重结论；同步失效旧解析
# 缓存，避免历史结果与当前算法混用。
ALGORITHM_VERSION = 10
MIN_EXACT_LENGTH = 9
MIN_EXACT_DISPLAY_LENGTH = 30
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
MAX_PDF_PAGES = 2500
# 独立 compare_documents 入口一次最多同时持有招标文件和两份投标文件；
# 总页数预算允许三份文件都达到单文件上限，字符预算继续承担 2 GB 服务器的内存保护。
MAX_COMPARISON_PAGES = MAX_PDF_PAGES * 3
MAX_EXTRACTED_CHARS = 8_000_000
MAX_COMPARISON_CHARS = 12_000_000
MAX_EXACT_UNITS_PER_FILE = 200_000
MAX_FUZZY_UNITS_PER_FILE = 50_000
MAX_TENDER_SOURCE_HASHES = 450_000
MIN_SHARED_EDIT_COVERAGE = 0.6
TENDER_DERIVED_RATIO = 0.78
TENDER_EXACT_SHINGLE_COVERAGE = 0.65
TENDER_FRAGMENT_SHINGLE_COVERAGE = 0.40
MIN_SHARED_NONTENDER_SHINGLES = 24
MIN_SHARED_NONTENDER_RATIO = 0.20
MIN_TENDER_FRAGMENT_COVERAGE = 0.20
MIN_SHARED_NONTENDER_RUN = 24
MIN_NUMERIC_NONTENDER_RUN = 12
DIRECT_TENDER_WINDOW = 10
TENDER_COVERAGE_CACHE_SIZE = 10_000
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
        self.tender_source_hashes = set()
        self.tender_field_templates = set()
        self.tender_field_values = {}
        self.tender_skeletons = set()
        self.tender_full_text = ""
        self.tender_entities = set()
        self.tender_pages = []
        self.tender_metadata = {}
        self.tender_stats = {}
        self.tender_units = []
        self.tender_unit_index = None
        self._tender_coverage_cache = {}
        self._nontender_runs_cache = {}
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

    @classmethod
    def tender_source_canonical(cls, text):
        """Normalize presentation-only punctuation while retaining numeric meaning."""
        normalized = cls.normalize(text)
        if not normalized:
            return ""

        # Decimal separators and numeric ratios can change a requirement, so keep
        # punctuation between digits.  Operators such as <, >, !, =, ≥ and ≤
        # always remain significant.
        normalized = re.sub(r"(?<!\d)[.,:]|[.,:](?!\d)", "", normalized)
        return re.sub(r"[;?\"'`()（）\[\]【】{}《》·、]", "", normalized)

    @staticmethod
    def _tender_source_digest(canonical):
        return hashlib.blake2b(
            canonical.encode("utf-8", errors="ignore"), digest_size=16
        ).digest()

    @staticmethod
    def _split_text_parts(text, split_commas=True):
        # ASCII ! may be a logical-not operator in technical requirements.
        # Only the full-width Chinese exclamation mark is treated as sentence
        # punctuation; != and ！= remain intact as well.
        pattern = r"[。.]|！(?![=＝])|[?？;；]"
        if split_commas:
            pattern += r"|[,，]"
        return re.split(pattern, text or "")

    @classmethod
    def _tender_field_signature(cls, text, require_placeholder=False):
        """Identify only strict document-control values, never free-form text."""
        normalized = cls.normalize(text)
        if not normalized or len(normalized) > 80:
            return ""
        match = re.match(
            r"^(?:第?[一二三四五六七八九十\d]+[、.)）]?)?"
            r"(项目编号|招标编号|采购编号|标段编号|包号|日期):(.+)$",
            normalized,
        )
        if not match:
            return ""

        label, value = match.groups()
        if label == "日期":
            placeholder_date = value.strip("()（）[]【】_-. ")
            if re.fullmatch(r"[年月日]{2,3}", placeholder_date):
                return label
            if require_placeholder:
                return ""
            if re.fullmatch(
                r"(?:\d{4}[-/.年])?\d{1,2}[-/.月]\d{1,2}日?",
                value,
            ):
                return label
            return ""

        placeholder = value.strip("()（）[]【】_-. ")
        is_placeholder = bool(re.fullmatch(
            r"(?:请)?(?:在此)?(?:填写|填入)?"
            r"(?:项目编号|招标编号|采购编号|标段编号|包号)",
            placeholder,
        ))
        if is_placeholder:
            return label
        if require_placeholder:
            return ""
        if re.fullmatch(r"[a-z0-9_./\\()（）-]+", value) and re.search(r"\d", value):
            return label
        return ""

    @classmethod
    def _tender_field_value(cls, text):
        normalized = cls.normalize(text)
        match = re.match(
            r"^(?:第?[一二三四五六七八九十\d]+[、.)）]?)?"
            r"(?:项目编号|招标编号|采购编号|标段编号|包号|日期):(.+)$",
            normalized,
        )
        return match.group(1).strip("()（）[]【】{}_:./\\ ") if match else ""

    @classmethod
    def _build_tender_source_hashes(cls, tender_exact_units, tender_pages=None):
        """Build a bounded index resilient to presentation punctuation boundaries."""
        hashes = set()

        def add_canonical(canonical):
            if len(canonical) < MIN_EXACT_LENGTH:
                return
            if len(hashes) >= MAX_TENDER_SOURCE_HASHES:
                return
            hashes.add(cls._tender_source_digest(canonical))

        def add_fragments(text):
            fragments = re.split(
                r"(?<!\d):|:(?!\d)|[;?\"'`()（）\[\]【】{}《》·、]+",
                cls.normalize(text),
            )
            for fragment in fragments:
                add_canonical(cls.tender_source_canonical(fragment))

        for unit in tender_exact_units:
            canonical = cls.tender_source_canonical(unit["text"])
            add_canonical(canonical)

            # get_exact_units already splits sentence punctuation. Split the
            # remaining ignorable punctuation so the reverse boundary change
            # (tender colon, bid comma) can still match safely.
            add_fragments(unit["text"])

        # Preserve short pieces that get dropped by get_exact_units. They may
        # become part of one longer unit when a bidder changes punctuation.
        for _, raw_text, _ in tender_pages or ():
            for line in raw_text.splitlines():
                raw_parts = [
                    part
                    for part in cls._split_text_parts(line)
                    if part.strip()
                ]
                parts = [cls.tender_source_canonical(part) for part in raw_parts]
                for raw_part, part in zip(raw_parts, parts):
                    add_canonical(part)
                    add_fragments(raw_part)
                for index in range(len(parts) - 1):
                    combined = parts[index] + parts[index + 1]
                    if len(combined) <= MAX_EXACT_BLOCK_LENGTH:
                        add_canonical(combined)

        return hashes

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
            self.tender_entities.update(self.extract_typed_entities(raw_text))

        if self.build_text_index:
            tender_exact_units = self.get_exact_units(pages)
            self.tender_exact_texts = {unit["text"] for unit in tender_exact_units}
            self.tender_source_hashes = self._build_tender_source_hashes(
                tender_exact_units, pages
            )
            self.tender_field_templates = {
                signature
                for unit in tender_exact_units
                if (
                    signature := self._tender_field_signature(
                        unit["text"], require_placeholder=True
                    )
                )
            }
            for unit in tender_exact_units:
                label = self._tender_field_signature(unit["text"])
                if not label or self._tender_field_signature(
                    unit["text"], require_placeholder=True
                ):
                    continue
                value = self._tender_field_value(unit["text"])
                if value:
                    self.tender_field_values.setdefault(label, set()).add(value)
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
            for part in self._split_text_parts(line):
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
            lines = raw_text.splitlines()
            for line_index, line in enumerate(lines):
                normalized_line = self.normalize(line)
                if (
                    not normalized_line
                    or normalized_line in repeated_lines
                    or self._is_page_number_line(
                        normalized_line, line_index >= len(lines) - 3
                    )
                ):
                    continue
                for part in self._split_text_parts(line):
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
        if text in self.tender_exact_texts:
            return True

        field_signature = self._tender_field_signature(text)
        if field_signature:
            if field_signature not in self.tender_field_templates:
                return False
            field_value = self._tender_field_value(text)
            return bool(
                len(re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", field_value)) >= 4
                and field_value
                in self.tender_field_values.get(field_signature, set())
            )

        if text in self.tender_full_text:
            return True

        canonical = self.tender_source_canonical(text)
        if (
            len(canonical) >= MIN_EXACT_LENGTH
            and self._tender_source_digest(canonical) in self.tender_source_hashes
        ):
            return True

        return False

    @staticmethod
    def _is_page_number_line(text, is_trailing=False):
        """Recognize standalone PDF page counters without touching content numbers."""
        normalized = CollusionDetector.normalize(text)
        if re.fullmatch(r"\d{1,4}[/／]\d{1,4}", normalized):
            return True
        if re.fullmatch(r"第?\d{1,4}页(?:共\d{1,4}页)?", normalized):
            return True
        return is_trailing and bool(re.fullmatch(r"\d{1,4}", normalized))

    @staticmethod
    def _is_low_value_boilerplate(text):
        """Suppress only high-confidence bid form boilerplate and generic fields."""
        normalized = CollusionDetector.normalize(text)
        if not normalized:
            return True

        if normalized.count("所有条款无偏差") >= 2:
            return True
        # 中小企业声明函等固定表单会把本包全部标的名称串成超长清单；该清单
        # 来自采购范围而非投标人原创，不因换行/少量漏项形成横向异常。
        if len(normalized) >= 140 and normalized.count("、") >= 12:
            return True
        if "无偏离" in normalized:
            return True
        if "招标文件要求" in normalized and "投标文件对应内容" in normalized:
            return True
        if "商务和技术偏差表" in normalized and (
            "偏差说明" in normalized or "所有条款无偏差" in normalized
        ):
            return True
        if (
            "投标文件" in normalized
            and "投标单位名称" in normalized
            and "法定代表人" in normalized
        ):
            return True
        if (
            "参加贵方组织" in normalized
            and "项目名称" in normalized
            and "项目编号" in normalized
            and "公开招标采购" in normalized
        ):
            return True
        # 封面、投标函和授权文件中的项目/主体填空虽会因各投标人都填入同一采购
        # 信息而高度相似，但本质是招标格式的正常完成，不具备横向异常价值。
        if "投标文件" in normalized and "项目名称" in normalized and "项目编号" in normalized and any(
            marker in normalized for marker in ("正本", "副本", "封面", "投标人名称")
        ):
            return True
        if "投标函" in normalized and "项目名称" in normalized and any(
            marker in normalized for marker in ("投标报价", "投标有效期", "遵守本投标文件")
        ):
            return True
        if "授权委托书" in normalized and "委托代理人" in normalized and any(
            marker in normalized for marker in ("法定代表人", "身份证", "授权")
        ):
            return True
        if (
            "政府采购" in normalized
            and "自愿参加本次政府采购活动" in normalized
            and "依法诚信经营" in normalized
        ):
            return True
        if (
            "开标一览表" in normalized
            and "投标报价" in normalized
            and "投标单位名称" in normalized
        ):
            return True
        if (
            "投标人提供的" in normalized
            and "售后服务承诺" in normalized
            and "故障处理措施" in normalized
        ):
            return True
        if "指导意见" in normalized and len(normalized) <= 100:
            return True
        if (
            re.match(r"^[一二三四五六七八九十]+[、.]商务部分资料", normalized)
            and "年度审计报告" in normalized
            and "获奖情况" in normalized
            and len(normalized) <= 140
        ):
            return True

        if "投标人:" in normalized and "电子签章" in normalized and any(
            marker in normalized
            for marker in ("法定代表人", "委托代理人", "身份证复印件")
        ):
            return True

        identity_form_fields = (
            "单位名称:",
            "单位性质:",
            "地址:",
            "成立时间:",
            "经营期限:",
            "姓名:",
            "性别:",
            "年龄:",
            "职务:",
        )
        if "法定代表人身份证明" in normalized and sum(
            field in normalized for field in identity_form_fields
        ) >= 4:
            return True

        if len(normalized) <= 100:
            if re.match(r"^\d+[.]?无\(?其他补充说明\)?", normalized):
                return True
            if normalized.startswith(("邮政编码:", "单位性质:")):
                return True
            if "代理人:" in normalized and "性别:" in normalized and "年龄:" in normalized:
                return True
            if normalized.startswith("增值税税率为"):
                return True
            if normalized.startswith("质量标准为满足国家及行业"):
                return True

        return False

    def _tender_shingle_coverage(self, text):
        if not self.tender_unit_index:
            return 0.0
        normalized = self.normalize(text)
        cached = self._tender_coverage_cache.get(normalized)
        if cached is not None:
            return cached
        signature = self._shingles(normalized)
        if not signature:
            return 0.0
        tender_postings = self.tender_unit_index["postings"]
        shingle_coverage = (
            sum(shingle in tender_postings for shingle in signature) / len(signature)
        )
        direct_coverage = 0.0
        if self.tender_full_text and len(normalized) >= DIRECT_TENDER_WINDOW:
            starts = list(range(
                0,
                len(normalized) - DIRECT_TENDER_WINDOW + 1,
                max(1, DIRECT_TENDER_WINDOW // 2),
            ))
            final_start = len(normalized) - DIRECT_TENDER_WINDOW
            if final_start not in starts:
                starts.append(final_start)
            direct_coverage = sum(
                normalized[start : start + DIRECT_TENDER_WINDOW]
                in self.tender_full_text
                for start in starts
            ) / len(starts)
        coverage = max(shingle_coverage, direct_coverage)
        if len(self._tender_coverage_cache) < TENDER_COVERAGE_CACHE_SIZE:
            self._tender_coverage_cache[normalized] = coverage
        return coverage

    def _shared_nontender_shingle_stats(self, text_a, text_b):
        """Measure shared wording that cannot be explained by the tender text."""
        shared = self._shingles(text_a) & self._shingles(text_b)
        if not shared:
            return 0, 0.0
        if not self.tender_unit_index:
            return len(shared), 1.0
        tender_postings = self.tender_unit_index["postings"]
        novel_count = sum(shingle not in tender_postings for shingle in shared)
        return novel_count, novel_count / len(shared)

    def _nontender_runs(self, text):
        """Return contiguous wording not covered by the tender's character shingles.

        PDF tables often interleave two columns and duplicate fragments.  A whole
        extracted row therefore may not align with a single tender unit even though
        nearly all of its wording comes from the tender.  Marking source-covered
        character spans avoids treating the artificial joins between those fragments
        as bidder-authored prose.
        """
        normalized = self.normalize(text)
        if not normalized or not self.tender_unit_index:
            return [normalized] if normalized else []
        cached = self._nontender_runs_cache.get(normalized)
        if cached is not None:
            return cached
        postings = self.tender_unit_index["postings"]
        covered = [False] * len(normalized)
        if len(normalized) < SHINGLE_SIZE:
            return [normalized]
        for start in range(len(normalized) - SHINGLE_SIZE + 1):
            if hash(normalized[start : start + SHINGLE_SIZE]) not in postings:
                continue
            for index in range(start, start + SHINGLE_SIZE):
                covered[index] = True
        if self.tender_full_text and len(normalized) >= DIRECT_TENDER_WINDOW:
            for start in range(len(normalized) - DIRECT_TENDER_WINDOW + 1):
                if all(covered[start : start + DIRECT_TENDER_WINDOW]):
                    continue
                if normalized[start : start + DIRECT_TENDER_WINDOW] not in self.tender_full_text:
                    continue
                for index in range(start, start + DIRECT_TENDER_WINDOW):
                    covered[index] = True
        runs = []
        start = None
        for index, is_covered in enumerate(covered + [True]):
            if not is_covered and start is None:
                start = index
            elif is_covered and start is not None:
                value = normalized[start:index].strip(" ,.;:!?，。；：！？、()（）[]【】{}")
                if value:
                    runs.append(value)
                start = None
        if len(self._nontender_runs_cache) < TENDER_COVERAGE_CACHE_SIZE:
            self._nontender_runs_cache[normalized] = runs
        return runs

    def _has_substantial_shared_nontender_content(self, text_a, text_b):
        """Keep mixed tender rows only when both bids share a real novel passage."""
        runs_a = self._nontender_runs(text_a)
        runs_b = self._nontender_runs(text_b)
        for left in runs_a:
            left_information = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", left)
            if len(left_information) < MIN_NUMERIC_NONTENDER_RUN:
                continue
            for right in runs_b:
                right_information = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", right)
                minimum_length = min(len(left_information), len(right_information))
                if minimum_length < MIN_NUMERIC_NONTENDER_RUN:
                    continue
                contains_numeric_detail = (
                    len(re.findall(r"\d+(?:\.\d+)?", left_information)) >= 2
                    and len(re.findall(r"\d+(?:\.\d+)?", right_information)) >= 2
                )
                if minimum_length < MIN_SHARED_NONTENDER_RUN and not contains_numeric_detail:
                    continue
                length_ratio = minimum_length / max(len(left_information), len(right_information))
                if length_ratio < 0.65:
                    continue
                if SequenceMatcher(
                    None, left_information, right_information, autojunk=False
                ).ratio() >= 0.88:
                    return True
        return False

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

    @staticmethod
    def _is_repeated_extraction_line(text):
        """Detect one table cell emitted twice on the same extracted line."""
        normalized = CollusionDetector.normalize(text)
        if len(normalized) < 12 or len(normalized) % 2:
            return False
        midpoint = len(normalized) // 2
        return normalized[:midpoint] == normalized[midpoint:]

    @staticmethod
    def _is_low_value_tender_edit(edit, tender_text=""):
        """Reject table artifacts and trivial structural changes in tender text."""
        original = edit["original"]
        modified = edit["modified"]

        # Row numbers can be interleaved into a cell during PDF extraction, for
        # example "32菜单管理".  They are not evidence of a bidder's edit.
        if re.search(r"\d[\u4e00-\u9fff]", original) and not re.search(
            r"\d+(?:\.\d+)?(?:年|月|日|小时|分钟|天|周|次|个|项|台|套|件|页|%|万元|元)",
            original,
        ):
            return True

        # PDF 提取会把表格里“数量 13套”和下一行序号“8系统软件”拼成
        # “13套8系统软件”插进句子中间。数量单位后紧跟另一个数字序号是表格
        # 拼接特征，该片段并非投标人对招标原文的真实删改。
        if re.search(
            r"\d+(?:\.\d+)?(?:台|套|件|项|个|张|把|批|块|组|辆|架|部)\d",
            original,
        ):
            return True

        # A short removed cell heading or suffix is normally caused by a
        # heading being rendered separately in the bidder's document.  It has
        # no independent review value.  Keep replacements (including numeric
        # changes) so substantive requirements remain detectable.
        if not modified and len(original) <= 6:
            return True
        if not modified and re.fullmatch(r"提供.{1,8}服务[,]?", original):
            return True
        if not original and len(modified) < 3:
            return True
        if not original and re.fullmatch(
            r"(?:【[^】]{1,10}】|[\u4e00-\u9fff]{1,8})[:：、]?", modified
        ):
            return True
        if original in {"≥", "≤", ">", "<"} and modified in {"", ":", "："}:
            return True
        if len(original) == 1 and len(modified) == 1 and all(
            value in "0123456789:：,，.;；、" for value in (original, modified)
        ):
            source_start = max(0, int(edit.get("source_start") or 0) - 12)
            source_end = min(
                len(tender_text), int(edit.get("source_end") or 0) + 12
            )
            context = tender_text[source_start:source_end]
            numeric_requirement = re.search(
                rf"(?:质保|保修|期限|工期|交付|响应|数量|得分|扣分|价格|金额)"
                rf"[^0-9]{{0,8}}{re.escape(original)}|"
                rf"{re.escape(original)}(?:年|月|日|天|小时|分钟|次|个|项|台|套|件|页|"
                rf"%|万元|元|mm|cm|kg|m²|m2|m3)",
                context,
            )
            if not numeric_requirement:
                return True
        if (
            len(modified) == 1
            and modified.isdigit()
            and re.search(r"[\u4e00-\u9fff]", original)
            and len(original) <= 16
            and not re.search(
                r"[一二三四五六七八九十百千万两\d]+"
                r"(?:年|月|日|天|小时|分钟|次|个|项|台|套|件|页|分|元)",
                original,
            )
        ):
            return True
        # 仅删除招标格式中的变量占位符（如“（标的名称）”）不会形成投标人之间
        # 的独立共同编辑；它与填表、模板转换的方式有关，不应进入串标线索。
        if not modified and re.fullmatch(r"[（(][^（）()]{1,24}[）)](?:[,，:：])?", original):
            return True
        return False

    @staticmethod
    def _is_form_field_completion(edit, tender_text):
        """识别招标格式中项目/主体字段的正常填写，不把它当作共同实质编辑。

        判断依赖原文局部字段标签而非具体项目名称、采购人或企业名称，因此可用于
        不同项目。只处理新增或替换占位符的情形，数值、期限、技术参数等仍完整保留。
        """
        original = str(edit.get("original") or "")
        modified = str(edit.get("modified") or "")
        if not modified:
            return False
        start = max(0, int(edit.get("source_start") or 0) - 40)
        end = min(len(tender_text), int(edit.get("source_end") or 0) + 40)
        context = tender_text[start:end]
        field_markers = (
            "项目名称", "项目编号", "采购人", "招标人", "采购代理", "供应商", "投标人",
            "法定代表人", "委托代理人", "联系人", "日期", "包号", "标包",
        )
        placeholder = bool(re.fullmatch(r"\s*(?:[（(][^（）()]{0,24}[）)])?\s*", original))
        return placeholder and any(marker in context for marker in field_markers)

    @staticmethod
    def _is_leading_table_title_insertion(edit, tender_text):
        """Recognize a table row title moved before the tender's cell text."""
        inserted = edit["modified"]
        return (
            not edit["original"]
            and edit["source_start"] == 0
            and edit["target_start"] == 0
            and 2 <= len(inserted) <= 12
            and not re.search(r"[,;:，；：]", inserted)
            and tender_text.startswith(("提供", "支持", "在", "可", "应"))
        )

    @staticmethod
    def _has_substantive_tender_change(changes):
        """Keep numeric changes, but not isolated one-character table shifts."""
        for change in changes:
            original = change["original"]
            modified = change["modified"]
            if original == "（此处新增）":
                original = ""
            if modified == "（删除）":
                modified = ""
            if original.isdigit() and modified.isdigit():
                return True
            if original and modified:
                return True
            if max(len(original), len(modified)) >= 3:
                return True
        return False

    @staticmethod
    def _mark_voice_adaptation_clusters(source, target, edits):
        """给“义务主体→第一人称”编辑簇打标，而非孤立地判断单个字符差异。

        ``SequenceMatcher`` 会把“实施方须→我方会”拆成“实施→我”与“须→会”，
        中间的“方”仍是 equal。若只标前一项，后一个语气词改动会使整条常规
        响应误判为实质共同改动。这里仅把两侧都至多隔一个相同字符的连续编辑
        合为簇，并要求整个簇严格符合“义务主体 + 可选义务语气”到“第一人称
        + 可选承诺语气”的模式；数值、期限等相邻之外的改动不会被一并降权。
        """
        ordered = sorted(
            edits.values(),
            key=lambda item: (item["source_start"], item["source_end"], item["target_start"]),
        )
        if not ordered:
            return
        clusters = []
        current = [ordered[0]]
        source_end = ordered[0]["source_end"]
        target_end = ordered[0]["target_end"]
        for edit in ordered[1:]:
            # “实施→我” 与 “须→会”之间仅隔同一个“方”；允许这种很短的
            # 等值连接，但不跨越“提供”等实质文本去吞并下一处数值改动。
            near_source = edit["source_start"] - source_end <= 1
            near_target = edit["target_start"] - target_end <= 1
            if near_source and near_target:
                current.append(edit)
            else:
                clusters.append(current)
                current = [edit]
            source_end = edit["source_end"]
            target_end = edit["target_end"]
        clusters.append(current)

        subject_terms = (
            "服务提供", "实施", "供货", "服务", "承包", "施工", "中标", "成交",
            "供应", "投标", "乙", "卖", "制造", "厂家",
        )
        subject_pattern = "|".join(map(re.escape, subject_terms))
        source_pattern = re.compile(
            rf"(?:{subject_pattern})(?:方|人|商|单位|企业)?(?:须|应|需|将|负责|承诺|保证)?$"
        )
        target_pattern = re.compile(
            r"(?:我|本)(?:方|司|公司|单位)?(?:会|将|应|需|负责|承诺|保证)?$"
        )
        for cluster in clusters:
            source_start = min(item["source_start"] for item in cluster)
            source_end = max(item["source_end"] for item in cluster)
            target_start = min(item["target_start"] for item in cluster)
            target_end = max(item["target_end"] for item in cluster)
            original = source[source_start:source_end]
            modified = target[target_start:target_end]
            if source_pattern.fullmatch(original) and target_pattern.fullmatch(modified):
                for item in cluster:
                    item["voice_adaptation"] = True

    def _is_segment_artifact_deletion(self, edit, tender_text):
        """排除换行/双栏分段造成的“删除”假象。

        PDF 提取会按版式切行：投标人填入空项（如“90 天”）或双栏排版都会让
        招标原文的一句话在投标文件中拆到相邻提取单元，对齐时被误判为整句删除。
        只有该片段在投标全文中、且紧邻其在招标原文的前后文锚点时，才认为它是
        被拆到相邻单元而非真实删除。不能仅因同一句在文件其他无关条款重复出现
        就压掉共同删除线索。
        """
        if edit["modified"]:
            return False
        haystacks = getattr(self, "_fulltext_haystacks", None)
        if not haystacks:
            return False
        needle = re.sub(r"\s+", "", edit["original"]).strip(
            " ,，。；;：:、（）()【】《》\"'“”‘’"
        )
        if len(needle) < 4:
            return False
        try:
            source_start, source_end = int(edit["source_start"]), int(edit["source_end"])
        except (KeyError, TypeError, ValueError):
            return False
        # 取删除点相邻的原文作为短锚点；优先使用左锚点，段首删除则使用右锚点。
        # 6 个字符足够区分大多数条款，又能容忍 PDF 提取中很短的断行或空项填入。
        left_anchor = tender_text[max(0, source_start - 14):source_start][-6:]
        right_anchor = tender_text[source_end:source_end + 14][:6]
        if len(left_anchor) < 4:
            left_anchor = ""
        if len(right_anchor) < 4:
            right_anchor = ""
        if not left_anchor and not right_anchor:
            return False
        for haystack in haystacks:
            start = 0
            while True:
                position = haystack.find(needle, start)
                if position < 0:
                    break
                # 锚点必须贴近该片段；较大的窗口会把前一个无关条款也误当作
                # 同一上下文。16 个归一化字符足以覆盖断行、标点和短填空。
                before = haystack[max(0, position - 16):position]
                after = haystack[position + len(needle):position + len(needle) + 16]
                # 左锚点只能出现在片段之前，右锚点只能出现在片段之后；否则
                # “后续条款的前文”会误命中右锚点，反而掩盖真实删除。
                if (left_anchor and left_anchor in before) or (right_anchor and right_anchor in after):
                    return True
                start = position + 1
        return False

    def _is_segment_artifact_insertion(self, edit, tender_text):
        """排除招标比较单元在换行/分页处截断造成的“新增”假象。

        招标原文的一句话被 PDF 提取拆成两个单元时，比较单元只保留前半句，
        投标文件中的完整句子对齐后会把招标原文自己的后半句误判为投标人
        “新增”。仅当该片段在招标全文中、且紧邻其在比较单元的截断锚点时，
        才认为它是被截断的原文续接而非真实新增。方向与
        ``_is_segment_artifact_deletion`` 相反，逻辑对称：删除侧查投标全文，
        插入侧查招标全文（``tender_full_text`` 已由 ``load_tender`` 归一化）。
        """
        if edit["original"]:
            return False
        haystack = getattr(self, "tender_full_text", "") or ""
        if not haystack:
            return False
        needle = re.sub(r"\s+", "", edit["modified"]).strip(
            " ,，。；;：:、（）()【】《》\"'“”‘’"
        )
        if len(needle) < 4:
            return False
        try:
            source_start = int(edit["source_start"])
        except (KeyError, TypeError, ValueError):
            return False
        # 插入点的前文作为左锚点；右锚点取插入点之后的原文开头（纯插入时
        # source_end == source_start）。锚点贴近片段出现位置才算同一上下文。
        left_anchor = tender_text[max(0, source_start - 14):source_start][-6:]
        right_anchor = tender_text[source_start:source_start + 14][:6]
        if len(left_anchor) < 4:
            left_anchor = ""
        if len(right_anchor) < 4:
            right_anchor = ""
        if not left_anchor and not right_anchor:
            return False
        start = 0
        while True:
            position = haystack.find(needle, start)
            if position < 0:
                break
            before = haystack[max(0, position - 16):position]
            after = haystack[position + len(needle):position + len(needle) + 16]
            if (left_anchor and left_anchor in before) or (
                right_anchor and right_anchor in after
            ):
                return True
            start = position + 1
        return False

    def _shared_tender_edit_evidence(self, tender_text, text_a, text_b):
        """Prove that A and B made substantially the same edits to tender text."""
        if not tender_text or tender_text == text_a or tender_text == text_b:
            return None
        if any(
            self._looks_like_table_extraction(text)
            for text in (tender_text, text_a, text_b)
        ):
            return None
        if "中小企业声明函" in tender_text:
            return None

        edits_a = self._edit_operations(tender_text, text_a)
        edits_b = self._edit_operations(tender_text, text_b)
        edits_a = {
            signature: edit
            for signature, edit in edits_a.items()
            if not self._is_low_value_tender_edit(edit, tender_text)
            and not self._is_form_field_completion(edit, tender_text)
            and not self._is_leading_table_title_insertion(edit, tender_text)
            and not self._is_segment_artifact_deletion(edit, tender_text)
            and not self._is_segment_artifact_insertion(edit, tender_text)
        }
        edits_b = {
            signature: edit
            for signature, edit in edits_b.items()
            if not self._is_low_value_tender_edit(edit, tender_text)
            and not self._is_form_field_completion(edit, tender_text)
            and not self._is_leading_table_title_insertion(edit, tender_text)
            and not self._is_segment_artifact_deletion(edit, tender_text)
            and not self._is_segment_artifact_insertion(edit, tender_text)
        }
        self._mark_voice_adaptation_clusters(tender_text, text_a, edits_a)
        self._mark_voice_adaptation_clusters(tender_text, text_b, edits_b)
        shared_signatures = {
            signature
            for signature in set(edits_a) & set(edits_b)
        }
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
            if edit.get("voice_adaptation"):
                public_change["voice_adaptation"] = True
            evidence.append(public_change)
        return {
            "changes": evidence,
            "coverage": round(coverage * 100, 1),
            # 这是基于全部共同编辑（而非仅展示的前三条）得到的语义摘要，供
            # 信号层安全降权，避免显示截断掩盖后续实质改动。
            "voice_adaptation_only": bool(shared_signatures) and all(
                bool(edits_a[key].get("voice_adaptation")) for key in shared_signatures
            ),
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
                if self._is_repeated_extraction_line(line):
                    continue
                for punctuation_match in punctuation_pattern.finditer(line):
                    cluster = punctuation_match.group()
                    is_ellipsis = len(cluster) >= 3 and set(cluster) <= {".", "。"}
                    if is_ellipsis:
                        continue
                    # “.；”“。；”等多用于参数表单元格结尾，并非重复标点。
                    # 只把同类标点的真正重复视为高置信错误。
                    canonical_cluster = cluster.translate(str.maketrans(
                        {"，": ",", "。": ".", "；": ";", "：": ":"}
                    ))
                    if len(set(canonical_cluster)) != 1:
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
                # 技术响应表常把“招标要求/投标响应”两列的同一行连续抽取两次。
                # 编号和正文都完全相同表示版式重复，不是编号错误；相同编号但正文
                # 不同仍按原逻辑报告。
                if self.normalize(previous["line"]) == self.normalize(current["line"]):
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
            # 同一表格单元格被 PDF 解析器在一行内重复输出时，左右括号也会
            # 跟着重复入栈。该行并不存在真实的括号缺失，不能在全局括号
            # 扫描阶段重新把它报成错误。
            if self._is_repeated_extraction_line(line):
                continue
            # 表格单元格或跨行提取常在顿号/逗号处被截断；此时单行内的左括号
            # 不完整不能证明源文件存在标点错误。
            if character in bracket_pairs and re.search(r"[,，、:：;；]\s*$", line):
                continue
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
            # 解析表格时，编号/标点常由单元格顺序或换行造成。该类错误没有独立
            # 来源价值；保留计算错误和正文文字错误，避免掩盖真正的共同异常。
            if issue_a["kind"] in {"punctuation", "numbering"} and self._looks_like_table_extraction(issue_a["text"]):
                continue
            coverage_a = self._tender_shingle_coverage(issue_a["text"])
            coverage_b = self._tender_shingle_coverage(issue_b["text"])
            if (
                min(coverage_a, coverage_b) >= MIN_TENDER_FRAGMENT_COVERAGE
                and not self._has_substantial_shared_nontender_content(
                    issue_a["text"], issue_b["text"]
                )
            ):
                continue
            matches.append(
                {
                    "type": "shared_error",
                    "error_kind": issue_a["kind"],
                    "text_a": issue_a["text"],
                    "text_b": issue_b["text"],
                    "page_a": issue_a["page"],
                    "page_b": issue_b["page"],
                    "similarity": 100.0,
                    "tender_coverage_a": round(coverage_a, 4),
                    "tender_coverage_b": round(coverage_b, 4),
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

        def append_candidate(candidate):
            if len(candidate) >= 3:
                groups.append(candidate)
                return
            strict_group = []
            for pair in candidate:
                if strict_group and not (
                    pair[0]["order"] == strict_group[-1][0]["order"] + 1
                    and pair[1]["order"] == strict_group[-1][1]["order"] + 1
                ):
                    groups.append(strict_group)
                    strict_group = []
                strict_group.append(pair)
            if strict_group:
                groups.append(strict_group)

        for unit_a, unit_b in pairs:
            if current:
                gap_a = unit_a["order"] - current[-1][0]["order"]
                gap_b = unit_b["order"] - current[-1][1]["order"]
                is_nearby = (
                    1 <= gap_a <= 4
                    and 1 <= gap_b <= 4
                    and abs(gap_a - gap_b) <= 1
                    and unit_a["page"] - current[-1][0]["page"] <= 1
                    and unit_b["page"] - current[-1][1]["page"] <= 1
                )
            else:
                is_nearby = True
            added_length = len(unit_a["text"]) + (1 if current else 0)
            if current and (
                not is_nearby
                or current_length + added_length > MAX_EXACT_BLOCK_LENGTH
            ):
                append_candidate(current)
                current = []
                current_length = 0
                added_length = len(unit_a["text"])
            current.append((unit_a, unit_b))
            current_length += added_length

        if current:
            append_candidate(current)
        return groups

    def _find_exact_collisions(self, units_a, units_b):
        filtered_a = [
            unit
            for unit in units_a
            if not self._is_tender_copy(unit["text"])
            and not self._is_low_value_boilerplate(unit["text"])
        ]
        filtered_b = [
            unit
            for unit in units_b
            if not self._is_tender_copy(unit["text"])
            and not self._is_low_value_boilerplate(unit["text"])
        ]
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
            if self._is_low_value_boilerplate(text):
                continue
            matched_texts.update(unit_a["text"] for unit_a, _ in group)
            tender_references = []
            shared_edits = []
            tender_similarities = []
            tender_derived_segments = 0
            tender_skeleton_segments = 0
            tender_shingle_segments = 0
            tender_edit_voice_only = True
            has_tender_edit = False

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
                if edit_evidence and not self._has_substantive_tender_change(
                    edit_evidence["changes"]
                ):
                    edit_evidence = None
                if not edit_evidence:
                    if tender_match["ratio"] >= TENDER_DERIVED_RATIO:
                        tender_derived_segments += 1
                    continue
                has_tender_edit = True
                tender_edit_voice_only = (
                    tender_edit_voice_only and bool(edit_evidence.get("voice_adaptation_only"))
                )
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

            tender_coverage = self._tender_shingle_coverage(text)
            if (
                not shared_edits
                and tender_coverage >= MIN_TENDER_FRAGMENT_COVERAGE
                and not self._has_substantial_shared_nontender_content(text, text)
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
                    "tender_coverage_a": round(tender_coverage, 4),
                    "tender_coverage_b": round(tender_coverage, 4),
                    "tender_text": "；".join(tender_references[:3]),
                    "shared_edits": shared_edits,
                    "voice_adaptation_only": bool(shared_edits) and has_tender_edit and tender_edit_voice_only,
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
            lines = raw_text.splitlines()
            for line_index, line in enumerate(lines):
                normalized_line = self.normalize(line)
                if (
                    normalized_line
                    and normalized_line not in repeated_lines
                    and not re.fullmatch(r"\d{1,4}", normalized_line)
                    and not self._is_page_number_line(
                        normalized_line, line_index >= len(lines) - 3
                    )
                ):
                    kept_lines.append(line.strip())
            page_text = "".join(kept_lines)
            for part in self._split_text_parts(page_text, split_commas=False):
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
        # 仅支持 18 位二代身份证（含校验位验证）。15 位一代证件在投标文件中
        # 基本不再出现，而报价等长数字串极易误命中，代价远大于漏检。
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
            rf"(?<!\d)(?:\d{separator_pattern}){{17}}[0-9Xx](?!\d)",
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

    def extract_typed_entities(self, text):
        """提取可横向核验的实体并保留类别，供工作台生成审慎线索。

        姓名、地址只接受带明确字段标签的写法，避免把普通中文或公共模板误判为关联。
        ``extract_entities`` 保持原返回值和行为，确保既有 /bijiao 流程兼容。
        """
        if not text:
            return set()
        normalized_text = unicodedata.normalize("NFKC", text)
        typed = set()
        separator_pattern = r"[\s\-—_]*"
        identities = re.findall(
            rf"(?<!\d)(?:\d{separator_pattern}){{17}}[0-9Xx](?!\d)",
            normalized_text,
        )
        for identity in identities:
            value = re.sub(r"[\s\-—_]+", "", identity).upper()
            if self._is_valid_cn_id(value):
                typed.add(("person_identity", value))
        phones = re.findall(rf"(?<!\d)1{separator_pattern}[3-9](?:{separator_pattern}\d){{9}}(?!\d)", normalized_text)
        typed.update(("phone", re.sub(r"[\s\-—_]+", "", phone)) for phone in phones)
        emails = re.findall(r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", normalized_text)
        typed.update(("email", email.lower()) for email in emails)

        name_pattern = re.compile(
            r"(?:法定代表人|委托代理人|授权代表|项目负责人|项目经理|技术负责人|联系人|姓名)\s*(?:[:：]|[ \t]+)\s*([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])"
        )
        invalid_names = {"签字", "盖章", "姓名", "联系人", "负责人", "代理人", "项目经理", "技术负责人"}
        for name in name_pattern.findall(normalized_text):
            if name not in invalid_names:
                typed.add(("person_name", name))

        address_pattern = re.compile(r"(?:注册地址|办公地址|通讯地址|联系地址|住址|地址)[ \t]*[:：][ \t]*([^\r\n。；;]{6,100})")
        address_markers = ("省", "市", "区", "县", "路", "街", "镇", "乡", "村", "号", "楼", "室", "园", "大厦", "街道")
        for raw_address in address_pattern.findall(normalized_text):
            value = re.split(r"(?:电话|邮编|邮箱|E-mail|Email)\s*[:：]", raw_address, maxsplit=1, flags=re.I)[0]
            value = re.sub(r"\s+", "", value).strip("，,；;。")
            if 6 <= len(value) <= 80 and "@" not in value and any(marker in value for marker in address_markers):
                typed.add(("address", value))
        return typed

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
        exact_prefixes = {}
        for exact_text in exact_sentences:
            if len(exact_text) < MIN_EXACT_LENGTH:
                continue
            prefix = exact_text[:MIN_EXACT_LENGTH]
            matches = exact_prefixes.setdefault(prefix, [])
            if matches is not None:
                if len(matches) < MAX_POSTINGS_PER_SHINGLE:
                    matches.append(exact_text)
                else:
                    exact_prefixes[prefix] = None

        filtered_b = [
            unit for unit in units_b if not self._is_low_value_boilerplate(unit["text"])
        ]
        index_b = self._build_unit_index(filtered_b)
        proposals = []
        seen_pairs = set()

        for index_a, unit_a in enumerate(units_a):
            text_a = unit_a["text"]
            if self._is_low_value_boilerplate(text_a):
                continue
            for candidate in self._best_candidates(text_a, index_b, minimum_ratio=0.78):
                unit_b = candidate["unit"]
                text_b = unit_b["text"]
                if text_a == text_b or (text_a in exact_sentences and text_b in exact_sentences):
                    continue
                if (text_a in exact_sentences and text_a in text_b) or (
                    text_b in exact_sentences and text_b in text_a
                ):
                    continue
                shared_exact_parts = set()
                for start in range(len(text_a) - MIN_EXACT_LENGTH + 1):
                    prefix_matches = exact_prefixes.get(
                        text_a[start : start + MIN_EXACT_LENGTH]
                    )
                    if not prefix_matches:
                        continue
                    shared_exact_parts.update(
                        exact_text
                        for exact_text in prefix_matches
                        if exact_text in text_b
                    )
                if any(
                    len(exact_text) >= MIN_FUZZY_DISPLAY_LENGTH
                    for exact_text in shared_exact_parts
                ) or (
                    len(shared_exact_parts) >= 2
                    and sum(map(len, shared_exact_parts)) >= 18
                ):
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
            voice_adaptation_only = False
            error_kind = ""
            verified_tender_edit = False

            table_a = self._looks_like_table_extraction(text_a)
            table_b = self._looks_like_table_extraction(text_b)
            tender_coverage_a = self._tender_shingle_coverage(text_a)
            tender_coverage_b = self._tender_shingle_coverage(text_b)
            novel_count, novel_ratio = self._shared_nontender_shingle_stats(
                text_a, text_b
            )
            has_substantial_nontender_table_content = (
                novel_count >= MIN_SHARED_NONTENDER_SHINGLES
                and novel_ratio >= MIN_SHARED_NONTENDER_RATIO
                and
                self._has_substantial_shared_nontender_content(text_a, text_b)
            )

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
                    voice_adaptation_only = bool(edit_evidence.get("voice_adaptation_only"))
                    verified_tender_edit = True
                    result_type = "tender_related"
                    badges = ["招标原文关联", "已验证共同修改"]
                    desc = (
                        f"A/B 相似度 {proposal['similarity']:.1f}%，相对招标原文存在 "
                        f"{len(shared_edits)} 处相同改动"
                    )

            if (
                not verified_tender_edit
                and min(tender_coverage_a, tender_coverage_b)
                >= MIN_TENDER_FRAGMENT_COVERAGE
                and not has_substantial_nontender_table_content
            ):
                continue
            if not verified_tender_edit and self.tender_unit_index:
                derived_a = tender_a or self._best_tender_match(
                    text_a, minimum_ratio=0.55, minimum_jaccard=0.12
                )
                derived_b = tender_b or self._best_tender_match(
                    text_b, minimum_ratio=0.55, minimum_jaccard=0.12
                )
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

                if is_tender_derived and not has_substantial_nontender_table_content:
                    continue

            proposal.pop("index_a")
            proposal.pop("index_b")
            proposal.update(
                {
                    "type": result_type,
                    "tender_text": tender_text,
                    "shared_edits": shared_edits,
                    "voice_adaptation_only": voice_adaptation_only,
                    "error_kind": error_kind,
                    "tender_coverage_a": round(tender_coverage_a, 4),
                    "tender_coverage_b": round(tender_coverage_b, 4),
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

    @staticmethod
    def _clean_metadata_value(value):
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()[:300]

    @classmethod
    def _is_meaningful_metadata_value(cls, value):
        normalized = cls._clean_metadata_value(value).casefold().strip(".-_/ ")
        return normalized not in {
            "",
            "unknown",
            "n/a",
            "na",
            "none",
            "null",
            "anonymous",
            "未知",
            "不详",
            "未设置",
        }

    @classmethod
    def _build_metadata_auxiliary(cls, metadata_a, metadata_b, metadata_tender=None):
        """Build explainable metadata clues without affecting collision scoring."""
        field_definitions = (
            ("title", "文档标题"),
            ("author", "作者/创建者"),
            ("creator", "创建软件"),
            ("producer", "PDF 生成工具"),
            ("creationDate", "创建时间"),
            ("modDate", "修改时间"),
        )

        def select_fields(metadata):
            source = metadata or {}
            return {
                key: cls._clean_metadata_value(source.get(key))
                for key, _ in field_definitions
            }

        files = {
            "file_a": select_fields(metadata_a),
            "file_b": select_fields(metadata_b),
            "tender": select_fields(metadata_tender),
        }
        matches = []
        weak_fields = {"title", "creator", "producer"}
        for key, label in field_definitions:
            value_a = files["file_a"][key]
            value_b = files["file_b"][key]
            if not cls._is_meaningful_metadata_value(
                value_a
            ) or not cls._is_meaningful_metadata_value(value_b):
                continue
            if value_a.casefold() != value_b.casefold():
                continue
            tender_value = files["tender"].get(key, "")
            also_in_tender = bool(
                tender_value and tender_value.casefold() == value_a.casefold()
            )
            matches.append(
                {
                    "field": key,
                    "label": label,
                    "value": value_a,
                    "strength": "weak" if key in weak_fields or also_in_tender else "reference",
                    "also_in_tender": also_in_tender,
                }
            )

        return {
            "notice": "文档属性可能被复制、清除或由同一软件批量生成，仅作辅助排查，不参与相似度分数和雷同结论。",
            "files": files,
            "matches": matches,
        }

    def find_collisions(
        self, path_a, path_b, check_entity=True, check_text=True, check_spelling=False
    ):
        raw_a, pages_a, metadata_a, stats_a = self.extract_text_with_pages(path_a)
        raw_b, pages_b, metadata_b, stats_b = self.extract_text_with_pages(path_b)
        # 供“共同删改”校验判断整句删除是否为换行/双栏分段假象（见
        # _is_segment_artifact_deletion）；与比较单元同样的归一化，内存占用与原文相当。
        self._fulltext_haystacks = (self.normalize(raw_a), self.normalize(raw_b))
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
                page_entities = self.extract_typed_entities(raw_text)
                entities_a.update(page_entities)
                for entity in page_entities:
                    entity_pages_a.setdefault(entity, page_number)

            entities_b = set()
            entity_pages_b = {}
            for page_number, raw_text, _ in pages_b:
                page_entities = self.extract_typed_entities(raw_text)
                entities_b.update(page_entities)
                for entity in page_entities:
                    entity_pages_b.setdefault(entity, page_number)

            for entity_kind, entity in sorted((entities_a & entities_b) - self.tender_entities):
                collisions.append(
                    {
                        "type": "entity",
                        "entity_kind": entity_kind,
                        "text_a": entity,
                        "text_b": entity,
                        "page_a": entity_pages_a.get((entity_kind, entity), 0),
                        "page_b": entity_pages_b.get((entity_kind, entity), 0),
                        "badges": ["敏感实体"],
                        "desc": f"发现相同的{entity_kind}实体信息: {entity}",
                    }
                )

        if check_text:
            exact_collisions, exact_matched_texts = self._find_exact_collisions(
                self.get_exact_units(pages_a),
                self.get_exact_units(pages_b),
            )
            exact_collisions = [
                item
                for item in exact_collisions
                if item["type"] == "tender_related"
                or len(item["text_a"]) >= MIN_EXACT_DISPLAY_LENGTH
            ]

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
                "auxiliary": self._build_metadata_auxiliary(
                    metadata_a, metadata_b, self.tender_metadata
                ),
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
