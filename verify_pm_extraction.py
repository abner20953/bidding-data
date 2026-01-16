from scraper import parse_project_details

files = ["debug_pm_neg.html", "debug_pm_consult.html"]

print("--- Testing Extraction Logic ---")
for fname in files:
    try:
        with open(fname, "r", encoding="utf-8") as f:
            html = f.read()
    except:
        with open(fname, "r", errors='ignore') as f:
            html = f.read()
            
    details = parse_project_details(html)
    print(f"File: {fname}")
    print(f"  Title: {details.get('标题', 'Unknown')}")
    print(f"  Method: {details.get('采购方式')}")
    print("-" * 30)
