from scraper import parse_project_details

with open("sample_procurement_1.html", "r", encoding="utf-8") as f:
    html = f.read()

details = parse_project_details(html)

print("=== 采购需求提取测试 ===")
print(f"标题: {details.get('标题', 'N/A')}")
print(f"采购需求: {details.get('采购需求', '未找到')[:200]}...")
print(f"采购方式: {details.get('采购方式')}")
print(f"预算: {details.get('预算限价项目')}")
