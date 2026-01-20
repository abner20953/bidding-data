from scraper import get_model
from sentence_transformers import util

# Improved Anchors
refined_anchors = [
    # Remove "公立医院", just "绩效考核平台"
    "绩效考核管理信息系统",
    # Remove "医院" where possible or make it very tech specific
    "医疗业务管理系统", 
    "核心业务系统HIS",
    "电子病历EMR系统",
    "远程会诊软件平台",
    # Keep others...
    "软件系统开发与定制", "应用平台建设运营", "业务信息系统升级", "电子政务系统",
    "大数据平台与数据分析", "云计算服务与云平台", "数据库与数据治理", "算法模型与人工智能应用", "文书档案数字化管理系统",
    "智能监管执法平台", "指挥调度信息管理平台", "物联网感知与智能控制", "智慧应用与数字化平台",
    "智慧信息平台", "智慧监管平台", "信息化系统建设", "视频督察与视频会议系统", 
    "机房智能化建设工程", "计算机网络系统集成", "弱电智能化系统工程", "网络信息安全防护系统", "高性能虚拟化平台",
    "系统集成实施服务", "软件运维技术支持", "信息系统测评监理", "网络安全等级保护", "沉浸式数字展厅体验系统", "城市运行数据采集系统",
    "档案数字化加工系统","电子政务外网建设","在线教学平台",
    "视频监控联网系统","雪亮工程信息化", "智能安防系统"
]

target = "临汾市人民医院第三方检测服务项目的采购公告"
model = get_model()
target_emb = model.encode(target)
anchor_embs = model.encode(refined_anchors)

scores = util.cos_sim(target_emb, anchor_embs)[0]
max_score = float(scores.max())
max_idx = int(scores.argmax())
max_anchor = refined_anchors[max_idx]

print(f"Target: {target}")
print(f"Max Score with Refined Anchors: {max_score:.4f} (Old was 0.6772)")
print(f"Most Similar Anchor: {max_anchor}")
