from flask import Flask, jsonify, render_template, request, redirect, url_for, send_from_directory
from flask_apscheduler import APScheduler
import pandas as pd
import threading
import datetime
from datetime import date, timedelta
import sys
import os
import glob
import re
from werkzeug.utils import secure_filename

# 配置目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, '..', 'results')
# 临时上传目录
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 添加上级目录到 path 以导入 scraper 和 utils
sys.path.append(os.path.join(BASE_DIR, '..'))
import scraper
from dashboard.utils.comparator import compare_documents

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024 # 300MB Limit
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- 定时任务日志存储 (改为文件存储以解决多进程/线程问题) ---
# --- 定时任务日志存储 (改为文件存储以解决多进程/线程问题) ---
LOG_FILE = os.path.join(BASE_DIR, 'scheduler.log')
print(f"DEBUG: BASE_DIR={BASE_DIR}")
print(f"DEBUG: LOG_FILE={LOG_FILE}")

# Ensure file exists
try:
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"[{datetime.datetime.now()}] System Started\n")
except Exception as e:
    print(f"DEBUG: Failed to init log file: {e}")

def log_scheduler(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {msg}"
    print(log_entry) # 打印到控制台
    print(f"DEBUG_LOG: 准备写入文件: {LOG_FILE}")
    
    # 追加写入文件，立即刷新缓冲区 - 移除异常捕获以便看到真正的错误
    with open(LOG_FILE, 'a', encoding='utf-8', buffering=1) as f:
        f.write(log_entry + '\n')
        f.flush()  # 强制刷新到磁盘
        os.fsync(f.fileno())  # 确保写入磁盘
    print(f"DEBUG_LOG: 写入完成")

@app.route('/api/scheduler/logs')
def api_scheduler_logs():
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            # 不使用缓存读取
            with open(LOG_FILE, 'r', encoding='utf-8', buffering=1) as f:
                lines = f.readlines()
                # 只取最后 100 行，并反序 (最新的在前)
                logs = [line.strip() for line in lines[-100:]]
                logs.reverse()
        except Exception as e:
            logs = [f"Error reading logs: {e}"]
    else:
        logs = ["日志文件不存在"]
            
    return jsonify({
        "logs": logs,
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# --- 全局爬虫状态 ---
SCRAPER_STATUS = {
    "is_running": False,
    "current_date": None,
    "progress": 0,
    "total": 0,
    "logs": [],
    "completed_files": []
}

def get_available_dates():
    """扫描目录获取所有可用的日期文件"""
    pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
    files = glob.glob(pattern)
    dates = []
    for f in files:
        basename = os.path.basename(f)
        match = re.search(r"shanxi_informatization_(.*)\.xlsx", basename)
        if match:
            dates.append(match.group(1))
    
    dates.sort(reverse=True)
    return dates

def run_auto_scrape_thread(dates, is_scheduled_task=False):
    """后台运行爬虫的线程函数"""
    global SCRAPER_STATUS
    SCRAPER_STATUS["is_running"] = True
    SCRAPER_STATUS["total"] = len(dates)
    SCRAPER_STATUS["progress"] = 0
    SCRAPER_STATUS["logs"] = []
    SCRAPER_STATUS["completed_files"] = []
    
    def status_callback(msg):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        SCRAPER_STATUS["logs"].append(f"[{timestamp}] {msg}")
        if len(SCRAPER_STATUS["logs"]) > 50:
            SCRAPER_STATUS["logs"].pop(0)

    try:
        status_callback(f"任务开始，计划采集 {len(dates)} 个日期")
        
        for i, day in enumerate(dates):
            date_str_iso = day.strftime("%Y年%m月%d日")
            
            SCRAPER_STATUS["current_date"] = date_str_iso
            status_callback(f"正在处理: {date_str_iso} ({i+1}/{len(dates)})")
            
            # 调用爬虫
            result = scraper.run_scraper_for_date(date_str_iso, callback=status_callback)
            
            if result.get("file"):
                SCRAPER_STATUS["completed_files"].append(result["file"])
                
            SCRAPER_STATUS["progress"] = i + 1
            
        status_callback("所有任务已完成！")
        
        # --- 定时任务日志回写 ---
        if is_scheduled_task:
            files_count = len(SCRAPER_STATUS["completed_files"])
            log_msg = f"   [报告] 后台采集任务完成。新增 {files_count} 个文件。"
            log_scheduler(log_msg)
            # 强制再打印一遍，确保不为空
            print(f"DEBUG_THREAD: {log_msg}")
        
    except Exception as e:
        status_callback(f"任务异常终止: {str(e)}")
        print(f"Scrape thread error: {e}")
        if is_scheduled_task:
            log_scheduler(f"   [错误] 后台采集线程发生异常: {str(e)}")
            
    finally:
        SCRAPER_STATUS["is_running"] = False
        SCRAPER_STATUS["current_date"] = None

# --- 路由配置 ---

@app.route('/')
def index():
    user_agent = request.user_agent.string.lower()
    if 'mobile' in user_agent or 'android' in user_agent or 'iphone' in user_agent:
        return redirect(url_for('mobile_view'))
    return redirect(url_for('dashboard_view'))

@app.route('/dashboard')
def dashboard_view():
    return render_template('index.html')

@app.route('/mobile')
def mobile_view():
    return render_template('mobile.html')

@app.route('/api/tools/download')
def download_tools():
    # 保留此路由用于首页按钮 (只下载 Beyond Compare)
    filename = "Beyond-Compare-Pro-5.0.4.30422-x64.7z"
    directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": f"File not found: {filename}"}), 404
        
    return send_from_directory(directory, filename, as_attachment=True)

# --- 白老师工具箱相关路由 ---

ALLOWED_TOOLS = {
    'Beyond-Compare-Pro-5.0.4.30422-x64.7z',
    'WPS2016单文件极简版.7z'
}

@app.route('/bai')
def tools_view():
    return render_template('tools.html')

@app.route('/api/file/<filename>')
def download_specific_file(filename):
    if filename not in ALLOWED_TOOLS:
        return jsonify({"error": "File not found or access denied"}), 404
        
    directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": f"File not found on server: {filename}"}), 404
        
    return send_from_directory(directory, filename, as_attachment=True)

# --- 投标文件对比功能 ---

@app.route('/bijiao')
def bijiao_view():
    return render_template('bijiao.html')

import uuid

@app.route('/api/compare', methods=['POST'])
def api_compare():
    # 1. Check files
    if 'file_a' not in request.files or 'file_b' not in request.files:
        return jsonify({"error": "请至少上传两个投标文件 (A和B)"}), 400
        
    file_a = request.files['file_a']
    file_b = request.files['file_b']
    file_tender = request.files.get('file_tender') # Optional
    
    if file_a.filename == '' or file_b.filename == '':
        return jsonify({"error": "未选择文件"}), 400

    # 2. Save temporarily using UUID to avoid Chinese filename issues
    try:
        def save_temp_file(file_obj):
            ext = os.path.splitext(file_obj.filename)[1].lower()
            if not ext:
                # If no extension, try to guess from mimetype or just assume something?
                # But user usually has extension. If secure_filename destroyed it, we use original filename here.
                # If original filename has no extension, that's a user error, but we can't do much.
                pass
            
            temp_filename = f"{uuid.uuid4()}{ext}"
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            file_obj.save(temp_path)
            return temp_path

        path_a = save_temp_file(file_a)
        path_b = save_temp_file(file_b)
        
        path_tender = None
        if file_tender and file_tender.filename != '':
            path_tender = save_temp_file(file_tender)
            
        # 3. Process
        results = compare_documents(path_a, path_b, path_tender)
        
        # 4. Clean up
        try:
            os.remove(path_a)
            os.remove(path_b)
            if path_tender:
                os.remove(path_tender)
        except Exception:
            pass 
            
        return jsonify({"status": "success", "data": results})
        
    except Exception as e:
        print(f"Error during comparison: {e}")
        return jsonify({"error": f"处理出错: {str(e)}"}), 500

@app.route('/api/dates')
def api_dates():
    dates = get_available_dates()
    return jsonify(dates)

@app.route('/api/data', methods=['GET', 'DELETE'])
def api_data():
    if request.method == 'DELETE':
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

            before_date_str = request.args.get('before_date')
            if before_date_str:
                try:
                    target_date = datetime.datetime.strptime(before_date_str, "%Y-%m-%d").date()
                    pattern = os.path.join(RESULTS_DIR, "shanxi_informatization_*.xlsx")
                    files = glob.glob(pattern)
                    deleted_count = 0
                    
                    for f in files:
                        basename = os.path.basename(f)
                        match = re.search(r"shanxi_informatization_(.*)\.xlsx", basename)
                        if match:
                            file_date_str = match.group(1)
                            try:
                                std_date_str = file_date_str.replace('年', '-').replace('月', '-').replace('日', '')
                                file_date = datetime.datetime.strptime(std_date_str, "%Y-%m-%d").date()
                                
                                if file_date < target_date:
                                    os.remove(f)
                                    deleted_count += 1
                            except Exception as e:
                                continue
                                
                    return jsonify({"status": "success", "message": f"已清除 {before_date_str} 之前的数据 (共{deleted_count}个文件)"})
                except ValueError:
                    return jsonify({"status": "error", "message": "日期格式错误，应为 YYYY-MM-DD"}), 400
            
            if date_str:
                possible_filenames = [f"shanxi_informatization_{date_str}.xlsx"]
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

    date_str = request.args.get('date')
    if not date_str:
        return jsonify({"error": "Missing date parameter"}), 400
    
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
         return jsonify({"error": f"File not found for date: {date_str}"}), 404
    
    try:
        df = pd.read_excel(target_filepath)
        df = df.fillna("")
        data = df.to_dict('records')
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/scrape/auto_start', methods=['POST'])
def api_scrape_start():
    if SCRAPER_STATUS["is_running"]:
        return jsonify({"status": "error", "message": "已有任务正在运行中"}), 409
    
    data = request.get_json()
    if not data or 'dates' not in data:
         return jsonify({"status": "error", "message": "请选择至少一个日期"}), 400

    date_strs = data['dates']
    if not isinstance(date_strs, list) or len(date_strs) == 0:
        return jsonify({"status": "error", "message": "请选择至少一个日期"}), 400
    
    if len(date_strs) > 5:
        return jsonify({"status": "error", "message": "一次最多只能采集5天"}), 400

    target_dates = []
    try:
        for d_str in date_strs:
            dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
            target_dates.append(dt)
    except ValueError:
        return jsonify({"status": "error", "message": "日期格式错误，应为 YYYY-MM-DD"}), 400

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

# --- 定时任务配置 ---
class Config:
    SCHEDULER_API_ENABLED = True
    JOBS = [] 

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
            # 启动采集线程，传递 is_scheduled_task=True
            thread = threading.Thread(target=run_auto_scrape_thread, args=(scrape_targets, True))
            thread.daemon = True
            thread.start()
            log_scheduler(f"   [启动] 已启动后台采集线程，目标: {len(scrape_targets)} 天")
    else:
        log_scheduler("   [完成] 所有目标日期数据均已就绪，无需采集。")

    # --- 2. 自动清理逻辑 (清理 前一天之前 的数据) ---
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

# 添加定时任务并启动调度器
try:
    scheduler.add_job(id='daily_task', func=scheduled_job, trigger='cron', hour=2, minute=0)
    scheduler.start()
    print("✅ 定时任务调度器已启动 (每天 02:00 执行)")
except Exception as e:
    print(f"⚠️ 调度器启动失败: {e}")

@app.route('/api/test/trigger_scheduler', methods=['POST'])
def api_trigger_scheduler():
    """手动触发定时任务 (测试用)"""
    try:
        print("DEBUG: 开始手动触发定时任务...")
        scheduled_job()
        print("DEBUG: 定时任务调用完成")
        return jsonify({"status": "success", "message": "定时任务已手动触发"})
    except Exception as e:
        print(f"DEBUG: 触发失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

