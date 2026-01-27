import unittest
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.utils.comparator import CollusionDetector

class TestCollusionDetector(unittest.TestCase):
    def setUp(self):
        # We will mock the extract_text method to avoid needing real PDFs for unit testing
        # This allows us to test the logic on the EXACT strings provided by the user
        self.detector = CollusionDetector(None)
        
        # Mock Data
        self.tender_raw = "我方投标文件的有效期和招标文件规定的投标有效期一致，我方承诺在招标文件规定的投标有效期内不撤销投标文件。"
        self.bid_a_raw = "我方投标文件的有效期和招标文件规定的投标有效期一致，我方承诺在招标文件规定的投标有效期内不撤销投标蚊件。联系电话：13934518882"
        self.bid_b_raw = "我方投标文件的有效期和招标文件规定的投标有效期一致，我方承诺在招标文件规定的投标有效期内不撤销投标蚊件。联系电话：13934518882"
        
        # Pre-load tender logic (Manually injecting for test)
        self.detector.tender_full_text = self.detector.normalize(self.tender_raw)
        self.detector.tender_sentences = set(self.detector.get_sentences(self.tender_raw))

    def mock_extract(self, path):
        # Return tuple: (full_text, pages_map, metadata)
        if path == "A": 
            return self.bid_a_raw, [(1, self.bid_a_raw)], {"author": "UserA"}
        if path == "B": 
            return self.bid_b_raw, [(1, self.bid_b_raw)], {"author": "UserB"}
        return "", [], {}
    
    def test_logic(self):
        # Monkey patch extraction
        self.detector.extract_text_with_pages = self.mock_extract
        
        print("\nTesting Collision Logic...")
        # Note: compare_documents instantiates a new detector, so we must call find_collisions on OUR instance
        # OR patch the class method in comparator. 
        # Easier: just call self.detector.find_collisions
        
        result = self.detector.find_collisions("A", "B")
        collisions = result['paragraphs']
        
        found_typo = False
        found_phone = False
        
        for c in collisions:
            print(f"Found: [{c['type']}] {c['text_a']}") # Changed content to text_a
            if "蚊件" in c['text_a']:
                found_typo = True
            if "13934518882" in c['text_a']:
                found_phone = True

                
        self.assertTrue(found_typo, "Should detect '蚊件' typo")
        self.assertTrue(found_phone, "Should detect phone number")
        
        # Verify Tender exclusion
        # The common sentence "我方投标文件的有效期..." should NOT be in collisions unless it contains the typo
        # Note: In our logic, we split by punctuation.
        # "我方承诺...不撤销投标文件" is in Tender.
        # "我方承诺...不撤销投标蚊件" is NOT in Tender.
        # So it should be detected.
        
if __name__ == '__main__':
    unittest.main()
