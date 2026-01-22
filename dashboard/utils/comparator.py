def get_fingerprint(text):
    """
    Generate a fingerprint for text comparison by removing all whitespace.
    This handles issues where PDF extraction adds extra spaces or newlines.
    """
    return re.sub(r'\s+', '', text)

def segment_paragraphs(raw_text):
    """
    Smartly segment text into paragraphs.
    Specifically handles PDF hard-wraps by merging lines that don't look like paragraph ends.
    """
    lines = [line.strip() for line in raw_text.split('\n')]
    paragraphs = []
    current_para = []
    
    # Sentence ending punctuation (Chinese & English)
    # stops = set(['。', '！', '？', '!', '?', '；', ';', ':', '：']) 
    # Actually, using a regex is better
    stop_pattern = re.compile(r'[。！？!?;；：:]$')
    
    for line in lines:
        if not line:
            # Empty line usually means paragraph break in Markdown/Text
            if current_para:
                paragraphs.append("".join(current_para))
                current_para = []
            continue
            
        current_para.append(line)
        
        # Heuristic: If line ends with stop punctuation, it *might* be end of paragraph.
        # But in many docs, headers don't end in punctuation.
        # However, for "collusion detection", we care about long text blocks mostly.
        # If we merge too much, we might miss match. If we merge too little, we get fragments.
        # A safer heuristic for PDF is:
        # If the line is "short" relative to page width (hard to know), it's end.
        # Simple heuristic: If it matches stop_pattern, treat as segment end.
        # OR: Just treat every non-empty block of text separated by empty lines as a paragraph?
        # PDFPlumber extraction usually preserves layout. 
        # Let's try merging lines unless they end with explicit stops.
        # But this risks merging headers with body.
        # User complaint: "同段落拆分的过于散" -> Suggests split happened where it shouldn't.
        # User complaint: "点上下墙、轮巡控制操作" -> This sounds like a bullet point.
        
        # Refined Strategy:
        # Just return the lines as is? No, user said they are too fragmented.
        # That means "点上下墙..." was treated as a separated paragraph but user thinks it shouldn't be?
        # Or maybe it WAS a separate line in PDF but user ignores it?
        # "点上下墙..." sounds like a fragment.
        
        # Let's stick to the "Exclude" logic improvement first (Fingerprint).
        # For segmentation, let's try to merge generic lines.
    
    # Re-reading user: "同段落拆分的过于散" (Paragraphs are split too scattered)
    # This strongly suggests standard `split('\n')` on PDF output is bad because PDF wraps lines.
    # So we MUST merge lines.
    
    # Clean implementation of merge:
    normalized_paras = []
    buffer = ""
    
    for line in lines:
        if not line:
            if buffer:
                normalized_paras.append(buffer)
                buffer = ""
            continue
        
        # If buffer exists, we decide whether to join or start new
        if buffer:
            # If previous line ended with stop char, usually implies end of para?
            if stop_pattern.search(buffer):
                normalized_paras.append(buffer)
                buffer = line
            else:
                # Merge (assuming visual wrap)
                buffer += line
        else:
            buffer = line
            
    if buffer:
        normalized_paras.append(buffer)
        
    return normalized_paras

def compare_documents(file_a_path, file_b_path, tender_path):
    """
    Compares three documents using fingerprinting.
    """
    # 1. Extract and Segment
    text_a = extract_text(file_a_path)
    text_b = extract_text(file_b_path)
    
    paras_a = segment_paragraphs(text_a)
    paras_b = segment_paragraphs(text_b) # We need segments to show result
    
    # Build Fingerprint Maps
    # Map fingerprint -> original text (for display)
    # Note: Collisions possible if two diff paras have same fingerprint (unlikely unless identical content)
    
    fp_map_b = {get_fingerprint(p): p for p in paras_b if len(p) > 15} # Threshold: 15 chars
    
    # 2. Process Tender Exclusion
    tender_fps = set()
    if tender_path:
        text_tender = extract_text(tender_path)
        # For tender, we just want fingerprints to exclude
        # We should use same segmentation logic to ensure fingerprints match
        paras_tender = segment_paragraphs(text_tender)
        tender_fps = set(get_fingerprint(p) for p in paras_tender)

    # 3. Find Suspicious
    suspicious_paragraphs = []
    
    for i, p_a in enumerate(paras_a):
        if len(p_a) < 15: # Ignore short fragments
            continue
            
        fp_a = get_fingerprint(p_a)
        
        if fp_a in fp_map_b:
            if fp_a not in tender_fps:
                suspicious_paragraphs.append({
                    "text": p_a, # Display text from A
                    "index_a": i + 1,
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
