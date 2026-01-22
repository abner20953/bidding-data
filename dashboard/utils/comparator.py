import re
from .text_extractor import extract_content, extract_metadata

def get_fingerprint(text):
    """
    Generate a fingerprint for text comparison by removing all whitespace.
    This handles issues where PDF extraction adds extra spaces or newlines.
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
    
    # Check if all_lines empty
    if not all_lines:
        return []

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
