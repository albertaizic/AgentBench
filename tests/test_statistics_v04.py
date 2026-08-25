"""v0.4 statistics: McNemar exact test, Pareto frontier, IQRs, pairwise stats."""

from __future__ import annotations

import math

import pytest

from agentbench.aggregate import (
    ConfigAggregate,
    aggregate_by_config,
    mcnemar_exact_p,
    pairwise_compare,
    pairwise_statistics,
    pareto_frontier,
)


class TestMcNemarExact:
    def test_no_discordant_pairs_is_none(self):
        assert mcnemar_exact_p(0, 0) is None

    def test_extreme_imbalance_gives_tiny_p(self):
        # 9 vs 0: p = 2 * (1/2)^9 ≈ 0.0039
        p = mcnemar_exact_p(9, 0)
        assert p == pytest.approx(2 / 512)

    def test_balanced_discordance_is_not_significant(self):
        assert mcnemar_exact_p(5, 5) == 1.0

    def test_known_value(self):
        # 1 vs 5: two-sided binomial, n=6
        expected = 2 * sum(math.comb(6, i) for i in range(0, 2)) / 64
        assert mcnemar_exact_p(1, 5) == pytest.approx(expected)
        assert mcnemar_exact_p(1, 5) < 0.3

    def test_textbook_case_matches_closed_form(self):
        # Classic table: 10 vs 1 discordant -> p = 24/2048 = 0.01171875
        assert mcnemar_exact_p(10, 1) == pytest.approx(24 / 2048)
        assert mcnemar_exact_p(1, 10) == pytest.approx(24 / 2048)


class TestPairwiseStatistics:
    @staticmethod
    def row(benchmark, trial, status, duration=None, tokens=None, cost=None):
        return {
            "benchmark": benchmark,
            "trial": trial,
            "status": status,
            "duration_seconds": duration,
            "total_tokens": tokens,
            "cost_usd": cost,
        }

    def test_counts_and_mcneamar(self):
        rows_a = [
            self.row("b", 1, "passed", 10.0, 100, 0.01),
            self.row("b", 2, "failed"),
            self.row("b", 3, "passed", 12.0, 120, 0.02),
            self.row("b", 4, "failed"),
        ]
        rows_b = [
            self.row("b", 1, "passed", 20.0, 200, 0.03),
            self.row("b", 2, "failed"),
            self.row("b", 3, "failed"),
            self.row("other", 9, "passed"),  # unmatched — ignored
        ]

        stats = pairwise_statistics(rows_a, rows_b)

        assert stats["matched"] == 3
        assert stats["both_pass"] == 1
        assert stats["a_only"] == 1
        assert stats["b_only"] == 0
        assert stats["both_fail"] == 1
        assert stats["mcnemar_p"] is not None and stats["mcnemar_p"] > 0.05
        # Mutual-pass summaries only cover trial 1.
        assert stats["a_median_duration_mutual_pass"] == 10.0
        assert stats["b_median_duration_mutual_pass"] == 20.0
        assert stats["a_median_tokens_mutual_pass"] == 100
        assert stats["a_median_cost_usd_mutual_pass"] == pytest.approx(0.01)

    def test_unmatched_only_returns_none(self):
        rows_a = [self.row("b", 1, "passed")]
        rows_b = [self.row("x", 1, "passed")]
        assert pairwise_statistics(rows_a, rows_b) is None
        assert pairwise_compare(rows_a, rows_b) is None


class TestParetoFrontier:
    def test_dominant_config_shadows_inferior_one(self):
        frontier = pareto_frontier([
            {"label": "fast-good", "pass_rate": 0.9, "median_duration": 10, "avg_cost_usd": 0.1},
            {"label": "slow-bad", "pass_rate": 0.8, "median_duration": 30, "avg_cost_usd": 0.4},
        ])
        assert frontier == ["fast-good"]

    def test_tradeoff_keeps_both(self):
        frontier = pareto_frontier([
            {"label": "quality", "pass_rate": 0.95, "median_duration": 60, "avg_cost_usd": 1.0},
            {"label": "cheap", "pass_rate": 0.70, "median_duration": 5, "avg_cost_usd": 0.01},
        ])
        assert set(frontier) == {"quality", "cheap"}

    def test_missing_metrics_rank_as_worst(self):
        frontier = pareto_frontier([
            {"label": "measured", "pass_rate": 0.9, "median_duration": 10, "avg_cost_usd": None},
            {"label": "unmeasured", "pass_rate": 0.9, "median_duration": None, "avg_cost_usd": 0.5},
        ])
        assert set(frontier) == {"measured", "unmeasured"} or frontier == ["measured"]
        # Either way, an entry with ALL metrics missing must not appear.
        assert "invisible" not in pareto_frontier([
            {"label": "measured", "pass_rate": 0.5, "median_duration": 10, "avg_cost_usd": 1.0},
            {"label": "invisible", "pass_rate": None, "median_duration": None, "avg_cost_usd": None},
        ])


class TestGroupIqrsAndViolations:
    def _rows(self):
        return [
            {"config_hash": "h1", "agent": "claude-code", "model": "sonnet",
             "status": "passed", "duration_seconds": d, "total_tokens": t,
             "cost_usd": c * 0.001, "violation_count": v}
            for d, t, c, v in [
                (10, 100, 1, 0),
                (20, 200, 2, 1),
                (30, 300, 3, 0),
                (40, 400, 4, 0),
            ]
        ]

    def test_iqr_and_violation_rate_flow_through_aggregation(self):
        groups = aggregate_by_config(self._rows())
        group = groups[0]

        assert group.duration_iqr[0] == pytest.approx(17.5)
        assert group.duration_iqr[1] == pytest.approx(32.5)
        assert group.token_iqr[0] == pytest.approx(175)
        assert group.token_iqr[1] == pytest.approx(325)
        assert group.median_cost_usd == pytest.approx(0.0025)
        assert group.protected_violation_rate == pytest.approx(0.25)
        assert group.runs == 4 and group.passes == 4

    def test_empty_groups_report_none(self):
        empty = ConfigAggregate(config_hash="x", label="y")
        assert empty.token_iqr is None
        assert empty.duration_iqr is None
        assert empty.protected_violation_rate is None
        assert empty.median_cost_usd is None
