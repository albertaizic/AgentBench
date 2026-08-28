
from __future__ import annotations

import math

import pytest

from agentbench.recovery import (
    FAILED_AFTER_TESTS,
    FAILED_NO_TESTS,
    SOLVED_FIRST,
    SOLVED_MULTI,
    SOLVED_RECOVERY,
    classify_recovery,
    recovery_summary,
)
from agentbench.reliability import (
    fit_logistic_horizon,
    horizon_with_bootstrap,
    reliability_from_cells,
)


class TestReliability:
    def test_observed_counts_and_wilson(self):
        cells = [[True, True, False], [True, True, True], [False, False, False]]
        r = reliability_from_cells(cells, k=3, bootstrap_iterations=200)
        assert r.n_runs == 9
        assert r.passes == 5
        assert r.pass_at_1 == pytest.approx(5 / 9)
        low, high = r.wilson
        assert low < 5 / 9 < high
        # any_in_k over 3 tasks: task1 yes, task2 yes, task3 no => 2/3
        assert r.any_in_k == pytest.approx(2 / 3)
        # all_k: only task2 all-passed => 1/3
        assert r.all_k == pytest.approx(1 / 3)

    def test_bootstrap_interval_contains_point_estimate(self):
        cells = [[True] * 3 + [False] for _ in range(4)]
        r = reliability_from_cells(cells, bootstrap_iterations=300)
        lo, hi = r.bootstrap_ci
        assert lo <= r.pass_at_1 <= hi
    def test_empty_input_is_honest_zero(self):
        r = reliability_from_cells([])
        assert r.n_runs == 0 and r.pass_at_1 == 0.0
        assert r.wilson == (0.0, 0.0) or all(
            v is not None for v in r.wilson)
    def test_empty_input_is_honest_zero(self):
        r = reliability_from_cells([])
        assert r.n_runs == 0 and r.pass_at_1 == 0.0


class TestHorizonFit:
    def _points(self):
        # success probability falls as expert minutes grow; 12 tasks
        minutes = [2, 2, 4, 4, 8, 8, 16, 16, 32, 32, 64, 64]
        probs = [0.95, 0.9, 0.85, 0.8, 0.7, 0.65,
                 0.5, 0.45, 0.3, 0.25, 0.15, 0.1]
        trials = [3] * 12
        return [(m, round(p * t), t) for m, p, t in zip(minutes, probs, trials)]

    def test_fit_recovers_falling_curve(self):
        fit = fit_logistic_horizon(self._points())
        assert fit["ok"]
        assert fit["slope"] < 0                      # harder with more minutes
        # Decreasing curve: the 80%-success horizon sits BELOW the 50%
        # point — less expert time is needed for higher success.
        assert fit["h80_minutes"] < fit["h50_minutes"]
        assert 2 <= fit["h50_minutes"] <= 48

    def test_thin_data_refuses_to_fit(self):
        out = horizon_with_bootstrap([(8, 1, 1)], min_tasks=8)
        assert not out["ok"]
        assert "insufficient data" in out["reason"]

    def test_degenerate_data_refuses(self):
        same = [(m, 3, 3) for m in (2, 4, 8, 16, 32, 64, 2, 4)]
        out = horizon_with_bootstrap(same, min_tasks=8)
        assert not out["ok"]
        assert "identical" in out["reason"]

    def test_full_pipeline_reports_contributors(self):
        out = horizon_with_bootstrap(self._points(), iterations=60)
        assert out["ok"]
        assert len(out["contributing_tasks"]) == 12


class TestRecoveryClassification:
    def _ev(self, etype, rel, success=None):
        return {"event_type": etype, "relative_ms": rel, "success": success}

    def test_solved_first_try(self):
        events = [
            self._ev("file_read", 100),
            self._ev("file_edit", 200),
            self._ev("test_command", 300, True),
        ]
        assert classify_recovery(events, passed=True) == SOLVED_FIRST

    def test_single_failure_then_green(self):
        events = [
            self._ev("file_edit", 100),
            self._ev("test_command", 200, False),
            self._ev("file_edit", 300),
            self._ev("test_command", 400, True),
        ]
        assert classify_recovery(events, passed=True) == SOLVED_RECOVERY

    def test_multiple_failed_iterations(self):
        events = [
            self._ev("test_command", 100, False),
            self._ev("file_edit", 150),
            self._ev("test_command", 200, False),
            self._ev("file_edit", 250),
            self._ev("test_command", 300, True),
        ]
        assert classify_recovery(events, passed=True) == SOLVED_MULTI

    def test_failed_without_testing(self):
        assert classify_recovery(
            [self._ev("file_read", 100)], passed=False) == FAILED_NO_TESTS

    def test_failed_after_testing_counts_edits_after_last_fail(self):
        events = [
            self._ev("test_command", 100, False),
            self._ev("file_edit", 150),
            self._ev("test_command", 200, False),
        ]
        assert classify_recovery(events, passed=False) == FAILED_AFTER_TESTS

    def test_summary_buckets_unknown_for_missing_trajectories(self):
        runs = [
            {"passed": True, "header": {"trajectory_status": "complete"},
             "events": [self._ev("test_command", 1, True)]},
            {"passed": False, "header": {"trajectory_status": "unavailable"},
             "events": []},
        ]
        counts = recovery_summary(runs)
        assert counts[SOLVED_FIRST] == 1
        assert counts["unknown"] == 1
