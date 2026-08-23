"""Hidden evaluator: correctness plus a deterministic comparison budget.

The naive scan performs ~(n-haystack x m-needle) comparisons. A correct,
sub-quadratic implementation (e.g. KMP) does O(n + m). The budget is set at
4x(n+m), generous enough for constant factors but far below quadratic growth.
"""

from __future__ import annotations

from fuzzysearch.search import ComparisonCounter, substring_count


def test_correctness_unchanged():
    assert substring_count("ababab", "ab") == 3
    assert substring_count("aaa", "aa") == 2


def test_comparison_budget_scales_linearly():
    haystack = "ab" * 2000  # 4000 chars, many partial matches to punish naive scans
    needle = "ab" * 5  # 10 chars

    counter = ComparisonCounter()
    result = substring_count(haystack, needle, counter)

    assert result == 1996  # overlapping occurrences
    budget = 4 * (len(haystack) + len(needle))
    assert counter.comparisons <= budget, (
        f"{counter.comparisons} comparisons exceeds linear budget {budget}"
    )


def test_worst_case_naive_input_stays_in_budget():
    haystack = "a" * 3000
    needle = "a" * 10

    counter = ComparisonCounter()
    assert substring_count(haystack, needle, counter) == 2991

    budget = 4 * (len(haystack) + len(needle))
    assert counter.comparisons <= budget
