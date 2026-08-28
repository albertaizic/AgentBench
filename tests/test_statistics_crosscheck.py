"""Independent statistical cross-checks (v0.6 release hardening, mission V).

Every implementation value is recomputed here from the closed-form reference
definition, so a future refactor that silently changes semantics fails.
"""

from __future__ import annotations

import math

from agentbench.aggregate import mcnemar_exact_p, quantile, wilson_interval
from agentbench.reliability import reliability_from_cells


def _mcnemar_reference(b_only: int, a_only: int) -> float | None:
    """Two-sided exact binomial over discordant pairs, computed from scratch."""
    discordant = b_only + a_only
    if discordant == 0:
        return None
    tail = sum(math.comb(discordant, i) for i in range(0, min(b_only, a_only) + 1))
    return min(1.0, 2.0 * tail / 2.0 ** discordant)


def test_mcnemar_matches_closed_form_on_all_small_splits():
    cases = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2),
             (3, 10), (10, 3), (5, 5), (20, 1)]
    for b, a in cases:
        got = mcnemar_exact_p(b, a)
        ref = _mcnemar_reference(b, a)
        if ref is None:
            assert got is None
        else:
            assert abs(got - ref) < 1e-12, (b, a)


def test_mcnemar_is_symmetric_in_discordant_counts():
    for b, a in [(3, 10), (10, 3), (7, 7), (20, 1)]:
        assert mcnemar_exact_p(b, a) == mcnemar_exact_p(a, b)


def test_mcnemar_known_value_ten_three():
    # v0.6 study of record orientation check.
    assert abs(mcnemar_exact_p(10, 3) - 0.0923) < 5e-4


def test_mcnemar_never_exceeds_one_and_clamps_even_split():
    assert mcnemar_exact_p(5, 5) == 1.0


def _wilson_reference(successes: int, total: int,
                      z: float = 1.959963984540054) -> tuple[float, float] | None:
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    spread = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
              / denominator)
    return max(0.0, center - spread), min(1.0, center + spread)


def test_wilson_matches_formula_and_stays_in_unit_interval():
    for k, n in [(0, 10), (10, 10), (3, 8), (1, 3), (20, 300), (13, 24)]:
        assert wilson_interval(k, n) == _wilson_reference(k, n)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0 < hi < 0.35   # rule-of-three ballpark
    lo, hi = wilson_interval(10, 10)
    assert 0.70 < lo < 1.0 and hi <= 1.0   # 10/10 → lower ≈ 0.722


def test_quantile_type7_convention():
    assert quantile([1, 2, 3, 4], 0.5) == 2.5      # even-count median interpolates
    assert quantile([1, 2, 3, 4], 0.25) == 1.75    # q*(n-1)=0.75 between 1 and 2
    assert quantile([5], 0.9) == 5                 # single sample
    assert quantile([], 0.5) is None
    assert quantile([3, 1, 2], 0.0) == 1           # unsorted input handled
    assert quantile([3, 1, 2], 1.0) == 3


def test_reliability_distinguishes_observed_any_all_k():
    # Observed repeated trials — NOT the combinatorial pass@k estimator.
    rel = reliability_from_cells(
        [[True, True, False], [True, True, True], [False, False, False]],
        k=3).to_dict()
    # any-in-k: tasks solved at least once within k observed trials = 2/3.
    assert rel["any_in_k"] == round(2 / 3, 4)
    # all-k: tasks solved every time = 1/3.
    assert rel["all_k"] == round(1 / 3, 4)
    # pooled pass@1 over all nine trials.
    assert rel["pass_at_1"] == round(5 / 9, 4)


def test_reliability_empty_input_is_zero_not_none_crash():
    rel = reliability_from_cells([], k=3).to_dict()
    assert rel["n_runs"] == 0 and rel["passes"] == 0
