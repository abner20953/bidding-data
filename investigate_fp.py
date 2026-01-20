from scraper import get_model
from sentence_transformers import util

# Re-read anchors from file to ensure we use the live ones
with open("scraper.py", "r", encoding="utf-8") as f:
    content = f.read()
import re
match = re.search(r'anchor_sentences = \[(.*?)\]', content, re.S)
if match:
    anchor_sentences = eval(f"[{match.group(1)}]")
else:
    print("Failed to load anchors")
    exit(1)

model = get_model()
target = "临汾市人民医院第三方检测服务项目的采购公告"
anchors = anchor_sentences

target_emb = model.encode(target)
anchor_embs = model.encode(anchors)

scores = util.cos_sim(target_emb, anchor_embs)[0]
max_score = float(scores.max())
max_idx = int(scores.argmax())
max_anchor = anchors[max_idx]

print(f"Target: {target}")
print(f"Max Score: {max_score:.4f} (Threshold: 0.60)")
print(f"Most Similar Anchor: {max_anchor}")

print("\n--- Top 3 Matches ---")
top_results = []
for i, score in enumerate(scores):
    top_results.append((anchors[i], float(score)))

top_results.sort(key=lambda x: x[1], reverse=True)
for anchor, score in top_results[:3]:
    print(f"Anchor: {anchor} | Score: {score:.4f}")
