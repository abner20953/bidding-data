import difflib
import re
from collections import defaultdict
from .text_extractor import extract_content, extract_metadata

# --- Configuration ---
# Threshold for candidate retrieval (N-gram overlap)
# Lower = safer (less likely to miss), Higher = faster.
# 0.3 means "if 30% of bigrams match, consider it a candidate for difflib check"
INDEX_RETRIEVAL_THRESHOLD = 0.3 
SEQ_MATCH_THRESHOLD = 0.85    # Final difflib threshold for "Suspicious"
TENDER_EXCLUDE_THRESHOLD = 0.8 # Final difflib threshold for "Tender Exclusion"

def get_fingerprint(text):
    """
    Generate a fingerprint for text comparison by removing all non-alphanumeric characters.
    """
    text = re.sub(r'\s+', '', text)
    return re.sub(r'[^\w\u4e00-\u9fa5]', '', text)

class SearchIndex:
    """
    Inverted Index for efficient N-gram search.
    Maps Bigrams -> Set of Paragraph IDs.
    """
    def __init__(self, items):
        """
        items: List of dicts (e.g. paragraphs), must have 'text' key.
        """
        self.items = items
        self.index = defaultdict(list)
        self.fingerprints = []
        self._build_index()

    def _get_ngrams(self, text, n=2):
        """Generate bigrams (2-chars) from fingerprint."""
        fp = get_fingerprint(text)
        if len(fp) < n:
            return {fp} if fp else set()
        return {fp[i:i+n] for i in range(len(fp) - n + 1)}

    def _build_index(self):
        for idx, item in enumerate(self.items):
            fp = get_fingerprint(item['text'])
            self.fingerprints.append(fp)
            ngrams = self._get_ngrams(item['text'])
            for gram in ngrams:
                self.index[gram].append(idx)

    def search_candidates(self, query_text, top_n=10):
        """
        Find items that share N-grams with query_text.
        Returns: List of (index, item, approx_score)
        """
        query_ngrams = self._get_ngrams(query_text)
        if not query_ngrams:
            return []

        # Count occurrences (Voting)
        hits = defaultdict(int)
        for gram in query_ngrams:
            if gram in self.index:
                for idx in self.index[gram]:
                    hits[idx] += 1
        
        # Filter candidates
        candidates = []
        total_grams = len(query_ngrams)
        
        for idx, count in hits.items():
            # Basic overlap ratio: matches / total_query_grams
            overlap = count / total_grams
            if overlap >= INDEX_RETRIEVAL_THRESHOLD:
                candidates.append((idx, self.items[idx], overlap))
        
        # Sort by overlap score descending
        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates[:top_n]
    
    def has_match(self, query_text, threshold):
        """
        Check if query matches any item in index with difflib ratio > threshold.
        Optimized: First find candidates, then run difflib.
        """
        fp_query = get_fingerprint(query_text)
        
        # 1. Exact/Substring Check (Fastest)
        # Note: This is an O(N) scan but on fingerprints, still fast enough for Tender check typically.
        # But for huge tender, we might rely on the index or set lookups if exact.
        # Let's trust the Candidate Search to handle "similar" ones.
        # For exact match, overlap is 1.0, so it will be retrieved.
        
        candidates = self.search_candidates(query_text, top_n=5)
        for _, item, _ in candidates:
            # Use difflib on fingerprint or text? 
            # Original logic used fingerprint for get_close_matches.
            # We can use fingerprint for speed consistency.
            fp_item = get_fingerprint(item['text'])
            
            # Exact check
            if fp_query == fp_item: return True
            if fp_query in fp_item: return True # Substring check implementation
            
            # Fuzzy check
            ratio = difflib.SequenceMatcher(None, fp_query, fp_item).ratio()
            if ratio >= threshold:
                return True
                
        return False

def segment_paragraphs_with_page(content_list):
    """
    Smartly segment text into paragraphs while tracking page numbers.
    (Preserved Original Logic)
    """
    paragraphs = []
    
    # Flatten all lines with their page numbers
    all_lines = []
    for item in content_list:
        page_lines = item['text'].split('\n')
        page_num = item['page']
        for line in page_lines:
            stripped = line.strip()
            if stripped:
                all_lines.append({"text": stripped, "page": page_num})
    
    if not all_lines:
        return []

    stop_pattern = re.compile(r'[。！？!?;；：:]$')
    # Regex to detect lines starting with numbers like "1.", "2、", "(3)", "（4）"
    bullet_pattern = re.compile(r'^(\d+[.、]|[（(]\d+[)）])')
    
    buffer = ""
    buffer_start_page = -1
    
    for item in all_lines:
        line = item['text']
        page = item['page']
        
        if not buffer:
            buffer = line
            buffer_start_page = page
        else:
            is_short_line = len(buffer) < 40
            has_stop = stop_pattern.search(buffer)
            # Check if the NEW line is a numbered item (force break)
            is_new_bullet = bullet_pattern.match(line)
            
            if has_stop or is_short_line or is_new_bullet:
                paragraphs.append({"text": buffer, "page": buffer_start_page})
                buffer = line
                buffer_start_page = page
            else:
                buffer += line
                
    if buffer:
        paragraphs.append({"text": buffer, "page": buffer_start_page})
        
    return paragraphs

COMMON_HEADERS = {
    "招标文件", "投标文件", "目录", "前言", "附录", 
    "技术参数", "技术规格", "商务条款", "评分标准",
    "投标人须知", "特别提示", "申明", "声明", "承诺书",
    "格式", "页码", "正文", "第一章", "第二章", "第三章",
    "第四章", "第五章", "第六章", "第七章", "第八章",
    "一、", "二、", "三、", "四、", "五、", "六、",
    "招标公告", "投标邀请", "法定代表人", "委托代理人",
    "单位名称", "日期", "盖章", "签字", "地址", "电话",
    "传真", "邮箱", "邮编", "年份", "月份", "金额", "备注"
}

def extract_entities(content_list):
    """
    Extracts high-value entities (Phones, ID Cards, Emails).
    """
    entities = {}
    phone_pattern = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
    id_pattern = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')
    email_pattern = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
    
    for item in content_list:
        text = item['text']
        page = item['page']
        
        for match in phone_pattern.findall(text):
            if match not in entities: entities[match] = {"text": match, "page": page}
        for match in id_pattern.findall(text):
            fp = match.upper()
            if fp not in entities: entities[fp] = {"text": match, "page": page}
        for match in email_pattern.findall(text):
            fp = match.lower()
            if fp not in entities: entities[fp] = {"text": match, "page": page}
                
    return entities

def is_significant(text, fingerprint):
    clean_text = text.replace(" ", "").replace(":", "").replace("：", "")
    if clean_text in COMMON_HEADERS: return False
    if len(fingerprint) <= 10: return False
    if re.match(r'^[\d\.,\-\(\)]+$', fingerprint) and len(fingerprint) < 10: return False
    return True

# --- New Helper Functions for Common Errors ---

def detect_broken_tail(text):
    """
    Detect if a paragraph ends abnormally (no punctuation) but is long enough to be a sentence.
    """
    if len(text) < 15: return False # Ignore short headers
    # Ends with common punctuation?
    if re.search(r'[。！？.!?”"”’]$', text.strip()):
        return False
    return True

def parse_sequence_number(text):
    """
    Extracts sequence number from start of text.
    Returns: (number_int, type_str) or (None, None)
    """
    # 1. format "1." or "1、"
    match = re.match(r'^(\d+)[.、]', text)
    if match: return int(match.group(1)), "digit_dot"
    
    # 2. format "(1)" or "（1）"
    match = re.match(r'^[\(（](\d+)[\)）]', text)
    if match: return int(match.group(1)), "paren_digit"
    
    return None, None

def strip_sequence_number_header(text):
    """
    Remove leading sequence numbers like "1.", "2.3", "（1）", "12、" etc.
    """
    clean_text = text.strip()
    return re.sub(r'^(\d+(\.\d+)*|[（(]?\d+[)）]?)\s*[.、:：]?\s*', '', clean_text)

def check_sequence_errors(paragraphs):
    """
    Scans a document for missing sequence numbers.
    Returns list of error dicts: { "missing": int, "context_fp": str, "page": int }
    """
    errors = []
    
    current_type = None
    next_num = None
    
    for i, p in enumerate(paragraphs):
        text = p['text'].strip()
        num, seq_type = parse_sequence_number(text)
        
        if num is None:
            continue
            
        if num == 1 or seq_type != current_type:
            current_type = seq_type
            next_num = 2
        else:
            if seq_type == current_type:
                if num == next_num:
                    next_num += 1
                elif num > next_num:
                    if next_num > 1: # Only report if we established a sequence
                        if (num - next_num) > 2:
                            next_num = num + 1
                        else:
                            errors.append({
                                "missing": next_num,
                                "found": num,
                                "text": text,
                                "page": p['page'],
                                "context_fp": get_fingerprint(text)[:50]
                            })
                            next_num = num + 1
                    else:
                        next_num = num + 1
                else:
                     next_num = num + 1

    return errors

def compare_documents(file_a_path, file_b_path, tender_path):
    # 0. Extract Metadata
    meta_a = extract_metadata(file_a_path)
    meta_b = extract_metadata(file_b_path)
    meta_tender = extract_metadata(tender_path) if tender_path else None

    # 1. Extract content
    content_a = extract_content(file_a_path)
    content_b = extract_content(file_b_path)
    content_tender = extract_content(tender_path) if tender_path else []
    
    # 2. Segment Paragraphs
    paras_a = segment_paragraphs_with_page(content_a)
    paras_b = segment_paragraphs_with_page(content_b)
    paras_tender = segment_paragraphs_with_page(content_tender)
    
    # 3. Build Indices
    # Build Inverted Index for B (optimization)
    index_b = SearchIndex(paras_b)
    fp_map_b = {get_fingerprint(p['text']): p for p in paras_b} # Fast lookup for exact match
    
    # Build Inverted Index for Tender (exclusion optimization)
    # Note: Tender Exclusion relies on full string concatenation for some checks, 
    # but we can check existence efficiently too.
    tender_full_fp = ""
    index_tender = None
    
    if paras_tender:
        tender_full_fp = "".join([get_fingerprint(p['text']) for p in paras_tender])
        index_tender = SearchIndex(paras_tender)

    # 5. Extract Entities
    entities_a = extract_entities(content_a)
    entities_b = extract_entities(content_b)

    suspicious_paragraphs = []
    seen_fps = set()

    # --- Phase 1: Entity Check ---
    for fp, item_a in entities_a.items():
        if fp in entities_b:
            entity_check_fp = get_fingerprint(fp)
            is_excluded = False
            # Check Tender Exclusion (Strict Substring)
            if tender_full_fp and entity_check_fp in tender_full_fp:
                is_excluded = True
            
            if not is_excluded:
                if fp not in seen_fps:
                    item_b = entities_b[fp]
                    suspicious_paragraphs.append({
                        "type": "entity",
                        "text_a": f"[敏感数据] {item_a['text']}",
                        "text_b": f"[敏感数据] {item_b['text']}",
                        "desc": "完全一致的敏感信息 (手机/身份证/邮箱)",
                        "page_a": item_a['page'],
                        "page_b": item_b['page'],
                        "score": 100,
                        "badges": ["敏感数据"]
                    })
                    seen_fps.add(fp)

    # --- Phase 2: Paragraph Comparison ---
    # Optimized loop: O(N) instead of O(N*M)
    
    for i, p_a in enumerate(paras_a):
        text_a = p_a['text']
        fp_a = get_fingerprint(text_a)
        
        if fp_a in seen_fps: continue 
        if not is_significant(text_a, fp_a): continue
        
        # Check if A is also in B (Exact Match) - Needed for Smart Exclusion decision
        is_exact_match_in_b = (fp_a in fp_map_b)
        
        # --- Exclusion Logic (Smart) ---
        if index_tender:
            # 1. Exact/Substring Match
            if tender_full_fp and fp_a in tender_full_fp:
                # If exact match in Tender, ALWAYS exclude (it's public info)
                continue
            
            # 2. Fuzzy Match Check (Threshold 0.8)
            # Find best match in tender using index
            tender_cands = index_tender.search_candidates(text_a, top_n=3)
            best_tender_item = None
            best_tender_ratio = 0.0
            
            for _, t_item, _ in tender_cands:
                t_ratio = difflib.SequenceMatcher(None, fp_a, get_fingerprint(t_item['text'])).ratio()
                if t_ratio > best_tender_ratio:
                    best_tender_ratio = t_ratio
                    best_tender_item = t_item

            if best_tender_ratio >= TENDER_EXCLUDE_THRESHOLD:
                # HIT: A is similar to Tender. Normally Exclude.
                # EXCEPTION: If A == B (Shared) AND A != Tender (Deviation), KEEP IT.
                
                # We already know A != Tender Exact (checked in step 1 above)
                if is_exact_match_in_b:
                     # This is a Shared Deviation! (A~Tender, A==B, A!=Tender)
                     # Pass through to comparison logic below to be flagged.
                     pass
                else:
                    continue # Exclude standard variations
        # -----------------------

        match_type = None
        item_b = None
        desc = ""
        score = 0
        badges = []
        
        # A. Exact Fingerprint Match (Fast Lookup)
        if is_exact_match_in_b:
            item_b = fp_map_b[fp_a]
            text_b = item_b['text']
            
            is_broken_a = detect_broken_tail(text_a)
            is_broken_b = detect_broken_tail(text_b)
            
            match_type = "exact"
            desc = "雷同段落 (内容一致)"
            score = 100
            
            if is_broken_a and is_broken_b:
                badges.append("共同异常断句")
                desc += "，且均存在异常断句"
            
            # --- Shared Deviation Check ---
            # If we are here, and index_tender exists, check if it was a Deviation
            if index_tender:
                # Re-check tender similarity to flag it properly
                # (We could cache this from exclusion step for perf, but fast enough)
                tender_cands = index_tender.search_candidates(text_a, top_n=1)
                for _, t_item, _ in tender_cands:
                     t_ratio = difflib.SequenceMatcher(None, fp_a, get_fingerprint(t_item['text'])).ratio()
                     if t_ratio >= TENDER_EXCLUDE_THRESHOLD:
                         desc += "；检测到与招标文件存在共同差异(疑似错别字/连带修)"
                         badges.append("疑似共同修改")
                         break
            # ------------------------------

            seen_fps.add(fp_a)

        # B. Fuzzy Match (Inverted Index Search)
        else:
            # OPTIMIZATION: Use Index Search for Candidates
            candidates = index_b.search_candidates(text_a, top_n=5)
            
            best_match_item = None
            best_ratio = 0.0
            
            # Check candidates using difflib
            for _, cand_item, _ in candidates:
                cand_fp = get_fingerprint(cand_item['text'])
                
                # Double Check Exclusion for B
                if index_tender:
                     if tender_full_fp and cand_fp in tender_full_fp: continue
                     # For fuzzy matching, we don't apply "Smart Deviation" quite yet (simplification)
                     # Only exclude if B is similar to Tender
                     if index_tender.has_match(cand_item['text'], threshold=TENDER_EXCLUDE_THRESHOLD): continue

                ratio = difflib.SequenceMatcher(None, fp_a, cand_fp).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match_item = cand_item
            
            if best_match_item and best_ratio >= SEQ_MATCH_THRESHOLD:
                item_b = best_match_item
                
                # Check Renumbering
                clean_a = strip_sequence_number_header(text_a)
                clean_b = strip_sequence_number_header(item_b['text'])
                
                if clean_a == clean_b and len(clean_a) < 60:
                     continue 
                
                # Final content display ratio
                display_ratio = difflib.SequenceMatcher(None, text_a, item_b['text']).ratio()
                
                match_type = "fuzzy"
                desc = f"疑似修改/拼写错误 (相似度 {int(display_ratio*100)}%)"
                score = int(display_ratio * 100)
                badges.append("拼写/修改痕迹")

        if match_type:
            suspicious_paragraphs.append({
                "type": match_type,
                "text_a": text_a,
                "text_b": item_b['text'],
                "desc": desc,
                "page_a": p_a['page'],
                "page_b": item_b['page'],
                "score": score,
                "badges": badges
            })

    # --- Phase 3: Common Sequence Errors ---
    seq_errors_a = check_sequence_errors(paras_a)
    seq_errors_b = check_sequence_errors(paras_b)
    
    common_seq_errors = []
    
    for err_a in seq_errors_a:
        for err_b in seq_errors_b:
            if err_a['missing'] == err_b['missing'] and err_a['found'] == err_b['found']:
                ratio = difflib.SequenceMatcher(None, err_a['context_fp'], err_b['context_fp']).ratio()
                if ratio > 0.8:
                    common_seq_errors.append({
                        "missing": err_a['missing'],
                        "found": err_a['found'],
                        "text_a": err_a['text'],
                        "text_b": err_b['text'],
                        "page_a": err_a['page'],
                        "page_b": err_b['page']
                    })
                    break

    suspicious_paragraphs.sort(key=lambda x: x['score'], reverse=True)

    return {
        "paragraphs": suspicious_paragraphs,
        "common_errors": {
            "sequence": common_seq_errors
        },
        "metadata": {
            "file_a": meta_a,
            "file_b": meta_b,
            "tender": meta_tender
        }
    }
