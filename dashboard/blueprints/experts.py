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
import base64
from dotenv import load_dotenv
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.exceptions.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.iai.v20200303 import iai_client, models

# 定义 Blueprint
experts_bp = Blueprint('experts', __name__,
                       template_folder='../templates',
                       url_prefix='/dlsgzs')

DB_NAME = 'experts.db'

# --- 腾讯云人脸识别 SDK 配置与加载 ---
# 1. 尝试从本地加载 .env 配置文件
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(base_dir, '..', '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv() # 降级读取系统环境变量

# 2. 读取腾讯云配置
SECRET_ID = os.getenv("TENCENTCLOUD_SECRET_ID", "").strip()
SECRET_KEY = os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip()
REGION = os.getenv("TENCENTCLOUD_REGION", "ap-beijing").strip()
GROUP_ID = os.getenv("TENCENTCLOUD_GROUP_ID", "experts_group").strip()

def _get_iai_client():
    """初始化并获取腾讯云人脸识别客户端"""
    if not SECRET_ID or not SECRET_KEY:
        return None
    cred = credential.Credential(SECRET_ID, SECRET_KEY)
    httpProfile = HttpProfile()
    httpProfile.endpoint = "iai.tencentcloudapi.com"
    clientProfile = ClientProfile()
    clientProfile.httpProfile = httpProfile
    client = iai_client.IaiClient(cred, REGION, clientProfile)
    return client

def init_face_group():
    """在应用初始化阶段尝试创建人员库，若已存在则忽略"""
    client = _get_iai_client()
    if not client:
        print("⚠️ 腾讯云人脸识别配置不全（SecretId/SecretKey缺失），跳过自动创建人脸库步骤。")
        return
        
    try:
        # 首先检查人员库是否存在
        req = models.DescribeGroupRequest()
        req.GroupId = GROUP_ID
        client.DescribeGroup(req)
        print(f"✅ 腾讯云人脸库 GroupId='{GROUP_ID}' 已经存在，无需重复创建。")
    except TencentCloudSDKException as e:
        # 错误码为 FailedOperation.GroupNotExist 或错误提示不存在时
        if "GroupNotExist" in str(e.code) or "GroupNotExist" in str(e):
            try:
                create_req = models.CreateGroupRequest()
                create_req.GroupName = "评标专家人脸库"
                create_req.GroupId = GROUP_ID
                create_req.FaceModelVersion = "3.0"
                client.CreateGroup(create_req)
                print(f"🎉 成功在腾讯云端创建人员库 GroupId='{GROUP_ID}', 算法版本='3.0'!")
            except Exception as create_err:
                print(f"❌ 自动创建腾讯云人脸库失败: {create_err}")
        else:
            print(f"⚠️ 检查人员库状态异常: {e}")


def _register_or_update_face(id_card, name, photo_path):
    """向腾讯云人脸库注册或更新人员"""
    client = _get_iai_client()
    if not client:
        return False, "腾讯云 SecretId/SecretKey 配置缺失"
        
    if not id_card or not name or not photo_path:
        return False, "专家必填项(身份证号、姓名或照片路径)缺失"
        
    # 定位并读取照片
    filename = os.path.basename(photo_path)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    photos_dir = os.path.join(base_dir, 'static', 'uploads', 'expert_photos')
    physical_path = os.path.join(photos_dir, filename)
    
    if not os.path.exists(physical_path):
        # 兼容相对路径或直接在 file 下的情况
        alternative_dir = os.path.join(base_dir, '..', 'file')
        physical_path = os.path.join(alternative_dir, filename)
        if not os.path.exists(physical_path):
            return False, f"物理磁盘上未找到该专家的照片文件: {filename}"
            
    # 读取照片并转为 Base64
    try:
        with open(physical_path, "rb") as f:
            img_data = f.read()
            img_base64 = base64.b64encode(img_data).decode("utf-8")
    except Exception as e:
        return False, f"读取人脸照片文件失败: {str(e)}"
        
    # 为了保证注册/更新 100% 成功，采取“先尝试删除，后创建”的合并策略
    try:
        del_req = models.DeletePersonRequest()
        del_req.PersonId = id_card.strip()
        client.DeletePerson(del_req)
    except Exception:
        # 如果人员原本不存在，删除报错可直接忽略
        pass
        
    try:
        create_req = models.CreatePersonRequest()
        create_req.GroupId = GROUP_ID
        create_req.PersonName = name.strip()
        create_req.PersonId = id_card.strip()
        create_req.Image = img_base64
        client.CreatePerson(create_req)
        return True, "成功注册同步至人脸库"
    except TencentCloudSDKException as e:
        return False, f"注册人脸至腾讯云失败: {e.message} (代码: {e.code})"
    except Exception as e:
        return False, f"注册人脸时发生未捕获异常: {str(e)}"


def _search_face(image_base64):
    """在腾讯云人员库中搜索匹配的人脸，返回最相似的 PersonId(身份证号) 和相似度得分 Score"""
    client = _get_iai_client()
    if not client:
        return None, 0, "腾讯云 SecretId/SecretKey 配置缺失"
        
    try:
        req = models.SearchFacesRequest()
        req.GroupIds = [GROUP_ID]
        req.Image = image_base64
        req.MaxFaceNum = 1 # 仅识别并检索现场图中最大的一张脸
        req.MaxPersonNum = 1 # 仅返回相似度最高的 Top 1 候选人
        req.NeedPersonInfo = 0 # 不需要返回额外的人员描述
        
        resp = client.SearchFaces(req)
        
        # 解析返回结果
        if resp.Results and len(resp.Results) > 0:
            result = resp.Results[0]
            if result.RetCode == 0 and result.Candidates and len(result.Candidates) > 0:
                top_candidate = result.Candidates[0]
                return top_candidate.PersonId, top_candidate.Score, None
            else:
                # RetCode 不为 0 说明检索该脸失败（比如照片模糊没匹配到）
                return None, 0, "人脸库中未匹配到相似度足够高的人脸"
        else:
            return None, 0, "图片中未检测到清晰人脸"
            
    except TencentCloudSDKException as e:
        return None, 0, f"腾讯云人脸搜索失败: {e.message} (代码: {e.code})"
    except Exception as e:
        return None, 0, f"人脸搜索发生异常: {str(e)}"



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

def get_db_conn():
    """获取启用了 WAL 模式及 timeout=30 的 SQLite 连接"""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
    except Exception:
        pass
    return conn

def update_all_experts_stats(conn):
    """通过高性能聚合 SQL 重新计算并写入所有专家的参评次数和最后一次参评时间 (同一天内多次参评合并计算为 1 次)"""
    try:
        c = conn.cursor()
        update_sql = """
        UPDATE experts 
        SET 
          project_count = COALESCE((
            SELECT COUNT(DISTINCT 
              CASE 
                WHEN p.process_time IS NOT NULL AND LENGTH(TRIM(p.process_time)) >= 10 
                THEN SUBSTR(TRIM(p.process_time), 1, 10)
                ELSE 'empty_proj_' || p.id 
              END
            )
            FROM project_experts pe 
            JOIN projects p ON pe.project_id = p.id
            WHERE pe.expert_id_card = experts.id_card AND pe.expert_id_card IS NOT NULL AND pe.expert_id_card != ''
          ), 0),
          last_project_time = (
            SELECT MAX(p.process_time) 
            FROM project_experts pe
            JOIN projects p ON pe.project_id = p.id
            WHERE pe.expert_id_card = experts.id_card AND pe.expert_id_card IS NOT NULL AND pe.expert_id_card != ''
          )
        """
        c.execute(update_sql)
        conn.commit()
    except Exception as e:
        print(f"Failed to update experts stats: {e}")

def update_expert_stats_by_idcard(conn, id_card):
    """根据身份证号重新计算并更新单个专家的参评次数和最后一次参评时间 (同一天内多次参评合并计算为 1 次)"""
    if not id_card:
        return
    try:
        c = conn.cursor()
        update_sql = """
        UPDATE experts 
        SET 
          project_count = COALESCE((
            SELECT COUNT(DISTINCT 
              CASE 
                WHEN p.process_time IS NOT NULL AND LENGTH(TRIM(p.process_time)) >= 10 
                THEN SUBSTR(TRIM(p.process_time), 1, 10)
                ELSE 'empty_proj_' || p.id 
              END
            )
            FROM project_experts pe 
            JOIN projects p ON pe.project_id = p.id
            WHERE pe.expert_id_card = ? AND pe.expert_id_card IS NOT NULL AND pe.expert_id_card != ''
          ), 0),
          last_project_time = (
            SELECT MAX(p.process_time) 
            FROM project_experts pe
            JOIN projects p ON pe.project_id = p.id
            WHERE pe.expert_id_card = ? AND pe.expert_id_card IS NOT NULL AND pe.expert_id_card != ''
          )
        WHERE id_card = ?
        """
        c.execute(update_sql, (id_card, id_card, id_card))
        conn.commit()
    except Exception as e:
        print(f"Failed to update expert stats for {id_card}: {e}")

def init_db():
    """初始化数据库表结构"""
    conn = get_db_conn()
    try:
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
                project_count INTEGER DEFAULT 0,
                last_project_time TEXT,
                is_face_synced INTEGER DEFAULT 0,
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
        
        # 平滑升级旧数据库：增加 status 字段与 remark 字段，以及参评统计字段
        try:
            c.execute("ALTER TABLE experts ADD COLUMN status TEXT DEFAULT '未获取'")
        except sqlite3.OperationalError:
            pass

        try:
            c.execute("ALTER TABLE experts ADD COLUMN remark TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        try:
            c.execute("ALTER TABLE experts ADD COLUMN project_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            c.execute("ALTER TABLE experts ADD COLUMN last_project_time TEXT")
        except sqlite3.OperationalError:
            pass

        # 建立相应的索引以提升过滤速度
        try:
            c.execute('CREATE INDEX IF NOT EXISTS idx_experts_project_count ON experts(project_count)')
        except sqlite3.OperationalError:
            pass

        try:
            c.execute('CREATE INDEX IF NOT EXISTS idx_experts_last_project_time ON experts(last_project_time)')
        except sqlite3.OperationalError:
            pass
            
        try:
            c.execute("ALTER TABLE experts ADD COLUMN is_face_synced INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            c.execute('CREATE INDEX IF NOT EXISTS idx_experts_is_face_synced ON experts(is_face_synced)')
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

        # 一次性历史数据清洗重算
        try:
            update_all_experts_stats(conn)
        except Exception as e:
            print(f"⚠️ 历史专家参评统计初始化更新失败: {e}")

        # 腾讯云人脸库自动检测建库
        try:
            init_face_group()
        except Exception as e:
            print(f"⚠️ 自动初始化腾讯云人脸库失败: {e}")
    finally:
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
    
    conn = get_db_conn()
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
        conn = get_db_conn()
        imported_count = 0
        matched_photo_count = 0
        total_in_file = 0
        added_count = 0
        updated_count = 0
        photos_dest_dir = get_photos_dir()
        
        # 记录本次操作的时间
        now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            c = conn.cursor()
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

                # 提前进行数据库查重，以便对于已存在且有照专家跳过压缩包照片的读取与复制流程
                c.execute("SELECT id, id_card, company, major, photo_path, raw_json FROM experts WHERE name = ? AND phone = ?", (name, phone))
                exist_record = c.fetchone()

                # 照片关联与去重覆盖逻辑
                # 照片可能命名为 "{姓名}_{手机号}.jpg"
                photo_key = f"{name}_{phone}".lower()
                photo_path_db = None
                
                should_process_photo = True
                if exist_record and exist_record[4]:
                    # 专家已存在且已有照片，则绝对不再覆盖更新，直接使用原有数据库的照片路径
                    should_process_photo = False
                    photo_path_db = exist_record[4]

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
                
                if matched_photos and should_process_photo:
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
                    # 若本次压缩包内未包含该专家的图片（或跳过不覆盖），且该专家在数据库已存在，则保持其旧图片路径不抹除
                    if exist_record:
                        photo_path_db = exist_record[4]

                # 追加与去重覆盖：由于在循环开始处已执行 SELECT，此处直接使用 exist_record
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
        finally:
            conn.close()
        
        md_message = ""
        if md_file:
            try:
                proj_added, proj_updated, rel_count = parse_and_import_md(md_file)
                proj_total = proj_added + proj_updated
                md_message = f" 另外检测到并成功导入项目关系文件，共解析导入项目 {proj_total} 个（其中新增 {proj_added} 个，重复覆盖更新 {proj_updated} 个），共关联参评专家 {rel_count} 人次。"
            except Exception as e:
                md_message = f" 但项目关系 MD 解析失败，错误: {str(e)}。"
        
        # 联动触发更新
        try:
            update_conn = get_db_conn()
            try:
                update_all_experts_stats(update_conn)
            finally:
                update_conn.close()
        except Exception as e:
            print(f"Error updating expert stats after upload: {e}")

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
            # 联动触发更新
            try:
                update_conn = get_db_conn()
                try:
                    update_all_experts_stats(update_conn)
                finally:
                    update_conn.close()
            except Exception as e:
                print(f"Error updating expert stats after upload_md: {e}")
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
    
    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    try:
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
        project_ids = [p_row['id'] for p_row in project_rows]
        
        # 批量拉取所有相关项目的专家数据，消除 N+1 查询性能瓶颈
        experts_map = {}
        if project_ids:
            placeholders = ",".join(["?"] * len(project_ids))
            c.execute(f"""
                SELECT project_id, expert_name, expert_id_card, expert_code 
                FROM project_experts 
                WHERE project_id IN ({placeholders})
            """, project_ids)
            expert_rows = c.fetchall()
            for e_row in expert_rows:
                p_id = e_row['project_id']
                if p_id not in experts_map:
                    experts_map[p_id] = []
                experts_map[p_id].append({
                    "name": e_row['expert_name'],
                    "id_card": e_row['expert_id_card'],
                    "code": e_row['expert_code']
                })
        
        for p_row in project_rows:
            p_id = p_row['id']
            experts_list = experts_map.get(p_id, [])
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
        
    conn = get_db_conn()
    try:
        c = conn.cursor()
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
    
    project_count = request.args.get('project_count', '').strip()
    last_project_time = request.args.get('last_project_time', '').strip()
    
    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    try:
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
            
        if project_count.isdigit():
            conditions.append("project_count >= ?")
            params.append(int(project_count))
            
        if last_project_time:
            conditions.append("last_project_time >= ? AND last_project_time IS NOT NULL AND last_project_time != ''")
            params.append(last_project_time)
            
        tag_ids_str = request.args.get('tag_ids', '').strip()
        if tag_ids_str:
            tag_id_list = [t.strip() for t in tag_ids_str.split(',') if t.strip()]
            digit_ids = [t for t in tag_id_list if t.isdigit()]
            has_unassigned = 'unassigned' in tag_id_list
            has_multi_photos = 'multi_photos' in tag_id_list
            
            sub_conds = []
            if digit_ids:
                placeholders = ",".join(["?"] * len(digit_ids))
                sub_conds.append(f"id IN (SELECT DISTINCT em.expert_id FROM expert_majors em JOIN tag_majors tm ON em.major_name = tm.major_name WHERE tm.tag_id IN ({placeholders}))")
                params.extend([int(tid) for tid in digit_ids])
                
            if has_unassigned:
                sub_conds.append("id NOT IN (SELECT DISTINCT em.expert_id FROM expert_majors em JOIN tag_majors tm ON em.major_name = tm.major_name)")
                
            if has_multi_photos:
                sub_conds.append("photo_path LIKE '%,%'")
                
            if sub_conds:
                conditions.append(f"({' OR '.join(sub_conds)})")
            
        # 精炼 SQL：不 SELECT raw_json 大文本字段，并通过子查询获取每个专家所对应的标签字符串
        sql = """
            SELECT id, name, phone, id_card, company, major, photo_path, status, remark, project_count, last_project_time,
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
    finally:
        conn.close()
    
    results = []
    for r in rows:
        item_status = r['status'] if r.keys() and 'status' in r.keys() and r['status'] else '未获取'
        item_remark = r['remark'] if r.keys() and 'remark' in r.keys() and r['remark'] else ''
        
        tags_list = []
        if r.keys() and 'tags_str' in r.keys() and r['tags_str']:
            tags_list = list(set([t.strip() for t in r['tags_str'].split(',') if t.strip()]))
                
        results.append({
            "id": r['id'],
            "name": r['name'],
            "phone": r['phone'],
            "id_card": r['id_card'],
            "company": r['company'],
            "major": r['major'],
            "photo_path": r['photo_path'],
            "status": item_status,
            "remark": item_remark,
            "tags": tags_list,
            "project_count": r['project_count'] if 'project_count' in r.keys() and r['project_count'] is not None else 0,
            "last_project_time": r['last_project_time'] if 'last_project_time' in r.keys() else None,
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
        
    conn = get_db_conn()
    try:
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
            
        _log_action("查看专家详情", f"姓名: {name}, 电话: {phone}")
        return jsonify({"success": True, "details": parsed_json, "tags": tags})
    except Exception as e:
        return jsonify({"success": False, "error": f"查询专家详情失败: {str(e)}"}), 500
    finally:
        conn.close()

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
            # 3. 动态写入还原说明教程
            readme_text = """========================================================================
                      评标专家系统 - 数据还原教程 (README)
========================================================================

本文件是系统自动生成的完整备份还原指引。通过以下步骤，您可以将当前备份数据
（数据库 + 专家照片）安全地还原到云服务器或本地运行环境。

一、 备份包包含内容
------------------------------------------------------------------------
1. experts.db      : 包含专家信息、项目信息、专业标签等所有数据库记录。
2. expert_photos/  : 包含专家身份证照片文件夹。

二、 还原到云服务器 (Docker 部署环境)
------------------------------------------------------------------------
如果您的云服务器是基于 Docker 构建的（默认标准环境），请按以下步骤操作：

1. 准备工作：
   请使用 SFTP（如 WinSCP 或 Termius）将解压后的 `experts.db` 以及 
   `expert_photos/` 目录下的所有照片上传至云服务器的项目根目录下。

2. 覆盖数据库文件：
   将 `experts.db` 拷贝覆盖到项目目录下的 `data/experts.db`。
   命令行操作示例：
   cp experts.db ./data/experts.db

3. 覆盖照片文件：
   将 `expert_photos/` 目录下的所有照片拷贝至项目目录下的 
   `dashboard/static/uploads/expert_photos/` 文件夹中。
   命令行操作示例：
   cp -r expert_photos/* ./dashboard/static/uploads/expert_photos/

4. 修复文件与目录权限：
   为了防止容器内进程无权限读写覆盖的数据，请在项目根目录下修复权限：
   chown -R 1000:1000 data/ dashboard/static/uploads/

5. 重启应用容器：
   在服务器终端运行以下命令重启服务以应用新数据：
   docker restart bidding-app

------------------------------------------------------------------------
三、 还原到本地开发环境 (Python Flask 运行环境)
------------------------------------------------------------------------
如果您是在本地开发调试环境下运行：

1. 覆盖数据库文件：
   将解压出来的 `experts.db` 直接复制到本地项目根目录下的 `data/experts.db`。

2. 覆盖照片文件：
   将解压出的 `expert_photos/` 文件夹整体复制到本地项目根目录下的 
   `dashboard/static/uploads/expert_photos/` 目录下。

3. 重新运行服务：
   双击或运行 `run.py` 重新启动 Flask 开发服务器即可。

========================================================================
                      生成时间: {timestamp}
========================================================================
""".format(timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            zipf.writestr("数据还原教程_README.txt", readme_text)
                        
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
    conn = get_db_conn()
    try:
        c = conn.cursor()
        c.execute('DELETE FROM experts')
        c.execute('DELETE FROM expert_majors')
        c.execute('DELETE FROM projects')
        c.execute('DELETE FROM project_experts')
        c.execute('DELETE FROM tags')
        c.execute('DELETE FROM tag_majors')
        conn.commit()
    except Exception as e:
        return jsonify({"success": False, "error": f"清空数据库失败: {str(e)}"}), 500
    finally:
        conn.close()
        
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
        
    conn = get_db_conn()
    try:
        c = conn.cursor()
        
        # 查找图片路径与身份证号
        c.execute("SELECT photo_path, id_card FROM experts WHERE name = ? AND phone = ?", (name, phone))
        row = c.fetchone()
        if row:
            photo_path = row[0]
            id_card = row[1]
            if photo_path:
                photo_paths = photo_path.split(',')
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
            
            # 从项目评审专家关联表中删除该专家的评审记录
            if id_card:
                c.execute("DELETE FROM project_experts WHERE expert_id_card = ?", (id_card,))
        
        # 从倒排关系表中物理删除
        c.execute("DELETE FROM expert_majors WHERE expert_id IN (SELECT id FROM experts WHERE name = ? AND phone = ?)", (name, phone))
        # 从数据库中物理删除
        c.execute("DELETE FROM experts WHERE name = ? AND phone = ?", (name, phone))
        conn.commit()
        
        _log_action("删除评标专家", f"专家姓名: {name}, 电话: {phone}")
        return jsonify({"success": True, "message": f"专家 {name} 及其相关照片与参评记录已删除。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"删除专家失败: {str(e)}"}), 500
    finally:
        conn.close()


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
        
    conn = get_db_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE experts SET status = ?, remark = ? WHERE name = ? AND phone = ?", (status, remark, name, phone))
        conn.commit()
        _log_action("更新专家状态", f"专家姓名: {name}, 电话: {phone}, 新状态: {status}, 备注: {remark}")
        return jsonify({"success": True, "message": f"专家 {name} 的状态与备注已成功更新。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"更新专家状态/备注失败: {str(e)}"}), 500
    finally:
        conn.close()


@experts_bp.route('/api/update_expert_profile', methods=['POST'])
def api_update_expert_profile():
    """更新评标专家的基本资料 (包括手机号、单位、专业及照片文件)"""
    # 接收 multipart/form-data
    old_name = request.form.get('old_name', '').strip()
    old_phone = request.form.get('old_phone', '').strip()
    new_phone = request.form.get('new_phone', '').strip()
    company = request.form.get('company', '').strip()
    major = request.form.get('major', '').strip()
    
    if not old_name or not old_phone:
        return jsonify({"success": False, "error": "定位专家的旧姓名与旧电话号码不能为空"}), 400
        
    if not new_phone:
        return jsonify({"success": False, "error": "新的电话号码不能为空"}), 400
        
    conn = get_db_conn()
    try:
        c = conn.cursor()
        
        # 1. 查找此专家是否存在
        c.execute("SELECT id, phone, photo_path FROM experts WHERE name = ? AND phone = ?", (old_name, old_phone))
        row = c.fetchone()
        if not row:
            return jsonify({"success": False, "error": f"未找到专家 {old_name} ({old_phone})"}), 404
            
        expert_id = row[0]
        db_phone = row[1]
        db_photo_path = row[2]
        
        # 2. 如果新手机号变了，校验是否与其他专家冲突（唯一联合索引 idx_experts_name_phone）
        if new_phone != old_phone:
            c.execute("SELECT id FROM experts WHERE name = ? AND phone = ?", (old_name, new_phone))
            conflict = c.fetchone()
            if conflict:
                return jsonify({"success": False, "error": f"修改失败：已存在相同姓名（{old_name}）和电话（{new_phone}）的其他专家，请核对后重试。"}), 409
                
        # 3. 处理照片更换
        new_photo_path = db_photo_path
        photo_file = request.files.get('photo_file')
        if photo_file and photo_file.filename != '':
            # 校验后缀
            ext = os.path.splitext(photo_file.filename)[1].lower()
            if ext not in ['.jpg', '.jpeg', '.png']:
                return jsonify({"success": False, "error": "上传的照片格式不正确，仅支持 .jpg, .jpeg, .png"}), 400
                
            # 保存新照片
            photos_dir = get_photos_dir()
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{old_name}_{new_phone}_uploaded_{timestamp}{ext}".lower()
            dest_path = os.path.join(photos_dir, filename)
            
            photo_file.save(dest_path)
            new_photo_path = f"/static/uploads/expert_photos/{filename}"
            
            # 删除老照片（为了防止爆盘，如果在 uploads 下且不等于新照片就删除）
            if db_photo_path:
                old_paths = db_photo_path.split(',')
                for p_path in old_paths:
                    p_path = p_path.strip()
                    if p_path:
                        old_filename = os.path.basename(p_path)
                        old_physical_path = os.path.join(photos_dir, old_filename)
                        if os.path.exists(old_physical_path):
                            try:
                                os.remove(old_physical_path)
                            except Exception:
                                pass
                                
        # 4. 执行更新
        c.execute("""
            UPDATE experts 
            SET phone = ?, company = ?, major = ?, photo_path = ? 
            WHERE id = ?
        """, (new_phone, company, major, new_photo_path, expert_id))
        
        # 5. 同步倒排专业表
        sync_expert_majors(conn, expert_id, major)
        
        # 6. 获取身份证号重算单个专家参评统计（代替耗时的全表重算）
        c.execute("SELECT id_card FROM experts WHERE id = ?", (expert_id,))
        row_id_card = c.fetchone()
        if row_id_card and row_id_card[0]:
            update_expert_stats_by_idcard(conn, row_id_card[0])
        
        conn.commit()

        # 7. 若更新了人脸照片，则同步注册到腾讯云人脸库
        if photo_file and photo_file.filename != '':
            c.execute("SELECT id_card, name, photo_path FROM experts WHERE id = ?", (expert_id,))
            exp_row = c.fetchone()
            if exp_row and exp_row[0]:
                id_card_val, name_val, photo_path_val = exp_row
                photo_list = [p.strip() for p in photo_path_val.split(',') if p.strip()]
                if photo_list:
                    ok, sync_err = _register_or_update_face(id_card_val, name_val, photo_list[0])
                    if ok:
                        c.execute("UPDATE experts SET is_face_synced = 1 WHERE id = ?", (expert_id,))
                        conn.commit()
                    else:
                        c.execute("UPDATE experts SET is_face_synced = 0 WHERE id = ?", (expert_id,))
                        conn.commit()
        
        _log_action("修改专家基本信息", f"专家姓名: {old_name}, 新电话: {new_phone}, 新单位: {company}, 新专业: {major}")
        return jsonify({"success": True, "message": f"专家 {old_name} 的个人资料已成功修改。"})
        
    except Exception as e:
        return jsonify({"success": False, "error": f"更新专家信息失败: {str(e)}"}), 500
    finally:
        conn.close()


@experts_bp.route('/api/delete_photo', methods=['POST'])
def api_delete_photo():
    """删除专家的某张身份证照片，并物理删除文件与更新数据库路径"""
    data = request.get_json() or {}
    expert_id = data.get('id')
    photo_to_delete = data.get('photo_path')
    
    if not expert_id or not photo_to_delete:
        return jsonify({"success": False, "error": "参数不足"}), 400
        
    conn = get_db_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT photo_path, name, phone FROM experts WHERE id = ?", (expert_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"success": False, "error": "未找到对应的专家记录"}), 404
            
        db_photo_path = row[0]
        name = row[1]
        phone = row[2]
        
        if not db_photo_path:
            return jsonify({"success": False, "error": "专家原本就没有照片"}), 400
            
        # 解析数据库中的照片路径
        photo_list = [p.strip() for p in db_photo_path.split(',') if p.strip()]
        
        # 检验要删除的照片是否确实存在于该专家的路径列表中
        cleaned_target = photo_to_delete.strip().lower()
        matched_db_path = None
        for p in photo_list:
            if p.lower() == cleaned_target or p.lower().endswith(cleaned_target) or cleaned_target.endswith(p.lower()):
                matched_db_path = p
                break
                
        if not matched_db_path:
            return jsonify({"success": False, "error": "该照片路径与数据库中记录不符"}), 400
            
        # 至少保留一张照片的约束
        if len(photo_list) <= 1:
            return jsonify({"success": False, "error": "专家只剩一张照片，无法执行删除，只能通过更换照片直接覆盖"}), 400
            
        # 1. 从列表中移除要删除的照片
        photo_list.remove(matched_db_path)
        new_photo_path_db = ",".join(photo_list)
        
        # 2. 从服务器磁盘上物理删除该图片文件
        filename = os.path.basename(matched_db_path)
        photos_dir = get_photos_dir()
        physical_path = os.path.join(photos_dir, filename)
        
        if os.path.exists(physical_path):
            try:
                os.remove(physical_path)
            except Exception as ex:
                # 记录文件占用异常，但不阻断数据库的正常更新
                print(f"物理删除磁盘照片文件失败 {physical_path}: {ex}")
                
        # 3. 更新数据库
        c.execute("UPDATE experts SET photo_path = ?, created_at = ? WHERE id = ?", 
                  (new_photo_path_db, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), expert_id))
        
        # 4. 删除照片后，若专家仍有照片，自动同步最新的第一张照片至腾讯云
        c.execute("SELECT id_card, name FROM experts WHERE id = ?", (expert_id,))
        info_row = c.fetchone()
        if info_row:
            id_card_val, name_val = info_row
            if id_card_val and photo_list:
                ok, sync_err = _register_or_update_face(id_card_val, name_val, photo_list[0])
                if ok:
                    c.execute("UPDATE experts SET is_face_synced = 1 WHERE id = ?", (expert_id,))
                else:
                    c.execute("UPDATE experts SET is_face_synced = 0 WHERE id = ?", (expert_id,))
        
        conn.commit()
        
        _log_action("删除专家部分照片", f"专家: {name}({phone}), 删除照片: {filename}")
        return jsonify({"success": True, "message": "照片已成功物理删除且路径已更新并重新同步人脸库"})
        
    except Exception as e:
        return jsonify({"success": False, "error": f"系统错误: {str(e)}"}), 500
    finally:
        conn.close()


@experts_bp.route('/api/tags', methods=['GET', 'POST'])
def api_tags():
    if request.method == 'GET':
        conn = get_db_conn()
        conn.row_factory = sqlite3.Row
        try:
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
            
            # 3. 动态检查是否存在具有多张照片的专家
            c.execute("SELECT COUNT(*) FROM experts WHERE photo_path LIKE '%,%'")
            has_multi_photos = c.fetchone()[0] > 0
            if has_multi_photos:
                # 在下拉列表头部塞入虚拟标签“多张照片”
                tags_list.insert(0, {
                    "id": "multi_photos",
                    "tag_name": "多张照片",
                    "majors": [],
                    "created_at": ""
                })
            
            return jsonify({"success": True, "tags": tags_list})
        except Exception as e:
            return jsonify({"success": False, "error": f"获取标签列表失败: {str(e)}"}), 500
        finally:
            conn.close()
            
    elif request.method == 'POST':
        data = request.json or {}
        tag_name = data.get('tag_name', '').strip()
        majors = data.get('majors', [])
        tag_id = data.get('tag_id')
        
        if not tag_name:
            return jsonify({"success": False, "error": "标签名称不能为空"}), 400
            
        conn = get_db_conn()
        try:
            c = conn.cursor()
            now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if tag_id:
                # 更新模式
                # 检查同名冲突（排除自身）
                c.execute("SELECT id FROM tags WHERE tag_name = ? AND id != ?", (tag_name, tag_id))
                if c.fetchone():
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
            
            _log_action(action_desc, f"标签: {tag_name}，包含专业共 {len(majors)} 个")
            return jsonify({"success": True, "message": "保存标签成功！", "tag_id": real_tag_id})
        except Exception as e:
            return jsonify({"success": False, "error": f"保存标签失败: {str(e)}"}), 500
        finally:
            conn.close()

@experts_bp.route('/api/tags/delete', methods=['POST'])
def api_delete_tag():
    data = request.json or {}
    tag_id = data.get('tag_id')
    if not tag_id:
        return jsonify({"success": False, "error": "缺失标签ID参数"}), 400
        
    conn = get_db_conn()
    try:
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
        
        _log_action("删除专家标签", f"标签: {tag_name}")
        return jsonify({"success": True, "message": "标签已成功删除。"})
    except Exception as e:
        return jsonify({"success": False, "error": f"删除标签失败: {str(e)}"}), 500
    finally:
        conn.close()

@experts_bp.route('/api/all_majors', methods=['GET'])
def api_all_majors():
    conn = get_db_conn()
    try:
        c = conn.cursor()
        # 从倒排索引表中快速拉出所有非空的小专业
        c.execute("SELECT DISTINCT major_name FROM expert_majors WHERE major_name != '' ORDER BY major_name ASC")
        majors = [row[0] for row in c.fetchall()]
        return jsonify({"success": True, "majors": majors})
    except Exception as e:
        return jsonify({"success": False, "error": f"获取专业列表失败: {str(e)}"}), 500
    finally:
        conn.close()


