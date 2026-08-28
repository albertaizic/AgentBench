"""Partial-score adversarial tests (v0.6 release hardening, mission VI).

Covers required/optional group failures, weight renormalization, duplicate
scorer ids, missing scorers, unparseable/out-of-range markers, empty scoring
definitions, and the legacy-manifests-with-declared-groups trap that used to
fabricate ``partial_score: 0.0`` for fully resolved runs.
"""

from __future__ import annotations

import math

import pytest

from agentbench.models import BenchmarkSpec, Scorer
from agentbench.scoring import (ScorerSpecView, ScoringSummary,
                                compute_scoring)


class Outcome:
    """Minimal stand-in for an executed evaluation outcome."""

    def __init__(self, name: str, passed: bool, stdout: str | None = None,
                 exit_code: int = 0):
        self.name = name
        self.passed = passed
        self.stdout = stdout
        self.exit_code = exit_code


GROUPS = {
    "core_behavior": {"weight": 0.5, "required": True},
    "edge_cases": {"weight": 0.5, "required": False},
}


def spec_view(scorer_id: str, groups=("default",), required=False,
              score_type="binary") -> ScorerSpecView:
    return ScorerSpecView(id=scorer_id, command="true", groups=tuple(groups),
                          required=required, score_type=score_type)


def test_required_group_failure_blocks_resolution_but_scores_partial():
    specs = [spec_view("core", ("core_behavior",), required=True),
             spec_view("edge", ("edge_cases",))]
    summary = compute_scoring(specs,
                              [Outcome("core", False), Outcome("edge", True)],
                              declared_groups=GROUPS)
    assert summary.resolved is False
    assert summary.partial_score == 0.5      # optional success still counts

def test_optional_group_failure_keeps_resolution():
    specs = [spec_view("core", ("core_behavior",), required=True),
             spec_view("edge", ("edge_cases",))]
    summary = compute_scoring(specs,
                              [Outcome("core", True), Outcome("edge", False)],
                              declared_groups=GROUPS)
    assert summary.resolved is True
    assert summary.partial_score == 0.5      # half the weight passed


def test_weights_renormalize_when_not_summing_to_one():
    groups = {"a": {"weight": 3.0}, "b": {"weight": 1.0}}
    specs = [spec_view("x1", ("a",)), spec_view("x2", ("b",))]
    summary = compute_scoring(specs,
                              [Outcome("x1", True), Outcome("x2", False)],
                              declared_groups=groups)
    # 3*1 + 1*0 over 4 → 0.75 despite weights summing to 4.
    assert abs(summary.partial_score - 0.75) < 1e-9


def _minimal_spec(scorers: list[dict]) -> dict:
    return {
        "name": "dup", "repository": "fixture", "commit": "a" * 40,
        "prompt": "p", "timeout_seconds": 60,
        "agent": {"type": "command", "argv": ["true"]},
        "evaluations": [{"name": "base", "command": "true"}],
        "scorers": scorers,
    }


def test_duplicate_scorer_ids_rejected_at_schema_level():
    with pytest.raises(ValueError, match="scorer ids must be unique"):
        BenchmarkSpec.model_validate(_minimal_spec([
            {"id": "same", "command": "true"},
            {"id": "same", "command": "false"},
        ]))


def test_zero_weight_group_rejected_by_schema():
    from agentbench.models import ScoringGroup
    with pytest.raises(ValueError):
        ScoringGroup(weight=0.0)


def test_missing_scorer_outcome_counts_as_failed_not_crash():
    specs = [spec_view("present", required=True),
             spec_view("absent", required=True)]
    summary = compute_scoring(specs, [Outcome("present", True)])
    assert summary.resolved is False
    absent = next(r for r in summary.scorers if r.id == "absent")
    assert absent.passed is False


def test_nan_and_infinity_markers_are_never_parsed():
    specs = [ScorerSpecView(id="f", command="c", score_type="fraction")]
    for hostile in ("agentbench-score: nan", "agentbench-score: inf",
                    "agentbench-score: -inf"):
        summary = compute_scoring(specs, [Outcome("f", True, hostile)])
        assert summary.scorers[0].score is None
    values = [r.score for r in summary.scorers]
    assert all(v is None or math.isfinite(v) for v in values)


def test_out_of_range_markers_clamp_into_unit_interval():
    specs = [ScorerSpecView(id="f", command="c", score_type="fraction")]
    hi = compute_scoring(specs, [Outcome("f", True, "agentbench-score: 42")])
    lo = compute_scoring(specs, [Outcome("f", False, "agentbench-score: -7")])
    assert hi.scorers[0].score == 1.0
    assert lo.scorers[0].score == 0.0


def test_empty_scoring_definition_never_resolves_vacuously():
    summary = compute_scoring([], [])
    assert summary.resolved is False
    assert summary.partial_score is None


def test_legacy_declared_groups_without_scorers_stay_honest():
    # The statelock pattern: weighted groups declared, only legacy hidden
    # evaluations execute in "default". Resolved runs used to report
    # partial=0.0; now the covered dimension scores honestly and the gap
    # is disclosed instead of silently counted as zero credit.
    specs = [spec_view("smoke")]
    passed = compute_scoring(specs, [Outcome("smoke", True)],
                             declared_groups=GROUPS)
    failed = compute_scoring(specs, [Outcome("smoke", False)],
                             declared_groups=GROUPS)
    assert passed.resolved is True and passed.partial_score == 1.0
    assert failed.resolved is True and failed.partial_score == 0.0
    assert sorted(passed.uncovered_groups) == ["core_behavior", "edge_cases"]
    payload = passed.to_dict()
    assert payload["uncovered_groups"] == passed.uncovered_groups


def test_partial_score_never_flips_binary_resolution():
    specs = [spec_view("req", required=True),
             spec_view("bonus", ("extras",))]
    groups = {"default": {"weight": 0.25, "required": True},
              "extras": {"weight": 3.0, "required": False}}
    summary = compute_scoring(
        specs,
        [Outcome("req", True), Outcome("bonus", True)],
        declared_groups=groups)
    assert summary.resolved is True
    # Even a perfect partial score cannot rescue a required failure:
    summary_bad = compute_scoring(
        specs,
        [Outcome("req", False), Outcome("bonus", True)],
        declared_groups=groups)
    assert summary_bad.resolved is False
    assert abs(summary_bad.partial_score - 0.9231) < 1e-4   # high, but not a pass


def test_unique_scorer_ids_are_accepted():
    spec = BenchmarkSpec.model_validate(_minimal_spec([
        {"id": "one", "command": "true"},
        {"id": "two", "command": "true"},
    ]))
    assert [s.id for s in spec.scorers] == ["one", "two"]
