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

def compare_documents(file_a_path, file_b_path, tender_path):
    """
    Compares three documents using fingerprinting.
    Returns:
    {
        "paragraphs": [ ... ],
        "metadata": {
            "file_a": {...},
            "file_b": {...},
            "tender": {...}
        }
    }
    """
    # 0. Extract Metadata
    meta_a = extract_metadata(file_a_path)
    meta_b = extract_metadata(file_b_path)
    meta_tender = extract_metadata(tender_path) if tender_path else None

    # 1. Extract content with pages
    content_a = extract_content(file_a_path)
    content_b = extract_content(file_b_path)
    
    # 2. Segment
    paras_a = segment_paragraphs_with_page(content_a)
    paras_b = segment_paragraphs_with_page(content_b)
    
    # 3. Build Fingerprint Maps for B
    fp_map_b = {}
    for p in paras_b:
        if len(p['text']) >= 5:
            fp = get_fingerprint(p['text'])
            if fp not in fp_map_b:
                fp_map_b[fp] = p
    
    # 4. Process Tender Exclusion
    tender_fps = set()
    if tender_path:
        content_tender = extract_content(tender_path)
        paras_tender = segment_paragraphs_with_page(content_tender)
        tender_fps = set(get_fingerprint(p['text']) for p in paras_tender)

    # 5. Find Suspicious
    suspicious_paragraphs = []
    
    for i, p_a in enumerate(paras_a):
        text_a = p_a['text']
        # Relaxed length check to detect short phrases/phone numbers
        if len(text_a) < 5: 
            continue
            
        fp_a = get_fingerprint(text_a)
        
        if fp_a in fp_map_b:
            if fp_a not in tender_fps:
                item_b = fp_map_b[fp_a]
                suspicious_paragraphs.append({
                    "text": text_a,
                    "index_a": i + 1,
                    "page_a": p_a['page'],
                    "page_b": item_b['page'],
                    "matches_in_b": True
                })

    return {
        "paragraphs": suspicious_paragraphs,
        "metadata": {
            "file_a": meta_a,
            "file_b": meta_b,
            "tender": meta_tender
        }
    }
