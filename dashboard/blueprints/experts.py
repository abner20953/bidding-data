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
                       url_prefix='/zj')

DB_NAME = 'experts.db'

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
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE experts ADD COLUMN remark TEXT DEFAULT ''")
        conn.commit()
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
        
    conn.commit()
    conn.close()

def parse_and_import_md(file_path):
    """解析并导入 Markdown 格式的项目评审与专家关系表 (流式读取逐行处理，对内存极度友好)"""
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
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                if not line_str.startswith('|') or not line_str.endswith('|'):
                    continue
                
                # 过滤表头和表格分割线
                if '项目名称' in line_str or ':---' in line_str:
                    continue
                
                parts = [p.strip() for p in line_str.split('|')]
                # 预期的 parts 格式: ['', 项目名称, 处理时间, 经办人姓名, 经办人部门, 评审专家列表, '']
                if len(parts) < 6:
                    continue
                
                project_name = parts[1]
                process_time = parts[2]
                agent_name = parts[3]
                agent_dept = parts[4]
                experts_cell = parts[5]
                
                if not project_name:
                    continue
                
                # 插入或更新项目基本信息，如果项目已存在，更新其基本元数据
                c.execute("SELECT id FROM projects WHERE project_name = ?", (project_name,))
                row_exist = c.fetchone()
                if row_exist:
                    project_id = row_exist[0]
                    c.execute('''
                        UPDATE projects 
                        SET process_time = ?, agent_name = ?, agent_dept = ?, created_at = ?
                        WHERE id = ?
                    ''', (process_time, agent_name, agent_dept, now_time, project_id))
                    # 防重覆盖机制：清空该项目之前所有的旧评审专家关联
                    c.execute("DELETE FROM project_experts WHERE project_id = ?", (project_id,))
                    project_updated += 1
                else:
                    c.execute('''
                        INSERT INTO projects (project_name, process_time, agent_name, agent_dept, created_at)
                        VALUES (?, ?, ?, ?, ?)
                      ''', (project_name, process_time, agent_name, agent_dept, now_time))
                    project_id = c.lastrowid
                    project_added += 1
                
                # 解析评审专家列表：通过 <br> 分隔
                # 专家格式: 姓名 / 身份证号 / 专家编码
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
            for f in files:
                if f.startswith('._'): # 忽略 Mac OS 的临时隐藏文件
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
                    shutil.copy2(p_file, dest_path)
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
            c.execute("SELECT id FROM experts WHERE name = ? AND phone = ?", (name, phone))
            exist_record = c.fetchone()
            
            if exist_record:
                # 已经存在，执行更新操作
                updated_count += 1
                c.execute('''
                    UPDATE experts 
                    SET id_card = ?, company = ?, major = ?, photo_path = ?, raw_json = ?, created_at = ?
                    WHERE id = ?
                ''', (id_card, company, major, photo_path_db, raw_json, now_time, exist_record[0]))
            else:
                # 不存在，执行插入（追加）操作
                added_count += 1
                c.execute('''
                    INSERT INTO experts (name, phone, id_card, company, major, photo_path, raw_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, phone, id_card, company, major, photo_path_db, raw_json, now_time))
                
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
            SELECT DISTINCT p.id, p.project_name, p.process_time, p.agent_name, p.agent_dept, p.created_at
            FROM projects p
            LEFT JOIN project_experts pe ON p.id = pe.project_id
        """
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY p.id DESC LIMIT ? OFFSET ?"
        
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
                "experts": experts_list
            })
            
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
        
    # 精炼 SQL：不 SELECT raw_json 大文本字段，使一千条数据能在 20ms 内查出并传输完成
    sql = "SELECT name, phone, id_card, company, major, photo_path, status, remark FROM experts"
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
                
        results.append({
            "name": r['name'],
            "phone": r['phone'],
            "id_card": r['id_card'],
            "company": r['company'],
            "major": r['major'],
            "photo_path": r['photo_path'],
            "status": item_status,
            "remark": item_remark,
            "details": {}  # 列表阶段置空，点击“查看完整档案”时通过 api_detail 懒加载
        })
        
    return jsonify({"success": True, "data": results})

@experts_bp.route('/api/detail', methods=['GET'])
def api_detail():
    """点对点懒加载查询单个专家原始 JSON 详情 (毫秒级响应)"""
    name = request.args.get('name', '').strip()
    phone = request.args.get('phone', '').strip()
    if not name or not phone:
        return jsonify({"success": False, "error": "姓名和手机号不能为空"}), 400
        
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        # 命中联合唯一索引 idx_experts_name_phone，查询仅需几微秒
        c.execute("SELECT raw_json FROM experts WHERE name = ? AND phone = ?", (name, phone))
        row = c.fetchone()
        conn.close()
        
        parsed_json = {}
        if row and row[0]:
            try:
                parsed_json = json.loads(row[0])
            except Exception:
                pass
        return jsonify({"success": True, "details": parsed_json})
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
        
        # 从数据库中物理删除
        c.execute("DELETE FROM experts WHERE name = ? AND phone = ?", (name, phone))
        conn.commit()
        conn.close()
        
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
        return jsonify({"success": True, "message": f"专家 {name} 的状态与备注已成功更新。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"更新专家状态/备注失败: {str(e)}"}), 500

