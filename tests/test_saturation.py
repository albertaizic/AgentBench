"""Saturation/difficulty classification: mechanical verdicts, honest sample sizes."""

from __future__ import annotations

from agentbench.saturation import (
    CLASS_DISCRIMINATING,
    CLASS_SATURATED,
    CLASS_TOO_HARD,
    CLASS_UNCALIBRATED,
    MIN_RUNS_DEFAULT,
    analyze,
    analyze_benchmark,
)


def row(benchmark="b", config="a", passed=True, **over):
    payload = {
        "benchmark": benchmark,
        "config_name": config,
        "status": "passed" if passed else "failed",
        "duration_seconds": 10.0,
        "total_tokens": 1000,
        "cost_usd": 0.01,
        "insertions": 5,
        "deletions": 2,
    }
    payload.update(over)
    return payload


class TestSampleSize:
    def test_below_minimum_is_uncalibrated_regardless_of_outcome(self):
        rows = [row(passed=True) for _ in range(MIN_RUNS_DEFAULT - 1)]
        verdict = analyze_benchmark("b", rows)
        assert verdict.classification == CLASS_UNCALIBRATED
        assert "only" in verdict.reason

    def test_single_saturated_config_is_still_uncalibrated(self):
        # One config passing 6/6 has a Wilson lower bound of ~54% — far too
        # shaky to declare a task saturated. Saturation needs every measured
        # config to agree, i.e. at least two trusted configs.
        rows = [row() for _ in range(MIN_RUNS_DEFAULT)]
        assert analyze_benchmark("b", rows).classification == CLASS_UNCALIBRATED

    def test_at_minimum_two_configs_can_classify(self):
        rows = [row(config="a") for _ in range(3)] + [row(config="b") for _ in range(3)]
        assert analyze_benchmark("b", rows).classification == CLASS_SATURATED


class TestVerdicts:
    def test_all_configs_fail_is_likely_too_hard(self):
        rows = [row(config=c, passed=False) for c in ("a", "b") for _ in range(4)]
        verdict = analyze_benchmark("b", rows)
        assert verdict.classification == CLASS_TOO_HARD
        assert verdict.overall_pass_rate == 0.0

    def test_every_config_saturates(self):
        rows = [row(config="a") for _ in range(5)] + [row(config="b") for _ in range(5)]
        verdict = analyze_benchmark("b", rows, min_runs=6)
        # 10 runs >= min; both configs at 100%
        assert verdict.classification == CLASS_SATURATED

    def test_config_gap_is_discriminating(self):
        rows = [row(config="strong") for _ in range(6)]
        rows += [row(config="weak", passed=False) for _ in range(6)]
        verdict = analyze_benchmark("b", rows)
        assert verdict.classification == CLASS_DISCRIMINATING
        assert verdict.best_pass_rate == 1.0
        assert verdict.worst_pass_rate == 0.0

    def test_small_spread_stays_uncalibrated(self):
        # 4/6 vs 3/6: spread ~17% < gap threshold.
        rows = [row(config="a") for _ in range(4)] + [row(config="a", passed=False) for _ in range(2)]
        rows += [row(config="b") for _ in range(3)] + [row(config="b", passed=False) for _ in range(3)]
        verdict = analyze_benchmark("b", rows)
        assert verdict.classification == CLASS_UNCALIBRATED

    def test_single_run_configs_cannot_anchor_verdict(self):
        # One config with a single failing run must not make this "too hard"
        # when the trusted evidence is thin overall... but total>=min forces
        # classification from the remaining trusted config only.
        rows = [row(config="thin", passed=False)] + [
            row(config="main", passed=False) for _ in range(MIN_RUNS_DEFAULT)
        ]
        verdict = analyze_benchmark("b", rows)
        assert verdict.classification == CLASS_TOO_HARD  # trusted main also fails
        # Now the thin config fails once but main saturates: not saturated,
        # because untrusted single-run configs are excluded from the verdict.
        rows2 = [row(config="thin", passed=False)] + [
            row(config="main") for _ in range(MIN_RUNS_DEFAULT)
        ]
        verdict2 = analyze_benchmark("b", rows2)
        assert verdict2.classification != CLASS_SATURATED


class TestAnalyzeAll:
    def test_groups_by_benchmark_and_sorts(self):
        rows = [row(benchmark="zeta"), row(benchmark="alpha")]
        verdicts = analyze(rows, min_runs=1)
        assert [v.benchmark for v in verdicts] == ["alpha", "zeta"]
        assert all(v.classification in (CLASS_UNCALIBRATED,) or v.classification for v in verdicts)

    def test_per_config_stats_are_populated(self):
        rows = [
            row(config="a", duration_seconds=8.0, cost_usd=0.02),
            row(config="a", duration_seconds=12.0, cost_usd=0.04),
            row(config="b", passed=False, duration_seconds=30.0, cost_usd=0.09),
            row(config="b", passed=False, duration_seconds=20.0, cost_usd=0.05),
        ]
        verdict = analyze_benchmark("b", rows, min_runs=4)
        labels = {c.label: c for c in verdict.configs}
        assert labels["a"].median_duration == 10.0
        assert labels["a"].median_cost_usd == 0.03
        assert labels["b"].pass_rate == 0.0
        assert labels["b"].interval is not None
        assert labels["a"].median_diff_lines == 7
