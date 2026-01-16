from bs4 import BeautifulSoup
import re

files = ["debug_pm_neg.html", "debug_pm_consult.html"]

for fname in files:
    print(f"\n{'='*20} {fname} {'='*20}")
    try:
        with open(fname, "r", encoding="utf-8") as f:
            html = f.read()
    except:
        # Curl might default to GBK or something else on Windows depending on locale, trying flexible open
        with open(fname, "r", errors='ignore') as f:
             html = f.read()
            
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(separator='\n')
    
    # 1. Look for explicit keyword
    match = re.search(r"采购方式[:：]\s*(.*)", text)
    if match:
        print(f"Found keyword match: '{match.group(0)}'")
    else:
        print("Keyword '采购方式' not found in text.")
        
    # 2. Check title just in case
    print(f"Title: {soup.title.string if soup.title else 'No Title'}")
    
    # 3. Check table (summary table)
    summary = soup.find('table', id='summaryTable')
    if summary:
         print("Found summaryTable")
         print(summary.get_text()[:200].replace('\n', ' '))
