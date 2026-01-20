from scraper import run_scraper_for_date
import traceback
import sys

def callback(msg):
    print(f"[CALLBACK] {msg}")

try:
    # Use a date likely to trigger data (e.g. today or yesterday)
    # User had errors "executing collection task".
    print("Starting scraper debug...")
    result = run_scraper_for_date("2026年01月20日", callback=callback)
    print("Result:", result)
except Exception:
    traceback.print_exc()
