from scraper import parse_project_details
import os

# 读取本地保存的 debug HTML
file_path = "debug_target.html"
if not os.path.exists(file_path):
    print(f"Error: {file_path} not found.")
    exit(1)

with open(file_path, "r", encoding="utf-8") as f:
    html = f.read()

print("Parsing details from local file...")
details = parse_project_details(html)

print("\n--- Extracted Details ---")
for key, value in details.items():
    print(f"{key}: {value}")

expected_location = "山西省太原市晋源区太原市长风商务区阳光城环球金融中心B座701阳光城"
actual_location = details["开标地点"]

if expected_location in actual_location:
    print("\n✅ Verification PASSED: Location extracted correctly.")
else:
    print(f"\n❌ Verification FAILED: Expected '{expected_location}', but got '{actual_location}'")
