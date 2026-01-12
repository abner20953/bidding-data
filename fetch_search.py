
import requests
from bs4 import BeautifulSoup

url = 'http://search.ccgp.gov.cn/bxsearch'
params = {
    'searchtype': '2',
    'page_index': '1',
    'bidSort': '0',
    'buyerName': '',
    'projectId': '',
    'pinMu': '0',
    'bidType': '0',
    'dbselect': 'bidx',
    'kw': '开标时间：2025年11月27日',
    'start_time': '2025:10:07',
    'end_time': '2026:01:06',
    'timeType': '6',
    'displayZone': '山西',
    'zoneId': '14',
    'pppStatus': '0',
    'agentName': ''
}
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

try:
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.encoding = 'utf-8'
    with open('search_sample.html', 'w', encoding='utf-8') as f:
        f.write(r.text)
    print("Fetched search_sample.html")
    
    soup = BeautifulSoup(r.text, 'html.parser')
    items = soup.select('ul.vT-srch-result-list-bid li')
    if items:
        first_item = items[0]
        print(f"First item HTML:\n{first_item.prettify()}")
except Exception as e:
    print(f"Error: {e}")
