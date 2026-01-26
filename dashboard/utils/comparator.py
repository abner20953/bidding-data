import difflib
import re
from .text_extractor import extract_content, extract_metadata

def get_fingerprint(text):
    """
    Generate a fingerprint for text comparison by removing all non-alphanumeric characters.
    """
    # Remove all whitespace
    text = re.sub(r'\s+', '', text)
    # Remove punctuation, keep word chars and Chinese
    return re.sub(r'[^\w\u4e00-\u9fa5]', '', text)

def segment_paragraphs_with_page(content_list):
    """
    Smartly segment text into paragraphs while tracking page numbers.
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
    # Pattern: Digit(s) + optional dots/digits + optional punctuation + whitespace
    # Matches: "1.", "1.2.", "12、", "（1）", "(1)", "20、", etc.
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
            
        # If new type or starting with 1, reset
        if num == 1 or seq_type != current_type:
            current_type = seq_type
            next_num = 2
        else:
            if seq_type == current_type:
                if num == next_num:
                    next_num += 1
                elif num > next_num:
                    if next_num > 1: # Only report if we established a sequence
                         # Check for large gaps (likely widely separated clauses, not missing items)
                        if (num - next_num) > 2:
                            # Too big a jump (e.g. 1 -> 8), probably unrelated. Reset.
                            next_num = num + 1
                        else:
                            # Found a jump! Expected 2, got 4. Missing 2, 3.
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
                     # num < next_num (duplicate or restart), reset
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
    
    # 3. Build Maps
    fp_map_b = {}
    for p in paras_b:
        fp = get_fingerprint(p['text'])
        if is_significant(p['text'], fp):
            if fp not in fp_map_b:
                fp_map_b[fp] = p
                
    # 4. Tender Exclusion
    tender_full_fp = ""
    tender_fps = []
    if paras_tender:
        tender_fps = [get_fingerprint(p['text']) for p in paras_tender]
        tender_full_fp = "".join(tender_fps)

    # 5. Extract Entities
    entities_a = extract_entities(content_a)
    entities_b = extract_entities(content_b)

    suspicious_paragraphs = []
    seen_fps = set()

    # --- Phase 1: Entity Check ---
    for fp, item_a in entities_a.items():
        if fp in entities_b:
            entity_check_fp = get_fingerprint(fp)
            is_excluded = tender_full_fp and (entity_check_fp in tender_full_fp)
            
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
    b_fingerprints = list(fp_map_b.keys())

    for i, p_a in enumerate(paras_a):
        text_a = p_a['text']
        fp_a = get_fingerprint(text_a)
        
        if fp_a in seen_fps: continue 
        if not is_significant(text_a, fp_a): continue
        
        # --- Exclusion Logic (Enhanced with Fuzzy Match) ---
        if tender_full_fp:
            # 1. Exact Match Check
            if fp_a in tender_full_fp:
                continue
            # 2. Fuzzy Match Check (Threshold 0.9)
            # Handle cases like "6." vs "6．" or minor OCR variance
            close_matches = difflib.get_close_matches(fp_a, tender_fps, n=1, cutoff=0.8)
            if close_matches:
                continue
        # ---------------------------------------------------

        match_type = None
        item_b = None
        desc = ""
        score = 0
        badges = []

        # A. Exact Fingerprint Match
        if fp_a in fp_map_b:
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
            
            # --- Shared Deviation Check (New Feature) ---
            # If A==B, but both differ slightly from Tender (e.g. Shared Typo "蚊件" vs "文件")
            if tender_fps:
                 tender_matches = difflib.get_close_matches(fp_a, tender_fps, n=1, cutoff=0.85)
                 if tender_matches:
                     # Found a close match in tender, but it wasn't exact (otherwise it would be excluded above)
                     # Wait, exclusion logic above skips ONLY if match >= 0.9. 
                     # Here cutoff is 0.85. So if it's between 0.85 and 0.9, OR if exclusion logic was skipped (unlikely if fuzzy is on).
                     # Actually, exclusion logic uses cutoff=0.9.
                     # If we find a match here with 0.85, check if it is NOT exact.
                     
                     matched_tender_fp = tender_matches[0]
                     if matched_tender_fp != fp_a:
                        # Ensure it wasn't excluded (it shouldn't be if we are here)
                        desc += "；检测到与招标文件存在共同差异(疑似错别字/连带修)"
                        badges.append("疑似共同修改")
            # --------------------------------------------

            seen_fps.add(fp_a)

        # B. Fuzzy Match (Spelling/Typos)
        else:
            matches = difflib.get_close_matches(fp_a, b_fingerprints, n=1, cutoff=0.85)
            if matches:
                matched_fp = matches[0]
                
                # --- Double Check Exclusion for Matched B ---
                if tender_full_fp:
                    if matched_fp in tender_full_fp: continue
                    close_tender_matches = difflib.get_close_matches(matched_fp, tender_fps, n=1, cutoff=0.8)
                    if close_tender_matches: continue
                # --------------------------------------------
                    
                item_b = fp_map_b[matched_fp]
                
                # --- Check for Renumbering Difference (New Feature) ---
                # e.g. "2.3 Title" vs "12. Title"
                # If content is identical after stripping number, and text is short (< 60 chars), ignore it
                clean_a = strip_sequence_number_header(text_a)
                clean_b = strip_sequence_number_header(item_b['text'])
                
                # Strict check on cleaned content
                if clean_a == clean_b and len(clean_a) < 60:
                     continue # Ignore renumbering differences on headers
                # ------------------------------------------------------
                
                ratio = difflib.SequenceMatcher(None, text_a, item_b['text']).ratio()
                
                match_type = "fuzzy"
                desc = f"疑似修改/拼写错误 (相似度 {int(ratio*100)}%)"
                score = int(ratio * 100)
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
            # Same missing number?
            if err_a['missing'] == err_b['missing'] and err_a['found'] == err_b['found']:
                # Same context?
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
