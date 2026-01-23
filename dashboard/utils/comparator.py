import re
from .text_extractor import extract_content, extract_metadata

def get_fingerprint(text):
    """
    Generate a fingerprint for text comparison by removing all non-alphanumeric characters.
    This handles issues where PDF extraction adds extra spaces, newlines, or inconsistent punctuation.
    Using stricter fingerprinting (removing punctuation) improves match rate for data fields.
    """
    # Remove all whitespace
    text = re.sub(r'\s+', '', text)
    # Remove generic punctuation (Keep only word chars and unicode ranges for Chinese)
    # \w matches [a-zA-Z0-9_] and various unicode word chars.
    # We want to STRIP punctuation like , . ; : etc.
    # Simple way: keep only alnum (and Chinese).
    # Regex: [^\w] removes punctuation.
    return re.sub(r'[^\w\u4e00-\u9fa5]', '', text)

def segment_paragraphs_with_page(content_list):
    """
    Smartly segment text into paragraphs while tracking page numbers.
    Args:
        content_list: List[{"text": str, "page": int}]
    Returns:
        List[{"text": str, "page": int}]
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
    
    buffer = ""
    buffer_start_page = -1
    
    for item in all_lines:
        line = item['text']
        page = item['page']
        
        if not buffer:
            buffer = line
            buffer_start_page = page
        else:
            # Check if we should merge with buffer
            # Do NOT merge if buffer is short (<40 chars) and has no punctuation 
            # - this likely indicates a Header, Title, or Data Field (Phone num).
            is_short_line = len(buffer) < 40
            has_stop = stop_pattern.search(buffer)
            
            if has_stop or is_short_line:
                # Previous sentence ended OR it was a short header. Commit buffer.
                paragraphs.append({"text": buffer, "page": buffer_start_page})
                buffer = line
                buffer_start_page = page
            else:
                # Merge (Long text flow)
                buffer += line
                # Keep start page of the paragraph
                
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
    Extracts high-value entities (Phones, ID Cards, Emails) from the entire document content.
    Returns: Dict { "fingerprint": { "text": original, "page": page_num } }
    """
    entities = {}
    
    # Regex for Phone (Mobile 11 digits)
    phone_pattern = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
    
    # Regex for ID Card (18 digits or 17+X)
    id_pattern = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')
    
    # Regex for Email
    email_pattern = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
    
    for item in content_list:
        text = item['text']
        page = item['page']
        
        # Phones
        for match in phone_pattern.findall(text):
            fp = match
            if fp not in entities:
                entities[fp] = {"text": match, "page": page}
                
        # IDs
        for match in id_pattern.findall(text):
            fp = match.upper()
            if fp not in entities:
                entities[fp] = {"text": match, "page": page}
                
        # Emails
        for match in email_pattern.findall(text):
            fp = match.lower() # Email is case insensitive
            if fp not in entities:
                entities[fp] = {"text": match, "page": page}
                
    return entities

def is_significant(text, fingerprint):
    """
    Determines if a short text is significant enough to report as a match.
    Strategy:
    1. Base Threshold: Fingerprint length > 10.
       (Rejects short headers, generic phrases, short sentences)
    """
    f_len = len(fingerprint)
    
    # 0. Global Blacklist Check
    clean_text = text.replace(" ", "").replace(":", "").replace("：", "")
    if clean_text in COMMON_HEADERS:
        return False
    
    # 1. Absolute Minimum for Paragraphs
    # User Request: "At least higher than 10" -> > 10 -> >= 11
    if f_len <= 10:
        return False
        
    # 2. Pure Numeric/Symbol Check
    if re.match(r'^[\d\.,\-\(\)]+$', fingerprint):
        # Even stricter for numbers? 
        # Phone numbers are 11 digits, so they pass > 10.
        # But generic numbers?
        if f_len < 10:
            return False
            
    return True

def compare_documents(file_a_path, file_b_path, tender_path):
    """
    Compares three documents using fingerprinting AND entity extraction.
    """
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
    
    # 3. Build Maps (Paragraphs)
    fp_map_b = {}
    for p in paras_b:
        fp = get_fingerprint(p['text'])
        if is_significant(p['text'], fp):
            if fp not in fp_map_b:
                fp_map_b[fp] = p
                
    # 4. Tender Exclusion (Full Content Fingerprint)
    # We construct a single giant string of all Tender content fingerprints.
    # This allows checking if a phrase (entity or paragraph) exists *anywhere* in the Tender 
    # document, even if segmented differently (e.g. split across lines).
    tender_full_fp = ""
    if paras_tender:
        tender_full_fp = "".join([get_fingerprint(p['text']) for p in paras_tender])

    # 5. Extract Entities (Independent of Paragraphs)
    entities_a = extract_entities(content_a)
    entities_b = extract_entities(content_b)

    # 6. Find Suspicious Items
    suspicious_paragraphs = []
    seen_fps = set()

    # A. Check Entities first (High Priority)
    for fp, item_a in entities_a.items():
        if fp in entities_b:
            # Check exclusion against full tender content
            # Must fingerprint the entity string to match the tender_full_fp format (no symbols)
            entity_check_fp = get_fingerprint(fp)
            
            # If tender_full_fp is empty, it means no tender doc provided, so don't exclude.
            is_excluded = tender_full_fp and (entity_check_fp in tender_full_fp)
            
            if not is_excluded:
                if fp not in seen_fps:
                    item_b = entities_b[fp]
                    suspicious_paragraphs.append({
                        "text": f"[敏感数据] {item_a['text']}",
                        "index_a": -1, # Special index
                        "page_a": item_a['page'],
                        "page_b": item_b['page'],
                        "matches_in_b": True
                    })
                    seen_fps.add(fp)

    # B. Check Paragraphs
    for i, p_a in enumerate(paras_a):
        text_a = p_a['text']
        fp_a = get_fingerprint(text_a)
        
        if fp_a in seen_fps: continue # Skip if already found as entity
        
        # Check significance
        if not is_significant(text_a, fp_a):
            continue
        
        if fp_a in fp_map_b:
            # Check exclusion against full tender content
            # fp_a is already a fingerprint
            is_excluded = tender_full_fp and (fp_a in tender_full_fp)
            
            if not is_excluded:
                item_b = fp_map_b[fp_a]
                suspicious_paragraphs.append({
                    "text": text_a,
                    "index_a": i + 1,
                    "page_a": p_a['page'],
                    "page_b": item_b['page'],
                    "matches_in_b": True
                })
                seen_fps.add(fp_a)

    return {
        "paragraphs": suspicious_paragraphs,
        "metadata": {
            "file_a": meta_a,
            "file_b": meta_b,
            "tender": meta_tender
        }
    }
