# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, jsonify, current_app, send_file
import sqlite3
import os
import zipfile
import tempfile
import pandas as pd
import shutil
import re
import datetime
import time
import json

# 定义 Blueprint
experts_bp = Blueprint('experts', __name__,
                       template_folder='../templates',
                       url_prefix='/dlsgzs')

DB_NAME = 'experts.db'

# --- 操作日志工具 ---
def _log_action(action, detail=""):
    """记录用户实质性操作到 visitor_logs.db"""
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(base_dir, '..', 'data')
        os.makedirs(data_dir, exist_ok=True)
        visitor_db = os.path.join(data_dir, 'visitor_logs.db')
        ip = request.remote_addr
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ua = request.user_agent
        ua_string = ua.string
        browser = f"{ua.browser} {ua.version}" if ua.browser else "Unknown"
        os_info = "Unknown"
        if 'Windows' in ua_string: os_info = 'Windows'
        elif 'Android' in ua_string: os_info = 'Android'
        elif 'iPhone' in ua_string or 'iPad' in ua_string: os_info = 'iOS'
        elif 'Mac' in ua_string: os_info = 'MacOS'
        elif 'Linux' in ua_string: os_info = 'Linux'
        device = "Mobile" if ('Mobile' in ua_string or 'Android' in ua_string or 'iPhone' in ua_string) else "PC"
        conn = sqlite3.connect(visitor_db)
        conn.execute('INSERT INTO action_logs (ip, action, detail, timestamp, user_agent, browser, os, device) VALUES (?,?,?,?,?,?,?,?)',
                     (ip, action, detail, timestamp, ua_string, browser, os_info, device))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Experts log_action error: {e}")

def get_db_path():
    """获取 SQLite 数据库物理路径"""
    try:
        base_dir = current_app.config.get('BASE_DIR')
    except RuntimeError:
        base_dir = None
    if not base_dir:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base_dir, '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, DB_NAME)

def get_photos_dir():
    """获取身份证照片存放的物理目录"""
    try:
        base_dir = current_app.config.get('BASE_DIR')
    except RuntimeError:
        base_dir = None
    if not base_dir:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    photos_dir = os.path.join(base_dir, 'static', 'uploads', 'expert_photos')
    os.makedirs(photos_dir, exist_ok=True)
    return photos_dir

def init_db():
    """初始化数据库表结构"""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    # 开启 WAL 模式以提升并发读写性能
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
    except Exception:
        pass
    c = conn.cursor()
    # 专家信息表
    c.execute('''
        CREATE TABLE IF NOT EXISTS experts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            id_card TEXT,
            company TEXT,
            major TEXT,
            photo_path TEXT,
            raw_json TEXT,
            status TEXT DEFAULT '未获取',
            remark TEXT DEFAULT '',
            created_at TEXT
        )
    ''')
    # 创建唯一索引以支撑高效查询与去重判断
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_experts_name_phone ON experts(name, phone)')
    # 创建普通索引以支撑按状态高效筛选与快速排序
    c.execute('CREATE INDEX IF NOT EXISTS idx_experts_status ON experts(status)')
    # 建立手机号与身份证号索引，加快高频精准/模糊匹配检索
    c.execute('CREATE INDEX IF NOT EXISTS idx_experts_phone ON experts(phone)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_experts_id_card ON experts(id_card)')
    
    # 平滑升级旧数据库：增加 status 字段与 remark 字段
    try:
        c.execute("ALTER TABLE experts ADD COLUMN status TEXT DEFAULT '未获取'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE experts ADD COLUMN remark TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
        
    # 初始化项目及参评专家关系数据库表
    c.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL UNIQUE,
            process_time TEXT,
            agent_name TEXT,
            agent_dept TEXT,
            project_name_en TEXT,
            project_code TEXT,
            project_id_str TEXT,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS project_experts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            expert_name TEXT NOT NULL,
            expert_id_card TEXT NOT NULL,
            expert_code TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    ''')
    # 建立高性能检索索引
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects(project_name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_project_experts_project_id ON project_experts(project_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_project_experts_id_card ON project_experts(expert_id_card)')
        
    # 平滑升级旧项目的 projects 表：增加新字段
    try:
        c.execute("ALTER TABLE projects ADD COLUMN project_name_en TEXT")
    except sqlite3.OperationalError:
        pass
        
    try:
        c.execute("ALTER TABLE projects ADD COLUMN project_code TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE projects ADD COLUMN project_id_str TEXT")
    except sqlite3.OperationalError:
        pass

    # 标签管理与高性能倒排检索相关表
    c.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_name TEXT UNIQUE NOT NULL,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS tag_majors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER,
            major_name TEXT NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS expert_majors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expert_id INTEGER,
            major_name TEXT NOT NULL
        )
    ''')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_name ON tags(tag_name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tag_majors_tag_id ON tag_majors(tag_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tag_majors_name ON tag_majors(major_name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_expert_majors_exp_id ON expert_majors(expert_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_expert_majors_name ON expert_majors(major_name)')

    conn.commit()

    # 一次性历史专家专业同步到倒排表
    try:
        c.execute("SELECT COUNT(*) FROM expert_majors")
        if c.fetchone()[0] == 0:
            c.execute("SELECT COUNT(*) FROM experts")
            if c.fetchone()[0] > 0:
                print("🔄 正在初始化升级：同步历史专家专业到高性能倒排表中...")
                c.execute("SELECT id, major FROM experts")
                all_experts = c.fetchall()
                for exp_id, major_str in all_experts:
                    if major_str:
                        majors = list(set([m.strip() for m in re.split(r'[,，]', major_str) if m.strip()]))
                        for m in majors:
                            c.execute("INSERT INTO expert_majors (expert_id, major_name) VALUES (?, ?)", (exp_id, m))
                conn.commit()
                print("✅ 历史专家专业同步初始化完成！")
    except Exception as e:
        print(f"⚠️ 历史专业同步初始化失败: {e}")

    conn.close()

def sync_expert_majors(conn, expert_id, major_str):
    """同步单个专家的专业到倒排表 (支持重写和更新)"""
    try:
        c = conn.cursor()
        c.execute("DELETE FROM expert_majors WHERE expert_id = ?", (expert_id,))
        if major_str:
            # 兼容中文逗号和英文逗号切分，并去重
            majors = list(set([m.strip() for m in re.split(r'[,，]', major_str) if m.strip()]))
            for m in majors:
                c.execute("INSERT INTO expert_majors (expert_id, major_name) VALUES (?, ?)", (expert_id, m))
    except Exception as e:
        print(f"Error syncing expert majors for ID {expert_id}: {e}")

def parse_and_import_md(file_path):
    """解析并导入 Markdown 格式的项目评审与专家关系表 (流式读取逐行处理，自适应新旧版表头，对内存极度友好)"""
    if not os.path.exists(file_path):
        return 0, 0, 0
    
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    project_added = 0
    project_updated = 0
    expert_relations_imported = 0
    
    try:
        # 第一步：先读取表头，以识别列名及其对应的索引
        header_indices = {}
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                if line_str.startswith('|') and line_str.endswith('|') and '项目名称' in line_str:
                    # 表头行
                    parts = [p.strip() for p in line_str.split('|')]
                    for i, part in enumerate(parts):
                        if not part:
                            continue
                        if '项目名称' in part:
                            header_indices['project_name'] = i
                        elif 'Project name' in part:
                            header_indices['project_name_en'] = i
                        elif 'Project code' in part or 'Project Code' in part:
                            header_indices['project_code'] = i
                        elif 'Project ID' in part or 'Project id' in part:
                            header_indices['project_id_str'] = i
                        elif '处理时间' in part:
                            header_indices['process_time'] = i
                        elif '经办人姓名' in part or ('经办人' in part and '部门' not in part):
                            header_indices['agent_name'] = i
                        elif '经办人部门' in part or '部门' in part:
                            header_indices['agent_dept'] = i
                        elif '评审专家' in part or '专家' in part:
                            header_indices['experts'] = i
                    break
        
        # 默认回退值（如果是旧版 MD 且没有在表头解析出来）
        if 'project_name' not in header_indices:
            header_indices['project_name'] = 1
        if 'process_time' not in header_indices:
            header_indices['process_time'] = 2
        if 'agent_name' not in header_indices:
            header_indices['agent_name'] = 3
        if 'agent_dept' not in header_indices:
            header_indices['agent_dept'] = 4
        if 'experts' not in header_indices:
            header_indices['experts'] = 5

        # 第二步：逐行读取数据行并解析
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                if not line_str.startswith('|') or not line_str.endswith('|'):
                    continue
                
                # 过滤表头和表格分割线
                if '项目名称' in line_str or ':---' in line_str:
                    continue
                
                parts = [p.strip() for p in line_str.split('|')]
                
                def get_val(key, default_val=None):
                    idx = header_indices.get(key)
                    if idx is not None and idx < len(parts):
                        val = parts[idx]
                        return val if val != '' else default_val
                    return default_val

                project_name = get_val('project_name', "")
                process_time = get_val('process_time', "")
                agent_name = get_val('agent_name', "")
                agent_dept = get_val('agent_dept', "")
                experts_cell = get_val('experts', "")
                
                # 新增的三个字段
                project_name_en = get_val('project_name_en')
                project_code = get_val('project_code')
                project_id_str = get_val('project_id_str')

                if not project_name:
                    continue
                
                # 插入或更新项目基本信息
                c.execute("SELECT id, project_name_en, project_code, project_id_str FROM projects WHERE project_name = ?", (project_name,))
                row_exist = c.fetchone()
                if row_exist:
                    project_id = row_exist[0]
                    # 如果上传的 MD 文件不包含新字段（旧版格式），则保留数据库中已有的这三个字段值，防止被覆盖为 NULL
                    final_name_en = project_name_en if 'project_name_en' in header_indices else row_exist[1]
                    final_code = project_code if 'project_code' in header_indices else row_exist[2]
                    final_id_str = project_id_str if 'project_id_str' in header_indices else row_exist[3]
                    
                    c.execute('''
                        UPDATE projects 
                        SET process_time = ?, agent_name = ?, agent_dept = ?, 
                            project_name_en = ?, project_code = ?, project_id_str = ?, created_at = ?
                        WHERE id = ?
                    ''', (process_time, agent_name, agent_dept, final_name_en, final_code, final_id_str, now_time, project_id))
                    # 防重覆盖机制：清空旧参评专家关联
                    c.execute("DELETE FROM project_experts WHERE project_id = ?", (project_id,))
                    project_updated += 1
                else:
                    c.execute('''
                        INSERT INTO projects (project_name, process_time, agent_name, agent_dept, 
                                             project_name_en, project_code, project_id_str, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                      ''', (project_name, process_time, agent_name, agent_dept, project_name_en, project_code, project_id_str, now_time))
                    project_id = c.lastrowid
                    project_added += 1
                
                # 解析评审专家列表
                expert_items = re.split(r'<br\s*/?>', experts_cell, flags=re.IGNORECASE)
                for item in expert_items:
                    item = item.strip()
                    if not item:
                        continue
                    
                    exp_parts = [e.strip() for e in item.split('/')]
                    if not exp_parts or not exp_parts[0]:
                        continue
                    
                    exp_name = exp_parts[0]
                    exp_id_card = exp_parts[1] if len(exp_parts) > 1 else ""
                    exp_code = exp_parts[2] if len(exp_parts) > 2 else ""
                    
                    # 写入项目与专家关系
                    c.execute('''
                        INSERT INTO project_experts (project_id, expert_name, expert_id_card, expert_code)
                        VALUES (?, ?, ?, ?)
                    ''', (project_id, exp_name, exp_id_card, exp_code))
                    expert_relations_imported += 1
                    
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
        
    return project_added, project_updated, expert_relations_imported

@experts_bp.route('/')
def experts_view():
    """评标专家管理主页（集成所有功能）"""
    _log_action("访问评标专家管理系统", "访问主页")
    from flask import make_response
    response = make_response(render_template('experts.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@experts_bp.route('/api/upload', methods=['POST'])
def api_upload():
    """上传并流式解析专家压缩包"""
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "未选择任何文件"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "文件名不能为空"}), 400
        
    if not file.filename.endswith('.zip'):
        return jsonify({"success": False, "error": "只支持上传 .zip 格式的压缩包"}), 400

    # 获取临时目录，流式解压
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, 'upload.zip')
        file.save(zip_path)
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
        except Exception as e:
            return jsonify({"success": False, "error": f"解压压缩包失败: {str(e)}"}), 400

        # 扫描解压目录，定位 Excel 表格、MD 项目关系与身份证照片
        excel_file = None
        md_file = None
        photos = {} # 格式：{ "姓名_手机号": "图片绝对路径" }
        
        for root_dir, dirs, files in os.walk(temp_dir):
            # 排除 Mac OS 特定的资源目录，防止里面同名的非图像垃圾元数据覆盖正常照片
            if '__macosx' in root_dir.lower():
                continue
            for f in files:
                if f.startswith('._') or f.lower() == 'thumbs.db': # 忽略 Mac OS 的临时隐藏文件与 Windows 缩略图缓存
                    continue
                ext = os.path.splitext(f)[1].lower()
                full_path = os.path.join(root_dir, f)
                if ext in ('.xls', '.xlsx'):
                    excel_file = full_path
                elif ext == '.md':
                    md_file = full_path
                elif ext in ('.jpg', '.jpeg', '.png'):
                    # 转换为统一的“姓名_手机号”作为 Key，去除空格和后缀
                    base_name = os.path.splitext(f)[0].strip().replace(" ", "")
                    photos[base_name.lower()] = full_path

        if not excel_file:
            return jsonify({"success": False, "error": "压缩包内未找到专家 Excel 列表文件 (.xls 或 .xlsx)"}), 400

        # 解析 Excel 表格，兼容偽装成 xls 的 HTML 表格
        df = None
        try:
            # 优先尝试读取 HTML 格式（针对伪装成 xls 的 HTML 文件）
            dfs = pd.read_html(excel_file)
            if dfs:
                df = dfs[0]
            else:
                raise ValueError("HTML 文件中未解析出表格数据")
        except Exception:
            try:
                # 备用方法：使用常规的 Excel 引擎加载
                df = pd.read_excel(excel_file)
            except Exception as ex:
                return jsonify({"success": False, "error": f"表格文件解析失败，请检查文件格式是否正确。错误: {str(ex)}"}), 400

        # 自适应匹配列名
        columns = df.columns.tolist()
        col_mapping = {}
        for col in columns:
            col_str = str(col).strip()
            if '姓名' in col_str or col_str.lower() == 'name':
                col_mapping['name'] = col
            elif '单位' in col_str or 'company' in col_str.lower() or 'organization' in col_str.lower():
                col_mapping['company'] = col
            elif '电话' in col_str or '手机' in col_str or 'phone' in col_str.lower() or 'telephone' in col_str.lower():
                col_mapping['phone'] = col
            elif '身份证' in col_str or 'id_card' in col_str.lower() or 'idcard' in col_str.lower():
                col_mapping['id_card'] = col
            elif '专业' in col_str or 'major' in col_str.lower():
                col_mapping['major'] = col
            elif 'json' in col_str.lower() or '原始' in col_str:
                col_mapping['raw_json'] = col

        # 检查核心列是否存在
        if 'name' not in col_mapping or 'phone' not in col_mapping:
            return jsonify({"success": False, "error": "表格中必须包含“姓名”和“电话”两列，解析失败"}), 400

        # 写入数据库与照片复制
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        imported_count = 0
        matched_photo_count = 0
        total_in_file = 0
        added_count = 0
        updated_count = 0
        photos_dest_dir = get_photos_dir()
        
        # 记录本次操作的时间
        now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for _, row in df.iterrows():
            name = str(row.get(col_mapping.get('name', ''), '')).strip()
            phone = str(row.get(col_mapping.get('phone', ''), '')).strip()
            
            # 清理电话格式（去除非数字，去掉浮点数表示如 .0）
            if phone.endswith('.0'):
                phone = phone[:-2]
            phone = re.sub(r'\D', '', phone)
            
            if not name or not phone:
                continue
                
            total_in_file += 1
                
            company = str(row.get(col_mapping.get('company', ''), '')).strip()
            id_card = str(row.get(col_mapping.get('id_card', ''), '')).strip()
            if id_card.endswith('.0'):
                id_card = id_card[:-2]
                
            major = str(row.get(col_mapping.get('major', ''), '')).strip()
            raw_json = str(row.get(col_mapping.get('raw_json', ''), '')).strip()

            # 照片关联与去重覆盖逻辑
            # 照片可能命名为 "{姓名}_{手机号}.jpg"
            photo_key = f"{name}_{phone}".lower()
            photo_path_db = None
            
            # 搜集所有符合条件的图片
            matched_photos = []
            
            # 1. 查找精确匹配
            if photo_key in photos:
                matched_photos.append(photos[photo_key])
            
            # 2. 查找模糊匹配（含有姓名和电话）
            for k, path in photos.items():
                if name.lower() in k and phone in k:
                    if path not in matched_photos:
                        matched_photos.append(path)
            
            if matched_photos:
                # 排序机制：优先展示文件名（不含扩展名）以 `_1` 或 `-1` 结尾的照片，其他按序号递增
                def get_photo_sort_key(path_str):
                    fname = os.path.splitext(os.path.basename(path_str))[0].strip()
                    # 匹配最后的 _数字 或者是 -数字（限制1-3位长度以避免误匹配手机号）
                    match = re.search(r'[-_](\d{1,3})$', fname)
                    if match:
                        num = int(match.group(1))
                        if num == 1:
                            return (0, 0) # 优先级最高
                        else:
                            return (2, num) # 排在无序号之后，按数字升序
                    else:
                        return (1, 0) # 无序号后缀的排在第二位
                
                matched_photos.sort(key=get_photo_sort_key)
                
                # 复制所有匹配的图片到静态资源目录
                copied_paths = []
                for p_file in matched_photos:
                    ext = os.path.splitext(p_file)[1].lower()
                    base_fname = os.path.splitext(os.path.basename(p_file))[0].strip().replace(" ", "").lower()
                    dest_filename = f"{base_fname}{ext}"
                    dest_path = os.path.join(photos_dest_dir, dest_filename)
                    # 采用事务型原子性覆盖写入，确保物理文件名大小写与数据库中存储的一致（防止在Linux等大小写敏感系统上发生404图片无法访问的问题）
                    # 同时也规避了写入时损坏/丢失原照片的风险。如果覆盖过程中出现任何错误，将自动回滚还原原照片，确保原有照片能够继续正常查看。
                    backup_path = dest_path + ".bak"
                    temp_write_path = dest_path + ".tmp"
                    has_backup = False
                    
                    try:
                        # 1. 如果旧文件存在，先做备份重命名，不直接删除
                        if os.path.exists(dest_path):
                            if os.path.exists(backup_path):
                                try:
                                    os.remove(backup_path)
                                except Exception:
                                    pass
                            try:
                                os.rename(dest_path, backup_path)
                                has_backup = True
                            except Exception:
                                # 如果 rename 失败（如文件锁定），不进行移动，后面直接写 temp 并尝试覆盖
                                pass
                        
                        # 2. 写入新图片内容到临时文件
                        with open(p_file, 'rb') as f_src:
                            img_content = f_src.read()
                        with open(temp_write_path, 'wb') as f_dest:
                            f_dest.write(img_content)
                            f_dest.flush()
                            try:
                                os.fsync(f_dest.fileno())
                            except Exception:
                                pass
                        
                        # 3. 将临时文件重命名为目标照片文件
                        if os.path.exists(dest_path) and not has_backup:
                            # 如果之前重命名备份失败了，这里尝试删除旧照片
                            try:
                                os.remove(dest_path)
                            except Exception:
                                pass
                        
                        os.rename(temp_write_path, dest_path)
                        
                        # 4. 成功后清理备份文件
                        if has_backup and os.path.exists(backup_path):
                            try:
                                os.remove(backup_path)
                            except Exception:
                                pass
                    except Exception as e:
                        # 发生异常，进行防丢失灾难恢复
                        # 清理临时文件
                        if os.path.exists(temp_write_path):
                            try:
                                os.remove(temp_write_path)
                            except Exception:
                                pass
                        
                        # 如果有备份，将备份还原为目标文件
                        if has_backup and os.path.exists(backup_path):
                            try:
                                if os.path.exists(dest_path):
                                    os.remove(dest_path)
                            except Exception:
                                pass
                            try:
                                os.rename(backup_path, dest_path)
                            except Exception:
                                try:
                                    shutil.copy2(backup_path, dest_path)
                                    os.remove(backup_path)
                                except Exception:
                                    pass
                        
                        # 兜底：如果 dest_path 确实不复存在，但备份还挂在旁边，强制把它恢复回去
                        if not os.path.exists(dest_path) and os.path.exists(backup_path):
                            try:
                                shutil.copy2(backup_path, dest_path)
                            except Exception:
                                pass
                        
                        # 最终兜底：如果新文件写入失败，且还原旧文件也因极其诡异的错误失败，而 dest_path 还是缺失，
                        # 则尝试最后一次使用原始 shutil.copy2 写入
                        if not os.path.exists(dest_path):
                            try:
                                shutil.copy2(p_file, dest_path)
                            except Exception:
                                pass
                    copied_paths.append(f"/static/uploads/expert_photos/{dest_filename}")
                    matched_photo_count += 1
                
                photo_path_db = ",".join(copied_paths)
            else:
                # 若本次压缩包内未包含该专家的图片，则保持数据库已存在的旧图片路径，不抹除
                c.execute("SELECT photo_path FROM experts WHERE name = ? AND phone = ?", (name, phone))
                row_exist = c.fetchone()
                if row_exist:
                    photo_path_db = row_exist[0]

            # 追加与去重覆盖：使用传统 SELECT 判断在 SQLite 下最兼容安全
            c.execute("SELECT id, id_card, company, major, photo_path, raw_json FROM experts WHERE name = ? AND phone = ?", (name, phone))
            exist_record = c.fetchone()
            
            if exist_record:
                # 已经存在，执行更新操作
                updated_count += 1
                
                db_id = exist_record[0]
                db_id_card = exist_record[1]
                db_company = exist_record[2]
                db_major = exist_record[3]
                db_photo_path = exist_record[4]
                db_raw_json = exist_record[5]
                
                # 只有当新解析的字段非空时，才更新覆盖；若新解析为空（例如 Excel 中该字段为空或根本没有该列），则保留原数据库字段
                final_id_card = id_card if id_card else db_id_card
                final_company = company if company else db_company
                final_major = major if major else db_major
                final_photo_path = photo_path_db if photo_path_db else db_photo_path
                final_raw_json = raw_json if raw_json else db_raw_json
                
                c.execute('''
                    UPDATE experts 
                    SET id_card = ?, company = ?, major = ?, photo_path = ?, raw_json = ?, created_at = ?
                    WHERE id = ?
                ''', (final_id_card, final_company, final_major, final_photo_path, final_raw_json, now_time, db_id))
                sync_expert_majors(conn, db_id, final_major)
            else:
                # 不存在，执行插入（追加）操作
                added_count += 1
                c.execute('''
                    INSERT INTO experts (name, phone, id_card, company, major, photo_path, raw_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, phone, id_card, company, major, photo_path_db, raw_json, now_time))
                new_id = c.lastrowid
                sync_expert_majors(conn, new_id, major)
                
            imported_count += 1

        conn.commit()
        conn.close()
        
        md_message = ""
        if md_file:
            try:
                proj_added, proj_updated, rel_count = parse_and_import_md(md_file)
                proj_total = proj_added + proj_updated
                md_message = f" 另外检测到并成功导入项目关系文件，共解析导入项目 {proj_total} 个（其中新增 {proj_added} 个，重复覆盖更新 {proj_updated} 个），共关联参评专家 {rel_count} 人次。"
            except Exception as e:
                md_message = f" 但项目关系 MD 解析失败，错误: {str(e)}。"
        
        _log_action("导入专家压缩包", f"解压出 {total_in_file} 位专家，新增 {added_count} 人，覆盖 {updated_count} 人。{md_message.strip()}")
        return jsonify({
            "success": True,
            "message": f"成功导入！文件中共解析出 {total_in_file} 位专家，其中实际新增上传 {added_count} 人，重复覆盖更新 {updated_count} 人，成功匹配并保存身份证照 {matched_photo_count} 张。{md_message}"
        })

@experts_bp.route('/api/upload_md', methods=['POST'])
def api_upload_md():
    """单独上传并解析项目评审关系 MD 文件 (性能友好，内存低消耗)"""
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "未选择任何文件"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "文件名不能为空"}), 400
        
    if not file.filename.endswith('.md'):
        return jsonify({"success": False, "error": "只支持上传 .md 格式的 Markdown 文件"}), 400

    # 使用临时文件保存并解析
    with tempfile.TemporaryDirectory() as temp_dir:
        md_path = os.path.join(temp_dir, 'temp_project.md')
        file.save(md_path)
        
        try:
            proj_added, proj_updated, rel_count = parse_and_import_md(md_path)
            proj_total = proj_added + proj_updated
            _log_action("导入项目关系MD", f"导入项目 {proj_total} 个（新增 {proj_added} 个，覆盖 {proj_updated} 个），关联参评专家 {rel_count} 人次。")
            return jsonify({
                "success": True,
                "message": f"项目评审关系导入成功！共解析导入项目 {proj_total} 个（其中新增 {proj_added} 个，重复覆盖更新 {proj_updated} 个），关联参评专家 {rel_count} 人次。"
            })
        except Exception as e:
            return jsonify({"success": False, "error": f"解析项目关系 MD 文件失败: {str(e)}"}), 500

@experts_bp.route('/api/search_projects', methods=['GET'])
def api_search_projects():
    """查询项目评审关系，支持项目名、参评专家姓名、身份证多条件模糊检索 (支持高性能分页)"""
    project_name = request.args.get('project_name', '').strip()
    expert_name = request.args.get('expert_name', '').strip()
    expert_id_card = request.args.get('expert_id_card', '').strip()
    min_year = request.args.get('min_year', '').strip()
    
    # 提取分页参数
    try:
        page = int(request.args.get('page', '1'))
        if page < 1:
            page = 1
    except ValueError:
        page = 1
        
    try:
        limit = int(request.args.get('limit', '20'))
        if limit < 1:
            limit = 20
    except ValueError:
        limit = 20
        
    offset = (page - 1) * limit
    
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    conditions = []
    params = []
    
    if project_name:
        conditions.append("p.project_name LIKE ?")
        params.append(f"%{project_name}%")
        
    if expert_name:
        conditions.append("pe.expert_name LIKE ?")
        params.append(f"%{expert_name}%")
        
    if expert_id_card:
        conditions.append("pe.expert_id_card LIKE ?")
        params.append(f"%{expert_id_card}%")
        
    if min_year:
        if len(min_year) == 4:
            process_time_limit = f"{min_year}-01-01 00:00:00"
        else:
            process_time_limit = f"{min_year} 00:00:00"
        conditions.append("p.process_time >= ?")
        params.append(process_time_limit)
        
    # 1. 检索符合条件的 DISTINCT 项目总数
    count_sql = """
        SELECT COUNT(DISTINCT p.id)
        FROM projects p
        LEFT JOIN project_experts pe ON p.id = pe.project_id
    """
    if conditions:
        count_sql += " WHERE " + " AND ".join(conditions)
        
    try:
        c.execute(count_sql, params)
        total = c.fetchone()[0]
        
        # 2. 精准分页拉取项目详情
        sql = """
            SELECT DISTINCT p.id, p.project_name, p.process_time, p.agent_name, p.agent_dept, p.created_at,
                            p.project_name_en, p.project_code, p.project_id_str
            FROM projects p
            LEFT JOIN project_experts pe ON p.id = pe.project_id
        """
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY p.process_time DESC, p.id DESC LIMIT ? OFFSET ?"
        
        query_params = list(params)
        query_params.extend([limit, offset])
        
        c.execute(sql, query_params)
        project_rows = c.fetchall()
        
        results = []
        for p_row in project_rows:
            p_id = p_row['id']
            # 查询该项目下的全部专家
            c.execute("""
                SELECT expert_name, expert_id_card, expert_code 
                FROM project_experts 
                WHERE project_id = ?
            """, (p_id,))
            expert_rows = c.fetchall()
            
            experts_list = []
            for e_row in expert_rows:
                experts_list.append({
                    "name": e_row['expert_name'],
                    "id_card": e_row['expert_id_card'],
                    "code": e_row['expert_code']
                })
                
            results.append({
                "id": p_id,
                "project_name": p_row['project_name'],
                "process_time": p_row['process_time'],
                "agent_name": p_row['agent_name'],
                "agent_dept": p_row['agent_dept'],
                "created_at": p_row['created_at'],
                "project_name_en": p_row['project_name_en'] or "",
                "project_code": p_row['project_code'] or "",
                "project_id_str": p_row['project_id_str'] or "",
                "experts": experts_list
            })
            
        _log_action("检索项目评审关系", f"条件: 项目名={project_name}, 专家={expert_name}, 身份证={expert_id_card}, 最低处理时间={min_year}，结果共 {total} 条")
        return jsonify({
            "success": True, 
            "data": results,
            "total": total,
            "page": page,
            "limit": limit
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"检索项目失败: {str(e)}"}), 500
    finally:
        conn.close()

@experts_bp.route('/api/detail_by_idcard', methods=['GET'])
def api_detail_by_idcard():
    """根据专家身份证号，查询其在主专家库中的基础匹配数据（用于前端弹窗联动）"""
    id_card = request.args.get('id_card', '').strip()
    if not id_card:
        return jsonify({"success": False, "error": "身份证号不能为空"}), 400
        
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    try:
        c.execute("SELECT name, phone FROM experts WHERE id_card = ?", (id_card,))
        row = c.fetchone()
        
        if row:
            return jsonify({
                "success": True,
                "found": True,
                "name": row[0],
                "phone": row[1]
            })
        else:
            return jsonify({
                "success": True,
                "found": False,
                "message": "该专家暂未录入主专家库，无法查看完整档案。"
            })
    except Exception as e:
        return jsonify({"success": False, "error": f"关联查询专家失败: {str(e)}"}), 500
    finally:
        conn.close()

@experts_bp.route('/api/search', methods=['GET'])
def api_search():
    """查询专家信息 (优化版本：剥离大字段 raw_json 加速，并支持 4 条件独立模糊检索)"""
    q = request.args.get('q', '').strip()
    name = request.args.get('name', '').strip()
    phone = request.args.get('phone', '').strip()
    id_card = request.args.get('id_card', '').strip()
    company = request.args.get('company', '').strip()
    major = request.args.get('major', '').strip()
    status = request.args.get('status', '').strip()
    
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    conditions = []
    params = []
    
    # 兼容老版全局搜索
    if q:
        conditions.append("(name LIKE ? OR phone LIKE ? OR id_card LIKE ? OR major LIKE ? OR company LIKE ?)")
        query_str = f"%{q}%"
        params.extend([query_str, query_str, query_str, query_str, query_str])
        
    # 新版四条件独立模糊搜索
    if name:
        # 将输入按半角/全角空格拆分成多个关键词，支持如输入 "孟 霞" 模糊匹配 "孟艳霞"
        name_parts = [p.strip() for p in re.split(r'[\s　]+', name) if p.strip()]
        if name_parts:
            name_conds = []
            for part in name_parts:
                name_conds.append("name LIKE ?")
                params.append(f"%{part}%")
            conditions.append(f"({' AND '.join(name_conds)})")
        
    if phone:
        cleaned_phone = phone.replace(" ", "")
        conditions.append("phone LIKE ?")
        params.append(f"%{cleaned_phone}%")
        
    if id_card:
        cleaned_id = id_card.replace(" ", "")
        conditions.append("id_card LIKE ?")
        params.append(f"%{cleaned_id}%")
        
    if company:
        cleaned_company = company.replace(" ", "")
        conditions.append("company LIKE ?")
        params.append(f"%{cleaned_company}%")
        
    if major:
        cleaned_major = major.replace(" ", "")
        conditions.append("major LIKE ?")
        params.append(f"%{cleaned_major}%")
        
    if status:
        conditions.append("status = ?")
        params.append(status)
        
    tag_ids_str = request.args.get('tag_ids', '').strip()
    if tag_ids_str:
        tag_id_list = [t.strip() for t in tag_ids_str.split(',') if t.strip()]
        digit_ids = [t for t in tag_id_list if t.isdigit()]
        has_unassigned = 'unassigned' in tag_id_list
        
        sub_conds = []
        if digit_ids:
            placeholders = ",".join(["?"] * len(digit_ids))
            sub_conds.append(f"id IN (SELECT DISTINCT em.expert_id FROM expert_majors em JOIN tag_majors tm ON em.major_name = tm.major_name WHERE tm.tag_id IN ({placeholders}))")
            params.extend([int(tid) for tid in digit_ids])
            
        if has_unassigned:
            sub_conds.append("id NOT IN (SELECT DISTINCT em.expert_id FROM expert_majors em JOIN tag_majors tm ON em.major_name = tm.major_name)")
            
        if sub_conds:
            conditions.append(f"({' OR '.join(sub_conds)})")
        
    # 精炼 SQL：不 SELECT raw_json 大文本字段，并通过子查询获取每个专家所对应的标签字符串
    sql = """
        SELECT id, name, phone, id_card, company, major, photo_path, status, remark,
               (
                   SELECT GROUP_CONCAT(t.tag_name, ',')
                   FROM expert_majors em
                   JOIN tag_majors tm ON em.major_name = tm.major_name
                   JOIN tags t ON tm.tag_id = t.id
                   WHERE em.expert_id = experts.id
               ) as tags_str
        FROM experts
    """
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC"
    
    c.execute(sql, params)
    rows = c.fetchall()
    conn.close()
    
    results = []
    for r in rows:
        item_status = r['status'] if r.keys() and 'status' in r.keys() and r['status'] else '未获取'
        item_remark = r['remark'] if r.keys() and 'remark' in r.keys() and r['remark'] else ''
        
        tags_list = []
        if r.keys() and 'tags_str' in r.keys() and r['tags_str']:
            tags_list = list(set([t.strip() for t in r['tags_str'].split(',') if t.strip()]))
                
        results.append({
            "name": r['name'],
            "phone": r['phone'],
            "id_card": r['id_card'],
            "company": r['company'],
            "major": r['major'],
            "photo_path": r['photo_path'],
            "status": item_status,
            "remark": item_remark,
            "tags": tags_list,
            "details": {}  # 列表阶段置空，点击“查看完整档案”时通过 api_detail 懒加载
        })
        
    _log_action("检索评标专家", f"条件: 姓名={name}, 手机={phone}, 身份证={id_card}, 单位={company}, 专业={major}, 状态={status}，共 {len(results)} 条")
    return jsonify({"success": True, "data": results})

@experts_bp.route('/api/detail', methods=['GET'])
def api_detail():
    """点对点懒加载查询单个专家原始 JSON 详情与标签 (毫秒级响应)"""
    name = request.args.get('name', '').strip()
    phone = request.args.get('phone', '').strip()
    if not name or not phone:
        return jsonify({"success": False, "error": "姓名和手机号不能为空"}), 400
        
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 1. 命中联合唯一索引 idx_experts_name_phone，获取详情 json
        c.execute("SELECT id, raw_json FROM experts WHERE name = ? AND phone = ?", (name, phone))
        row = c.fetchone()
        
        parsed_json = {}
        tags = []
        if row:
            expert_id = row[0]
            if row[1]:
                try:
                    parsed_json = json.loads(row[1])
                except Exception:
                    pass
            
            # 2. 查询该专家匹配到的所有标签
            c.execute("""
                SELECT DISTINCT t.tag_name
                FROM expert_majors em
                JOIN tag_majors tm ON em.major_name = tm.major_name
                JOIN tags t ON tm.tag_id = t.id
                WHERE em.expert_id = ?
            """, (expert_id,))
            tag_rows = c.fetchall()
            tags = [tr[0] for tr in tag_rows if tr[0]]
            
        conn.close()
        
        _log_action("查看专家详情", f"姓名: {name}, 电话: {phone}")
        return jsonify({"success": True, "details": parsed_json, "tags": tags})
    except Exception as e:
        return jsonify({"success": False, "error": f"查询专家详情失败: {str(e)}"}), 500

@experts_bp.route('/api/backup', methods=['GET'])
def api_backup():
    """将 SQLite 数据库与所有专家照片打包为 zip 下载备份 (针对 2核2G 物理防暴)"""
    base_dir = current_app.config.get('BASE_DIR')
    if not base_dir:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
    # 建立备份存放目录
    backups_dir = os.path.join(base_dir, 'uploads', 'backups')
    os.makedirs(backups_dir, exist_ok=True)
    
    # 清理 5 分钟前产生的老旧备份，保护磁盘不被撑爆
    now = time.time()
    for f in os.listdir(backups_dir):
        fp = os.path.join(backups_dir, f)
        if os.path.isfile(fp) and now - os.path.getmtime(fp) > 300:
            try:
                os.remove(fp)
            except Exception:
                pass

    # 生成备份压缩包
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"experts_backup_{timestamp}.zip"
    zip_path = os.path.join(backups_dir, zip_filename)
    
    db_path = get_db_path()
    photos_dir = get_photos_dir()
    
    try:
        # 流式写入，对 2G 内存友好
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 1. 写入数据库文件
            if os.path.exists(db_path):
                zipf.write(db_path, "experts.db")
            # 2. 递归写入照片目录下的所有身份证照
            if os.path.exists(photos_dir):
                for root_dir, dirs, files in os.walk(photos_dir):
                    for file in files:
                        file_path = os.path.join(root_dir, file)
                        arcname = os.path.join("expert_photos", file)
                        zipf.write(file_path, arcname)
                        
        _log_action("备份评标专家库", f"文件名: {zip_filename}")
        return send_file(zip_path, as_attachment=True, download_name=zip_filename)
    except Exception as e:
        return jsonify({"success": False, "error": f"生成备份压缩包失败: {str(e)}"}), 500

@experts_bp.route('/api/clear', methods=['POST'])
def api_clear():
    """清空专家库的所有数据与身份证图片"""
    # 安全校验：清空专家库必须输入密码 108
    try:
        data = request.json or {}
        password = data.get('password', '')
    except Exception:
        password = ''
        
    if password != '108':
        return jsonify({"success": False, "error": "安全校验失败：清空密码错误或无权限"}), 403

    # 1. 清空数据库表
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('DELETE FROM experts')
        c.execute('DELETE FROM expert_majors')
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"success": False, "error": f"清空数据库失败: {str(e)}"}), 500
        
    # 2. 删除存储的照片文件
    photos_dir = get_photos_dir()
    if os.path.exists(photos_dir):
        for f in os.listdir(photos_dir):
            fp = os.path.join(photos_dir, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
                    
    _log_action("清空专家数据库", "清空了全部专家数据和照片")
    return jsonify({"success": True, "message": "评标专家数据库与所有照片已成功清空。"})

@experts_bp.route('/api/delete', methods=['POST'])
def api_delete():
    """删除单个专家并删除物理图片"""
    data = request.json or {}
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    
    if not name or not phone:
        return jsonify({"success": False, "error": "姓名和手机号不能为空"}), 400
        
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 查找图片路径并从磁盘删除
        c.execute("SELECT photo_path FROM experts WHERE name = ? AND phone = ?", (name, phone))
        row = c.fetchone()
        if row and row[0]:
            photo_paths = row[0].split(',')
            photos_dir = get_photos_dir()
            for p_path in photo_paths:
                p_path = p_path.strip()
                if p_path:
                    filename = os.path.basename(p_path)
                    physical_photo_path = os.path.join(photos_dir, filename)
                    if os.path.exists(physical_photo_path):
                        try:
                            os.remove(physical_photo_path)
                        except Exception:
                            pass
        
        # 从倒排关系表中物理删除
        c.execute("DELETE FROM expert_majors WHERE expert_id IN (SELECT id FROM experts WHERE name = ? AND phone = ?)", (name, phone))
        # 从数据库中物理删除
        c.execute("DELETE FROM experts WHERE name = ? AND phone = ?", (name, phone))
        conn.commit()
        conn.close()
        
        _log_action("删除评标专家", f"专家姓名: {name}, 电话: {phone}")
        return jsonify({"success": True, "message": f"专家 {name} 及其身份证照已删除。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"删除专家失败: {str(e)}"}), 500


@experts_bp.route('/api/update_status', methods=['POST'])
def api_update_status():
    """更新单个专家的标记状态与备注"""
    data = request.json or {}
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    status = data.get('status', '').strip()
    remark = data.get('remark', '')
    
    if not name or not phone or not status:
        return jsonify({"success": False, "error": "姓名、手机号和状态不能为空"}), 400
        
    if status not in ['已获取', '无法登录', '未获取']:
        return jsonify({"success": False, "error": "无效的标记状态，必须为：已获取、无法登录、未获取之一"}), 400
        
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("UPDATE experts SET status = ?, remark = ? WHERE name = ? AND phone = ?", (status, remark, name, phone))
        conn.commit()
        conn.close()
        _log_action("更新专家状态", f"专家姓名: {name}, 电话: {phone}, 新状态: {status}, 备注: {remark}")
        return jsonify({"success": True, "message": f"专家 {name} 的状态与备注已成功更新。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"更新专家状态/备注失败: {str(e)}"}), 500


@experts_bp.route('/api/tags', methods=['GET', 'POST'])
def api_tags():
    db_path = get_db_path()
    if request.method == 'GET':
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            # 1. 查出所有标签
            c.execute("SELECT id, tag_name, created_at FROM tags ORDER BY id DESC")
            tags_rows = c.fetchall()
            
            tags_list = []
            for t in tags_rows:
                t_id = t['id']
                # 2. 查出每个标签关联的小专业
                c.execute("SELECT major_name FROM tag_majors WHERE tag_id = ?", (t_id,))
                majors = [row['major_name'] for row in c.fetchall()]
                tags_list.append({
                    "id": t_id,
                    "tag_name": t['tag_name'],
                    "majors": majors,
                    "created_at": t['created_at']
                })
            
            conn.close()
            return jsonify({"success": True, "tags": tags_list})
        except Exception as e:
            return jsonify({"success": False, "error": f"获取标签列表失败: {str(e)}"}), 500
            
    elif request.method == 'POST':
        data = request.json or {}
        tag_name = data.get('tag_name', '').strip()
        majors = data.get('majors', [])
        tag_id = data.get('tag_id')
        
        if not tag_name:
            return jsonify({"success": False, "error": "标签名称不能为空"}), 400
            
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if tag_id:
                # 更新模式
                # 检查同名冲突（排除自身）
                c.execute("SELECT id FROM tags WHERE tag_name = ? AND id != ?", (tag_name, tag_id))
                if c.fetchone():
                    conn.close()
                    return jsonify({"success": False, "error": f"已存在同名的标签: {tag_name}"}), 400
                    
                c.execute("UPDATE tags SET tag_name = ? WHERE id = ?", (tag_name, tag_id))
                c.execute("DELETE FROM tag_majors WHERE tag_id = ?", (tag_id,))
                real_tag_id = tag_id
                action_desc = "修改专家标签"
            else:
                # 新增模式
                # 检查同名
                c.execute("SELECT id FROM tags WHERE tag_name = ?", (tag_name,))
                if c.fetchone():
                    conn.close()
                    return jsonify({"success": False, "error": f"已存在同名的标签: {tag_name}"}), 400
                    
                c.execute("INSERT INTO tags (tag_name, created_at) VALUES (?, ?)", (tag_name, now_time))
                real_tag_id = c.lastrowid
                action_desc = "创建专家标签"
                
            # 插入新的小专业关联关系
            if majors:
                # 去重且清洗
                clean_majors = list(set([m.strip() for m in majors if m.strip()]))
                for m in clean_majors:
                    c.execute("INSERT INTO tag_majors (tag_id, major_name) VALUES (?, ?)", (real_tag_id, m))
                    
            conn.commit()
            conn.close()
            
            _log_action(action_desc, f"标签: {tag_name}，包含专业共 {len(majors)} 个")
            return jsonify({"success": True, "message": "保存标签成功！", "tag_id": real_tag_id})
        except Exception as e:
            return jsonify({"success": False, "error": f"保存标签失败: {str(e)}"}), 500

@experts_bp.route('/api/tags/delete', methods=['POST'])
def api_delete_tag():
    data = request.json or {}
    tag_id = data.get('tag_id')
    if not tag_id:
        return jsonify({"success": False, "error": "缺失标签ID参数"}), 400
        
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 查出标签名称用于日志记录
        c.execute("SELECT tag_name FROM tags WHERE id = ?", (tag_id,))
        row = c.fetchone()
        tag_name = row[0] if row else f"ID_{tag_id}"
        
        # 删除主表记录
        c.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        # 手动清理关联表数据
        c.execute("DELETE FROM tag_majors WHERE tag_id = ?", (tag_id,))
        
        conn.commit()
        conn.close()
        
        _log_action("删除专家标签", f"标签: {tag_name}")
        return jsonify({"success": True, "message": "标签已成功删除。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"删除标签失败: {str(e)}"}), 500

@experts_bp.route('/api/all_majors', methods=['GET'])
def api_all_majors():
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        # 从倒排索引表中快速拉出所有非空的小专业
        c.execute("SELECT DISTINCT major_name FROM expert_majors WHERE major_name != '' ORDER BY major_name ASC")
        majors = [row[0] for row in c.fetchall()]
        conn.close()
        return jsonify({"success": True, "majors": majors})
    except Exception as e:
        return jsonify({"success": False, "error": f"获取专业列表失败: {str(e)}"}), 500


