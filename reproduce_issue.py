from scraper import fetch_page, parse_project_details

target_url = "https://www.ccgp.gov.cn/cggg/dfgg/jzxtpgg/202601/t20260112_26066593.htm"

print(f"Fetching URL: {target_url}")
html = fetch_page(target_url)

if html:
    print("Successfully fetched HTML.")
    # Save HTML for analysis
    with open("debug_target.html", "w", encoding="utf-8") as f:
        f.write(html)
    
    print("\nParsing details...")
    details = parse_project_details(html)
    for key, value in details.items():
        print(f"{key}: {value}")
else:
    print("Failed to fetch HTML.")
