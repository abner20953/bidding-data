import requests
from bs4 import BeautifulSoup
import datetime
import time
import re
import concurrent.futures
import pandas as pd
import os
from openpyxl.styles import Alignment


BASE_URL = "http://search.ccgp.gov.cn/bxsearch"
REGION_NAME = "山西"
REGION_ID = "14"
DAYS_AGO = 90
MAX_PAGES = 100  # 安全上限（自动分页会提前停止）
OUTPUT_DIR = "results"



# 高置信度特征词（命中即判定为信息化，解决长标题语义稀释问题）
# 只要项目标题包含这些核心技术词汇，直接判定为“是”
STRONG_IT_KEYWORDS = [
    '信息化', '软件', '大数据', '云计算', '物联网', '人工智能',
    '智慧监管', '智慧平台', '智能平台', '智慧信息', '网络安全',
    '数据中心', '调度中心', '监控系统',
    # 组合词替代单字，防止误判（如“开发区”、“空调系统”）
    '软件开发', '系统开发', '平台开发', '网站建设', 'APP开发', '小程序', '公众号',
    '信息系统', '管理系统', '办公系统', '操作系统', '运维服务'
]



if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

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

def fetch_page(url, params=None):
    """发送请求获取页面内容 (带重试机制)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive"
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response.text
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"请求失败，正在重试 ({attempt + 1}/{max_retries})...")
                time.sleep(2)
            else:
                print(f"请求失败 (已重试{max_retries}次): {url}, 错误: {e}")
                return None

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
    
    # 尝试匹配 HH:MM 或 HH:MM:SS 格式的时刻
    # 兼容多种分隔符如 : ： 点 分
    match = re.search(r"(\d{1,2})[:：点](\d{1,2})", text)
    if not match:
        return text
    
    h, m = match.group(1), match.group(2)
    return f"{int(h):02d}:{int(m):02d}"

def extract_date_str(text):
    """
    提取日期字符串并标准化为 YYYY-MM-DD
    """
    if not text: return None
    match = re.search(r"(\d{4})[年.-](\d{1,2})[月.-](\d{1,2})", text)
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
    
    # 构建搜索池
    search_pool = ""
    if location and location != "未找到" and location != "待采集":
        search_pool = location
    else:
        search_pool = f"{title} {purchaser} {agency}"
    
    city_found = "未知市"
    # 策略 A：优先从标题和采购人（核心信息）识别地级市
    core_info = f"{title} {purchaser}"
    for city in cities:
        if city in core_info:
            city_found = f"{city}市"
            break
    
    # 策略 B：若核心信息无结果，再从全文（包含开标地点和代理机构）识别
    if city_found == "未知市":
        for city in cities:
            if city in search_pool:
                city_found = f"{city}市"
                break
            
    # 尝试匹配县/区/县级市
    # 采用更严谨的正则，并先寻找县/区
    potential_districts = re.findall(r"([\u4e00-\u9fa5]{2,6}(?:县|区))", search_pool)
    # 如果没找到县区，再找县级市（需排除地级市）
    if not potential_districts:
        potential_districts = re.findall(r"([\u4e00-\u9fa5]{2,6}市)", search_pool)
        
    district_found = ""
    # 噪声黑名单
    blacklist = ['山西省', '中国', '中共', '共产党', '委员会', '办公室', '采购', '项目', '招标', '代理']
    
    # 合并搜索池，核心信息中的匹配项排在前面
    all_districts = [d for d in potential_districts if d in core_info] + \
                    [d for d in potential_districts if d not in core_info]

    for d in all_districts:
        temp_d = d
        # 1. 检查是否为地级市
        is_major_city = False
        for city in cities:
            if temp_d == f"{city}市" or temp_d == city:
                is_major_city = True
                break
        if is_major_city: continue

        # 2. 移除地理层级前缀干扰
        # 先移除典型的“xx省”、“xx市”前缀
        temp_d = re.sub(r"^(?:山西省|山西|省)", "", temp_d)
        for city in cities:
            temp_d = temp_d.replace(city, "").replace("市", "")
        
        # 3. 最终清洗：仅保留 县/区 字样前的核心字
        clean_match = re.search(r"([\u4e00-\u9fa5]{2,4})(县|区|市)?$", temp_d)
        if clean_match:
            base_name = clean_match.group(1)
            suffix = clean_match.group(2) or ""
            # 如果后缀是“市”，按用户要求不填（即县级市只留名）
            if suffix == "市": suffix = ""
            
            # 过滤黑名单
            if not any(b in base_name for b in blacklist) and len(base_name) >= 2:
                district_found = base_name + suffix
                break

    return city_found, district_found

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
        "项目编号": "未找到"
    }

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

    # 策略 2：正则兜底（如果表格不存在或字段缺失）
    # 移除脚本和样式
    for script_or_style in soup(["script", "style"]):
        script_or_style.decompose()
    text = soup.get_text(separator='\n')

    # 0. 特殊处理：更正公告的时间提取 (High Priority)
    # 如果是更正公告，优先从“更正信息”段落提取时间，并取最后一个（通常是更正后的）
    if "更正" in soup.title.string or "变更" in soup.title.string or "更正" in details.get("标题", ""):
        # 修正正则：
        # 1. 移除 '四、' 以防止表格内容中引用 '四、响应文件提交' 时导致截断
        # 2. 增加 \n\s* 前缀，确保匹配的是章节标题而非行内文本
        correction_section = re.search(r"(?:更正信息|变更信息).*?(?=\n\s*三[、\.]|\n\s*其他补充事宜|$)", text, re.S)
        if correction_section:
            section_text = correction_section.group(0)
            # 提取所有完整的时间点
            # 修复：增加 (?:\s*(?:上午|下午))? 以匹配 “2026年1月28日上午10:00” 这种格式
            # 修复：增加 '时' 以匹配 "9时00分"
            all_times = re.findall(r"(?:20\d{2}年\d{1,2}月\d{1,2}日\s*(?:上午|下午)?\s*[\d:：点分时]{4,8})", section_text)
            
            if all_times:
                # 取最后一个，假设为最新更正的时间
                # 需清洗掉“上午”“下午”字样，以便 extract_time_only 处理
                raw_match = all_times[-1].replace("上午", "").replace("下午", "")
                raw_time = raw_match.replace("点", ":").replace("分", "").replace("：", ":").replace("时", ":") # Handle '时' replacement too
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
            r"(?:开标时间|截止时间|开标时间（北京时间）).*?[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日\s*[\d:：点分]{4,8})",
            r"(?:开标时间|截止时间).*?[:：]\s*(\d{4}-\d{1,2}-\d{1,2}\s*[\d:：点分]{4,8})",
            r"时间[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日\s*[\d:：点分]{4,8})"
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
    if details["开标地点"] == "未找到" or "线上" in details["开标地点"] or "网上" in details["开标地点"]:
        location_patterns = [
            r"(?:开标|投标|提交投标文件|响应文件开启)地\s*点[:：]?\s*(.*?)(?=\n|；|。|$|（|注：)",
            r"(?:响应文件开启|开标信息|开标).{0,200}?地\s*点[:：]?\s*(.*?)(?=\n|；|。|$|（|注：)"
        ]
        
        for p in location_patterns:
            location_match = re.search(p, text, re.S)
            if location_match:
                loc = location_match.group(1).strip()
                # 修复BUG：增加黑名单过滤
                # 1. 过滤包含“时间”的（误抓取了时间行）
                # 2. 过滤以“和”、“及”开头的（误抓了“地点和方式”中的“和方式”）
                if (loc and "线上" not in loc and "网上" not in loc and 
                    "时间" not in loc and not loc.startswith("和") and not loc.startswith("及")):
                     details["开标地点"] = loc
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

    return details

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


def fetch_and_parse_details(item):
    """多线程调用的包装函数"""
    url = item['链接']
    if not url.startswith("http"):
        return item
    
    html = fetch_page(url)
    if html:
        detail_data = parse_project_details(html)
        item.update(detail_data)
        
        # --- 更正公告回填逻辑 ---
        if item.get('标题', '').endswith("更正公告") and \
           (item.get("预算限价项目") == "未找到" or item.get("开标地点") == "未找到"):
            
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
        # ------------------------

        if detail_data.get("预算限价项目") != "未找到":
             print(f"    成功提取详情: {item['标题'][:20]}...")
    else:
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
                        "代理机构": "待采集"
                    }
                    has_new_data = True
            
            # 智能停止：如果当前页数据量小于20，说明已到最后一页
            if len(list_items) < 20:
                print(f"  检测到最后一页（仅 {len(list_items)} 条），停止翻页。")
                break
            
            time.sleep(5) # 增加延迟以防 WAF 拦截
    print(f"\n采集结束。共采集到 {len(raw_results)} 条包含目标日期的原始记录。")

# 全局模型实例
MODEL = None

def get_model():
    global MODEL
    if MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            
            # 定义模型搜索路径优先级
            # 1. Docker 容器内固定路径
            # 2. 当前项目目录下的 model_data (本地开发用)
            search_paths = [
                '/app/model_data',
                os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model_data'),
                'model_data'
            ]
            
            model_path = None
            for path in search_paths:
                if os.path.exists(path):
                    model_path = path
                    break
            
            if model_path:
                print(f"正在加载本地语义模型: {model_path}")
                MODEL = SentenceTransformer(model_path)
            else:
                # 严格禁止自动联网下载，抛出错误提示用户手动准备
                error_msg = (
                    "错误: 未找到本地模型文件！\n"
                    "应用程序已配置为严格离线模式，禁止运行时下载。\n"
                    "请运行 'python download_model.py' 手动下载模型 (BAAI/bge-small-zh-v1.5) 到 'model_data' 目录，"
                    "或者确保 Docker 镜像构建时已包含 '/app/model_data'。"
                )
                print(error_msg)
                raise RuntimeError(error_msg)
                 
        except ImportError:
            print("错误: 未找到 sentence-transformers 库")
            return None
        except Exception as e:
            print(f"模型加载失败: {e}")
            return None
    return MODEL

def run_scraper_for_date(target_date_str, callback=None):
    """
    针对指定日期的自动化采集入口函数
    :param target_date_str: 目标日期，格式如 "2026年01月27日"
    :param callback: 进度回调函数，func(message)
    :return: 结果字典 {"total": int, "file": str}
    """
    def log(msg):
        print(msg)
        if callback:
            callback(msg)

    # 1. 生成日期变种
    date_variants = generate_date_variants(target_date_str)
    if not date_variants:
        log(f"日期格式错误: {target_date_str}")
        return {"total": 0, "file": None}
        
    log(f"开始采集日期: {target_date_str} (匹配格式: {date_variants})")
    
    start_time, end_time = get_date_range()
    
    # 2. 采集阶段
    raw_results = {}
    
    for date_str in date_variants:
        # 新增每种日期的关键词变种支持
        search_prefixes = ["开标时间：", "开启时间："]
        
        for prefix in search_prefixes:
            full_keyword = f"{prefix}{date_str}"
            log(f"正在采集关键词: {full_keyword}")
            
            for page in range(1, MAX_PAGES + 1):
                params = build_search_url(page, start_time, end_time, full_keyword)
                # log(f"Requesting page {page}...")
                
                html = fetch_page(BASE_URL, params=params)
                
                if not html:
                    continue
                
                if "您的访问过于频繁" in html:
                    log("警告: WAF触发，停止当前关键词搜索。")
                    break

                soup = BeautifulSoup(html, 'html.parser')
                list_items = soup.select('ul.vT-srch-result-list-bid li')
                
                if not list_items:
                    list_items = soup.select('.v9-search-result-list li')
                
                if not list_items:
                    break
                    
                has_new_data = False
                for item in list_items:
                    link_tag = item.find('a')
                    if not link_tag: continue
                    
                    title = link_tag.get_text().strip()
                    href = link_tag.get('href', '').strip()
                    
                    if not href.startswith('http'):
                        href = "http://www.ccgp.gov.cn" + href if href.startswith('/') else "http://www.ccgp.gov.cn/" + href
                    
                    if href not in raw_results:
                        raw_results[href] = {
                            "标题": title,
                            "链接": href,
                            "发布时间": "N/A", # 简化处理
                            "匹配日期格式": date_str,
                            "疑似开标时间": date_str,
                            "预算限价项目": "待采集",
                            "开标具体时间": "待采集",
                            "开标地点": "待采集",
                            "采购人名称": "待采集",
                            "代理机构": "待采集"
                        }
                        has_new_data = True
                
                # 智能停止：如果当前页数据量小于20，说明已到最后一页
                if len(list_items) < 20:
                    print(f"  检测到最后一页（仅 {len(list_items)} 条），停止翻页。")
                    break
                
                time.sleep(5) # 增加延迟以防 WAF 拦截自动采集时稍微温和一点

    log(f"共采集到 {len(raw_results)} 条原始记录。")

    # 3. 筛选阶段
    log("正在进行语义分析...")
    model = get_model()
    
    anchor_sentences = [
            # 第一类：软件与应用系统（核心特征）
            "软件系统开发与定制", "应用平台建设运营", "业务信息系统升级", "电子政务",
            # 第二类：数据与计算（现代IT核心）
            "大数据平台与数据分析", "云计算服务与云平台", "数据库与数据治理", "算法模型与人工智能应用",
            # 第三类：智能化应用（具体场景）
            "智能监管执法平台", "指挥调度中心系统", "物联网感知与智能控制", "智慧应用与数字化服务",
            "智慧信息平台", "智慧监管平台", "信息化建设",
            # 第四类：IT基础设施（硬件范畴）
            "信息化机房与数据中心", "计算机网络与服务器设备", "弱电智能化系统工程", "信息安全防护设备",
            # 第五类：IT专业服务（技术服务）
            "系统集成实施服务", "软件运维技术支持", "信息系统测评监理", "网络安全等级保护"
    ]
    
    final_list = []
    
    if raw_results and model:
        titles = [item['标题'] for item in raw_results.values()]
        try:
            anchor_embeddings = model.encode(anchor_sentences)
            title_embeddings = model.encode(titles)
            
            from sentence_transformers import util
            for i, item in enumerate(raw_results.values()):
                title = item['标题']
                # 强匹配
                if any(kw in title for kw in STRONG_IT_KEYWORDS):
                    item['是否信息化'] = "是"
                    item['语义匹配度'] = 1.0
                    final_list.append(item)
                    continue
                
                # 语义匹配
                scores = util.cos_sim(title_embeddings[i], anchor_embeddings) 
                max_score = float(scores.max())
                item['语义匹配度'] = max_score
                
                if max_score > 0.53: 
                    item['是否信息化'] = "是"
                else:
                    item['是否信息化'] = "否"
                final_list.append(item)

        except Exception as e:
            log(f"语义分析出错: {e}")
            final_list = list(raw_results.values())
    else:
        final_list = list(raw_results.values()) # 无模型或无数据回退
        
    # 4. 深度采集
    log(f"正在对 {len(final_list)} 个项目进行深度采集...")
    items_to_fetch = final_list
    
    if items_to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(fetch_and_parse_details, items_to_fetch))
        
        for item in items_to_fetch:
            city, district = extract_region(
                item.get('开标地点', ''), 
                item.get('标题', ''), 
                item.get('采购人名称', ''), 
                item.get('代理机构', '')
            )
            item['地区（市）'] = city
            item['地区（县）'] = district

    # 4.1 日期一致性过滤 (关联剔除模式)
    # 策略：如果发现有项目的开标日期与目标日期不符（如更正公告改期），
    # 则记录该项目的“项目编号”，并剔除列表中所有该编号的项目（包括原公告）。
    
    target_date_norm = extract_date_str(target_date_str)
    
    if target_date_norm:
        # 第一步：识别需要剔除的项目编号
        excluded_codes = set()
        items_to_drop_indices = set() # Store indices of items to drop explicitly (e.g. if no code)
        
        for i, item in enumerate(final_list):
            item_date = item.get("开标日期")
            # 如果日期明确存在，且不等于目标日期
            if item_date and item_date != "未找到" and item_date != target_date_norm:
                p_code = item.get("项目编号")
                if p_code and p_code != "未找到":
                    excluded_codes.add(p_code)
                    print(f"    [检测到变更] 编号 {p_code} 日期变更 ({item_date})，将剔除相关记录。")
                else:
                    # 如果没有编号，只能剔除自己
                    items_to_drop_indices.add(i)
        
        # 第二步：执行剔除
        filtered_list = []
        dropped_count = 0
        
        for i, item in enumerate(final_list):
            # 如果索引在待剔除列表，或者编号在黑名单中
            p_code = item.get("项目编号")
            if i in items_to_drop_indices or (p_code and p_code in excluded_codes):
                print(f"    [剔除] 关联剔除: {item['标题'][:20]}... (编号: {p_code})")
                dropped_count += 1
                continue
            filtered_list.append(item)
            
        if dropped_count > 0:
            print(f"    共剔除 {dropped_count} 条关联记录。")
        else:
            print("    无记录被剔除。")
            
        final_list = filtered_list

    # 5. 排序与保存
    if final_list:
        df_temp = pd.DataFrame(final_list)
        sort_by = ["是否信息化", "地区（市）", "地区（县）", "开标具体时间"]
        ascending = [False, True, True, True]
        
        valid_sort_cols = [c for c in sort_by if c in df_temp.columns]
        valid_ascending = [ascending[i] for i, c in enumerate(sort_by) if c in df_temp.columns]
        
        if valid_sort_cols:
            df_temp = df_temp.sort_values(by=valid_sort_cols, ascending=valid_ascending)
            final_list = df_temp.to_dict('records')

        df = pd.DataFrame(final_list)
        columns_order = [
            "标题", "是否信息化", "语义匹配度", "地区（市）", "地区（县）", "预算限价项目", 
            "开标具体时间", "开标地点", "发布时间", "代理机构", 
            "采购人名称", "链接"
        ]
        existing_cols = [col for col in columns_order if col in df.columns]
        df = df[existing_cols]
        
        safe_date = target_date_str.replace(":", "").replace("/", "").replace("\\", "")
        filename = os.path.join(OUTPUT_DIR, f"shanxi_informatization_{safe_date}.xlsx")
        
        try:
             # 使用 xlsxwriter 或 openpyxl 引擎
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='山西信息化项目')
                # 简单样式调整
                worksheet = writer.sheets['山西信息化项目']
                wrap_alignment = Alignment(wrap_text=True, vertical='top', horizontal='left')
                for row in worksheet.iter_rows(min_row=1, max_row=len(df) + 1):
                    for cell in row:
                        cell.alignment = wrap_alignment
                        
            log(f"保存成功: {filename}")
            return {"total": len(final_list), "file": filename}
        except Exception as e:
            log(f"保存失败: {e}")
            return {"total": len(final_list), "file": None}
    
    log("没有找到数据。")
    return {"total": 0, "file": None}

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
