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
        
        target_map = {t: normalize_check(t) for t in raw_targets}
        found_map = {t: False for t in raw_targets}
        
        # import re removed
        
        # Print first 20 collisions
        for i, c in enumerate(collisions):
            text = c['text_a'] # This is normalized content from detector
            if i < 10: 
                print(f"[{i}] [{c['type']}] {text[:50]}... (Page {c['page_a']})")
            
            for raw, norm_target in target_map.items():
                if norm_target in text:
                    found_map[raw] = True
                    
        print("\nVerification Results:")
        for k, v in found_map.items():
            print(f"  Target: {k[:20]}...")
            print(f"  Status: {'✅ FOUND' if v else '❌ MISSING'}")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_real_files()

