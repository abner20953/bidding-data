
import os

path = r"d:\ai_project\1\dashboard\templates\file_list.html"
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Line numbers are 1-based. List indices are 0-based.
# We want to keep lines 1 to 652.
# Index 0 to 651.
# Delete lines 653 to 810.
# Index 652 to 809.
# Resume at line 811.
# Index 810.

start_index = 652 
end_index = 810 

print(f"Line {start_index+1} content: {lines[start_index].strip()}")
print(f"Line {end_index} content (last deleted): {lines[end_index-1].strip()}")
print(f"Line {end_index+1} content (resume): {lines[end_index].strip()}")

if "Stealth Chat Logic" in lines[start_index] and "Close modal" in lines[end_index]:
    print("Verification Passed. Writing file...")
    new_lines = lines[:start_index] + lines[end_index:]
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("Done.")
else:
    print("Verification Failed.")
