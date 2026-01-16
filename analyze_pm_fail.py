from bs4 import BeautifulSoup
import re

fname = "debug_pm_fail.html"
try:
    with open(fname, "r", encoding="utf-8") as f:
        html = f.read()
except:
    with open(fname, "r", errors='ignore') as f:
        html = f.read()

soup = BeautifulSoup(html, 'html.parser')
text = soup.get_text(separator='\n')

print(f"Title: {soup.title.string if soup.title else 'No Title'}")

# 1. Search for explicit Procurement Method string
pm_matches = re.findall(r"采购方式[:：]\s*(.*)", text)
if pm_matches:
    print(f"Explicit Matches in text: {pm_matches}")
else:
    print("No '采购方式:' found in text.")

# 2. Check title fallback
print(f"Checking title for keywords: {soup.title.string}")
if "竞争性磋商" in str(soup.title.string):
    print("Title contains '竞争性磋商'")
elif "竞争性谈判" in str(soup.title.string):
    print("Title contains '竞争性谈判'")
else:
    print("Title does not contain method keywords.")

# 3. Check table specifically
summary = soup.find('table', id='summaryTable')
if summary:
    print("Summary Table found.")
    print(summary.get_text()[:300].replace('\n', ' '))
else:
    print("No Summary Table.")
