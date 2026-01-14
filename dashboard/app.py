from flask import Flask, jsonify, render_template, request, redirect, url_for
import pandas as pd
import os
import glob
import re

from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_apscheduler import APScheduler

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
    """智能路由：根据 User-Agent 分流"""
    user_agent = request.user_agent.string.lower()
    if 'mobile' in user_agent or 'android' in user_agent or 'iphone' in user_agent:
        return redirect(url_for('mobile_view'))
    return redirect(url_for('dashboard_view'))

@app.route('/dashboard')
def dashboard_view():
    """电脑端控制台 (原首页)"""
    return render_template('index.html')

@app.route('/mobile')
def mobile_view():
    """移动端专属界面"""
    return render_template('mobile.html')

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

            # 新增: 清除指定日期前的数据
            before_date_str = request.args.get('before_date')
            if before_date_str:
                try:
                    target_date = datetime.datetime.strptime(before_date_str, "%Y-%m-%d").date()
                    pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
                    files = glob.glob(pattern)
                    deleted_count = 0
                    
                    for f in files:
                        basename = os.path.basename(f)
                        # Extract date from filename (e.g., ..._2026年01月01日.xlsx)
                        match = re.search(r"shanxi_informatization_(.*)\.xlsx", basename)
                        if match:
                            file_date_str = match.group(1)
                            # Convert Chinese date to date object
                            try:
                                # Replace Chinese chars with hyphens for parsing
                                std_date_str = file_date_str.replace('年', '-').replace('月', '-').replace('日', '')
                                file_date = datetime.datetime.strptime(std_date_str, "%Y-%m-%d").date()
                                
                                if file_date < target_date:
                                    os.remove(f)
                                    deleted_count += 1
                            except Exception as e:
                                print(f"Skipping file {basename}: {e}")
                                continue
                                
                    return jsonify({"status": "success", "message": f"已清除 {before_date_str} 之前的数据 (共{deleted_count}个文件)"})
                except ValueError:
                    return jsonify({"status": "error", "message": "日期格式错误，应为 YYYY-MM-DD"}), 400
            
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

# --- 定时任务日志存储 ---
SCHEDULER_LOGS = []

def log_scheduler(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {msg}"
    SCHEDULER_LOGS.append(log_entry)
    # 保留最近 100 条日志
    if len(SCHEDULER_LOGS) > 100:
        SCHEDULER_LOGS.pop(0)

@app.route('/api/scheduler/logs')
def api_scheduler_logs():
    return jsonify({
        "logs": sorted(SCHEDULER_LOGS, reverse=True), # 最新的在前面
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# --- 定时任务配置 ---
class Config:
    SCHEDULER_API_ENABLED = True

app.config.from_object(Config())

scheduler = APScheduler()
scheduler.init_app(app)

def scheduled_job():
    """
    每天凌晨 02:00 执行的任务:
    1. 自动采集 [今天, 明天, 后天] 的数据 (如果文件不存在)。
    2. 清理 [前一天之前] 的数据 (保留前一天及以后)。
    """
    log_scheduler("⏰ 定时任务触发: 开始每日采集与清理...")
    print(f"[{datetime.datetime.now()}] ⏰ 定时任务触发: 开始每日采集与清理...")
    
    today = date.today()
    
    # --- 1. 自动采集逻辑 (今天、明天、后天) ---
    scrape_targets = []
    for i in range(3):
        target_date = today + timedelta(days=i) # 0=今天, 1=明天, 2=后天
        
        # 检查文件是否已存在
        date_str_iso = target_date.strftime("%Y年%m月%d日")
        filename = f"shanxi_informatization_{date_str_iso}.xlsx"
        filepath = os.path.join(RESULTS_DIR, filename)
        
        if os.path.exists(filepath):
            log_scheduler(f"   [跳过] {date_str_iso} 数据已存在。")
        else:
            log_scheduler(f"   [计划] {date_str_iso} 加入采集队列。")
            scrape_targets.append(target_date)
            
    if scrape_targets:
        if SCRAPER_STATUS["is_running"]:
            log_scheduler("   [跳过] 爬虫正在运行中，本次定时任务取消采集。")
        else:
            # 启动采集线程
            thread = threading.Thread(target=run_auto_scrape_thread, args=(scrape_targets,))
            thread.daemon = True
            thread.start()
            log_scheduler(f"   [启动] 已启动后台采集线程，目标: {len(scrape_targets)} 天")
    else:
        log_scheduler("   [完成] 所有目标日期数据均已就绪，无需采集。")

    # --- 2. 自动清理逻辑 (清理 前一天之前 的数据) ---
    # 定义: 
    #   今天 = T
    #   前一天 = T-1
    #   保留 >= T-1 的数据
    #   删除 < T-1 的数据
    cutoff_date = today - timedelta(days=1)
    log_scheduler(f"   [清理] 正在清理 {cutoff_date} 之前的数据...")
    
    pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
    files = glob.glob(pattern)
    deleted_count = 0
    
    for f in files:
        basename = os.path.basename(f)
        match = re.search(r"shanxi_informatization_(.*)\.xlsx", basename)
        if match:
            file_date_str = match.group(1)
            try:
                # 转换中文日期: 2026年01月01日 -> 2026-01-01
                std_date_str = file_date_str.replace('年', '-').replace('月', '-').replace('日', '')
                file_date = datetime.datetime.strptime(std_date_str, "%Y-%m-%d").date()
                
                # 如果文件日期 < 文档保留日期 (前一天)
                if file_date < cutoff_date:
                    os.remove(f)
                    log_scheduler(f"      [删除] {basename} (早于 {cutoff_date})")
                    deleted_count += 1
            except Exception as e:
                log_scheduler(f"      [错误] 解析 {basename} 失败: {e}")
                
    log_scheduler(f"   [清理完成] 共删除了 {deleted_count} 个过期文件。")

# --- 定时任务配置 ---
class Config:
    SCHEDULER_API_ENABLED = True

app.config.from_object(Config())

scheduler = APScheduler()
scheduler.init_app(app)

def scheduled_job():
    """
    每天凌晨 02:00 执行的任务:
    1. 自动采集 [今天, 明天, 后天] 的数据 (如果文件不存在)。
    2. 清理 [前一天之前] 的数据 (保留前一天及以后)。
    """
    print(f"[{datetime.datetime.now()}] ⏰ 定时任务触发: 开始每日采集与清理...")
    
    today = date.today()
    
    # --- 1. 自动采集逻辑 (今天、明天、后天) ---
    scrape_targets = []
    for i in range(3):
        target_date = today + timedelta(days=i) # 0=今天, 1=明天, 2=后天
        
        # 检查文件是否已存在
        date_str_iso = target_date.strftime("%Y年%m月%d日")
        filename = f"shanxi_informatization_{date_str_iso}.xlsx"
        filepath = os.path.join(RESULTS_DIR, filename)
        
        if os.path.exists(filepath):
            print(f"   [跳过] {date_str_iso} 数据已存在。")
        else:
            print(f"   [计划] {date_str_iso} 加入采集队列。")
            scrape_targets.append(target_date)
            
    if scrape_targets:
        if SCRAPER_STATUS["is_running"]:
            print("   [跳过] 爬虫正在运行中，本次定时任务取消采集。")
        else:
            # 启动采集线程
            thread = threading.Thread(target=run_auto_scrape_thread, args=(scrape_targets,))
            thread.daemon = True
            thread.start()
            print(f"   [启动] 已启动后台采集线程，目标: {len(scrape_targets)} 天")
    else:
        print("   [完成] 所有目标日期数据均已就绪，无需采集。")

    # --- 2. 自动清理逻辑 (清理 前一天之前 的数据) ---
    # 定义: 
    #   今天 = T
    #   前一天 = T-1
    #   保留 >= T-1 的数据
    #   删除 < T-1 的数据
    cutoff_date = today - timedelta(days=1)
    print(f"   [清理] 正在清理 {cutoff_date} 之前的数据...")
    
    pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
    files = glob.glob(pattern)
    deleted_count = 0
    
    for f in files:
        basename = os.path.basename(f)
        match = re.search(r"shanxi_informatization_(.*)\.xlsx", basename)
        if match:
            file_date_str = match.group(1)
            try:
                # 转换中文日期: 2026年01月01日 -> 2026-01-01
                std_date_str = file_date_str.replace('年', '-').replace('月', '-').replace('日', '')
                file_date = datetime.datetime.strptime(std_date_str, "%Y-%m-%d").date()
                
                # 如果文件日期 < 文档保留日期 (前一天)
                if file_date < cutoff_date:
                    os.remove(f)
                    print(f"      [删除] {basename} (早于 {cutoff_date})")
                    deleted_count += 1
            except Exception as e:
                print(f"      [错误] 解析 {basename} 失败: {e}")
                
    print(f"   [清理完成] 共删除了 {deleted_count} 个过期文件。")

# 添加定时任务: 每天 02:00 执行
scheduler.add_job(id='daily_task', func=scheduled_job, trigger='cron', hour=2, minute=0)
scheduler.start()
print("✅ 定时任务调度器已启动 (每天 02:00 执行)")


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
