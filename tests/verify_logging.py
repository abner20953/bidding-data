import requests
import time
import sys

def verify_logging():
    base_url = "http://127.0.0.1:5000"
    
    # 1. Simulate Visits
    print("Simulating visits...")
    
    # Desktop Visit
    headers_pc = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    }
    try:
        requests.get(f"{base_url}/all", headers=headers_pc)
        print("  Visited /all (PC)")
    except Exception as e:
        print(f"  Error visiting /all: {e}")
        return

    # Mobile Visit
    headers_mobile = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
    }
    requests.get(f"{base_url}/mobile", headers=headers_mobile)
    print("  Visited /mobile (Mobile)")

    # 2. Check Logs via API
    print("Checking logs via API...")
    time.sleep(1) # Wait for db write? (Should be instant)
    
    try:
        resp = requests.get(f"{base_url}/api/visitor_logs")
        if resp.status_code != 200:
            print(f"  ❌ API Error: {resp.status_code} {resp.text}")
            return
            
        logs = resp.json()
        print(f"  Retrieved {len(logs)} logs.")
        
        # Verify PC Log
        pc_log = next((l for l in logs if l['path'] == '/all'), None)
        if pc_log:
            print(f"  ✅ PC Log Found: {pc_log['path']} - {pc_log['device']} - {pc_log['os']}")
            if pc_log['device'] == 'PC' and 'Windows' in pc_log['os']:
                print("     Details Match!")
            else:
                print(f"     ❌ Details Mismatch: Expected PC/Windows, got {pc_log['device']}/{pc_log['os']}")
        else:
            print("  ❌ PC Log NOT Found")

        # Verify Mobile Log
        mobile_log = next((l for l in logs if l['path'] == '/mobile'), None)
        if mobile_log:
            print(f"  ✅ Mobile Log Found: {mobile_log['path']} - {mobile_log['device']} - {mobile_log['os']}")
            if mobile_log['device'] == 'Mobile' and 'iOS' in mobile_log['os']:
                print("     Details Match!")
            else:
                print(f"     ❌ Details Mismatch: Expected Mobile/iOS, got {mobile_log['device']}/{mobile_log['os']}")
        else:
            print("  ❌ Mobile Log NOT Found")
            
    except Exception as e:
        print(f"  Error checking logs: {e}")

if __name__ == "__main__":
    verify_logging()
