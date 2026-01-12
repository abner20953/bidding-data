from flask import Flask, jsonify, render_template, request
import pandas as pd
import os
import glob
import re

app = Flask(__name__)

# 配置结果目录 (相对于 app.py 所在的 dashboard 目录，results 在上一级)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, '..', 'results')

def get_available_dates():
    """扫描目录获取所有可用的日期文件"""
    pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
    files = glob.glob(pattern)
    dates = []
    for f in files:
        basename = os.path.basename(f)
        # 提取文件名中的日期: shanxi_informatization_2025年11月27日.xlsx
        match = re.search(r"shanxi_informatization_(.*)\.xlsx", basename)
        if match:
            dates.append(match.group(1))
    
    # 按日期排序 (倒序，最近的在前)
    dates.sort(reverse=True)
    return dates

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/dates')
def api_dates():
    dates = get_available_dates()
    return jsonify(dates)

@app.route('/api/data', methods=['GET', 'DELETE'])
def api_data():
    if request.method == 'DELETE':
        # 处理删除逻辑
        try:
            delete_all = request.args.get('all') == 'true'
            date_str = request.args.get('date')
            
            if delete_all:
                pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
                files = glob.glob(pattern)
                deleted_count = 0
                for f in files:
                    try:
                        os.remove(f)
                        deleted_count += 1
                    except Exception as e:
                        print(f"Error deleting {f}: {e}")
                return jsonify({"status": "success", "message": f"已清除所有历史数据 (共{deleted_count}个文件)"})
            
            if date_str:
                # 尝试多种文件名格式
                possible_filenames = [
                    f"shanxi_informatization_{date_str}.xlsx",
                ]
                # 如果是 YYYY-MM-DD 格式，尝试转换为 YYYY年MM月DD日
                if "-" in date_str:
                    try:
                        parts = date_str.split("-")
                        if len(parts) == 3:
                            chinese_date = f"{int(parts[0])}年{int(parts[1]):02d}月{int(parts[2]):02d}日"
                            possible_filenames.append(f"shanxi_informatization_{chinese_date}.xlsx")
                    except:
                        pass
                
                deleted = False
                for filename in possible_filenames:
                    filepath = os.path.join(RESULTS_DIR, filename)
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                            deleted = True
                        except Exception as e:
                            print(f"Error removing {filepath}: {e}")
                
                if deleted:
                    return jsonify({"status": "success", "message": f"已删除 {date_str} 的数据"})
                else:
                    return jsonify({"status": "error", "message": f"文件不存在: {date_str}"}), 404
            
            return jsonify({"status": "error", "message": "缺少参数"}), 400
            
        except Exception as e:
             return jsonify({"status": "error", "message": str(e)}), 500

    # GET 请求处理
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({"error": "Missing date parameter"}), 400
    
    # 尝试多种文件名格式查找
    target_filepath = None
    possible_filenames = [f"shanxi_informatization_{date_str}.xlsx"]
    if "-" in date_str:
        try:
            parts = date_str.split("-")
            if len(parts) == 3:
                chinese_date = f"{int(parts[0])}年{int(parts[1]):02d}月{int(parts[2]):02d}日"
                possible_filenames.append(f"shanxi_informatization_{chinese_date}.xlsx")
        except:
            pass

    for fname in possible_filenames:
        fpath = os.path.join(RESULTS_DIR, fname)
        if os.path.exists(fpath):
            target_filepath = fpath
            break
            
    if not target_filepath:
         print(f"[DEBUG] File not found for date: {date_str}. Tried: {possible_filenames}")
         return jsonify({"error": f"File not found for date: {date_str}"}), 404
    
    try:
        # 读取 Excel
        df = pd.read_excel(target_filepath)
        
        # 处理 NaN 值
        df = df.fillna("")
        
        # 转换为字典列表
        data = df.to_dict('records')
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

import threading
import datetime
from datetime import date, timedelta
import sys
import os

# 添加上级目录到 path 以导入 scraper
# 注意：BASE_DIR 已在文件头部定义
sys.path.append(os.path.join(BASE_DIR, '..'))

import scraper

# 全局爬虫状态
SCRAPER_STATUS = {
    "is_running": False,
    "current_date": None,
    "progress": 0,
    "total": 0,
    "logs": [],
    "completed_files": []
}

def get_next_week_workdays():
    """获取下周的所有工作日 (周一到周五，暂不考虑法定节假日)"""
    today = date.today()
    # 计算下周一
    days_ahead = 7 - today.weekday()  # 0 = Monday
    if days_ahead <= 0:
        days_ahead += 7
    next_monday = today + timedelta(days=days_ahead)
    
    workdays = []
    # 下周一到周五
    for i in range(5):
        day = next_monday + timedelta(days=i)
        workdays.append(day)
            
    return workdays

def run_auto_scrape_thread(dates):
    """后台运行爬虫的线程函数"""
    global SCRAPER_STATUS
    SCRAPER_STATUS["is_running"] = True
    SCRAPER_STATUS["total"] = len(dates)
    SCRAPER_STATUS["progress"] = 0
    SCRAPER_STATUS["logs"] = []
    SCRAPER_STATUS["completed_files"] = []
    
    def status_callback(msg):
        # 限制日志长度
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        SCRAPER_STATUS["logs"].append(f"[{timestamp}] {msg}")
        if len(SCRAPER_STATUS["logs"]) > 50:
            SCRAPER_STATUS["logs"].pop(0)

    try:
        status_callback(f"任务开始，计划采集 {len(dates)} 个日期")
        for i, day in enumerate(dates):
            date_str_iso = day.strftime("%Y年%m月%d日")
            date_str_alt = day.strftime("%Y年%m月%d日").replace("年", "年").replace("月", "月").replace("日", "日") # Just preserving custom format
            
            SCRAPER_STATUS["current_date"] = date_str_iso
            status_callback(f"正在处理: {date_str_iso} ({i+1}/{len(dates)})")
            
            # 调用爬虫
            result = scraper.run_scraper_for_date(date_str_iso, callback=status_callback)
            
            if result.get("file"):
                SCRAPER_STATUS["completed_files"].append(result["file"])
                
            SCRAPER_STATUS["progress"] = i + 1
            
        status_callback("所有任务已完成！")
        
    except Exception as e:
        status_callback(f"任务异常终止: {str(e)}")
        print(f"Scrape thread error: {e}")
    finally:
        SCRAPER_STATUS["is_running"] = False
        SCRAPER_STATUS["current_date"] = None

@app.route('/api/scrape/auto_start', methods=['POST'])
@app.route('/api/scrape/auto_start', methods=['POST'])
def api_scrape_start():
    if SCRAPER_STATUS["is_running"]:
        return jsonify({"status": "error", "message": "已有任务正在运行中"}), 409
    
    data = request.get_json()
    if not data or 'dates' not in data:
         # 兼容旧的自动逻辑或报错? 既然有了新UI，就强制要求参数
         # 但为了方便，如果没参数，就保持默认行为(暂不建议)?
         return jsonify({"status": "error", "message": "请选择至少一个日期"}), 400

    date_strs = data['dates']
    if not isinstance(date_strs, list) or len(date_strs) == 0:
        return jsonify({"status": "error", "message": "请选择至少一个日期"}), 400
    
    if len(date_strs) > 5:
        return jsonify({"status": "error", "message": "一次最多只能采集5天"}), 400

    # 转换字符串为 date 对象以便 scraper 可能需要的格式，或者直接传字符串
    # 爬虫接受 "YYYY年MM月DD日" 格式，或者我们统一在这里转换
    # 假设前端传来的格式是 "YYYY-MM-DD"
    target_dates = []
    try:
        for d_str in date_strs:
            # 简单校验
            dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
            target_dates.append(dt)
    except ValueError:
        return jsonify({"status": "error", "message": "日期格式错误，应为 YYYY-MM-DD"}), 400

    # 启动后台线程
    thread = threading.Thread(target=run_auto_scrape_thread, args=(target_dates,))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "status": "success", 
        "message": f"已启动采集 {len(target_dates)} 个日期", 
        "target_dates": date_strs
    })

@app.route('/api/scrape/status')
def api_scrape_status():
    return jsonify(SCRAPER_STATUS)

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
