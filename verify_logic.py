
import sys
import os

# Add project root to path
sys.path.append(os.path.join(os.getcwd(), 'dashboard'))

from dashboard.utils.comparator import check_sequence_errors, detect_broken_tail, get_fingerprint

def test_broken_tail():
    print("--- Testing Broken Tail ---")
    
    # Cases that should be True (Broken)
    broken_cases = [
        "这是一个非常长的句子但是没有标点符号结尾",
        "This is a long sentence without punctuation",
        "段落的内容很长很长，但是最后竟然忘了加句号"
    ]
    
    # Cases that should be False (OK)
    ok_cases = [
        "这是一段正常的句子。",
        "Short.",
        "Short", # Too short (<15)
        "1. Title", # Too short
        "这是一个长句子，且有结尾。",
        "What about question?",
        "He said: \"Quote.\""
    ]
    
    for c in broken_cases:
        res = detect_broken_tail(c)
        print(f"['{c[:10]}...'] Expected: True, Got: {res} -> {'PASS' if res else 'FAIL'}")

    for c in ok_cases:
        res = detect_broken_tail(c)
        print(f"['{c[:10]}...'] Expected: False, Got: {res} -> {'PASS' if not res else 'FAIL'}")

def test_sequence_errors():
    print("\n--- Testing Sequence Errors ---")
    
    # Case 1: Simple Skip
    paras_1 = [
        {"text": "1. First", "page": 1},
        {"text": "2. Second", "page": 1},
        {"text": "4. Fourth", "page": 1} # Skips 3
    ]
    errors = check_sequence_errors(paras_1)
    if len(errors) == 1 and errors[0]['missing'] == 3:
        print("Case 1 (Simple Skip): PASS")
    else:
        print(f"Case 1 (Simple Skip): FAIL -> {errors}")

    # Case 2: Reset on new 1
    paras_2 = [
        {"text": "1. List A", "page": 1},
        {"text": "3. Error A", "page": 1}, # Missing 2
        {"text": "Bla bla", "page": 1},
        {"text": "1. List B", "page": 2},
        {"text": "2. List B", "page": 2}
    ]
    errors = check_sequence_errors(paras_2)
    # Should catch missing 2 in List A, but List B is fine.
    if len(errors) == 1 and errors[0]['missing'] == 2:
         print("Case 2 (Reset): PASS")
    else:
         print(f"Case 2 (Reset): FAIL -> {errors}")
         
    # Case 3: Parenthesis
    paras_3 = [
        {"text": "(1) Item", "page": 1},
        {"text": "(2) Item", "page": 1},
        {"text": "(4) Item", "page": 1} # Missing 3
    ]
    errors = check_sequence_errors(paras_3)
    if len(errors) == 1 and errors[0]['missing'] == 3:
        print("Case 3 (Parens): PASS")
    else:
        print(f"Case 3 (Parens): FAIL -> {errors}")

if __name__ == "__main__":
    test_broken_tail()
    test_sequence_errors()
