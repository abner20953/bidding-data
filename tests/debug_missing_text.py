import sys
import os
import fitz
import re

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.utils.comparator import CollusionDetector

def debug_missing_text():
    base_dir = r"C:\Users\lilac\Desktop\测试\1"
    tender_file = os.path.join(base_dir, "272、晋能控股装备制造集团有限公司采供分公司煤矿信息化设备在线监测系统1(3).pdf")
    bid_a = os.path.join(base_dir, "山西启智卓识标书.pdf")
    bid_b = os.path.join(base_dir, "郞腾标书1.pdf")
    
    target_raw = "凡我公司售出的产品，保修期间一切因产品质量而引起的产品故障及损坏，本中心均将提供免费上门维修及更换零配件服务"
    
    detector = CollusionDetector(tender_file)
    target_norm = detector.normalize(target_raw)
    
    print(f"Target Raw: {target_raw}")
    print(f"Target Norm: {target_norm}")
    print("-" * 50)
    
    # Check TENDER
    print(f"Checking Tender Exclusion...")
    is_in_tender_full = target_norm in detector.tender_full_text
    is_in_tender_sentences = target_norm in detector.tender_sentences
    print(f"  In Tender Full Text? {is_in_tender_full}")
    print(f"  In Tender Sentences? {is_in_tender_sentences}")
    
    if is_in_tender_full:
        print("  ❌ REASON FOUND: Text is present in Tender document, so it was excluded.")
        return

    # Check BIDS
    # Now that we split by comma, we should check if the segments of target are found
    target_segments = re.split(r'[，,]', target_norm)
    target_segments = [s for s in target_segments if len(s) > 8]

    def check_file(name, path):
        text, _, _ = detector.extract_text_with_pages(path)
        # Note: detector.get_sentences now uses comma, so we check if target segments exist in those sentences
        # OR just check finding in normalized text (which doesn't care about Split)
        # BUT CollusionDetector uses "sentences" for intersection.
        # So we must verify that target_segments exist in detector.get_sentences(text)
        
        file_sentences = set(detector.get_sentences(text))
        
        all_found = True
        for seg in target_segments:
            if seg in file_sentences:
                print(f"  ✅ Segment Found in {name}: {seg[:10]}...")
            else:
                print(f"  ❌ Segment NOT FOUND in {name}: {seg[:10]}...")
                all_found = False
        return all_found

    print("Checking Bid Documents...")
    found_a = check_file("Bid A", bid_a)
    found_b = check_file("Bid B", bid_b)
    
    if found_a and found_b:
        print("-" * 50)
        print("Checking logic why it wasn't reported...")
        # Simulating the logic in find_collisions
        sentences_a = set(detector.get_sentences(detector.extract_text_with_pages(bid_a)[0]))
        sentences_b = set(detector.get_sentences(detector.extract_text_with_pages(bid_b)[0]))
        
        # Check if normalization/sentence splitting matches EXACTLY
        # Maybe get_sentences splits it differently than the target string?
        
        # Let's see what get_sentences produces for the target string
        target_sents = detector.get_sentences(target_raw)
        print(f"Target split into {len(target_sents)} sentences by detector:")
        for s in target_sents:
            print(f"  -> {s}")
            if s in sentences_a and s in sentences_b:
                print("     ✅ Identical sentence found in both Bids' sentence sets.")
            else:
                 print(f"     ❌ Not found in intersection. (In A: {s in sentences_a}, In B: {s in sentences_b})")

if __name__ == "__main__":
    debug_missing_text()
