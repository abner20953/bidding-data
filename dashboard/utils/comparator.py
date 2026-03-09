import fitz  # PyMuPDF
import re
import os
import gc
from collections import Counter
from difflib import SequenceMatcher

class CollusionDetector:
    def __init__(self, tender_path):
        self.tender_path = tender_path
        self.tender_sentences = set()
        self.tender_skeletons = set()
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

    def get_skeleton(self, text):
        """
        获取中文骨架（仅保留中文字符）。
        用于忽略 数字/符号/标点 的差异（如 "≥14" vs "14"）。
        """
        # Range for CJK Unified Ideographs: 4E00-9FFF
        return re.sub(r'[^\u4e00-\u9fa5]', '', text)

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
                pages_map.append((page.number + 1, raw, self.normalize(raw)))
                full_text += raw
            doc.close()
        except Exception as e:
            print(f"Error extracting text from {pdf_path}: {e}")
            return "", [], {}
            
        return full_text, pages_map, metadata

    def load_tender(self):
        """
        加载招标文件，建立索引（全文句子 + 骨架）。
        """
        print(f"Loading tender: {self.tender_path}")
        text, _, _ = self.extract_text_with_pages(self.tender_path)
        self.tender_full_text = self.normalize(text)
        
        # 建立句子索引
        sentences = self.get_sentences(text)
        self.tender_sentences = set(sentences)
        
        # 建立骨架索引 (set of Chinese-only strings)
        self.tender_skeletons = set()
        for s in sentences:
            skel = self.get_skeleton(s)
            if len(skel) > 1: # 只有骨架足够长才索引，防止两字词误杀
                self.tender_skeletons.add(skel)
                
        gc.collect()

    def get_sentences(self, text):
        """
        简单的分句逻辑。
        """
        lines = text.split('\n')
        sentences = []
        for line in lines:
            parts = re.split(r'[。.!！?？;；,，]', line)
            for p in parts:
                p = p.strip()
                if len(p) > 8: # Increase threshold slightly to avoid noise from short comma segments
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
        for page_num, raw_text, norm_text in pages_map:
            if target_text in norm_text:
                return page_num
        return 0

    def _find_similar_in_tender(self, sent, threshold=0.7):
        """
        在招标文件句子中查找与 sent 最相似的句子。
        用于检测两份投标文件与招标文件的共同偏差（拼写错误、文字修改等）。
        返回 (best_ratio, best_match, diffs)。
        diffs 格式: [(招标原文片段, 投标文本片段), ...]
        """
        if not self.tender_sentences:
            return 0, None, []

        best_ratio = 0
        best_match = None
        len_sent = len(sent)

        for tender_sent in self.tender_sentences:
            len_tender = len(tender_sent)
            if len_tender == 0:
                continue
            # 长度差异超过 30% 则跳过
            if abs(len_sent - len_tender) / max(len_sent, len_tender) > 0.3:
                continue

            sm = SequenceMatcher(None, tender_sent, sent)
            # quick_ratio() 是 O(n) 的快速上界，大部分不相似的句子在此被过滤
            if sm.quick_ratio() < threshold:
                continue
            r = sm.ratio()
            if r > best_ratio:
                best_ratio = r
                best_match = tender_sent

        if best_ratio < threshold or not best_match:
            return 0, None, []

        # 计算具体差异
        sm = SequenceMatcher(None, best_match, sent)
        diffs = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != 'equal':
                diffs.append((best_match[i1:i2], sent[j1:j2]))

        return best_ratio, best_match, diffs

    def find_collisions(self, path_a, path_b):
        """
        核心比对逻辑
        """
        raw_text_a, pages_a, meta_a = self.extract_text_with_pages(path_a)
        raw_text_b, pages_b, meta_b = self.extract_text_with_pages(path_b)
        

        
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
                 # Check Skeleton (Chinese only) to ignore numeric/symbol differences (User Request)
                 skel = self.get_skeleton(sent)
                 if len(skel) > 1 and skel in self.tender_skeletons:
                     continue # Safe exclusion: textual content is identical to tender, only symbols/numbers differ

                 if len(sent) > 8: 
                    page_a = self.find_page_for_text(sent, pages_a)
                    page_b = self.find_page_for_text(sent, pages_b)

                    badges = ["完全匹配"]
                    desc = "发现非招标文件雷同语句"

                    # 检测是否为招标文件内容的近似修改（共同差异/拼写错误）
                    if self.tender_sentences:
                        ratio, match, diffs = self._find_similar_in_tender(sent)
                        if ratio > 0.7 and diffs:
                            diff_parts = [f'"{ d[0] }"→"{ d[1] }"' for d in diffs if d[0] or d[1]]
                            if diff_parts:
                                diff_desc = '; '.join(diff_parts[:3])  # 最多展示3处差异
                                badges = ["共同差异", "疑似修改"]
                                desc = f"与招标文件对比发现共同偏差: {diff_desc}"

                    collisions.append({
                        "type": "text",
                        "text_a": sent, 
                        "text_b": sent,
                        "page_a": page_a,
                        "page_b": page_b,
                        "badges": badges,
                        "desc": desc
                    })
                    processed_contents.add(sent)



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
