
import pandas as pd
import os

file_path = "results/shanxi_informatization_2025年11月27日.xlsx"
if not os.path.exists(file_path):
    print("File not found.")
else:
    df = pd.read_excel(file_path)
    info = df[df['是否信息化'] == '是']
    print(f"Total Informatization Projects: {len(info)}")
    
    fields = ["预算限价项目", "开标具体时间", "开标地点", "采购人名称", "代理机构"]
    for field in fields:
        found = info[info[field].notna() & (info[field] != "未找到") & (info[field] != "待采集") & (info[field] != "采集失败/被封")]
        print(f"Field '{field}': {len(found)} / {len(info)} found.")
        if len(found) > 0:
            print(f"  Example: {found.iloc[0]['标题'][:20]} -> {found.iloc[0][field]}")
