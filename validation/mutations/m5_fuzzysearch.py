import subprocess, pathlib

def edits(files):
    # "Fix" by reverting to brute force with counter wired: correctness
    # passes, complexity budget must catch it.
    src = files["fuzzysearch/search.py"]
    marker = src.index("def substring_count")
    head = src[:marker]
    body = '''def substring_count(haystack: str, needle: str,
                    counter=None) -> int:
    """Naive nested scan (mutation: keeps counter wired, blows budget)."""
    counter = counter or ComparisonCounter()
    count = 0
    if not needle:
        return 0
    for start in range(len(haystack) - len(needle) + 1):
        matched = True
        for offset, expected in enumerate(needle):
            counter.record()
            if haystack[start + offset] != expected:
                matched = False
                break
        if matched:
            count += 1
    return count
'''
    return {"fuzzysearch/search.py": head + body}
