"""Deterministic generator for the fuzzysearch fixture (algorithmic complexity)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "fuzzysearch"\nversion = "0.1.0"\n',
    # BUG (performance): naive quadratic scan; API (counter hook) must stay.
    "fuzzysearch/search.py": (
        '"""Substring counting with an instrumentation hook."""\n'
        '\nfrom __future__ import annotations\n\n\n'
        'class ComparisonCounter:\n'
        '    """Counts character comparisons; defaults to a throwaway counter."""\n\n'
        '    def __init__(self) -> None:\n'
        '        self.comparisons = 0\n\n'
        '    def record(self) -> None:\n'
        '        self.comparisons += 1\n\n\n'
        'def substring_count(haystack: str, needle: str,\n'
        '                    counter: ComparisonCounter | None = None) -> int:\n'
        '    """Count (possibly overlapping) occurrences of needle in haystack."""\n'
        '    counter = counter or ComparisonCounter()\n'
        '    if not needle:\n'
        '        return 0\n'
        '    count = 0\n'
        '    for start in range(len(haystack) - len(needle) + 1):\n'
        '        matched = True\n'
        '        for offset, expected in enumerate(needle):\n'
        '            counter.record()\n'
        '            if haystack[start + offset] != expected:\n'
        '                matched = False\n'
        '                break\n'
        '        if matched:\n'
        '            count += 1\n'
        '    return count\n'
    ),
    "tests/test_search.py": (
        '"""Public correctness tests (must keep passing)."""\n\n'
        'from fuzzysearch.search import substring_count\n\n\n'
        'def test_counts_overlapping_matches():\n'
        '    assert substring_count("ababab", "ab") == 3\n\n'
        'def test_empty_needle_counts_zero():\n'
        '    assert substring_count("abc", "") == 0\n\n'
        'def test_needle_longer_than_haystack():\n'
        '    assert substring_count("ab", "abc") == 0\n\n'
        'def test_counter_records_comparisons_within_budget():\n'
        '    from fuzzysearch.search import ComparisonCounter\n\n'
        '    counter = ComparisonCounter()\n'
        '    haystack, needle = "abc", "b"\n'
        '    substring_count(haystack, needle, counter)\n'
        '    # Contract: comparisons proportional to n + m — any linear-time\n'
        '    # algorithm qualifies (small constant factors differ between\n'
        '    # KMP/Z/etc., and preprocessing counts); only naive scans blow it.\n'
        '    assert counter.comparisons <= 2 * (len(haystack) + len(needle))\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "fuzzysearch: substring counting", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
