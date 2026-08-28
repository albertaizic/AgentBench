"""Alternative fuzzysearch: Z-array based search instead of KMP."""

def edits(files):
    src = files["fuzzysearch/search.py"]
    marker = src.index("def substring_count")
    head, tail = src[:marker], src[marker:]
    replacement = '''def _z_blocks(s: str, counter) -> list[int]:
    """Z-array: for each position, length of the longest prefix-match."""
    z = [0] * len(s)
    z[0] = len(s)
    left = right = 0
    for i in range(1, len(s)):
        if i < right:
            z[i] = min(right - i, z[i - left])
        while i + z[i] < len(s):
            counter.record()
            if s[z[i]] != s[i + z[i]]:
                break
            z[i] += 1
        if i + z[i] > right:
            left, right = i, i + z[i]
    return z


def substring_count(haystack: str, needle: str,
                    counter=None) -> int:
    """Count overlapping occurrences using the Z-algorithm: O(n + m)."""
    from typing import Optional  # noqa: F401
    counter = counter or ComparisonCounter()
    if not needle or len(needle) > len(haystack):
        return 0
    combined = needle + "\\x00" + haystack
    z = _z_blocks(combined, counter)
    return sum(1 for v in z[len(needle):] if v >= len(needle))


'''
    return {"fuzzysearch/search.py": head + replacement}
