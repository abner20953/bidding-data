from flask import Flask, jsonify, render_template, request, redirect, url_for, send_from_directory, session, make_response
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
import hashlib
import json
import copy
import subprocess
from dotenv import load_dotenv

# 配置目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '..', '.env'))
RESULTS_DIR = os.path.join(BASE_DIR, '..', 'results')
# 统一的数据库数据目录
DATA_DIR = os.path.join(BASE_DIR, '..', 'data')
os.makedirs(DATA_DIR, exist_ok=True)
# 临时上传目录
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

# Admin Password Configuration
ADMIN_PASSWORD = "108"
CHAT_DB = os.path.join(DATA_DIR, 'chat.db')

def init_chat_db():
    conn = sqlite3.connect(CHAT_DB)
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
    except Exception:
        pass
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  uid TEXT,
                  ip TEXT,
                  content TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# Initialize chat DB on startup
if not os.path.exists(CHAT_DB):
    init_chat_db()

def verify_request_password(req):
    """
    Check if request contains valid admin password in JSON body or query args.
    Returns (True, None) or (False, response_tuple).
    """
    # Check JSON body
    if req.is_json:
        data = req.get_json(silent=True)
        if data and data.get('password') == ADMIN_PASSWORD:
            return True, None
            
    # Check Query Args
    if req.args.get('password') == ADMIN_PASSWORD:
        return True, None
        
    return False, (jsonify({"error": "Admin password required or invalid"}), 403)

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
        LOG_FILE = os.path.join(DATA_DIR, 'scheduler.log')
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
             LOG_FILE = os.path.join(DATA_DIR, 'scheduler.log')
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
from dashboard.utils.comparator import ComparisonLimitError, compare_documents

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['BASE_DIR'] = BASE_DIR
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
LOG_FILE = os.path.join(DATA_DIR, 'scheduler.log')
VISITOR_DB = os.path.join(DATA_DIR, 'visitor_logs.db')
print(f"DEBUG: BASE_DIR={BASE_DIR}")
print(f"DEBUG: LOG_FILE={LOG_FILE}")
print(f"DEBUG: VISITOR_DB={VISITOR_DB}")

def init_visitor_db():
    try:
        conn = sqlite3.connect(VISITOR_DB)
        try:
            conn.execute('PRAGMA journal_mode=WAL;')
        except Exception:
            pass
        cursor = conn.cursor()
        # 新表：记录用户实质性操作而非页面访问
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT,
                action TEXT,
                detail TEXT,
                timestamp TEXT,
                user_agent TEXT,
                browser TEXT,
                os TEXT,
                device TEXT
            )
        ''')
        # 如果旧表 logs 存在，可以删除（数据迁移无意义）
        cursor.execute("DROP TABLE IF EXISTS logs")
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
    "completed_files": [],
    "result_status": "idle",
    "errors": [],
    "warnings": [],
    "date_results": []
}
SCRAPER_STATE_LOCK = threading.Lock()
SCRAPER_LOCK_FILE = os.path.join(DATA_DIR, "scraper_task.lock")
SCHEDULER_LOCK_FILE = os.path.join(DATA_DIR, "scheduler_leader.lock")
SCHEDULER_LOCK_HANDLE = None


def acquire_process_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, IOError):
        handle.close()
        return None


def release_process_lock(handle):
    if not handle:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        pass
    finally:
        handle.close()

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

def run_auto_scrape_thread(dates, is_scheduled_task=False, process_lock=None):
    """后台运行爬虫的线程函数"""
    global SCRAPER_STATUS

    def status_callback(msg):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        with SCRAPER_STATE_LOCK:
            SCRAPER_STATUS["logs"].append(f"[{timestamp}] {msg}")
            if len(SCRAPER_STATUS["logs"]) > 100:
                SCRAPER_STATUS["logs"].pop(0)

    def record_result(date_str_iso, result):
        result_status = result.get("status", "failed")
        with SCRAPER_STATE_LOCK:
            SCRAPER_STATUS["date_results"].append({
                "date": date_str_iso,
                "status": result_status,
                "total": result.get("total", 0),
                "file": result.get("file"),
                "error": result.get("error"),
                "warnings": result.get("warnings", []),
            })
            if result.get("file"):
                SCRAPER_STATUS["completed_files"].append(result["file"])
            if result.get("error"):
                SCRAPER_STATUS["errors"].append(f"{date_str_iso}: {result['error']}")
            SCRAPER_STATUS["warnings"].extend(result.get("warnings", []))
            SCRAPER_STATUS["progress"] = len(SCRAPER_STATUS["date_results"])

        if is_scheduled_task:
            if result_status == "no_data":
                log_scheduler(f"   [无数据] {date_str_iso} 已确认无招标数据。")
            elif result_status == "failed":
                log_scheduler(f"   [失败] {date_str_iso}: {result.get('error', '未知错误')}")
            elif result_status == "partial":
                log_scheduler(f"   [部分成功] {date_str_iso}: {'; '.join(result.get('warnings', []))}")

    worker = None
    try:
        status_callback(f"任务开始，计划采集 {len(dates)} 个日期")
        date_args = [day.strftime("%Y-%m-%d") for day in dates]
        worker_path = os.path.abspath(os.path.join(BASE_DIR, "..", "scrape_worker.py"))
        if not os.path.isfile(worker_path):
            error_message = f"采集子进程脚本不存在: {worker_path}"
            status_callback(error_message)
            for day in dates:
                date_str_iso = day.strftime("%Y年%m月%d日")
                record_result(date_str_iso, {
                    "status": "failed",
                    "total": 0,
                    "file": None,
                    "error": error_message,
                })
            with SCRAPER_STATE_LOCK:
                SCRAPER_STATUS["result_status"] = "failed"
            if is_scheduled_task:
                log_scheduler(f"   [错误] {error_message}")
            return
        worker = subprocess.Popen(
            [sys.executable, "-u", worker_path, *date_args],
            cwd=os.path.abspath(os.path.join(BASE_DIR, "..")),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        completed_dates = set()
        for raw_line in worker.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            if line.startswith("__SCRAPER_EVENT__"):
                try:
                    event = json.loads(line[len("__SCRAPER_EVENT__"):])
                except json.JSONDecodeError:
                    status_callback(f"无法解析采集进程事件: {line[:200]}")
                    continue
                if event.get("type") == "date_start":
                    with SCRAPER_STATE_LOCK:
                        SCRAPER_STATUS["current_date"] = event["date"]
                    status_callback(
                        f"正在处理: {event['date']} ({event['index']}/{event['total']})"
                    )
                elif event.get("type") == "date_result":
                    completed_dates.add(event["date"])
                    record_result(event["date"], event["result"])
                continue
            status_callback(line)
        return_code = worker.wait()

        for day in dates:
            date_str_iso = day.strftime("%Y年%m月%d日")
            if date_str_iso not in completed_dates:
                record_result(date_str_iso, {
                    "status": "failed", "total": 0, "file": None,
                    "error": f"采集子进程异常退出，返回码 {return_code}",
                })

        with SCRAPER_STATE_LOCK:
            failed_count = sum(1 for item in SCRAPER_STATUS["date_results"] if item["status"] == "failed")
            partial_count = sum(1 for item in SCRAPER_STATUS["date_results"] if item["status"] == "partial")
            if failed_count == len(dates):
                SCRAPER_STATUS["result_status"] = "failed"
            elif failed_count or partial_count:
                SCRAPER_STATUS["result_status"] = "partial"
            else:
                SCRAPER_STATUS["result_status"] = "success"
        status_callback(f"任务结束，状态: {SCRAPER_STATUS['result_status']}")

        if is_scheduled_task:
            files_count = len(SCRAPER_STATUS["completed_files"])
            log_msg = f"   [报告] 后台采集任务结束，状态 {SCRAPER_STATUS['result_status']}，生成 {files_count} 个文件。"
            log_scheduler(log_msg)
    except Exception as e:
        if worker and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
        status_callback(f"任务异常终止: {str(e)}")
        with SCRAPER_STATE_LOCK:
            SCRAPER_STATUS["result_status"] = "failed"
            SCRAPER_STATUS["errors"].append(str(e))
        print(f"Scrape thread error: {e}")
        if is_scheduled_task:
            log_scheduler(f"   [错误] 后台采集线程发生异常: {str(e)}")
    finally:
        with SCRAPER_STATE_LOCK:
            SCRAPER_STATUS["is_running"] = False
            SCRAPER_STATUS["current_date"] = None
        release_process_lock(process_lock)


def start_scrape_task(dates, is_scheduled_task=False):
    process_lock = acquire_process_lock(SCRAPER_LOCK_FILE)
    if process_lock is None:
        return False, "已有采集任务正在运行中"
    with SCRAPER_STATE_LOCK:
        if SCRAPER_STATUS["is_running"]:
            release_process_lock(process_lock)
            return False, "已有采集任务正在运行中"
        SCRAPER_STATUS.update({
            "is_running": True,
            "current_date": None,
            "progress": 0,
            "total": len(dates),
            "logs": [],
            "completed_files": [],
            "result_status": "running",
            "errors": [],
            "warnings": [],
            "date_results": [],
        })
    try:
        thread = threading.Thread(
            target=run_auto_scrape_thread,
            args=(dates, is_scheduled_task, process_lock),
            daemon=True,
        )
        thread.start()
    except Exception:
        with SCRAPER_STATE_LOCK:
            SCRAPER_STATUS["is_running"] = False
            SCRAPER_STATUS["result_status"] = "failed"
        release_process_lock(process_lock)
        raise
    return True, "采集任务已启动"

# --- 用户操作日志工具函数 ---

def _parse_user_agent():
    """从请求中提取设备信息"""
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
    
    return ua_string, browser, os_info, device

def log_user_action(action, detail=""):
    """记录用户实质性操作到数据库"""
    try:
        ip = request.remote_addr
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ua_string, browser, os_info, device = _parse_user_agent()
        
        conn = sqlite3.connect(VISITOR_DB)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO action_logs (ip, action, detail, timestamp, user_agent, browser, os, device)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (ip, action, detail, timestamp, ua_string, browser, os_info, device))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error logging user action: {e}")

# --- Middleware: 保留 session return_url 功能 ---

@app.after_request
def log_request(response):
    try:
        # 保留知识库页面的 return_url 逻辑
        if request.path.startswith('/zhishi/view/') or request.path.startswith('/zhishi/edit/'):
            session['return_url'] = request.url
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
            cursor.execute('DELETE FROM action_logs')
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "message": "All logs cleared."})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    try:
        ip_query = request.args.get('ip', '').strip()
        action_query = request.args.get('action', '').strip()
        date_query = request.args.get('date', '').strip()
        
        conn = sqlite3.connect(VISITOR_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = "SELECT * FROM action_logs WHERE 1=1"
        params = []
        
        if ip_query and ip_query != 'all':
            query += " AND ip = ?"
            params.append(ip_query)
            
        if action_query and action_query != 'all':
            query += " AND action = ?"
            params.append(action_query)

        if date_query:
            query += " AND timestamp LIKE ?"
            params.append(f"{date_query}%")
             
        query += " ORDER BY id DESC LIMIT 500"
        
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        
        logs = [dict(row) for row in rows]
            
        conn.close()
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/visitor_logs/options')
def api_visitor_log_options():
    try:
        conn = sqlite3.connect(VISITOR_DB)
        cursor = conn.cursor()
        
        cursor.execute("SELECT DISTINCT ip FROM action_logs ORDER BY id DESC")
        ips = [row[0] for row in cursor.fetchall()]
        
        cursor.execute("SELECT DISTINCT action FROM action_logs ORDER BY action ASC")
        actions = [row[0] for row in cursor.fetchall()]
        
        conn.close()
        return jsonify({"ips": ips, "actions": actions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# --- 黄老师网站展示路由 ---
@app.route('/huang')
def huang_redirect():
    return redirect('/huang/')

@app.route('/huang/')
@app.route('/huang/<path:filename>')
def serve_huang_website(filename="index.html"):
    huang_dir = os.path.join(BASE_DIR, '..', 'huang')
    return send_from_directory(huang_dir, filename)

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

@app.route('/face')
def face_search_view():
    """独立专家人脸检索页，复用专家库现有的人脸搜索接口。"""
    response = make_response(render_template('face.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

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

# --- 归档辅助函数与索引 ---

def calculate_md5(file_path):
    """计算文件的 MD5 值"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])

class FileIndex:
    """管理归档文件的 MD5 索引 (双向映射)"""
    def __init__(self, index_file):
        self.index_file = index_file
        self.md5_to_name = {}
        self.name_to_md5 = {}
        self.load()

    def load(self):
        """加载索引，如果不存在或已损坏则重建"""
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.md5_to_name = data.get('md5_to_name', {})
                    # 重建反向索引
                    self.name_to_md5 = {v: k for k, v in self.md5_to_name.items()}
            except Exception as e:
                print(f"⚠️ Index load failed, rebuilding: {e}")
                self.rebuild()
        else:
            self.rebuild()

    def save(self):
        """保存索引到磁盘"""
        try:
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump({'md5_to_name': self.md5_to_name}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ Failed to save index: {e}")

    def rebuild(self):
        """扫描归档目录重建索引"""
        print("🔄 Rebuilding file index...")
        self.md5_to_name = {}
        self.name_to_md5 = {}
        
        if not os.path.exists(ARCHIVE_FOLDER):
            return

        try:
            with os.scandir(ARCHIVE_FOLDER) as it:
                for entry in it:
                    if entry.is_file() and not entry.name.endswith('.txt') and entry.name != 'file_index.json':
                        try:
                            md5 = calculate_md5(entry.path)
                            self.md5_to_name[md5] = entry.name
                            self.name_to_md5[entry.name] = md5
                        except Exception as e:
                            print(f"Error indexing {entry.name}: {e}")
            self.save()
            print(f"✅ Index rebuilt. Count: {len(self.md5_to_name)}")
        except Exception as e:
            print(f"❌ Index rebuild failed: {e}")

    def get_file_by_md5(self, md5):
        """根据 MD5 获取文件名 (如果文件实际存在)"""
        filename = self.md5_to_name.get(md5)
        if filename:
            filepath = os.path.join(ARCHIVE_FOLDER, filename)
            if os.path.exists(filepath):
                return filename
            else:
                # 索引过期 (文件被删)，清理之
                self.remove_file(filename)
        return None

    def get_md5_by_name(self, filename):
        """根据文件名获取 MD5"""
        return self.name_to_md5.get(filename)

    def add_file(self, md5, filename):
        """添加或更新文件映射"""
        self.md5_to_name[md5] = filename
        self.name_to_md5[filename] = md5
        self.save()

    def remove_file(self, filename):
        """移除文件映射"""
        md5 = self.name_to_md5.get(filename)
        if md5:
            del self.name_to_md5[filename]
            if md5 in self.md5_to_name:
                del self.md5_to_name[md5]
            self.save()

# 初始化全局索引
INDEX_FILE = os.path.join(ARCHIVE_FOLDER, 'file_index.json')
file_index = FileIndex(INDEX_FILE)

def cleanup_file_archive():
    """
    检查归档目录大小，如果超过 1GB，则清理旧文件直到小于 600MB
    同时清理对应的备注文件 (.txt) 和更新索引
    """
    try:
        total_size = 0
        file_list = []
        
        # 扫描所有文件
        with os.scandir(ARCHIVE_FOLDER) as it:
            for entry in it:
                if entry.is_file() and entry.name != 'file_index.json': # 跳过索引文件
                    if entry.name.endswith('.txt') and os.path.exists(os.path.join(ARCHIVE_FOLDER, entry.name[:-4])):
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
                        
                        # Update Index
                        file_index.remove_file(f['name'])
                        
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
    将临时文件归档到 ARCHIVE_FOLDER。
    逻辑:
    1. 计算 MD5。
    2. 如果 MD5 已存在于库中 -> 返回已有文件路径 (不保存副本)。
    3. 如果 MD5 是新的:
       - 检查 original_filename 是否冲突。
       - 如果冲突，自动重命名 (append size)。
       - 保存文件并更新索引。
    返回: 最终归档文件的绝对路径。
    """
    try:
        md5 = calculate_md5(source_path)
        
        # 1. 检查 MD5 是否已存在
        existing_filename = file_index.get_file_by_md5(md5)
        if existing_filename:
            # 只是因为多线程保险，再次检查物理文件是否存在
            existing_path = os.path.join(ARCHIVE_FOLDER, existing_filename)
            if os.path.exists(existing_path):
                # print(f"Duplicate file detected (MD5 match). Using existing: {existing_filename}")
                return existing_path
        
        # 2. 准备保存，处理文件名冲突
        target_filename = os.path.basename(original_filename)
        
        # 简单循环检查文件名是否存在
        if os.path.exists(os.path.join(ARCHIVE_FOLDER, target_filename)):
            base_name, ext = os.path.splitext(target_filename)
            size_bytes = os.path.getsize(source_path)
            size_str = format_size(size_bytes).replace(" ", "")
            
            # 尝试 1: 加大小后缀 (如 _10.2MB)
            candidate = f"{base_name}_{size_str}{ext}"
            
            # 尝试 2: 如果加大小后缀后仍冲突，则加时间戳
            if os.path.exists(os.path.join(ARCHIVE_FOLDER, candidate)):
                candidate = f"{base_name}_{size_str}_{int(time.time()*1000)}{ext}"
            
            target_filename = candidate
            
        target_path = os.path.join(ARCHIVE_FOLDER, target_filename)
        
        # 3. 复制文件
        shutil.copy2(source_path, target_path)
        
        # 4. 更新索引
        file_index.add_file(md5, target_filename)
        
        return target_path
    except Exception as e:
        print(f"Error archiving file {original_filename}: {e}")
        # 如果归档失败，为了不阻断比对流程，返回原临时路径? 
        # 用户要求优先确保文档保存成功。如果这里失败，应该报错。
        raise e

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
            # Update Index
            file_index.remove_file(filename)
            
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
                            
                            # Update Index
                            file_index.remove_file(entry.name)
                            
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



@app.route('/bijiao/file')
def file_list_view():
    files = []
    total_size = 0
    show_all = request.args.get('filter') == 'all'
    three_days_ago = time.time() - (3 * 24 * 3600)

    try:
        with os.scandir(ARCHIVE_FOLDER) as it:
            for entry in it:
                if entry.is_file():
                    if not entry.name.lower().endswith('.pdf'):
                        continue
                    
                    stat = entry.stat()
                    
                    # Filter logic: Default to recent 3 days unless show_all is true
                    if not show_all and stat.st_mtime < three_days_ago:
                        continue

                    total_size += stat.st_size
                    
                    # 获取 MD5 (如果没有则计算并更新)
                    md5 = file_index.get_md5_by_name(entry.name)
                    if not md5:
                         try:
                             md5 = calculate_md5(entry.path)
                             file_index.add_file(md5, entry.name)
                         except:
                             md5 = "Error"

                    files.append({
                        "name": entry.name,
                        "size": format_size(stat.st_size),
                        "time": datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                        "timestamp": stat.st_mtime,
                        "md5": md5
                    })
        # Sort by time descend
        files.sort(key=lambda x: x['timestamp'], reverse=True)
    except Exception as e:
        print(f"Error listing archive: {e}")
        
    return render_template('file_list.html', files=files, total_size=format_size(total_size), show_all=show_all)

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

# --- Experts Blueprint ---
from dashboard.blueprints.experts import experts_bp, init_db as init_experts_db
app.register_blueprint(experts_bp)
try:
    with app.app_context():
        init_experts_db()
    print("✅ Experts DB Initialized")
except Exception as e:
    print(f"⚠️ Experts DB Init Failed: {e}")

# --- Shared Recognition Records Blueprint ---
from dashboard.blueprints.shared_records import shared_records_bp, init_shared_records_db
app.register_blueprint(shared_records_bp)

# 工作台为独立模块；只注册路由，不在应用启动时创建任务进程或加载模型。
from dashboard.blueprints.evaluation_workbench import evaluation_workbench_bp
app.register_blueprint(evaluation_workbench_bp)
try:
    with app.app_context():
        init_shared_records_db()
    print("✅ Shared Records DB Initialized")
except Exception as e:
    print(f"⚠️ Shared Records DB Init Failed: {e}")

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

    submitted_files = [file_a, file_b]
    if file_tender and file_tender.filename:
        submitted_files.append(file_tender)
    invalid_files = [
        file_obj.filename
        for file_obj in submitted_files
        if os.path.splitext(file_obj.filename or "")[1].lower() != ".pdf"
    ]
    if invalid_files:
        return jsonify({"error": f"仅支持 PDF 文件: {', '.join(invalid_files)}"}), 400

    # 2. Save temporarily using UUID to avoid Chinese filename issues
    temp_paths = []
    try:
        def save_and_archive(file_obj):
            # Save to Temp
            ext = os.path.splitext(file_obj.filename)[1].lower()
            temp_filename = f"{uuid.uuid4()}{ext}"
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            file_obj.save(temp_path)
            temp_paths.append(temp_path)
            
            # --- Archive Immediately (Ensure Persistence) ---
            # Returns the absolute path of the archived file
            archived_path = archive_file(temp_path, file_obj.filename)
            # --------------------
            
            return temp_path, archived_path

        # 保存并归档 (优先确保文档保存成功)
        temp_a, archive_a = save_and_archive(file_a)
        temp_b, archive_b = save_and_archive(file_b)
        
        path_tender = None
        archive_tender = None
        if file_tender and file_tender.filename != '':
            path_tender, archive_tender = save_and_archive(file_tender)
            
        # 3. Validation: Check for duplicates
        # Compare actual archived paths. If they are the same path, it means MD5s were identical.
        if archive_a == archive_b:
            return jsonify({"error": "投标文件A与投标文件B内容重复 (MD5一致)"}), 400
            
        if archive_tender:
            if archive_a == archive_tender:
                 return jsonify({"error": "投标文件A与招标文件内容重复 (MD5一致)"}), 400
            if archive_b == archive_tender:
                 return jsonify({"error": "投标文件B与招标文件内容重复 (MD5一致)"}), 400
            
        # 4. Process archived files so repeated comparisons can reuse extraction cache.
        results = compare_documents(archive_a, archive_b, archive_tender,
                                     check_entity=request.form.get('check_entity') == '1',
                                     check_text=request.form.get('check_text') == '1',
                                     check_spelling=request.form.get('check_spelling') == '1')

        # 记录操作日志
        detail = f"{file_a.filename} vs {file_b.filename}"
        if file_tender and file_tender.filename:
            detail += f" (招标: {file_tender.filename})"
        log_user_action("文件比对", detail)
            
        return jsonify({"status": "success", "data": results})
        
    except ComparisonLimitError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"Error during comparison: {e}")
        return jsonify({"error": f"处理出错: {str(e)}"}), 500
    finally:
        for temp_path in temp_paths:
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

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
        log_user_action("查询开标记录", f"日期: {date_str}，共 {len(data)} 条")
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/scrape/auto_start', methods=['POST'])
def api_scrape_start():
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
            if dt < date.today() - timedelta(days=scraper.DAYS_AGO):
                return jsonify({
                    "status": "error",
                    "message": f"{d_str} 超出政府采购网近 {scraper.DAYS_AGO} 天查询范围"
                }), 400
            target_dates.append(dt)
    except ValueError:
        return jsonify({"status": "error", "message": "日期格式错误，应为 YYYY-MM-DD"}), 400

    started, message = start_scrape_task(target_dates)
    if not started:
        return jsonify({"status": "error", "message": message}), 409
    
    log_user_action("启动数据采集", f"目标日期: {', '.join(date_strs)}")
    
    return jsonify({
        "status": "success", 
        "message": f"已启动采集 {len(target_dates)} 个日期", 
        "target_dates": date_strs
    })

@app.route('/api/scrape/status')
def api_scrape_status():
    with SCRAPER_STATE_LOCK:
        return jsonify(copy.deepcopy(SCRAPER_STATUS))

# --- 定时任务配置 ---
class Config:
    SCHEDULER_API_ENABLED = True
    JOBS = [] 

app.config.from_object(Config())

scheduler = APScheduler()
scheduler.init_app(app)

def scheduled_job():
    """
    每天 07:00 执行的任务:
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
            valid, reason = scraper.validate_result_file(filepath)
            if valid:
                log_scheduler(f"   [跳过] {date_str_iso} 有效数据已存在。")
                continue
            log_scheduler(f"   [重采] {date_str_iso} 现有文件无效: {reason}")
            scrape_targets.append(target_date)
        else:
            log_scheduler(f"   [计划] {date_str_iso} 加入采集队列。")
            scrape_targets.append(target_date)
            
    if scrape_targets:
        started, message = start_scrape_task(scrape_targets, is_scheduled_task=True)
        if started:
            log_scheduler(f"   [启动] 已启动后台采集线程，目标: {len(scrape_targets)} 天")
        else:
            log_scheduler(f"   [跳过] {message}，本次定时任务取消采集。")
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

    # --- 3. 操作日志清理逻辑 (保留最近 7 天) ---
    try:
        cleanup_date_limit = (today - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        log_scheduler(f"   [日志维护] 正在清理 {cleanup_date_limit} 之前的操作记录...")
        
        conn = sqlite3.connect(VISITOR_DB)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM action_logs WHERE timestamp < ?", (cleanup_date_limit,))
        deleted_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        log_scheduler(f"   [日志维护] 清理完成，删除了 {deleted_rows} 条旧日志。")
    except Exception as e:
        log_scheduler(f"   [日志维护] 错误: {str(e)}")

# 添加定时任务并启动调度器
try:
    SCHEDULER_LOCK_HANDLE = acquire_process_lock(SCHEDULER_LOCK_FILE)
    if SCHEDULER_LOCK_HANDLE:
        scheduler.add_job(
            id='daily_task', func=scheduled_job, trigger='cron', hour=7, minute=0,
            max_instances=1, coalesce=True, misfire_grace_time=1800
        )
        scheduler.start()
        print("✅ 定时任务调度器已启动 (每天 07:00 执行)")
    else:
        print("ℹ️ 当前进程不是调度器主实例，跳过重复调度器启动")
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

# --- Chat API ---
@app.route('/api/chat/send', methods=['POST'])
def send_chat():
    data = request.get_json()
    content = data.get('content')
    uid = data.get('uid', 'anonymous')
    
    if not content:
        return jsonify({'status': 'error', 'message': 'No content'})
        
    ip = request.remote_addr
    
    try:
        conn = sqlite3.connect(CHAT_DB, timeout=10.0)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        
        # Auto-delete messages older than 24 hours (moved here from list API to avoid locks during polling)
        c.execute("DELETE FROM messages WHERE timestamp < datetime('now', '-24 hours')")
        
        c.execute("INSERT INTO messages (uid, ip, content) VALUES (?, ?, ?)", (uid, ip, content))
        conn.commit()
        conn.close()
        log_user_action("发送聊天消息", f"用户: {uid}")
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/chat/list', methods=['GET'])
def get_chat():
    last_id = request.args.get('last_id', 0, type=int)
    try:
        conn = sqlite3.connect(CHAT_DB, timeout=10.0)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        
        # Only reads are allowed in this high frequency polling endpoint
        # We explicitly filter out messages older than 24 hours here so they aren't shown even if they haven't been physically deleted yet
        c.execute("SELECT id, uid, content, timestamp FROM messages WHERE id > ? AND timestamp >= datetime('now', '-24 hours') ORDER BY id ASC LIMIT 50", (last_id,))
        rows = c.fetchall()
        messages = []
        for r in rows:
            messages.append({
                'id': r[0],
                'uid': r[1],
                'content': r[2],
                'timestamp': r[3]
            })
        conn.close()
        return jsonify({'status': 'success', 'messages': messages})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    try:
        conn = sqlite3.connect(CHAT_DB)
        c = conn.cursor()
        # Delete all messages
        c.execute("DELETE FROM messages")
        # Insert a system command to signal clearance to other clients
        # Using a special formatted content that frontend will recognize
        c.execute("INSERT INTO messages (uid, ip, content) VALUES (?, ?, ?)", 
                 ('system', '127.0.0.1', 'CMD:CLEAR_HISTORY'))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/chat/rename', methods=['POST'])
def rename_chat():
    data = request.get_json()
    old_uid = data.get('old_uid')
    new_uid = data.get('new_uid')
    
    if not old_uid or not new_uid:
        return jsonify({'status': 'error', 'message': 'Missing arguments'})
        
    ip = request.remote_addr
    
    try:
        conn = sqlite3.connect(CHAT_DB, timeout=10.0)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        
        # Retroactively apply new nickname to all past messages
        c.execute("UPDATE messages SET uid = ? WHERE uid = ?", (new_uid, old_uid))
        
        # Broadcast the change dynamically to live clients
        c.execute("INSERT INTO messages (uid, ip, content) VALUES (?, ?, ?)", 
                 ('system', ip, f"CMD:RENAME:{old_uid}:{new_uid}"))
        
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

