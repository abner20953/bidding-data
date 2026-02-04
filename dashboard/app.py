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
import shutil
import time
import math
import sqlite3
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# 配置目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, '..', 'results')
# 临时上传目录
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

# Admin Password Configuration
ADMIN_PASSWORD = "108"

def verify_request_password(req):
    """
    Check if request contains valid admin password in JSON body or query args.
    Returns (True, None) or (False, error_response).
    """
    # Check JSON body
    if req.is_json:
        data = req.get_json()
        if data and data.get('password') == ADMIN_PASSWORD:
            return True, None
            
    # Check Query Args
    if req.args.get('password') == ADMIN_PASSWORD:
        return True, None
        
    return False, jsonify({"error": "Admin password required or invalid"}), 403

# --- EMERGENCY STARTUP CLEANUP (Fix for "No space left on device") ---
def free_up_space():
    try:
        print("🧹 Running Emergency Startup Cleanup...")
        
        # 1. Clear Uploads (Temp files)
        if os.path.exists(UPLOAD_FOLDER):
            try:
                for f in os.listdir(UPLOAD_FOLDER):
                    p = os.path.join(UPLOAD_FOLDER, f)
                    if os.path.isfile(p):
                        os.remove(p)
                        print(f"Deleted temp file: {f}")
            except Exception as e:
                print(f"Error cleaning uploads: {e}")

        # 2. Truncate Log File if > 50MB
        LOG_FILE = os.path.join(BASE_DIR, 'scheduler.log')
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 50 * 1024 * 1024:
            try:
                with open(LOG_FILE, 'w') as f:
                    f.write(f"[{datetime.datetime.now()}] Log truncated due to size limit.\n")
                print("Truncated oversized scheduler.log")
            except Exception as e:
                print(f"Error truncating log: {e}")
                
    except Exception as e:
        print(f"Cleanup failed: {e}")

free_up_space()

# Now try to create directory
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except OSError as e:
    if e.errno == 28: # No space left on device
        print("❌ CRITICAL: Disk full even after cleanup. Attempting to delete more...")
        # Desperate measure: Delete scheduler log entirely
        try:
             LOG_FILE = os.path.join(BASE_DIR, 'scheduler.log')
             if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
             # Try makedirs again
             os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        except Exception:
             pass
    if not os.path.exists(UPLOAD_FOLDER):
        print("❌ FAILED TO CREATE UPLOAD FOLDER - APP MAY CRASH")
        # Proceed anyway, maybe read-only works
    else:
        raise e # Re-raise if it wasn't fixed

# 归档目录 (D:/ai_project/1/file)
ARCHIVE_FOLDER = os.path.join(BASE_DIR, '..', 'file')
# 如果是绝对路径需求，可以硬编码，但建议相对路径以适应不同部署
# 用户请求: /file 目录 (可能是 D:/file 或项目根目录/file)
# 这里假设是项目根目录下的 file 文件夹
os.makedirs(ARCHIVE_FOLDER, exist_ok=True)

# 添加上级目录到 path 以导入 scraper 和 utils
sys.path.append(os.path.join(BASE_DIR, '..'))
import scraper
from dashboard.utils.comparator import compare_documents

app = Flask(__name__)
# Apply ProxyFix to handle X-Forwarded-For headers from Nginx/LoadBalancer
# x_for=1 means trust the first X-Forwarded-For value
# x_proto=1 means trust X-Forwarded-Proto
# x_host=1 means trust X-Forwarded-Host
# x_port=1 means trust X-Forwarded-Port
# x_prefix=1 means trust X-Forwarded-Prefix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024 # 300MB Limit
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- 定时任务日志存储 (改为文件存储以解决多进程/线程问题) ---
# --- 定时任务日志存储 (改为文件存储以解决多进程/线程问题) ---
LOG_FILE = os.path.join(BASE_DIR, 'scheduler.log')
print(f"DEBUG: BASE_DIR={BASE_DIR}")
LOG_FILE = os.path.join(BASE_DIR, 'scheduler.log')
VISITOR_DB = os.path.join(BASE_DIR, 'visitor_logs.db')
print(f"DEBUG: BASE_DIR={BASE_DIR}")
print(f"DEBUG: LOG_FILE={LOG_FILE}")
print(f"DEBUG: VISITOR_DB={VISITOR_DB}")

def init_visitor_db():
    try:
        conn = sqlite3.connect(VISITOR_DB)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT,
                path TEXT,
                method TEXT,
                status_code INTEGER,
                timestamp TEXT,
                user_agent TEXT,
                browser TEXT,
                os TEXT,
                device TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error initializing visitor db: {e}")

init_visitor_db()

# Ensure file exists
try:
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"[{datetime.datetime.now()}] System Started\n")
except Exception as e:
    print(f"DEBUG: Failed to init log file: {e}")

# Version Print (Visible in Docker Logs)
print("="*50)
print(f"🚀 SYSTEM STARTUP: VERSION 2026-01-22-LIBREOFFICE-PATCH")
print(f"🚀 TIME: {datetime.datetime.now()}")
print("="*50)
sys.stdout.flush()

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

# --- Global Pending Visits Store ---
PENDING_VISITS = {} # Format: { ip: { 'path': ..., 'timestamp': datetime, 'data': ... } }
PENDING_LOCK = threading.Lock()

# --- Middleware for Visitor Logging ---

@app.after_request
def log_request(response):
    try:
        # 1. Basic Filters
        if request.path.startswith('/static') or request.path.startswith('/api/file') or request.path == '/favicon.ico':
            return response
            
        # If trying to access specific knowledge pages, store return URL
        if request.path.startswith('/zhishi/view/') or request.path.startswith('/zhishi/edit/'):
            session['return_url'] = request.url
            
        # 2. Identify Request Type
        is_api = request.path.startswith('/api/') or '/api/' in request.path
        
        # 3. Get IP (Proxy safe)
        ip = request.remote_addr
            
        current_time = datetime.datetime.now()

        # --- LOGIC BRANCH ---
        with PENDING_LOCK:
            if is_api:
                # [API Request] -> Check & Flush Pending Page Visits
                # Does NOT log the API request itself (as per requirements)
                
                if ip in PENDING_VISITS:
                    pending = PENDING_VISITS[ip]
                    visit_time = pending['timestamp']
                    
                    # Check time window (5 minutes)
                    if (current_time - visit_time) < datetime.timedelta(minutes=5):
                        # VALID VISIT! Write to DB
                        try:
                            # Re-construct user agent info from pending data
                            # (We use the pending data because that was the page visit context)
                            p_data = pending['data']
                            
                            conn = sqlite3.connect(VISITOR_DB)
                            cursor = conn.cursor()
                            cursor.execute('''
                                INSERT INTO logs (ip, path, method, status_code, timestamp, user_agent, browser, os, device)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (ip, pending['path'], p_data['method'], p_data['status_code'], 
                                  visit_time.strftime("%Y-%m-%d %H:%M:%S"), 
                                  p_data['ua_string'], p_data['browser'], p_data['os'], p_data['device']))
                            conn.commit()
                            conn.close()
                            # print(f"DEBUG: Validated visit for {ip} -> {pending['path']}")
                        except Exception as e:
                            print(f"Error writing validated log: {e}")
                    
                    # Clear pending after processing (whether valid or expired)
                    del PENDING_VISITS[ip]
                    
            else:
                # [Page Request] -> Store as Pending
                # Only log if it matches a valid route
                if request.url_rule:
                    
                    # Prepare Data
                    ua = request.user_agent
                    ua_string = ua.string
                    
                    browser = f"{ua.browser} {ua.version}" if ua.browser else "Unknown"
                    os_info = f"{ua.platform} {ua.version}" if ua.platform else "Unknown"
                    
                    if 'Windows' in ua_string: os_info = 'Windows'
                    elif 'Android' in ua_string: os_info = 'Android'
                    elif 'iPhone' in ua_string or 'iPad' in ua_string: os_info = 'iOS'
                    elif 'Mac' in ua_string: os_info = 'MacOS'
                    elif 'Linux' in ua_string: os_info = 'Linux'
                    
                    device = "PC"
                    if 'Mobile' in ua_string or 'Android' in ua_string or 'iPhone' in ua_string:
                        device = "Mobile"
                    
                    # Overwrite any existing pending visit for this IP (User moved to new page)
                    PENDING_VISITS[ip] = {
                        'path': request.path,
                        'timestamp': current_time,
                        'data': {
                            'method': request.method,
                            'status_code': response.status_code,
                            'ua_string': ua_string,
                            'browser': browser,
                            'os': os_info,
                            'device': device
                        }
                    }
                    # print(f"DEBUG: Pending visit stored for {ip} -> {request.path}")

    except Exception as e:
        print(f"Logging error: {e}")
        
    return response

# --- New Routes for Visitor Logs ---

@app.route('/fangke')
def visitor_logs_view():
    return render_template('access_logs.html')

@app.route('/api/visitor_logs', methods=['GET', 'DELETE'])
def api_get_visitor_logs():
    if request.method == 'DELETE':
        try:
            conn = sqlite3.connect(VISITOR_DB)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM logs')
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "message": "All logs cleared."})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    try:
        ip_query = request.args.get('ip', '').strip()
        path_query = request.args.get('path', '').strip()
        date_query = request.args.get('date', '').strip()
        
        conn = sqlite3.connect(VISITOR_DB)
        conn.row_factory = sqlite3.Row # Allow dict-like access
        cursor = conn.cursor()
        
        query = "SELECT * FROM logs WHERE 1=1"
        params = []
        
        if ip_query and ip_query != 'all':
            query += " AND ip = ?"
            params.append(ip_query)
            
        if path_query and path_query != 'all':
             query += " AND path = ?"
             params.append(path_query)

        if date_query:
             query += " AND timestamp LIKE ?"
             params.append(f"{date_query}%")
             
        # Order by latest first
        query += " ORDER BY id DESC LIMIT 500" # Increase limit for searching
        
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        
        logs = []
        for row in rows:
            logs.append(dict(row))
            
        conn.close()
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/visitor_logs/options')
def api_visitor_log_options():
    try:
        conn = sqlite3.connect(VISITOR_DB)
        cursor = conn.cursor()
        
        # Get distinct IPs and Paths from the last 7 days (logs table is self-cleaning)
        cursor.execute("SELECT DISTINCT ip FROM logs ORDER BY id DESC")
        ips = [row[0] for row in cursor.fetchall()]
        
        cursor.execute("SELECT DISTINCT path FROM logs ORDER BY path ASC")
        paths = [row[0] for row in cursor.fetchall()]
        
        conn.close()
        return jsonify({"ips": ips, "paths": paths})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 路由配置 ---

@app.route('/')
def index():
    user_agent = request.user_agent.string.lower()
    if 'mobile' in user_agent or 'android' in user_agent or 'iphone' in user_agent:
        return redirect(url_for('mobile_view'))
    return redirect(url_for('dashboard_view'))

@app.route('/dashboard')
def dashboard_view():
    return render_template('index.html', show_collect=False)

@app.route('/caiji')
def collection_view():
    """数据采集专用入口"""
    return render_template('index.html', show_collect=True)

@app.route('/mobile')
def mobile_view():
    return render_template('mobile.html')

@app.route('/all')
def navigation_index():
    return render_template('nav_index.html')

@app.route('/a11')
def navigation_index_full():
    return render_template('nav_index_full.html')

@app.route('/api/tools/download')
def download_tools():
    # 保留此路由用于首页按钮 (只下载 Beyond Compare)
    filename = "Beyond-Compare-Pro-5.0.4.30422-x64.7z"
    directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": f"File not found: {filename}"}), 404
        
    return send_from_directory(directory, filename, as_attachment=True)

# --- 工具箱相关路由 ---

ALLOWED_TOOLS = {
    'Beyond-Compare-Pro-5.0.4.30422-x64.7z',
    'WPS2016单文件极简版.7z',
    'WPS2023专业增强版-v12.1.0.23542-激活优化版.exe',
    'WPS2019专业增强版_v11.8.2.10972_中石油定制版.exe'
}

@app.route('/bai')
def tools_view():
    return render_template('tools.html')

@app.route('/api/file/<filename>')
def download_specific_file(filename):
    if filename not in ALLOWED_TOOLS:
        return jsonify({"error": "File not found or access denied"}), 404
        
    # Priority 1: Check mounted external tools directory (Docker volume)
    # Mounted from /root/bidding-data -> /app/tools
    external_tools_dir = '/app/tools'
    if os.path.exists(os.path.join(external_tools_dir, filename)):
        return send_from_directory(external_tools_dir, filename, as_attachment=True)

    # Priority 2: Fallback to old behavior (project root)
    directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    file_path = os.path.join(directory, filename)
    
    if not os.path.exists(file_path):
        # Debug: List files in /app/tools to see what is mounted
        try:
            mount_files = os.listdir(external_tools_dir)
        except Exception as e:
            mount_files = str(e)
            
        return jsonify({
            "error": f"File not found. Looked in: {external_tools_dir} AND {file_path}",
            "mount_content": mount_files,
            "cwd": os.getcwd()
        }), 404
        
    return send_from_directory(directory, filename, as_attachment=True)

# --- 归档辅助函数 ---

def cleanup_file_archive():
    """
    检查归档目录大小，如果超过 1GB，则清理旧文件直到小于 600MB
    同时清理对应的备注文件 (.txt)
    """
    try:
        total_size = 0
        file_list = []
        
        # 扫描所有文件
        with os.scandir(ARCHIVE_FOLDER) as it:
            for entry in it:
                if entry.is_file():
                    # Skip remark files themselves from counting logic to simplify, 
                    # or include them? Let's include everything but handle pairings during deletion.
                    # Or simpler: just count everything. 
                    # If we delete "A.pdf", we also delete "A.pdf.txt".
                    # If we encounter "A.pdf.txt" in the loop, we might accidentally delete it if we sort by time?
                    # Better strategy: Only track main files for deletion candidates, but add size of remarks to total.
                    
                    if entry.name.endswith('.txt') and os.path.exists(os.path.join(ARCHIVE_FOLDER, entry.name[:-4])):
                         # This is a remark file and the main file exists. skip adding to list to avoid double checking.
                         # But add size.
                         total_size += entry.stat().st_size
                         continue
                         
                    size = entry.stat().st_size
                    mtime = entry.stat().st_mtime
                    total_size += size
                    file_list.append({"path": entry.path, "name": entry.name, "size": size, "mtime": mtime})
        
        limit_3gb = 3 * 1024 * 1024 * 1024 # 3GB
        target_2_2gb = int(2.2 * 1024 * 1024 * 1024)   # 2.2GB
        
        if total_size > limit_3gb:
            print(f"Archive clean up started. Current size: {total_size / (1024*1024):.2f} MB")
            # 按修改时间排序 (旧文件在前)
            file_list.sort(key=lambda x: x['mtime'])
            
            deleted_size = 0
            for f in file_list:
                if total_size <= target_2_2gb:
                    break
                
                try:
                    # Delete main file
                    if os.path.exists(f['path']):
                        os.remove(f['path'])
                        total_size -= f['size']
                        deleted_size += f['size']
                        
                    # Delete remark file if exists
                    remark_path = f['path'] + ".txt"
                    if os.path.exists(remark_path):
                        r_size = os.path.getsize(remark_path)
                        os.remove(remark_path)
                        total_size -= r_size
                        deleted_size += r_size
                        
                except Exception as e:
                    print(f"Error deleting archived file {f['path']}: {e}")
            
            print(f"Archive clean up finished. Deleted {deleted_size / (1024*1024):.2f} MB. New size: {total_size / (1024*1024):.2f} MB")
            
    except Exception as e:
        print(f"Error in cleanup_file_archive: {e}")

def archive_file(source_path, original_filename):
    """
    将临时文件保存到归档目录
    """
    try:
        if not original_filename:
            return
            
        target_path = os.path.join(ARCHIVE_FOLDER, original_filename)
        
        # 如果文件已存在，不覆盖，直接跳过
        if os.path.exists(target_path):
            return
            
        shutil.copy2(source_path, target_path)
    except Exception as e:
        print(f"Error archiving file {original_filename}: {e}")

# --- 归档页面路由 ---

@app.route('/api/file/remark/<filename>', methods=['GET'])
def api_get_remark(filename):
    """获取文件备注"""
    try:
        remark_path = os.path.join(ARCHIVE_FOLDER, filename + ".txt")
        if not os.path.exists(remark_path):
            return jsonify({"status": "success", "content": ""})
            
        with open(remark_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({"status": "success", "content": content})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/file/remark/<filename>', methods=['POST'])
def api_save_remark(filename):
    """保存文件备注"""
    try:
        data = request.get_json()
        content = data.get('content', '')
        
        # Ensure main file exists
        file_path = os.path.join(ARCHIVE_FOLDER, filename)
        if not os.path.exists(file_path):
             return jsonify({"status": "error", "message": "Original file not found"}), 404
             
        remark_path = os.path.join(ARCHIVE_FOLDER, filename + ".txt")
        with open(remark_path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/file/archive/<filename>', methods=['DELETE'])
def api_delete_archive(filename):
    try:
        data = request.get_json()
        password = data.get('password')
        
        if password != '108':
            return jsonify({"status": "error", "message": "密码错误"}), 403
            
        file_path = os.path.join(ARCHIVE_FOLDER, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            
        remark_path = os.path.join(ARCHIVE_FOLDER, filename + ".txt")
        if os.path.exists(remark_path):
            os.remove(remark_path)
            
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/file/archive/batch', methods=['DELETE'])
def api_batch_delete_archive():
    try:
        data = request.get_json()
        password = data.get('password')
        date_str = data.get('date')
        
        if password != '108':
            return jsonify({"status": "error", "message": "密码错误"}), 403
            
        if not date_str:
            return jsonify({"status": "error", "message": "请选择日期"}), 400
            
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        
        deleted_count = 0
        freed_size = 0
        
        with os.scandir(ARCHIVE_FOLDER) as it:
            for entry in it:
                if entry.is_file():
                    # Check modification time
                    mtime = datetime.datetime.fromtimestamp(entry.stat().st_mtime)
                    if mtime < target_date:
                        try:
                            size = entry.stat().st_size
                            os.remove(entry.path)
                            freed_size += size
                            deleted_count += 1
                            
                            # Try delete remark
                            remark_path = entry.path + ".txt"
                            if os.path.exists(remark_path):
                                freed_size += os.path.getsize(remark_path)
                                os.remove(remark_path)
                        except Exception as e:
                            print(f"Error deleting {entry.name}: {e}")
                            
        return jsonify({
            "status": "success", 
            "deleted_count": deleted_count,
            "freed_size": format_size(freed_size)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])

@app.route('/bijiao/file')
def file_list_view():
    files = []
    total_size = 0
    try:
        with os.scandir(ARCHIVE_FOLDER) as it:
            for entry in it:
                if entry.is_file():
                    if not entry.name.lower().endswith('.pdf'):
                        continue
                        
                    stat = entry.stat()
                    total_size += stat.st_size
                    files.append({
                        "name": entry.name,
                        "size": format_size(stat.st_size),
                        "time": datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                        "timestamp": stat.st_mtime
                    })
        # Sort by time descend
        files.sort(key=lambda x: x['timestamp'], reverse=True)
    except Exception as e:
        print(f"Error listing archive: {e}")
        
    return render_template('file_list.html', files=files, total_size=format_size(total_size))

@app.route('/bijiao/file/<path:filename>')
def download_archived_file(filename):
    return send_from_directory(ARCHIVE_FOLDER, filename, as_attachment=True)

# --- Knowledge Base Blueprint ---
from dashboard.blueprints.knowledge import knowledge_bp, init_db as init_knowledge_db
app.register_blueprint(knowledge_bp)
# Initialize DB at startup
try:
    with app.app_context():
        init_knowledge_db()
    print("✅ Knowledge Base DB Initialized")
except Exception as e:
    print(f"⚠️ Knowledge Base DB Init Failed: {e}")

# --- 投标文件对比功能 ---

@app.route('/bijiao')
def bijiao_view():
    return render_template('bijiao.html')

import uuid

@app.route('/api/compare', methods=['POST'])
def api_compare():
    # 0. Auto Cleanup Archive if needed
    cleanup_file_archive()

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
            temp_filename = f"{uuid.uuid4()}{ext}"
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            file_obj.save(temp_path)
            
            # --- Archive Hook ---
            # Save original file to archive folder
            archive_file(temp_path, file_obj.filename)
            # --------------------
            
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
        # Verify Password
        is_valid, error = verify_request_password(request)
        if not is_valid:
            # Checking JSON explicitly for this endpoint as it might be called differently
            data = request.get_json(silent=True)
            if not data or data.get('password') != ADMIN_PASSWORD:
                 return error

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
    
    # --- 1. 自动采集逻辑 (今天、明天、后天、大后天) ---
    scrape_targets = []
    for i in range(4):
        target_date = today + timedelta(days=i) # 0=今天, 1=明天, 2=后天, 3=大后天
        
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

    # --- 3. 访客日志清理逻辑 (保留最近 7 天) ---
    try:
        cleanup_date_limit = (today - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        log_scheduler(f"   [日志维护] 正在清理 {cleanup_date_limit} 之前的访客记录...")
        
        conn = sqlite3.connect(VISITOR_DB)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM logs WHERE timestamp < ?", (cleanup_date_limit,))
        deleted_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        log_scheduler(f"   [日志维护] 清理完成，删除了 {deleted_rows} 条旧日志。")
    except Exception as e:
        log_scheduler(f"   [日志维护] 错误: {str(e)}")

# 添加定时任务并启动调度器
try:
    scheduler.add_job(id='daily_task', func=scheduled_job, trigger='cron', hour=7, minute=0)
    scheduler.start()
    print("✅ 定时任务调度器已启动 (每天 07:00 执行)")
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

