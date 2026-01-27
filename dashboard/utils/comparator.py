import fitz  # PyMuPDF
import re
import os
import gc
from collections import Counter

class CollusionDetector:
    def __init__(self, tender_path):
        self.tender_path = tender_path
        self.tender_sentences = set()
        self.tender_full_text = ""
        if tender_path and os.path.exists(tender_path):
            self.load_tender()

    def normalize(self, text):
        """
        标准化文本：去除空白字符，统一符号。
        """
        if not text:
            return ""
        # 统一全角符号
        text = text.replace('，', ',').replace('。', '.').replace('：', ':').replace('；', ';')
        text = text.replace('“', '"').replace('”', '"').replace("‘", "'").replace("’", "'")
        # 去除空白
        text = re.sub(r'\s+', '', text)
        return text

    def extract_text_with_pages(self, pdf_path):
        """
        提取文本并保留页码映射。
        Returns: 
            full_text (str): normalized full text for fast diff
            pages_map (list): [(page_num, raw_text), ...]
            metadata (dict): PDF metadata
        """
        full_text = ""
        pages_map = []
        metadata = {}
        
        try:
            doc = fitz.open(pdf_path)
            metadata = doc.metadata
            for page in doc:
                raw = page.get_text()
                pages_map.append((page.number + 1, raw))
                full_text += raw
            doc.close()
        except Exception as e:
            print(f"Error extracting text from {pdf_path}: {e}")
            return "", [], {}
            
        return full_text, pages_map, metadata

    def load_tender(self):
        """
        加载招标文件，建立索引。
        """
        print(f"Loading tender: {self.tender_path}")
        text, _, _ = self.extract_text_with_pages(self.tender_path)
        self.tender_full_text = self.normalize(text)
        # 建立句子索引
        self.tender_sentences = set(self.get_sentences(text))
        gc.collect()

    def get_sentences(self, text):
        """
        简单的分句逻辑。
        """
        lines = text.split('\n')
        sentences = []
        for line in lines:
            parts = re.split(r'[。.!！?？;；]', line)
            for p in parts:
                p = p.strip()
                if len(p) > 5:
                    sentences.append(self.normalize(p))
        return sentences

    def find_page_for_text(self, target_text, pages_map):
        """
        在 pages_map 中查找 target_text (normalized) 出现的页码。
        返回第一次出现的页码，找不到返回 0。
        """
        # target_text is already normalized.
        # We need to normalize page content on the fly or pre-calc?
        # On the fly is slower but saves memory.
        for page_num, raw_text in pages_map:
            if target_text in self.normalize(raw_text):
                return page_num
        return 0

    def find_collisions(self, path_a, path_b):
        """
        核心比对逻辑
        """
        raw_text_a, pages_a, meta_a = self.extract_text_with_pages(path_a)
        raw_text_b, pages_b, meta_b = self.extract_text_with_pages(path_b)
        
        norm_a = self.normalize(raw_text_a)
        norm_b = self.normalize(raw_text_b) # Use only if needed for global comparison
        
        collisions = []
        
        # --- 策略 1: 实体雷同 (手机号、身份证、邮箱) ---
        # 优化：直接在 extract_text 阶段或此处对 raw_text 做 entity 提取
        entities_a = self.extract_entities(raw_text_a)
        entities_b = self.extract_entities(raw_text_b)
        
        common_entities = entities_a.intersection(entities_b)
        
        # 排除招标文件中的实体
        tender_entities = self.extract_entities(self.tender_full_text) # tender_full_text is normalized? No wait.
        # Correction: tender_full_text was normalized in load_tender. 
        # extract_entities logic should handle normalized text or raw.
        # Let's adjust extract_entities to handle loose matching.
        
        # Actually, entities usually rely on digits/letters, so normalization (removing spaces) is good.
        suspect_entities = common_entities - tender_entities
        
        for entity in suspect_entities:
            page_a = self.find_page_for_text(entity, pages_a)
            page_b = self.find_page_for_text(entity, pages_b)
            collisions.append({
                "type": "entity",
                "text_a": entity,
                "text_b": entity,
                "page_a": page_a,
                "page_b": page_b,
                "badges": ["敏感实体"],
                "desc": f"发现相同的实体信息: {entity}"
            })

        # --- 策略 2: 长文本/句子雷同 ---
        sentences_a = set(self.get_sentences(raw_text_a))
        sentences_b = set(self.get_sentences(raw_text_b))
        
        common_sentences = sentences_a.intersection(sentences_b)
        
        processed_contents = set()

        for sent in common_sentences:
            if sent in processed_contents: continue
            
            # Exclusion logic
            if sent not in self.tender_sentences and sent not in self.tender_full_text:
                 if len(sent) > 8: 
                    page_a = self.find_page_for_text(sent, pages_a)
                    page_b = self.find_page_for_text(sent, pages_b)
                    
                    # Improve desc depending on content
                    desc = "发现非招标文件雷同语句"
                    badges = ["完全匹配"]
                    
                    # Detect Typo "蚊件"
                    if "蚊件" in sent:
                        desc = "发现共同的可疑错别字 (蚊件)"
                        badges.append("拼写错误")
                    
                    collisions.append({
                        "type": "text",
                        "text_a": sent, # Normalized text might be hard to read, but frontend calls escapeHtml. 
                                        # Ideally we map back to raw text, but that's hard. 
                                        # For now simply return the normalized matched constraint.
                        "text_b": sent,
                        "page_a": page_a,
                        "page_b": page_b,
                        "badges": badges,
                        "desc": desc
                    })
                    processed_contents.add(sent)

        # --- 策略 3: 滑窗/片段 (针对 "蚊件" 且被断句切开的情况) ---
        # 简单实现：检查特定高频错别字
        keywords = ["蚊件"] 
        
        for kw in keywords:
            kw_norm = self.normalize(kw)
            if kw_norm in norm_a and kw_norm in norm_b:
                # check exclusion
                if kw_norm not in self.tender_full_text:
                    # check if already covered
                    if not any(kw_norm in c['text_a'] for c in collisions):
                        page_a = self.find_page_for_text(kw_norm, pages_a)
                        page_b = self.find_page_for_text(kw_norm, pages_b)
                        collisions.append({
                            "type": "text_fragment",
                            "text_a": f"...{kw}...",
                            "text_b": f"...{kw}...",
                            "page_a": page_a,
                            "page_b": page_b,
                            "badges": ["关键词匹配"],
                            "desc": f"发现可疑关键词: {kw}"
                        })

        # --- Result Formatting ---
        return {
            "metadata": {
                "file_a": meta_a,
                "file_b": meta_b,
                "tender": {} # TODO: extract tender meta?
            },
            "paragraphs": collisions,
            "common_errors": {"sequence": []} # Placeholder
        }

    def extract_entities(self, text):
        entities = set()
        # Normalization removes spaces, so regex usually works better on that for strict patterns
        # But phones might look like 139 3451 8882.
        # Since we pass in raw_text usually... wait, in find_collisions I passed raw_text to extract_entities?
        # Yes.
        
        # 1. Phone (loose)
        phones = re.findall(r'1[3-9]\d{9}', text.replace(" ", "").replace("-", ""))
        entities.update(phones)
        
        # 2. ID Card
        ids = re.findall(r'\d{15}|\d{18}|\d{17}[xX]', text)
        for i in ids:
             if len(i) >= 15: entities.add(i)
        
        # 3. Email
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        entities.update(emails)
        
        return entities

def compare_documents(path_a, path_b, path_tender=None):
    detector = CollusionDetector(path_tender)
    return detector.find_collisions(path_a, path_b)
