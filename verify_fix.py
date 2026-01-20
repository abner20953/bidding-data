from scraper import get_model
from sentence_transformers import util

# Read from file to get the EXACT new anchors
with open("scraper.py", "r", encoding="utf-8") as f:
    content = f.read()
import re
match = re.search(r'anchor_sentences = \[(.*?)\]', content, re.S)
if match:
    # Use eval but with caution, ensure string is clean
    anchors = eval(f"[{match.group(1)}]")
else:
    print("Failed to parse anchors")
    exit(1)

target = "临汾市人民医院第三方检测服务项目的采购公告"
model = get_model()
target_emb = model.encode(target)
anchor_embs = model.encode(anchors)

scores = util.cos_sim(target_emb, anchor_embs)[0]
max_score = float(scores.max())
max_idx = int(scores.argmax())
max_anchor = anchors[max_idx]

print(f"Target: {target}")
print(f"Max Score: {max_score:.4f} (Threshold: 0.60)")
print(f"Most Similar Anchor: {max_anchor}")
