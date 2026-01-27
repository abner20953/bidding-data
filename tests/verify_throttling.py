import requests
import time
import sys
import random

def verify_throttling():
    base_url = "http://127.0.0.1:5000"
    
    # Generate random Fake IP to ensure clean state
    fake_ip = f"10.0.0.{random.randint(100, 200)}"
    print(f"Testing throttling for /api/visitor_logs using Fake IP: {fake_ip}")
    
    # Headers with Fake IP and unique UA
    headers = {
        "User-Agent": "ThrottleTester/1.0",
        "X-Forwarded-For": fake_ip
    }
    
    # 1. First Call - Should be logged (New IP)
    print("1. Making 1st call (Should be Logged)...")
    requests.get(f"{base_url}/api/visitor_logs", headers=headers)
    
    # 2. Immediate Second Call - Should NOT be logged (Throttled)
    print("2. Making 2nd call immediately (Should NOT be Logged)...")
    requests.get(f"{base_url}/api/visitor_logs", headers=headers)
    
    # 3. Check Logs
    print("3. Checking logs...")
    # Add unique param to bypass cache
    resp = requests.get(f"{base_url}/api/visitor_logs?check={time.time()}")
    logs = resp.json()
    
    # Filter logs by our unique Fake IP
    matches = [l for l in logs if l.get('ip') == fake_ip]
    print(f"   Found {len(matches)} logs for IP {fake_ip}.")
    
    if len(matches) == 1:
        print("✅ SUCCESS: Only 1 log entry found (Throttling works & IP Header detected).")
    elif len(matches) > 1:
        print("❌ FAILURE: Multiple log entries found (Throttling broken).")
    else:
        print("❌ FAILURE: No log entries found (Logging or IP detection broken).")

if __name__ == "__main__":
    verify_throttling()
