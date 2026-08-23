"""Tests for aggregation math (agentbench.aggregate)."""

from __future__ import annotations

from agentbench.aggregate import (
    aggregate_by_config,
    format_count,
    format_duration,
    format_percent,
)


def row(run_id: str, *, status="passed", duration=10.0, files=1, ins=3, dels=1,
        tokens=None, cost=None, model="m1", commit="c1", config_hash="cfg1", agent="claude-code"):
    return {
        "run_id": run_id,
        "status": status,
        "duration_seconds": duration,
        "files_changed": files,
        "insertions": ins,
        "deletions": dels,
        "total_tokens": tokens,
        "cost_usd": cost,
        "model": model,
        "resolved_commit": commit,
        "config_hash": config_hash,
        "agent": agent,
    }


class TestAggregateByConfig:
    def test_same_config_groups_together_with_medians(self):
        rows = [
            row("r1", status="passed", duration=10.0, files=1, ins=3, dels=1, tokens=100),
            row("r2", status="evaluation_failed", duration=30.0, files=3, ins=9, dels=3, tokens=300),
            row("r3", status="passed", duration=20.0, files=2, ins=6, dels=2, tokens=200),
        ]

        groups = aggregate_by_config(rows)

        assert len(groups) == 1
        group = groups[0]
        assert group.runs == 3
        assert group.passes == 2
        assert group.pass_rate == 2 / 3
        assert group.median_duration == 20.0
        assert group.median_files_changed == 2
        assert group.median_lines_changed == 8
        assert group.median_total_tokens == 200

    def test_different_config_hashes_never_merge(self):
        rows = [row("r1", config_hash="aaa"), row("r2", config_hash="bbb")]

        assert len(aggregate_by_config(rows)) == 2

    def test_mixed_commits_are_reported_per_group(self):
        rows = [
            row("r1", commit="commit-a"),
            row("r2", commit="commit-b"),
        ]

        group = aggregate_by_config(rows)[0]

        assert group.resolved_commits == {"commit-a", "commit-b"}

    def test_missing_token_metrics_excluded_from_median(self):
        rows = [
            row("r1", tokens=100),
            row("r2", tokens=None),  # stub agent: metrics unavailable
        ]

        group = aggregate_by_config(rows)[0]

        assert group.median_total_tokens == 100
        assert group.avg_cost_usd is None

    def test_labels_include_model_when_known(self):
        label = aggregate_by_config([row("r1", model="sonnet")])[0].label

        assert label == "claude-code/sonnet"

    def test_label_without_model(self):
        label = aggregate_by_config([row("r1", model=None)])[0].label

        assert label == "claude-code"


class TestFormatters:
    def test_duration_formatting(self):
        assert format_duration(None) == "—"
        assert format_duration(38.4) == "38s"
        assert format_duration(252.0) == "4:12"

    def test_count_formatting(self):
        assert format_count(None) == "—"
        assert format_count(142_000) == "142k"
        assert format_count(1_250_000) == "1.2m"
        assert format_count(42) == "42"

    def test_percent_formatting(self):
        assert format_percent(None) == "—"
        assert format_percent(0.9) == "90%"
        assert format_percent(2 / 3) == "67%"
