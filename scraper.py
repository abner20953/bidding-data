import requests
from bs4 import BeautifulSoup
import datetime
import time
import re
import concurrent.futures
import pandas as pd
import os
import gc
import hashlib
import json
import sqlite3
import threading
import uuid
from urllib.parse import urljoin
from openpyxl.styles import Alignment
from requests.adapters import HTTPAdapter


BASE_URL = "http://search.ccgp.gov.cn/bxsearch"
REGION_NAME = "山西"
REGION_ID = "14"
DAYS_AGO = 90
MAX_PAGES = 100  # 安全上限（自动分页会提前停止）
MAX_RUN_SECONDS = 15 * 60
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.getenv("SCRAPER_OUTPUT_DIR", os.path.join(PROJECT_DIR, "results"))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
DETAIL_CACHE_DB = os.getenv("SCRAPER_CACHE_DB", os.path.join(DATA_DIR, "scraper_cache.db"))
DETAIL_CACHE_TTL_SECONDS = 6 * 60 * 60
DETAIL_CACHE_MAX_ROWS = 10000
DETAIL_CACHE_MAX_BYTES = 200 * 1024 * 1024
REQUIRED_RESULT_COLUMNS = {
    "标题", "是否信息化", "语义匹配度", "开标具体时间", "开标地点", "链接"
}

_http_local = threading.local()
_model_lock = threading.Lock()
_cache_init_lock = threading.Lock()
_cache_initialized = False



# 高置信度特征词（命中即判定为信息化，解决长标题语义稀释问题）
# 只要项目标题包含这些核心技术词汇，直接判定为“是”
STRONG_IT_KEYWORDS = [
    '信息化', '软件', '大数据', '云计算', '物联网', '人工智能',
    '智慧监管', '智慧平台', '智能平台', '智慧信息', '网络安全',
    '数据中心', '调度中心', '监控系统',
    # 组合词替代单字，防止误判（如“开发区”、“空调系统”）
    '软件开发', '系统开发', '平台开发', '网站建设', 'APP开发', '小程序', '公众号',
    '信息系统', '管理系统', '办公系统', '操作系统', '运维服务',
    # 补漏关键词 (Step 664 + Step 705)
    '智慧黑板', '互联智慧', '系统录入', '数字乡村', '平台建设', '云服务', '政务云',
    '培训平台', '管理平台', '在线平台',
    # 新增数字化相关 (Step 1045)
    '电子化', '数字化', '档案数字化', '档案电子化',
    # 新增补漏 (Step 1069)
    '视频会议', '会议系统', '考核平台', '信息采集',
    # 新增补漏 (Step 1088)
    '虚拟化', '智慧医院',
    # 新增补漏 (User Request)
    '日志审计', '数据服务', '数智化', '监管平台'
]



os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


def _get_http_session():
    session = getattr(_http_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _http_local.session = session
    return session


def _decode_response(response):
    content_type = response.headers.get("Content-Type", "").lower()
    declared = response.encoding
    if not declared or declared.lower() in {"iso-8859-1", "ascii"}:
        declared = response.apparent_encoding
    if "charset=" in content_type:
        declared = response.encoding or declared
    return response.content.decode(declared or "utf-8", errors="replace")


def _init_detail_cache():
    global _cache_initialized
    if _cache_initialized:
        return
    with _cache_init_lock:
        if _cache_initialized:
            return
        os.makedirs(os.path.dirname(DETAIL_CACHE_DB), exist_ok=True)
        conn = sqlite3.connect(DETAIL_CACHE_DB, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS detail_cache (
                       url TEXT PRIMARY KEY,
                       details_json TEXT NOT NULL,
                       content_hash TEXT,
                       fetched_at REAL NOT NULL
                   )"""
            )
            conn.commit()
        finally:
            conn.close()
        _cache_initialized = True


def _get_cached_details(url):
    try:
        _init_detail_cache()
        conn = sqlite3.connect(DETAIL_CACHE_DB, timeout=30)
        try:
            row = conn.execute(
                "SELECT details_json, fetched_at FROM detail_cache WHERE url = ?", (url,)
            ).fetchone()
        finally:
            conn.close()
        if not row or time.time() - row[1] > DETAIL_CACHE_TTL_SECONDS:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _cache_details(url, details, html):
    try:
        _init_detail_cache()
        conn = sqlite3.connect(DETAIL_CACHE_DB, timeout=30)
        try:
            conn.execute(
                """INSERT INTO detail_cache(url, details_json, content_hash, fetched_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                       details_json=excluded.details_json,
                       content_hash=excluded.content_hash,
                       fetched_at=excluded.fetched_at""",
                (
                    url,
                    json.dumps(details, ensure_ascii=False),
                    hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest(),
                    time.time(),
                ),
            )
            count = conn.execute("SELECT COUNT(*) FROM detail_cache").fetchone()[0]
            if count > DETAIL_CACHE_MAX_ROWS:
                conn.execute(
                    "DELETE FROM detail_cache WHERE url IN "
                    "(SELECT url FROM detail_cache ORDER BY fetched_at ASC LIMIT ?)",
                    (count - int(DETAIL_CACHE_MAX_ROWS * 0.8),),
                )
            conn.commit()
            cache_size = sum(
                os.path.getsize(path)
                for path in (DETAIL_CACHE_DB, DETAIL_CACHE_DB + "-wal", DETAIL_CACHE_DB + "-shm")
                if os.path.exists(path)
            )
            if cache_size > DETAIL_CACHE_MAX_BYTES:
                conn.execute(
                    "DELETE FROM detail_cache WHERE url IN "
                    "(SELECT url FROM detail_cache ORDER BY fetched_at ASC LIMIT ?)",
                    (max(count // 3, 1),),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
        finally:
            conn.close()
    except Exception:
        pass

def get_date_range():
    """获取近三个月的时间范围"""
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=DAYS_AGO)
    return start_date.strftime("%Y:%m:%d"), end_date.strftime("%Y:%m:%d")

def generate_date_variants(date_str):
    """
    根据用户输入的日期生成多种格式变种
    输入示例: 2026年01月27日, 2026-1-27, 2026.01.27
    """
    # 尝试解析年、月、日
    match = re.search(r'(\d{4})[.\-年](\d{1,2})[.\-月](\d{1,2})', date_str)
    if not match:
        print("日期格式无法识别，请使用如 2026年01月27日 或 2026-01-27 的格式")
        return []
        
    y, m, d = match.groups()
    m_int, d_int = int(m), int(d)
    
    variants = set()
    
    # 变种 1: 2026年01月27日
    variants.add(f"{y}年{m_int:02d}月{d_int:02d}日")
    # 变种 2: 2026年1月27日
    variants.add(f"{y}年{m_int}月{d_int}日")

    return list(variants)

def build_search_url(page_index, start_time, end_time, keyword):
    """构造搜索 URL"""
    params = {
        "searchtype": "2",
        "page_index": str(page_index),
        "bidSort": "0",
        "buyerName": "",
        "projectId": "",
        "pinMu": "0",
        "bidType": "0",
        "dbselect": "bidx",
        "kw": keyword,
        "start_time": start_time,
        "end_time": end_time,
        "timeType": "6",
        "displayZone": REGION_NAME,
        "zoneId": REGION_ID,
        "pppStatus": "0",
        "agentName": ""
    }
    return params

def fetch_page(url, params=None, with_status=False):
    """Fetch HTML with bounded retries; optionally return a machine-readable status."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive"
    }
    
    max_retries = 3
    last_status = "network_error"
    for attempt in range(max_retries):
        try:
            response = _get_http_session().get(
                url, params=params, headers=headers, timeout=(10, 30)
            )
            response.raise_for_status()
            html = _decode_response(response)
            if "您的访问过于频繁" in html or "访问频繁" in html:
                last_status = "waf"
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return (None, last_status) if with_status else None
            return (html, "ok") if with_status else html
        except requests.HTTPError as e:
            last_status = f"http_{getattr(e.response, 'status_code', 'error')}"
            if getattr(e.response, "status_code", 0) in {400, 401, 403, 404}:
                break
        except Exception as e:
            last_status = "network_error"
            if attempt == max_retries - 1:
                print(f"请求失败 (已重试{max_retries}次): {url}, 错误: {e}")
        if attempt < max_retries - 1:
            print(f"请求失败，正在重试 ({attempt + 1}/{max_retries})...")
            time.sleep(2 ** (attempt + 1))
    return (None, last_status) if with_status else None

def normalize_budget(text):
    """
    归一化预算金额：提取数字并统一转换为“万元”为单位的阿拉伯数字。
    示例: "138,500.00元" -> "13.85 万元"
          "350万元" -> "350.00 万元"
          "0.35亿元" -> "3500.00 万元"
    """
    if not text or text == "未找到" or text == "待采集":
        return text

    # 清洗文本：移除逗号、空格、括号等干扰
    clean_text = text.replace(",", "").replace("，", "").replace(" ", "").replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    
    # 提取数字部分
    match = re.search(r"(\d+\.?\d*)", clean_text)
    if not match:
        return text
    
    val = float(match.group(1))
    
    # 单位判定逻辑：从大到小判定，若含有“亿”或“万”，优先作为单位
    if "亿元" in clean_text or "亿" in clean_text:
        val *= 10000
    elif "万元" in clean_text or "万" in clean_text:
        pass # 已经是万元，无需换算
    elif "元" in clean_text or "￥" in clean_text or "人民币" in clean_text:
        val /= 10000 # 元转万元
    else:
        # 无明确单位时的兜底策略
        if val > 10000: # 假设大数值为“元”
            val /= 10000
            
    return f"{val:.2f} 万元"

def extract_time_only(text):
    """
    精简时间：只保留具体的时刻（HH:MM）。
    示例: "2026年1月13日 09:30:00" -> "09:30"
          "09:00" -> "09:00"
    """
    if not text or text == "未找到" or text == "待采集":
        return text
    
    # 预处理：移除空格
    clean_text = text.replace(" ", "")
    
    # 尝试匹配 HH:MM 或 HH:MM:SS 格式的时刻
    # 兼容多种分隔符如 : ： 点 分
    match = re.search(r"(\d{1,2})[:：点](\d{1,2})", clean_text)
    if not match:
        return text
    
    h, m = match.group(1), match.group(2)
    return f"{int(h):02d}:{int(m):02d}"

def extract_date_str(text):
    """
    提取日期字符串并标准化为 YYYY-MM-DD
    """
    if not text: return None
    clean_text = text.replace(" ", "")
    match = re.search(r"(\d{4})[年.-](\d{1,2})[月.-](\d{1,2})", clean_text)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return None

def extract_region(location, title, purchaser, agency):
    """
    智能识别所属市、县（区）。
    优先级：1. 开标地点 2. 标题/采购人/代理机构
    返回: (地区_市, 地区_县)
    """
    cities = ['太原', '大同', '朔州', '忻州', '阳泉', '吕梁', '晋中', '长治', '晋城', '临汾', '运城']
    
    city_found = "未知市"
    valid_location = location if location and location not in ["未找到", "待采集"] else ""
    core_info = f"{title} {purchaser}"
    full_text = f"{valid_location} {title} {purchaser} {agency}"
    
    # 策略 A：优先从开标/评标地点提取地级市（最准确）
    if valid_location:
        for city in cities:
            if city in valid_location:
                city_found = f"{city}市"
                break
                
    # 策略 B：若地点无结果，再从核心信息（标题、采购人）识别
    if city_found == "未知市":
        for city in cities:
            if city in core_info:
                city_found = f"{city}市"
                break
                
    # 策略 C：若前两者均无结果，从全文搜索
    if city_found == "未知市":
        for city in cities:
            if city in full_text:
                city_found = f"{city}市"
                break
            
    # 提取县/区：同样分为三个优先级进行匹配：1. 地点 2. 核心信息 3. 代理机构
    district_found = ""
    blacklist = ['山西省', '中国', '中共', '共产党', '委员会', '办公室', '采购', '项目', '招标', '代理']
    
    def find_district(text):
        if not text: return ""
        potential_districts = re.findall(r"([\u4e00-\u9fa5]{2,6}(?:县|区))", text)
        if not potential_districts:
            potential_districts = re.findall(r"([\u4e00-\u9fa5]{2,6}市)", text)
            
        for d in potential_districts:
            temp_d = d
            is_major_city = any((temp_d == f"{c}市" or temp_d == c) for c in cities)
            if is_major_city: continue
            
            temp_d = re.sub(r"^(?:山西省|山西|省)", "", temp_d)
            for city in cities:
                temp_d = temp_d.replace(city, "").replace("市", "")
                
            clean_match = re.search(r"([\u4e00-\u9fa5]{2,4})(县|区|市)?$", temp_d)
            if clean_match:
                base_name = clean_match.group(1)
                suffix = clean_match.group(2) or ""
                if suffix == "市": suffix = ""
                
                if not any(b in base_name for b in blacklist) and len(base_name) >= 2:
                    return base_name + suffix
        return ""

    # 按优先级尝试提取县区
    if valid_location:
        district_found = find_district(valid_location)
    if not district_found:
        district_found = find_district(core_info)
    if not district_found:
        district_found = find_district(agency)

    return city_found, district_found

def extract_requirements(soup, text):
    """
    辅助函数：从页面中提取采购需求（简要规格描述）
    """
    req_text = "未找到"
    
    # 方式1：尝试从 bookmark-item 提取（政采云模板常见结构）
    try:
        brief_spec_samp = soup.find('samp', class_=re.compile(r'.*briefSpecificationDesc.*'))
        if brief_spec_samp:
            val = brief_spec_samp.get_text(strip=True)
            if val and len(val) > 10:
                req_text = val[:500]
    except:
        pass
            
    # 方式2：正则兜底
    if req_text == "未找到":
        req_patterns = [
            r"简要规格描述[:：]\s*(.{10,800})",
            r"采购需求[:：]\s*(.{10,800})"
        ]
        for pattern in req_patterns:
            try:
                match = re.search(pattern, text, re.S)
                if match:
                    raw_text = match.group(1).strip()
                    raw_text = re.sub(r'<[^>]+>', '', raw_text)
                    if raw_text and len(raw_text) > 10:
                        req_text = raw_text[:500]
                        break
            except:
                continue
    
    return req_text

def parse_project_details(html_content):
    """从详情页 HTML 中提取详细信息，优先从公告概要中提取"""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    details = {
        "预算限价项目": "未找到",
        "开标具体时间": "未找到",
        "开标日期": "未找到",
        "开标地点": "未找到",
        "采购人名称": "未找到",
        "代理机构": "未找到",
        "项目编号": "未找到",
        "采购方式": "公开招标",  # 默认为公开招标
        "采购需求": "未找到",  # 新增字段
        "标题": "未找到"
    }

    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""

    # 尝试提取标题 (Meta 优先)
    meta_title = soup.find("meta", {"name": "ArticleTitle"})
    if meta_title:
        details["标题"] = meta_title.get("content", "").strip()
    elif page_title:
        details["标题"] = page_title

    # 策略 1：尝试从结构化的“公告概要”中提取（最准确）
    # CCGP 经常在 div.table 或 table#summaryTable 中放置隐藏的概要数据
    summary_container = soup.find('table', id='summaryTable') or soup.find(class_='table')
    if summary_container:
        # 提取该容器内所有的行
        rows = summary_container.find_all('tr')
        for row in rows:
            tds = row.find_all('td')
            # 处理一行两列 (标签: 属性) 或一行四列 (标签1: 属性1, 标签2: 属性2)
            cell_contents = [td.get_text().strip() for td in tds]
            
            # 将 cell_contents 变为键值对列表
            pairs = []
            if len(cell_contents) >= 2:
                for i in range(0, len(cell_contents) - 1, 2):
                    pairs.append((cell_contents[i], cell_contents[i+1]))
            
            for field_label, field_value in pairs:
                if not field_value or field_value == "None": continue
                
                label_clean = field_label.replace(" ", "").replace("　", "")
                
                if "开标时间" in label_clean or "投标截止时间" in label_clean:
                    details["开标具体时间"] = extract_time_only(field_value)
                elif "开标地点" in label_clean or "投标地点" in label_clean:
                    # 优先检查是否是线上获取，若是则跳过（针对此字段）
                    if "线上" in field_value or "网上" in field_value:
                        continue
                    details["开标地点"] = field_value
                elif "采购人名称" in label_clean or (label_clean == "采购人" and "名称" not in label_clean):
                     # 如果只有"采购人"，则也可能是单位名称
                    details["采购人名称"] = field_value
                elif "单位名称" in label_clean and "采购人" in label_clean:
                    details["采购人名称"] = field_value
                elif "代理机构" in label_clean and ("名称" in label_clean or "单位" in label_clean):
                    details["代理机构"] = field_value
                elif "预算金额" in label_clean or "最高限价" in label_clean:
                    details["预算限价项目"] = normalize_budget(field_value)
                elif "采购方式" in label_clean:
                    if "谈判" in field_value: details["采购方式"] = "竞争性谈判"
                    elif "磋商" in field_value: details["采购方式"] = "竞争性磋商"
                    elif "询价" in field_value: details["采购方式"] = "询价"
                    elif "单一来源" in field_value: details["采购方式"] = "单一来源"
                    elif "公开招标" in field_value: details["采购方式"] = "公开招标"

    # 策略 2：正则兜底（如果表格不存在或字段缺失）
    # 移除脚本和样式
    for script_or_style in soup(["script", "style"]):
        script_or_style.decompose()
    text = soup.get_text(separator='\n')

    # 0. 特殊处理：更正公告的时间提取 (High Priority)
    # 如果是更正公告，优先从“更正信息”段落提取时间，并取最后一个（通常是更正后的）
    if "更正" in page_title or "变更" in page_title or "更正" in details.get("标题", ""):
        # 修正正则：
        # 1. 移除 '四、' 以防止表格内容中引用 '四、响应文件提交' 时导致截断
        # 2. 增加 \n\s* 前缀，确保匹配的是章节标题而非行内文本
        # 3. 放宽结尾匹配，移除 \n\s* 强制要求
        # 4. 增加 "更正内容" 作为起始标记
        # 5. [Fixed 2026-02] 强制要求结束标记前必须有换行符，防止表格内容包含 "四、" 导致提前截断
        # 6. [Fixed 2026-02-26] 将更正信息的结束标记限定得更严格，防止匹配到表格内类似 "四、提交投标文件截止时间" 从而导致截断
        regex_pattern = r"(?:(?:二[、\.]\s*)?(?:更正信息|变更信息|更正内容)).*?(?:(?:\n\s*[三四五六七][、\.]\s*(?:其他补充事宜|凡对本次|联系方式|对(?:本次)?采购|对(?:本次)?招标|项目联系方式))|$)"
        correction_section = re.search(regex_pattern, text, re.S)
        if correction_section:
            section_text = correction_section.group(0)
            # 提取所有完整的时间点
            # 修复：增加 (?:\s*(?:上午|下午))? 以匹配 “2026年1月28日上午10:00” 这种格式
            # 修复：增加 '时' 以匹配 "9时00分"
            # 修复：增加 \s* 以匹配 "2026 年" 这种带空格的格式
            # 修复：增加 \s 允许时间中出现空格 (如 "10 : 00")
            # 修复：使用严格的时间格式 \d{1,2}[:：点时]\d{1,2}，防止匹配到纯空格或"更正日期"
            # 修复：增加 \s* 以允许年、月、日之间有空格 (如 "2026 年 2 月 10 日")
            all_times = re.findall(r"(?:20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*(?:上午|下午)?\s*\d{1,2}\s*[:：点时]\s*\d{1,2}(?:\s*分)?)", section_text)
            
            if all_times:
                # 取最后一个，假设为最新更正的时间
                # 需清洗掉“上午”“下午”字样，以便 extract_time_only 处理
                raw_match = all_times[-1].replace("上午", "").replace("下午", "")
                # 清洗空格，统一格式
                raw_time = raw_match.replace("点", ":").replace("分", "").replace("：", ":").replace("时", ":").replace(" ", "") 
                extracted = extract_time_only(raw_time)
                if extracted and extracted != "未找到":
                    details["开标具体时间"] = extracted
                    # extract date as well
                    date_val = extract_date_str(raw_time)
                    if date_val:
                        details["开标日期"] = date_val
                    print(f"    [更正模式] 提取时间: {details['开标具体时间']} 日期: {details['开标日期']}")

    # 1. 预算限价
    # 1. 预算限价
    if details["预算限价项目"] == "未找到":
        budget_patterns = [
            r"(?:预算金额|最高限价|预算金额（元）).*?[:：]\s*([\d,，\.]+)\s*(?:元|万元)?",
            r"预算金额.*?([\d,，\.]+)\s*万元",
            r"项目预算.*?([\d,，\.]+)\s*元"
        ]
        for p in budget_patterns:
            match = re.search(p, text, re.S)
            if match:
                val_str = match.group(1)
                # 修复BUG：必须包含至少一个数字，防止匹配到 ", " 或 "."
                if not re.search(r"\d", val_str):
                    continue
                    
                num_match = re.search(r"([\d,，\.]+)", val_str)
                if num_match:
                    clean_val = num_match.group(1).replace("，", ",")
                    raw_val = f"{clean_val} 万元" if "万元" in match.group(0) else f"{clean_val} 元"
                    details["预算限价项目"] = normalize_budget(raw_val)
                    break

    # 2. 开标具体时间
    if details["开标具体时间"] == "未找到":
        time_patterns = [
            # 增加 \s* 允许年月日的空格
            r"(?:开标时间|截止时间|开标时间（北京时间）).*?[:：]\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*[\d:：点分\s]{4,8})",
            r"(?:开标时间|截止时间).*?[:：]\s*(\d{4}-\d{1,2}-\d{1,2}\s*[\d:：点分\s]{4,8})",
            r"时间[:：]\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*[\d:：点分\s]{4,8})"
        ]
        for p in time_patterns:
            match = re.search(p, text, re.S)
            if match:
                raw_time = match.group(1).replace("点", ":").replace("分", "").replace("：", ":")
                details["开标具体时间"] = extract_time_only(raw_time)
                # 顺便提取日期
                d_val = extract_date_str(raw_time)
                if d_val: details["开标日期"] = d_val
                break

    # 3. 开标地点 (强制要求“开标”、“投标”或“提交投标文件”前缀，防止误抓“获取招标文件地点”)
    # 3. 开标地点 (强制要求“开标”、“投标”或“提交投标文件”前缀，防止误抓“获取招标文件地点”)
    if details["开标地点"] == "未找到" or "线上" in details["开标地点"] or "网上" in details["开标地点"]:
        # 修复：增加 [^”"“] 排除引号，防止匹配到表头如 '开标时间和地点”'
        # 修复：要求匹配内容至少2个字符 {2,}
        location_patterns = [
            r"(?:开标|投标|提交投标文件|响应文件开启)地\s*点[:：]?\s*([^”\"“\n]{2,})(?=\n|；|。|$|（|注：)",
            r"(?:响应文件开启|开标信息|开标).{0,200}?地\s*点[:：]?\s*([^”\"“\n]{2,})(?=\n|；|。|$|（|注：)"
        ]
        
        found_loc = False
        for p in location_patterns:
            # 使用 finditer 遍历所有匹配项，防止第一个匹配项是无效的（如“网址”）导致跳过正确项
            for location_match in re.finditer(p, text, re.S):
                loc = location_match.group(1).strip()
                
                # 黑名单检查
                if (loc and "线上" not in loc and "网上" not in loc and 
                    "时间" not in loc and not loc.startswith("和") and not loc.startswith("及") and
                    "网址" not in loc and "登录" not in loc):
                     details["开标地点"] = loc
                     found_loc = True
                     break
            if found_loc:
                break

    # 4. 采购人名称
    if details["采购人名称"] == "未找到":
        purchaser_section = re.search(r"1\.\s*(?:采购人信息|单位信息|采购人).*?(?=2\.|六、|$)", text, re.S)
        if purchaser_section:
            section_text = purchaser_section.group(0)
            name_match = re.search(r"名\s*称[:：]\s*(.*?)(?=\n|\s|地\s*址|联系方式|$)", section_text)
            if name_match:
                details["采购人名称"] = name_match.group(1).strip()

    # 5. 代理机构信息
    if details["代理机构"] == "未找到":
        agency_section = re.search(r"2\.\s*(?:采购代理机构信息|代理机构信息|代理机构).*?(?=3\.|七、|$)", text, re.S)
        if agency_section:
            section_text = agency_section.group(0)
            name_match = re.search(r"名\s*称[:：]\s*(.*?)(?=\n|\s|地\s*址|联系方式|$)", section_text)
            if name_match:
                details["代理机构"] = name_match.group(1).strip()

    # 6. 项目编号提取
    if details["项目编号"] == "未找到":
        # 常见格式：项目编号：xxxx
        pid_match = re.search(r"(?:项目编号|编号)[:：]\s*([A-Za-z0-9\-\_]+)", text)
        if pid_match:
             details["项目编号"] = pid_match.group(1).strip()
             
    # 7. 采购方式提取 (Regex Fallback)
    # 如果默认为公开招标，尝试从文中提取明确的“采购方式”
    # 优先检查标题 (最强信号)
    if "竞争性谈判" in details.get("标题", "") or "竞争性谈判" in page_title:
         details["采购方式"] = "竞争性谈判"
    elif "竞争性磋商" in details.get("标题", "") or "竞争性磋商" in page_title:
         details["采购方式"] = "竞争性磋商"
    elif "询价" in details.get("标题", "") or "询价" in page_title:
         details["采购方式"] = "询价"
    elif "单一来源" in details.get("标题", "") or "单一来源" in page_title:
         details["采购方式"] = "单一来源"
    
    # 如果标题没找到，再找正文
    if details["采购方式"] == "公开招标":
        # 移除 lookahead 中的 strict constraint，改用更宽松的非贪婪匹配
        pm_match = re.search(r"采购方式[:：]\s*(\S{2,10})", text)
        if pm_match:
            val = pm_match.group(1)
            if "谈判" in val: details["采购方式"] = "竞争性谈判"
            elif "磋商" in val: details["采购方式"] = "竞争性磋商"
            elif "询价" in val: details["采购方式"] = "询价"
            elif "单一来源" in val: details["采购方式"] = "单一来源"
            elif "单一来源" in val: details["采购方式"] = "单一来源"
            elif "公开招标" in val: details["采购方式"] = "公开招标"

    # 8. 更正公告特殊处理：回溯原始公告 (Backfill Logic)
    # 如果是更正公告且采购方式未提取成功（或为默认），尝试寻找原公告链接
    is_correction = (
        "更正" in details.get("标题", "")
        or "变更" in details.get("标题", "")
        or "更正" in page_title
        or "变更" in page_title
    )
    
    if is_correction and details["采购方式"] == "公开招标":
        # 寻找原公告链接
        # 常见文本： "原公告地址" 或 "首次公告"
        origin_link = None
        for a in soup.find_all('a', href=True):
            if "原公告" in a.get_text() or "首次公告" in a.get_text():
                origin_link = a['href']
                break
        
        if origin_link:
            print(f"    [更正回溯] 发现原公告链接: {origin_link}，尝试回溯采集采购方式...")
            try:
                # 处理相对路径 (如果需要) - CCGP 通常是绝对路径或同级相对
                # 简单起见，假设是绝对路径或完整URL, 实际情况如果出错则忽略
                if not origin_link.startswith("http"):
                    origin_link = urljoin("http://www.ccgp.gov.cn/", origin_link)
                
                # Fetch original content
                # 注意：这里调用 fetch_page 会产生额外的网络请求
                # 为防止递归死循环，只做一层回溯，且不完全递归 parse_project_details
                orig_html = fetch_page(origin_link)
                if orig_html:
                    orig_soup = BeautifulSoup(orig_html, 'html.parser')
                    orig_text = orig_soup.get_text(separator='\n')
                    
                    # 使用相同的逻辑提取
                    found_pm = None
                    # 1. Title
                    if orig_soup.title:
                        t = orig_soup.title.get_text(" ", strip=True)
                        if "竞争性谈判" in t: found_pm = "竞争性谈判"
                        elif "竞争性磋商" in t: found_pm = "竞争性磋商"
                        elif "询价" in t: found_pm = "询价"
                        elif "单一来源" in t: found_pm = "单一来源"
                    
                    # 2. Regex
                    if not found_pm:
                         pm_match = re.search(r"采购方式[:：]\s*(\S{2,10})", orig_text)
                         if pm_match:
                            val = pm_match.group(1)
                            if "谈判" in val: found_pm = "竞争性谈判"
                            elif "磋商" in val: found_pm = "竞争性磋商"
                            elif "询价" in val: found_pm = "询价"
                            elif "单一来源" in val: found_pm = "单一来源"
                    
                    if found_pm:
                        details["采购方式"] = found_pm
                        print(f"    [更正回溯] 成功从原公告提取采购方式: {found_pm}")
                    
                    # 同时回溯提取采购需求 (Step 1309)
                    backfill_req = extract_requirements(orig_soup, orig_text)
                    if backfill_req != "未找到":
                         details["采购需求"] = backfill_req
                         print(f"    [更正回溯] 成功从原公告提取采购需求")

            except Exception as e:
                print(f"    [更正回溯] 失败: {e}")

    # 9. 提取"采购需求" (简要规格描述) - 如果回溯没有填补的话
    if details["采购需求"] == "未找到":
        details["采购需求"] = extract_requirements(soup, text)

    return details



# Actual plan:
# 1. Insert `extract_requirements` function before `parse_project_details`.
# 2. Modify `parse_project_details`:
#    - Inside the "Correction Backfill" block (around line 517), add logic to extract requirements from `orig_soup`/`orig_text`.
#    - At the end of function, use `extract_requirements` for the current page details.


def find_original_project(project_code, current_url):
    """
    根据项目编号搜索并尝试找到最早的原始公告（倾向于招标公告）
    """
    if not project_code or project_code == "未找到":
        return None
        
    print(f"    正在回溯搜索项目编号: {project_code} ...")
    start_time, end_time = get_date_range()
    
    # 扩大搜索范围，防止原始公告太久远（比如半年前）
    # 这里简单起见，使用更宽的时间范围或多次尝试，目前先沿用全局的时间配置，
    # 但实际场景中原始公告可能早于 90 天，这里可能需要调整 DAYS_AGO 或单独传参
    # 暂时先用当前配置
    
    params = build_search_url(1, start_time, end_time, project_code)
    html = fetch_page(BASE_URL, params=params)
    
    if not html: return None
    
    soup = BeautifulSoup(html, 'html.parser')
    list_items = soup.select('ul.vT-srch-result-list-bid li')
    if not list_items:
        list_items = soup.select('.v9-search-result-list li')
        
    candidates = []
    for item in list_items:
        link_tag = item.find('a')
        if not link_tag: continue
        
        href = link_tag.get('href', '').strip()
        if not href.startswith('http'):
             href = "http://www.ccgp.gov.cn" + href if href.startswith('/') else "http://www.ccgp.gov.cn/" + href
             
        if href == current_url:
            continue
            
        title = link_tag.get_text().strip()
        
        # 提取发布时间用于排序
        pub_date = "0000-00-00"
        date_match = re.search(r'\d{4}\.\d{2}\.\d{2}', item.get_text())
        if date_match:
            pub_date = date_match.group(0)
            
        candidates.append({
            "url": href,
            "title": title,
            "date": pub_date
        })
    
    if not candidates:
        return None
        
    # 按日期排序，取最早的
    candidates.sort(key=lambda x: x['date'])
    
    # 优先找标题里不带“更正”、“结果”、“变更”的
    original_candidates = [c for c in candidates if not any(k in c['title'] for k in ['更正', '变更', '结果', '终止'])]
    
    target_project = None
    if original_candidates:
        target_project = original_candidates[0]
    else:
        # 如果都是更正，那取最早的那个更正可能也没用，但也试一试
        target_project = candidates[0]
        
    print(f"    -> 找到疑似原始公告: {target_project['title']} ({target_project['date']})")
    return target_project['url']


def fetch_and_parse_details(item, deadline=None):
    """多线程调用的包装函数"""
    url = item['链接']
    if deadline and time.monotonic() >= deadline:
        item["_detail_status"] = "deadline_exceeded"
        return item
    if not url.startswith("http"):
        item["_detail_status"] = "invalid_url"
        return item

    cached_details = _get_cached_details(url)
    if cached_details:
        item.update(cached_details)
        item["_detail_status"] = "cached"
        return item

    html, fetch_status = fetch_page(url, with_status=True)
    if html:
        try:
            detail_data = parse_project_details(html)
        except Exception as exc:
            item["_detail_status"] = "parse_error"
            item["_detail_error"] = str(exc)
            print(f"    详情页解析失败: {item['标题'][:20]}... {exc}")
            return item
        item.update(detail_data)
        item["_detail_status"] = "ok"
        _cache_details(url, detail_data, html)
        
        # --- 更正公告回填逻辑 ---
        # 只要是更正公告，且缺任意一项关键信息（预算、地点、采购需求），都尝试回溯
        is_correction = "更正" in item.get('标题', '') or "变更" in item.get('标题', '')
        
        needs_backfill = (item.get("预算限价项目") == "未找到" or 
                          item.get("开标地点") == "未找到" or
                          item.get("采购需求") == "未找到")

        if is_correction and needs_backfill:
            
            project_code = item.get("项目编号")
            if project_code and project_code != "未找到":
                original_url = find_original_project(project_code, url)
                if original_url:
                    original_html = fetch_page(original_url)
                    if original_html:
                        original_details = parse_project_details(original_html)
                        
                        if item.get("预算限价项目") == "未找到" and original_details.get("预算限价项目") != "未找到":
                            item["预算限价项目"] = original_details["预算限价项目"] + " (来自原始公告)"
                            print(f"    [回填成功] 预算: {item['预算限价项目']}")
                            
                        if item.get("开标地点") == "未找到" and original_details.get("开标地点") != "未找到":
                            item["开标地点"] = original_details["开标地点"] + " (来自原始公告)"
                            print(f"    [回填成功] 地点: {item['开标地点']}")
                            
                        if item.get("采购需求") == "未找到" and original_details.get("采购需求") != "未找到":
                            item["采购需求"] = original_details["采购需求"]
                            print(f"    [回填成功] 需求: 已获取")
        # ------------------------
        # ------------------------

        if detail_data.get("预算限价项目") != "未找到":
             print(f"    成功提取详情: {item['标题'][:20]}...")
    else:
        item["_detail_status"] = fetch_status
        print(f"    详情页请求失败: {item['标题'][:20]}...")
    return item



def scrape():
    target_date_input = input("请输入开标日期 (例如 2026年01月27日): ").strip()
    if not target_date_input:
        return

    # 1. 生成日期变种
    date_variants = generate_date_variants(target_date_input)
    if not date_variants:
        return
        
    print(f"将搜索以下日期格式: {date_variants}")
    print(f"注意: 仅在服务器搜索日期，信息化筛选将在本地进行。")
    
    start_time, end_time = get_date_range()
    
    # 2. 采集阶段：获取所有匹配日期的原始数据 (不进行任何主题筛选)
    raw_results = {} # 使用字典去重，key为URL
    
    for date_str in date_variants:
        full_keyword = f"开标时间：{date_str}"
        print(f"\n--- 正在采集关键词: {full_keyword} ---")
        
        for page in range(1, MAX_PAGES + 1):
            # 关键点：这里只传日期关键词，不传信息化关键词
            params = build_search_url(page, start_time, end_time, full_keyword)
            print(f"  正在请求: {params['kw']} ...")
            # print(f"  完整URL: {BASE_URL}?{quote(str(params))}") # Debug
            
            html = fetch_page(BASE_URL, params=params)
            
            if not html:
                continue
            
            if "您的访问过于频繁" in html:
                print("警告: 访问过于频繁 (WAF触发)，停止当前关键词搜索。")
                break

            soup = BeautifulSoup(html, 'html.parser')
            list_items = soup.select('ul.vT-srch-result-list-bid li')
            
            if not list_items:
                list_items = soup.select('.v9-search-result-list li')
            
            if not list_items:
                print(f"  页 {page}: 未找到数据 (或已到末尾)")
                if page == 1:
                    with open("debug.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    print("  已保存 debug.html 以便分析原因 (可能无数据或被拦截)")
                break
                
            print(f"  页 {page}: 找到 {len(list_items)} 条原始数据")
            
            has_new_data = False
            for item in list_items:
                link_tag = item.find('a')
                if not link_tag: continue
                
                title = link_tag.get_text().strip()
                href = link_tag.get('href', '').strip()
                
                if not href.startswith('http'):
                    href = "http://www.ccgp.gov.cn" + href if href.startswith('/') else "http://www.ccgp.gov.cn/" + href
                
                if href not in raw_results:
                    # 提取发布时间
                    pub_date = "N/A"
                    text_content = item.get_text()
                    date_match = re.search(r'\d{4}\.\d{2}\.\d{2}', text_content)
                    if date_match:
                        pub_date = date_match.group(0)
                        
                    raw_results[href] = {
                        "标题": title,
                        "链接": href,
                        "发布时间": pub_date,
                        "匹配日期格式": date_str,
                        "疑似开标时间": date_str,
                        "预算限价项目": "待采集",
                        "开标具体时间": "待采集",
                        "开标地点": "待采集",
                        "采购人名称": "待采集",
                        "代理机构": "待采集",
                        "采购方式": "待采集"
                    }
                    has_new_data = True
            
            # 智能停止：如果当前页数据量小于20，说明已到最后一页
            if len(list_items) < 20:
                print(f"  检测到最后一页（仅 {len(list_items)} 条），停止翻页。")
                break
            
            time.sleep(5) # 增加延迟以防 WAF 拦截
    print(f"\n采集结束。共采集到 {len(raw_results)} 条包含目标日期的原始记录。")

# 全局模型实例：同一批多日期任务只加载一次，任务结束后由调用方释放。
MODEL = None
ANCHOR_EMBEDDINGS = None
SEMANTIC_RUNTIME_VALIDATED = False

ANCHOR_SENTENCES = [
    "软件系统开发与定制", "应用平台建设运营", "业务信息系统升级", "电子政务管理系统", "医务绩效考核软件系统",
    "大数据平台与数据分析", "云计算服务与云平台", "数据库与数据治理", "算法模型与人工智能应用", "文书档案数字化管理系统",
    "智能监管执法平台", "指挥调度信息管理平台", "物联网感知与智能控制", "智慧应用与数字化平台",
    "智慧信息平台", "智慧监管平台", "信息化系统建设", "视频督察与视频会议系统", "医院核心业务信息系统HIS",
    "机房智能化建设工程", "计算机网络系统集成", "弱电智能化系统工程", "网络信息安全防护系统", "高性能虚拟化平台",
    "系统集成实施服务", "软件运维技术支持", "信息化项目测评与监理", "网络安全等级保护", "沉浸式数字展厅体验系统", "城市运行数据采集系统",
    "档案数字化加工系统", "电子政务外网建设", "远程会诊平台软件", "在线教学平台",
    "文物与古建筑数字化保护", "文化遗产数字资源建设", "实景三维建模数字化项目",
    "视频监控联网系统", "雪亮工程信息化", "智能安防系统",
    "网络安全日志审计系统", "数据库运维审计系统", "网络流量分析与监测",
    "互联网数据采集与分析服务", "行业大数据应用服务", "公共数据治理与服务",
    "全流程数智化监管平台", "餐饮食品安全智慧监管", "数智化管理系统建设",
]


def get_model():
    global MODEL
    if MODEL is not None:
        return MODEL
    with _model_lock:
        if MODEL is not None:
            return MODEL
        from sentence_transformers import SentenceTransformer

        search_paths = [
            "/app/model_data",
            os.path.join(PROJECT_DIR, "model_data"),
            "model_data",
        ]
        model_path = next((path for path in search_paths if os.path.exists(path)), None)
        if not model_path:
            raise RuntimeError("未找到本地语义模型 model_data，禁止运行时联网下载")
        print(f"正在加载本地语义模型: {model_path}")
        MODEL = SentenceTransformer(model_path)
    return MODEL


def validate_semantic_runtime():
    global SEMANTIC_RUNTIME_VALIDATED
    if SEMANTIC_RUNTIME_VALIDATED:
        return get_model()

    import numpy as np
    import torch

    if int(np.__version__.split(".")[0]) >= 2 and torch.__version__.startswith("2.2."):
        raise RuntimeError(
            f"NumPy {np.__version__} 与 PyTorch {torch.__version__} 不兼容"
        )
    try:
        torch.tensor([1.0]).numpy()
    except Exception as exc:
        raise RuntimeError(f"PyTorch 无法调用 NumPy: {exc}") from exc

    model = get_model()
    probe = model.encode(
        ["语义模型健康检查"], batch_size=1, show_progress_bar=False, convert_to_numpy=True
    )
    if getattr(probe, "shape", (0,))[0] != 1:
        raise RuntimeError("语义模型健康检查未返回有效向量")
    SEMANTIC_RUNTIME_VALIDATED = True
    return model


def get_anchor_embeddings(model):
    global ANCHOR_EMBEDDINGS
    if ANCHOR_EMBEDDINGS is not None:
        return ANCHOR_EMBEDDINGS
    with _model_lock:
        if ANCHOR_EMBEDDINGS is None:
            ANCHOR_EMBEDDINGS = model.encode(
                ANCHOR_SENTENCES,
                batch_size=16,
                show_progress_bar=False,
                convert_to_tensor=True,
            )
    return ANCHOR_EMBEDDINGS


def release_model():
    global MODEL, ANCHOR_EMBEDDINGS, SEMANTIC_RUNTIME_VALIDATED
    with _model_lock:
        MODEL = None
        ANCHOR_EMBEDDINGS = None
        SEMANTIC_RUNTIME_VALIDATED = False
    gc.collect()
    if os.name == "posix":
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

def validate_result_file(filepath, require_complete=True):
    if not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        return False, "文件不存在或为空"
    try:
        with pd.ExcelFile(filepath) as excel:
            frame = pd.read_excel(excel, sheet_name=0)
            metadata = (
                pd.read_excel(excel, sheet_name="采集元数据")
                if "采集元数据" in excel.sheet_names else None
            )
    except Exception as exc:
        return False, f"Excel 无法读取: {exc}"
    if frame.empty:
        return False, "Excel 没有数据行"
    missing = REQUIRED_RESULT_COLUMNS - set(frame.columns)
    if missing:
        return False, f"缺少必要字段: {', '.join(sorted(missing))}"
    classifications = set(frame["是否信息化"].dropna().astype(str).unique())
    if not classifications or not classifications.issubset({"是", "否"}):
        return False, "是否信息化字段存在无效值"
    scores = pd.to_numeric(frame["语义匹配度"], errors="coerce")
    if scores.isna().any():
        return False, "语义匹配度存在非数字值"
    if metadata is not None and not metadata.empty:
        status = str(metadata.iloc[0].get("status", ""))
        if require_complete and status != "success":
            return False, f"上次采集状态为 {status or 'unknown'}"
    return True, ""


def _extract_publish_date(search_item):
    text = search_item.get_text(" ", strip=True)
    match = re.search(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?", text)
    if not match:
        return "未找到"
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _page_explicitly_has_no_results(html):
    normalized = re.sub(r"\s+", "", html or "")
    return any(marker in normalized for marker in ("没有找到相关", "暂无相关", "未检索到", "共0条"))


def run_scraper_for_date(target_date_str, callback=None):
    """Collect, classify and atomically save one opening date."""
    def log(message):
        print(message)
        if callback:
            callback(message)

    date_variants = generate_date_variants(target_date_str)
    if not date_variants:
        message = f"日期格式错误: {target_date_str}"
        log(message)
        return {"status": "failed", "total": 0, "file": None, "error": message}

    log(f"开始采集日期: {target_date_str} (匹配格式: {date_variants})")
    deadline = time.monotonic() + MAX_RUN_SECONDS
    start_time, end_time = get_date_range()
    raw_results = {}
    source_errors = []
    successful_keywords = 0
    search_requests = 0
    last_search_request = 0.0

    for date_str in date_variants:
        for prefix in ("开标时间：", "开启时间："):
            full_keyword = f"{prefix}{date_str}"
            log(f"正在采集关键词: {full_keyword}")
            keyword_ok = False
            for page in range(1, MAX_PAGES + 1):
                if time.monotonic() >= deadline:
                    message = f"单日期采集超过 {MAX_RUN_SECONDS // 60} 分钟限制"
                    log(message)
                    return {
                        "status": "failed", "total": 0, "file": None, "error": message,
                        "metrics": {"raw_records": len(raw_results), "search_requests": search_requests},
                    }
                elapsed = time.monotonic() - last_search_request
                if search_requests and elapsed < 1.5:
                    time.sleep(1.5 - elapsed)
                params = build_search_url(page, start_time, end_time, full_keyword)
                html, fetch_status = fetch_page(BASE_URL, params=params, with_status=True)
                search_requests += 1
                last_search_request = time.monotonic()

                if not html:
                    message = f"关键词 {full_keyword} 第 {page} 页请求失败: {fetch_status}"
                    source_errors.append(message)
                    log(message)
                    break

                soup = BeautifulSoup(html, "html.parser")
                list_items = soup.select("ul.vT-srch-result-list-bid li")
                if not list_items:
                    list_items = soup.select(".v9-search-result-list li")

                if not list_items:
                    if page == 1 and not _page_explicitly_has_no_results(html):
                        message = f"关键词 {full_keyword} 返回页面结构异常，未找到结果列表"
                        source_errors.append(message)
                        log(message)
                    else:
                        keyword_ok = True
                    break

                keyword_ok = True
                for search_item in list_items:
                    link_tag = search_item.find("a")
                    if not link_tag:
                        continue
                    title = link_tag.get_text(" ", strip=True)
                    href = urljoin("http://www.ccgp.gov.cn/", link_tag.get("href", "").strip())
                    if not href or href in raw_results:
                        continue
                    raw_results[href] = {
                        "标题": title,
                        "链接": href,
                        "发布时间": _extract_publish_date(search_item),
                        "匹配日期格式": date_str,
                        "疑似开标时间": date_str,
                        "预算限价项目": "待采集",
                        "开标具体时间": "待采集",
                        "开标地点": "待采集",
                        "采购人名称": "待采集",
                        "代理机构": "待采集",
                        "采购方式": "待采集",
                    }
                if len(list_items) < 20:
                    break
            if keyword_ok:
                successful_keywords += 1

    log(f"共采集到 {len(raw_results)} 条原始记录。")
    if not raw_results:
        if source_errors:
            message = "；".join(source_errors[:3])
            return {
                "status": "failed", "total": 0, "file": None,
                "error": message,
                "metrics": {"search_requests": search_requests, "source_errors": source_errors},
            }
        log("没有找到数据。")
        return {
            "status": "no_data", "total": 0, "file": None,
            "metrics": {"search_requests": search_requests},
        }

    log("正在进行语义分析...")
    try:
        model = validate_semantic_runtime()
        anchor_embeddings = get_anchor_embeddings(model)
        titles = [item["标题"] for item in raw_results.values()]
        title_embeddings = model.encode(
            titles, batch_size=16, show_progress_bar=False, convert_to_tensor=True
        )
        from sentence_transformers import util

        final_list = []
        for index, item in enumerate(raw_results.values()):
            title = item["标题"]
            if any(keyword in title for keyword in STRONG_IT_KEYWORDS):
                item["是否信息化"] = "是"
                item["语义匹配度"] = 1.0
            else:
                score = float(util.cos_sim(title_embeddings[index], anchor_embeddings).max())
                item["语义匹配度"] = score
                item["是否信息化"] = "是" if score > 0.61 else "否"
            final_list.append(item)
    except Exception as exc:
        message = f"语义分析失败，任务终止: {exc}"
        log(message)
        return {
            "status": "failed", "total": 0, "file": None, "error": message,
            "metrics": {"raw_records": len(raw_results), "search_requests": search_requests},
        }

    log(f"正在对 {len(final_list)} 个项目进行深度采集...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda item: fetch_and_parse_details(item, deadline), final_list))

    detail_failures = 0
    cache_hits = 0
    for item in final_list:
        status = item.get("_detail_status")
        if status == "cached":
            cache_hits += 1
        elif status != "ok":
            detail_failures += 1
        city, district = extract_region(
            item.get("开标地点", ""), item.get("标题", ""),
            item.get("采购人名称", ""), item.get("代理机构", "")
        )
        item["地区（市）"] = city
        item["地区（县）"] = district

    warnings = []
    result_status = "success"
    if source_errors or detail_failures:
        result_status = "partial"
        if source_errors:
            warnings.append(f"{len(source_errors)} 个搜索请求异常")
        if detail_failures:
            warnings.append(f"{detail_failures} 个详情页未完整解析")

    target_date_norm = extract_date_str(target_date_str)
    dropped_count = 0
    if target_date_norm:
        excluded_codes = set()
        drop_indices = set()
        for index, item in enumerate(final_list):
            item_date = item.get("开标日期")
            if item_date and item_date != "未找到" and item_date != target_date_norm:
                project_code = item.get("项目编号")
                if project_code and project_code != "未找到":
                    excluded_codes.add(project_code)
                else:
                    drop_indices.add(index)
        filtered = []
        for index, item in enumerate(final_list):
            project_code = item.get("项目编号")
            if index in drop_indices or (project_code and project_code in excluded_codes):
                dropped_count += 1
                continue
            filtered.append(item)
        final_list = filtered
        if dropped_count:
            log(f"日期变更关联剔除 {dropped_count} 条记录。")

    if not final_list:
        log("筛选后没有可保存数据。")
        return {"status": "no_data", "total": 0, "file": None}

    frame = pd.DataFrame(final_list)
    sort_columns = ["是否信息化", "地区（市）", "地区（县）", "开标具体时间"]
    frame = frame.sort_values(by=sort_columns, ascending=[False, True, True, True])
    columns_order = [
        "标题", "是否信息化", "采购方式", "语义匹配度", "地区（市）", "地区（县）",
        "预算限价项目", "开标具体时间", "开标地点", "采购需求", "发布时间",
        "代理机构", "采购人名称", "链接",
    ]
    frame = frame[columns_order]

    safe_date = target_date_str.replace(":", "").replace("/", "").replace("\\", "")
    filename = os.path.join(OUTPUT_DIR, f"shanxi_informatization_{safe_date}.xlsx")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    temp_filename = os.path.join(
        OUTPUT_DIR, f".{os.path.basename(filename)}.{uuid.uuid4().hex}.tmp.xlsx"
    )
    try:
        with pd.ExcelWriter(temp_filename, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="山西信息化项目")
            pd.DataFrame([{
                "status": result_status,
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "raw_records": len(raw_results),
                "saved_records": len(frame),
                "detail_failures": detail_failures,
                "source_errors": len(source_errors),
            }]).to_excel(writer, index=False, sheet_name="采集元数据")
            worksheet = writer.sheets["山西信息化项目"]
            writer.sheets["采集元数据"].sheet_state = "hidden"
            alignment = Alignment(wrap_text=True, vertical="top", horizontal="left")
            for row in worksheet.iter_rows(min_row=1, max_row=len(frame) + 1):
                for cell in row:
                    cell.alignment = alignment
        valid, reason = validate_result_file(temp_filename, require_complete=False)
        if not valid:
            raise ValueError(f"结果文件校验失败: {reason}")
        os.replace(temp_filename, filename)
    except Exception as exc:
        if os.path.exists(temp_filename):
            try:
                os.remove(temp_filename)
            except OSError:
                pass
        message = f"保存失败: {exc}"
        log(message)
        return {"status": "failed", "total": len(final_list), "file": None, "error": message}

    log(f"保存成功: {filename}")
    return {
        "status": result_status,
        "total": len(final_list),
        "file": filename,
        "warnings": warnings,
        "metrics": {
            "raw_records": len(raw_results),
            "search_requests": search_requests,
            "successful_keywords": successful_keywords,
            "source_errors": source_errors,
            "detail_failures": detail_failures,
            "cache_hits": cache_hits,
            "dropped_by_date_change": dropped_count,
        },
    }

def scrape():
    target_date_input = input("请输入开标日期 (例如 2026年01月27日): ").strip()
    if not target_date_input:
        return
    
    # 简单的控制台回调
    def console_log(msg):
        print(f"[Scraper] {msg}")
        
    run_scraper_for_date(target_date_input, callback=console_log)


if __name__ == "__main__":
    scrape()
