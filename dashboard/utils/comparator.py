import re
from .text_extractor import extract_text

def normalize_text(text):
    """
    Normalize text for comparison: remove whitespace, punctuation?
    For now, let's just strip whitespace and newline characters implies strict paragraph matching.
    Actually, let's keep it simple: exact paragraph text match.
    """
    return text.strip()

def compare_documents(file_a_path, file_b_path, tender_path):
    """
    Compares three documents:
    1. Extracts text from Bidder A, Bidder B, and Tender.
    2. Finds paragraphs common to A and B.
    3. Excludes paragraphs present in Tender.
    Returns a list of suspicious paragraphs.
    """
    res_a = extract_text_with_index(file_a_path)
    res_b = extract_text_with_index(file_b_path)
    
    # Tender is optional? The requirement implies it exists.
    # "三个文件中有两个是不同投标单位的投标文件，一个是招标文件"
    res_tender = set()
    if tender_path:
         # For tender, we just need the set of content for exclusion
        tender_text = extract_text(tender_path)
        # Split tender text similarly
        res_tender = set(p.strip() for p in tender_text.split('\n') if p.strip())

    # Find Intersection
    suspicious_paragraphs = []
    
    # We iterate over A's paragraphs and check if they exist in B
    # And check if they are NOT in Tender
    
    # To optimize B lookup
    set_b = set(p['text'] for p in res_b)
    
    for item_a in res_a:
        text = item_a['text']
        
        # Too short paragraphs (e.g. page numbers, headers) might be noise
        # Let's set a minimum length threshold, say 10 chars?
        if len(text) < 10:
            continue
            
        if text in set_b:
            if text not in res_tender:
                # Found a suspicious paragraph!
                # Now we want to find where it is in B as well?
                # The requirement says "list consistent paragraphs and their positions"
                
                # Find index in B (first occurrence)
                # This is a bit slow if we search list every time, but list_b isn't huge? 
                # Or we can build a map for B?
                
                suspicious_paragraphs.append({
                    "text": text,
                    "index_a": item_a['index'], # Line number or paragraph index
                    "matches_in_b": True # Just a flag
                })

    # Deduplicate results? No, if A has same paragraph twice, do we report twice?
    # Probably yes, location matters.
    
    return suspicious_paragraphs

def extract_text_with_index(filepath):
    """
    Extracts text and keeps track of index (paragraph number).
    Returns list of dicts: {'index': i, 'text': '...'}
    """
    raw_text = extract_text(filepath)
    lines = raw_text.split('\n')
    result = []
    for i, line in enumerate(lines):
        if line.strip():
            result.append({
                "index": i + 1, # 1-based index
                "text": line.strip()
            })
    return result
