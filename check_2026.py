
import pandas as pd
import os

file_path = "results/shanxi_informatization_2026年01月16日.xlsx"
if not os.path.exists(file_path):
    print("File not found.")
else:
    df = pd.read_excel(file_path)
    info = df[df['是否信息化'] == '是']
    print(f"Total Informatization Projects: {len(info)}")
    
    fields = ["预算限价项目", "开标具体时间", "开标地点", "采购人名称", "代理机构"]
    for field in fields:
        found = info[info[field].notna() & (info[field] != "未找到") & (info[field] != "待采集") & (info[field] != "采集失败/被封")]
        print(f"\n--- Field '{field}': {len(found)} / {len(info)} found ---")
        if len(found) > 0:
            for i in range(min(2, len(found))):
                print(f"  Project: {found.iloc[i]['标题'][:30]}...")
                print(f"  Value: {found.iloc[i][field]}")
