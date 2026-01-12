
import pandas as pd
import os
import time
import re
import concurrent.futures
from scraper import fetch_page, parse_project_details

def recover_excel_data(file_path):
    if not os.path.exists(file_path):
        print(f"错误: 文件不存在 {file_path}")
        return

    print(f"正在读取文件: {file_path}")
    df = pd.read_excel(file_path)
    
    # 确保必要的列存在
    required_cols = ['标题', '链接', '是否信息化']
    for col in required_cols:
        if col not in df.columns:
            print(f"错误: Excel 缺少必要列 {col}")
            return

    # 筛选需要补全的信息化项目
    # 情况：是否信息化为'是'，且详细字段中任意一个为'未找到'或缺失
    detail_cols = ["预算限价项目", "开标具体时间", "开标地点", "采购人名称", "代理机构"]
    
    # 初始化缺失列
    for col in detail_cols:
        if col not in df.columns:
            df[col] = "待采集"

    mask = (df['是否信息化'] == '是') & (
        (df['预算限价项目'] == '未找到') | (df['预算限价项目'] == '待采集') | (df['预算限价项目'].isna()) |
        (df['采购人名称'] == '未找到') | (df['采购人名称'] == '待采集') | (df['采购人名称'].isna()) |
        (df['代理机构'] == '未找到') | (df['代理机构'] == '待采集') | (df['代理机构'].isna()) |
        (df['开标地点'] == '未找到') | (df['开标地点'] == '待采集') | (df['开标地点'].isna()) |
        (df['开标地点'].str.contains("线上|网上", na=False)) |
        (df['开标具体时间'] == '未找到') | (df['开标具体时间'] == '待采集') | (df['开标具体时间'].isna())
    )
    
    targets = df[mask].to_dict('records')
    
    if not targets:
        print("未发现需要补全或修正的数据项。")
        return

    print(f"发现 {len(targets)} 个信息化项目需要补全或修正详情。正在开始...")

    def process_item(item):
        url = item['链接']
        print(f"  过程中: {item['标题'][:20]}...")
        # 增加延迟避免再次被封
        time.sleep(1) 
        html = fetch_page(url)
        if html:
            details = parse_project_details(html)
            return details
        return None

    # 使用单线程或少量线程以确保安全
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(process_item, targets))

    # 写回 DataFrame
    target_indices = df[mask].index
    for idx, result in zip(target_indices, results):
        if result:
            for key, value in result.items():
                df.at[idx, key] = value
        else:
            for key in detail_cols:
                df.at[idx, key] = "采集失败/被封"

    # 保存更新后的文件
    try:
        df.to_excel(file_path, index=False)
        print(f"\n成功！更新后的数据已保存至: {file_path}")
    except PermissionError:
        new_path = file_path.replace(".xlsx", "_fixed.xlsx")
        print(f"\n警告: 无法写入原文件（可能已打开）。尝试保存至: {new_path}")
        df.to_excel(new_path, index=False)
    except Exception as e:
        print(f"保存失败: {e}")

if __name__ == "__main__":
    import sys
    print("--- 信息化项目详情补全工具 ---")
    
    if len(sys.argv) > 1:
        target_file = sys.argv[1]
        if os.path.exists(target_file):
            recover_excel_data(target_file)
        else:
            print(f"找不到文件: {target_file}")
    else:
        results_dir = "results"
        files = [f for f in os.listdir(results_dir) if f.endswith('.xlsx')]
        
        if not files:
            print("results 目录下没有找到 Excel 文件。")
        else:
            print("可用文件:")
            for i, f in enumerate(files):
                print(f"[{i}] {f}")
            
            try:
                choice = input("\n请选择要处理的文件编号 (直接输入文件名也可以): ").strip()
                if choice.isdigit():
                    idx = int(choice)
                    if 0 <= idx < len(files):
                        target_file = os.path.join(results_dir, files[idx])
                        recover_excel_data(target_file)
                elif choice.endswith(".xlsx"):
                    target_file = os.path.join(results_dir, choice) if not os.path.dirname(choice) else choice
                    recover_excel_data(target_file)
                else:
                    print("无效的选择。")
            except ValueError:
                print("请输入有效的编号或文件名。")
