import sys
import os
import json
import time
import re

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.utils.comparator import compare_documents

def test_real_files():
    base_dir = r"C:\Users\lilac\Desktop\测试\1"
    tender_file = os.path.join(base_dir, "272、晋能控股装备制造集团有限公司采供分公司煤矿信息化设备在线监测系统1(3).pdf")
    bid_a = os.path.join(base_dir, "山西启智卓识标书.pdf")
    bid_b = os.path.join(base_dir, "郞腾标书1.pdf")
    
    print("Starting real file verification...")
    print(f"Tender: {tender_file}")
    
    start_time = time.time()
    try:
        result = compare_documents(bid_a, bid_b, tender_file)
        duration = time.time() - start_time
        
        print(f"Comparison finished in {duration:.2f} seconds.")
        
        collisions = result['paragraphs']
        print(f"Found {len(collisions)} collisions.")
        
        # User provided raw strings
        raw_targets = [
            "我方投标文件的有效期和招标文件规定的投标有效期一致，我方承诺在招标文件规定的投标有效期内不撤销投标蚊件",
            "13934518882",
            "SQL Server Always On"
        ]
        
        # Helper to normalize for checking
        def normalize_check(t):
             text = t.replace('，', ',').replace('。', '.').replace('：', ':').replace('；', ';')
             text = text.replace('“', '"').replace('”', '"').replace("‘", "'").replace("’", "'")
             return re.sub(r'\s+', '', text)
        
        # Define targets with expected segments if needed
        # We just need to know if coverage is "good enough" or "found something"
        raw_targets = [
            "我方投标文件的有效期和招标文件规定的投标有效期一致，我方承诺在招标文件规定的投标有效期内不撤销投标蚊件",
            "13934518882",
            "SQL Server Always On",
            "凡我公司售出的产品，保修期间一切因产品质量而引起的产品故障及损坏，本中心均将提供免费上门维修及更换零配件服务"
        ]

        target_map = {}
        for t in raw_targets:
            # key: raw target
            # value: list of normalized segments (split by comma)
            norm = normalize_check(t)
            segs = re.split(r'[,]', norm)
            target_map[t] = [s for s in segs if len(s) > 5]

        found_map = {t: set() for t in raw_targets} # Track which segments found
        
        # Print first 20 collisions
        print("-" * 30)
        for i, c in enumerate(collisions):
            text = c['text_a'] # This is normalized content
            if i < 10: 
                print(f"[{i}] [{c['type']}] {text[:50]}... (Page {c['page_a']})")
            
            for raw, segs in target_map.items():
                for seg in segs:
                    if seg in text:
                        found_map[raw].add(seg)
        
        print("\nVerification Results:")
        for raw, found_segs in found_map.items():
            total_segs = len(target_map[raw])
            found_count = len(found_segs)
            status = "✅ FOUND" if found_count > 0 else "❌ MISSING"
            print(f"  Target: {raw[:20]}... [{found_count}/{total_segs} segments match]")
            if found_count > 0 and found_count < total_segs:
                print(f"     (Partial match. Found: {list(found_segs)})")
        
        # Explicit check for typo keyword in ALL collisions
        typo_found = False
        for c in collisions:
             if "蚊件" in c['text_a']:
                 print(f"  🔍 TYPO CONFIRMED in collision: {c['text_a']}")
                 typo_found = True
                 break
        if not typo_found:
             print("  ⚠️ TYPO '蚊件' NOT FOUND in any collision.")

            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_real_files()

