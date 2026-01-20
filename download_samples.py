from scraper import fetch_page

urls = [
    "https://www.ccgp.gov.cn/cggg/dfgg/jzxcs/202601/t20260111_26062354.htm",
    "https://www.ccgp.gov.cn/cggg/dfgg/jzxcs/202601/t20260109_26058217.htm"
]

for i, url in enumerate(urls, 1):
    print(f"Fetching sample {i}...")
    html = fetch_page(url)
    if html:
        filename = f"sample_procurement_{i}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  Saved to {filename}")
        
        # Check for "采购需求"
        if "采购需求" in html:
            print(f"  ✓ Contains '采购需求'")
            # Find context
            idx = html.find("采购需求")
            snippet = html[max(0, idx-100):min(len(html), idx+500)]
            print(f"  Context: {snippet[:200]}...")
        else:
            print(f"  ✗ No '采购需求' found")
    else:
        print(f"  Failed to fetch")
    print()
