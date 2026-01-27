import sys
import os
import unittest

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.utils.comparator import CollusionDetector

class TestSkeletonExclusion(unittest.TestCase):
    def setUp(self):
        self.detector = CollusionDetector(None)
        # Mock Tender content
        # Tender: "Screen Size: >= 14 inches"
        tender_raw = "屏幕尺寸：≥14 英寸。最大工作电流≥2A。不撤销投标文件。"
        self.detector.extract_text_with_pages = lambda p: (tender_raw, [], {}) 
        # Manually load tender since we mocked extract
        self.detector.tender_path = "mock_tender.pdf"
        self.detector.load_tender() # This will index skeletons: "屏幕尺寸英寸", "最大工作电流", "不撤销投标文件"
        
    def test_response_parameters(self):
        # Case 1: Response matches skeleton but differs in symbols/digits
        # Bid: "(10) Screen Size: 14 inches"
        bid_text = "(10)屏幕尺寸:14英寸"
        skeleton = self.detector.get_skeleton(bid_text)
        print(f"Bid: {bid_text} -> Skeleton: {skeleton}")
        
        # Check exclusion logic manually (simulating find_collisions loop)
        is_exact_match = bid_text in self.detector.tender_sentences
        is_skel_match = (len(skeleton) > 4 and skeleton in self.detector.tender_skeletons)
        
        print(f"  Exact Match? {is_exact_match}")
        print(f"  Skeleton Match? {is_skel_match}")
        
        self.assertTrue(is_skel_match, "Should match skeleton of Tender")
        self.assertFalse(is_exact_match, "Should NOT match exact text")
        
    def test_short_parameters(self):
        # Case 3: Short Chinese keys (Storage, Memory)
        # Bid: "存储:32GBeMMC" -> Skel "存储" (Len 2)
        # Bid: "内存:4GBLPDDR4" -> Skel "内存" (Len 2)
        
        # Add to Tender skeletons manually for test
        self.detector.tender_skeletons.add("存储")
        self.detector.tender_skeletons.add("内存")
        self.detector.tender_skeletons.add("刷新率")
        
        cases = [
            ("存储:32GBeMMC", "存储"),
            ("内存:4GBLPDDR4", "内存"),
            ("刷新率:144Hz", "刷新率")
        ]
        
        for bid_text, expected_skel in cases:
            skel = self.detector.get_skeleton(bid_text)
            print(f"Bid: {bid_text} -> Skel: {skel}")
            
            # Now with fix, this should be True (Excluded)
            is_skel_match = (len(skel) > 1 and skel in self.detector.tender_skeletons)
            self.assertTrue(is_skel_match, f"Fix should allow exclusion for {skel}")

    def test_typo_preservation(self):
        # Case 2: Typo "Mosquito"
        # Bid: "...不撤销投标蚊件"
        bid_text = "不撤销投标蚊件"
        skeleton = self.detector.get_skeleton(bid_text)
        print(f"Bid: {bid_text} -> Skeleton: {skeleton}")
        
        is_skel_match = (len(skeleton) > 4 and skeleton in self.detector.tender_skeletons)
        print(f"  Skeleton Match? {is_skel_match}")
        
        # Tender has "不撤销投标文件". Skeleton "不撤销投标文件".
        # Bid has "不撤销投标蚊件". Skeleton "不撤销投标蚊件".
        # They differ. So is_skel_match should be False.
        # find_collisions would NOT Exclude. -> KEEP.
        
        self.assertFalse(is_skel_match, "Should NOT match skeleton (Typo must be preserved)")

if __name__ == '__main__':
    unittest.main()
