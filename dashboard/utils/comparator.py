import difflib
import re
import time
import psutil
import os
from .text_extractor import extract_content, extract_metadata

# --- Configuration ---
SEQ_MATCH_THRESHOLD = 0.85     # Final difflib threshold for "Suspicious"
TENDER_EXCLUDE_THRESHOLD = 0.8 # Threshold for "Tender Exclusion"
MEMORY_SAFE_LIMIT_MB = 150     # Min Available RAM in MB to continue
TIMEOUT_LIMIT = 300            # Seconds per comparison

def get_fingerprint(text):
    text = re.sub(r'\s+', '', text)
    return re.sub(r'[^\w\u4e00-\u9fa5]', '', text)

# --- V4 Low Memory Helper ---
def quick_check_pass(fp_a, fp_b, min_ratio=0.3):
    """
    Very fast pre-check before running expensive difflib.
    Returns True if likelihood of match > min_ratio.
    """
    len_a = len(fp_a)
    len_b = len(fp_b)
    
    if len_a == 0 or len_b == 0: return False
    
    # 1. Length Check
    # If length diff is huge, no way they match 85%
    # e.g. 100 vs 200 -> max Ratio approach 0.66
    if abs(len_a - len_b) / max(len_a, len_b) > 0.6: 
        return False
        
    # 2. Commons Check (Set Intersection)
    # This is O(N) but Python sets are optimized in C.
    # For very short texts, sets overhead might be slightly high but faster than SequenceMatcher
    # We use simple set(fp)
    set_a = set(fp_a)
    set_b = set(fp_b)
    
    intersect = len(set_a.intersection(set_b))
    union = len(set_a.union(set_b))
    
    if union == 0: return False
    jaccard = intersect / union
    
    if jaccard < min_ratio:
        return False
        
    return True

def check_resources(start_time):
    """
    Interrupt if Available Memory < 150MB or Timeout.
    """
    # Timeout check
    if time.time() - start_time > TIMEOUT_LIMIT:
        raise TimeoutError("Processing timed out (300s limit).")
        
    # Memory check (every N checks called)
    mem = psutil.virtual_memory()
    # Check absolute available memory instead of percentage
    # ( Percentage is misleading on machines with high background load )
    if mem.available < MEMORY_SAFE_LIMIT_MB * 1024 * 1024:
        raise MemoryError(f"Memory critical: Only {mem.available / 1024 / 1024:.1f}MB available.")

def is_exact_match(fp_a, fp_map_b):
    return fp_a in fp_map_b

def segment_paragraphs_with_page(content_list):
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

def detect_broken_tail(text):
    if len(text) < 15: return False
    if re.search(r'[。！？.!?”"”’]$', text.strip()):
        return False
    return True

def parse_sequence_number(text):
    match = re.match(r'^(\d+)[.、]', text)
    if match: return int(match.group(1)), "digit_dot"
    match = re.match(r'^[\(（](\d+)[\)）]', text)
    if match: return int(match.group(1)), "paren_digit"
    return None, None

def strip_sequence_number_header(text):
    clean_text = text.strip()
    return re.sub(r'^(\d+(\.\d+)*|[（(]?\d+[)）]?)\s*[.、:：]?\s*', '', clean_text)

def check_sequence_errors(paragraphs):
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
                    if next_num > 1:
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
    start_time = time.time()
    
    # 0. Extract Metadata
    meta_a = extract_metadata(file_a_path)
    meta_b = extract_metadata(file_b_path)
    meta_tender = extract_metadata(tender_path) if tender_path else None

    # 1. Extract content (NOW SAFE: flush_cache implemented)
    content_a = extract_content(file_a_path)
    content_b = extract_content(file_b_path)
    content_tender = extract_content(tender_path) if tender_path else []
    
    # 2. Segment Paragraphs
    paras_a = segment_paragraphs_with_page(content_a)
    paras_b = segment_paragraphs_with_page(content_b)
    paras_tender = segment_paragraphs_with_page(content_tender)
    
    # Pre-compute fingerprints for B to avoid recomputing in loop
    # Storing tuple (fp, item)
    # Using specific list for efficient iteration
    fp_b_list = []
    fp_map_b = {}
    
    for p in paras_b:
        fp = get_fingerprint(p['text'])
        if is_significant(p['text'], fp):
            fp_b_list.append((fp, p))
            fp_map_b[fp] = p # Fast exact lookup

    # Tender Prep
    tender_fps = []
    tender_full_fp = ""
    if paras_tender:
        # Just store fingerprints, no heavy index
        for p in paras_tender:
            tender_fps.append(get_fingerprint(p['text']))
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
            is_excluded = False
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

    # --- Phase 2: Paragraph Comparison (V4 Streaming Filter) ---
    check_counter = 0
    
    for i, p_a in enumerate(paras_a):
        # Resource Guard Check (Periodically)
        check_counter += 1
        if check_counter % 50 == 0:
            check_resources(start_time)

        text_a = p_a['text']
        fp_a = get_fingerprint(text_a)
        
        if fp_a in seen_fps: continue 
        if not is_significant(text_a, fp_a): continue
        
        # Check Exact Match presence in B (Cached Map)
        is_exact_in_b = fp_a in fp_map_b
        
        # --- Exclusion Logic (Iterative Scan but FAST) ---
        if tender_fps:
            # 1. Exact/Substring
            if tender_full_fp and fp_a in tender_full_fp:
                continue
            
            # 2. Fuzzy Exclusion
            # Only check loop if exact match failed.
            # Use quick_check filter against tender lines.
            is_fuzzy_excluded = False
            
            # Optimization: If A matches B (Exact), we apply "Smart Deviation" check.
            # If A != B, we just want to execute Exclusion normally.
            
            # To avoid N*M scan of Tender, we limit check.
            # But Tender exclusion is critical.
            # If Tender is HUGE, this loop is slow.
            # Using quick_check_pass helps.
            
            best_tender_ratio = 0.0
            
            for t_fp in tender_fps:
                if quick_check_pass(fp_a, t_fp, min_ratio=0.5):
                    ratio = difflib.SequenceMatcher(None, fp_a, t_fp).ratio()
                    if ratio > best_tender_ratio:
                        best_tender_ratio = ratio
                        if ratio > TENDER_EXCLUDE_THRESHOLD:
                            # Early break if high enough
                            break
            
            if best_tender_ratio >= TENDER_EXCLUDE_THRESHOLD:
                # HIT: A is similar to Tender. Normally Exclude.
                if is_exact_in_b:
                     # Smart Deviation: A==B, A!=Tender (Sim > 0.8) -> KEEP
                     # Note: We already know A!=Tender Exact (checked above)
                     pass
                else:
                    continue # Exclude
        # -----------------------

        match_type = None
        item_b = None
        desc = ""
        score = 0
        badges = []
        
        # A. Exact Fingerprint Match
        if is_exact_in_b:
            item_b = fp_map_b[fp_a]
            text_b = item_b['text']
            
            match_type = "exact"
            desc = "雷同段落 (内容一致)"
            score = 100
             
            is_broken_a = detect_broken_tail(text_a)
            is_broken_b = detect_broken_tail(text_b)
            if is_broken_a and is_broken_b:
                badges.append("共同异常断句")
                desc += "，且均存在异常断句"
            
            # Shared Deviation Badge
            if best_tender_ratio >= TENDER_EXCLUDE_THRESHOLD:
                 desc += "；检测到与招标文件存在共同差异(疑似错别字/连带修)"
                 badges.append("疑似共同修改")
            
            seen_fps.add(fp_a)

        # B. Fuzzy Match (Filtered Iteration)
        else:
            best_match_item = None
            best_ratio = 0.0
            
            for fp_b, p_b in fp_b_list:
                # Use Quick Filter (Length + Set Jaccard)
                if quick_check_pass(fp_a, fp_b, min_ratio=0.3):
                    
                    # Double Check Exclusion for B (Simulated)
                    # If B is basically Tender, skip
                    if tender_fps:
                        # Only check if A wasn't excluded (it wasn't)
                        # We skip heavy check for B for speed, assume if A passed, 
                        # and A~B, then B is likely fine or we catch it.
                        pass

                    ratio = difflib.SequenceMatcher(None, fp_a, fp_b).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match_item = p_b
            
            if best_match_item and best_ratio >= SEQ_MATCH_THRESHOLD:
                item_b = best_match_item
                
                clean_a = strip_sequence_number_header(text_a)
                clean_b = strip_sequence_number_header(item_b['text'])
                
                if clean_a == clean_b and len(clean_a) < 60:
                     continue 
                
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
