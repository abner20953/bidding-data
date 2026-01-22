import re
from .text_extractor import extract_content

def get_fingerprint(text):
    """
    Generate a fingerprint for text comparison by removing all whitespace.
    """
    return re.sub(r'\s+', '', text)

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
    
    current_para_text = []
    current_para_start_page = -1
    
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
            if stop_pattern.search(buffer):
                # Previous sentence ended. Commit buffer.
                paragraphs.append({"text": buffer, "page": buffer_start_page})
                buffer = line
                buffer_start_page = page
            else:
                # Merge
                buffer += line
                # Keep start page of the paragraph
                
    if buffer:
        paragraphs.append({"text": buffer, "page": buffer_start_page})
        
    return paragraphs

def compare_documents(file_a_path, file_b_path, tender_path):
    """
    Compares three documents using fingerprinting.
    Returns list of items with page numbers.
    """
    # 1. Extract content with pages
    content_a = extract_content(file_a_path)
    content_b = extract_content(file_b_path)
    
    # 2. Segment
    paras_a = segment_paragraphs_with_page(content_a)
    paras_b = segment_paragraphs_with_page(content_b)
    
    # 3. Build Fingerprint Maps for B
    # Map fingerprint -> Info dict (include page)
    # Note: If multiple occurrences in B, we might want all? For now take first.
    fp_map_b = {}
    for p in paras_b:
        if len(p['text']) > 15:
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
        if len(text_a) < 15: 
            continue
            
        fp_a = get_fingerprint(text_a)
        
        if fp_a in fp_map_b:
            if fp_a not in tender_fps:
                item_b = fp_map_b[fp_a]
                suspicious_paragraphs.append({
                    "text": text_a,
                    "index_a": i + 1, # Keep sequential index just in case
                    "page_a": p_a['page'],
                    "page_b": item_b['page'],
                    "matches_in_b": True
                })

    return suspicious_paragraphs

def extract_text_with_index(filepath):
    """
    Legacy helper, not used in new logic but kept if referenced elsewhere (unlikely).
    Actually, we can remove it or keep it for compatibility if I imported it elsewhere.
    I didn't export it in __init__, so safe to remove or ignore.
    """
    pass
