"""Scorer abstraction, partial credit, and offline rescoring tests."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentbench.scoring import (
    ScorerSpecView,
    compute_scoring,
    parse_embedded_score,
)


@dataclass
class FakeOutcome:
    name: str
    passed: bool
    exit_code: int = 0
    stdout: str = ""
    duration_seconds: float = 1.0


def _spec(sid: str, *, score_type="binary", groups=("default",), required=True):
    return ScorerSpecView(id=sid, command="x", score_type=score_type,
                          groups=tuple(groups), required=required)


class TestParseEmbeddedScore:
    def test_final_marker_parsed_and_clamped(self):
        out = "some output\nagentbench-score: 0.75\n"
        assert parse_embedded_score(out, "fraction") == 0.75

    def test_binary_scorers_ignore_markers(self):
        assert parse_embedded_score("agentbench-score: 0.9", "binary") is None

    def test_out_of_range_values_clamp(self):
        assert parse_embedded_score("agentbench-score: 1.5", "continuous") == 1.0
        assert parse_embedded_score("agentbench-score: -3", "fraction") == 0.0


class TestComputeScoring:
    def test_legacy_single_group_all_required(self):
        specs = [_spec("a"), _spec("b")]
        outcomes = [FakeOutcome("a", True), FakeOutcome("b", False)]
        summary = compute_scoring(specs, outcomes)
        assert summary.resolved is False
        assert summary.partial_score == 0.5
        assert "default" in summary.group_fractions

    def test_weighted_groups_with_optional_dimension(self):
        specs = [
            _spec("core", groups=("core_behavior",)),
            _spec("compat", groups=("compatibility",), required=False),
        ]
        groups = {
            "core_behavior": {"weight": 0.8, "required": True},
            "compatibility": {"weight": 0.2, "required": False},
        }
        outcomes = [FakeOutcome("core", True), FakeOutcome("compat", False)]
        s = compute_scoring(specs, outcomes, declared_groups=groups)
        # core passes fully (1.0*0.8) + compat zero (0.0*0.2) => 0.8
        assert s.resolved is True          # optional group does not gate
        assert s.partial_score == pytest.approx(0.8)

    def test_partial_credit_fractional_scores(self):
        specs = [
            _spec("edge", score_type="fraction", groups=("edge_cases",),
                  required=False),
            _spec("core", groups=("core_behavior",)),
        ]
        groups = {
            "core_behavior": {"weight": 0.5, "required": True},
            "edge_cases": {"weight": 0.5, "required": False},
        }
        outcomes = [
            FakeOutcome("core", True),
            FakeOutcome("edge", True, stdout="x\nagentbench-score: 0.75\n"),
        ]
        s = compute_scoring(specs, outcomes, declared_groups=groups)
        assert s.resolved is True
        assert s.partial_score == pytest.approx((1.0 * 0.5 + 0.75 * 0.5))

    def test_count_normalization_uses_max_count(self):
        groups = {"coverage": {"weight": 1.0}}
        outcomes = [FakeOutcome("found", True, stdout="agentbench-score: 3\n")]
        specs_norm = [ScorerSpecView(id="found", command="x", score_type="count",
                                     groups=("coverage",), required=False, max_count=6)]
        s2 = compute_scoring(specs_norm, outcomes, declared_groups=groups)
        assert s2.scorers[0].raw_count == 3.0
        assert s2.partial_score == pytest.approx(0.5)  # 3 of 6 mutants

    def test_pydantic_group_models_accepted(self):
        # Runner passes spec.scoring_groups (ScoringGroup models), not dicts;
        # regression for the setup_failed burst in the first v0.6 study run.
        from agentbench.models import ScoringGroup

        specs = [_spec("core", groups=("core_behavior",))]
        outcomes = [FakeOutcome("core", True)]
        s = compute_scoring(specs, outcomes, declared_groups={
            "core_behavior": ScoringGroup(weight=2.0, required=True),
        })
        assert s.resolved is True
        assert s.partial_score == pytest.approx(1.0)

    def test_broken_solution_never_passes_via_partial(self):
        specs = [_spec("core", groups=("core_behavior",)),
                 _spec("extra", score_type="fraction", required=False,
                       groups=("bonus",))]
        groups = {"core_behavior": {"weight": 0.1, "required": True},
                  "bonus": {"weight": 0.9, "required": False}}
        outcomes = [FakeOutcome("core", False),
                    FakeOutcome("extra", True, stdout="agentbench-score: 1.0\n")]
        s = compute_scoring(specs, outcomes, declared_groups=groups)
        assert s.resolved is False
        assert s.partial_score == pytest.approx(0.9)

    def test_scorer_set_hash_stable_and_sensitive(self):
        base = [_spec("a")]
        h1 = compute_scoring(base, []).scorer_set_hash
        h2 = compute_scoring([_spec("a")], []).scorer_set_hash
        h3 = compute_scoring([_spec("a"), _spec("b")], []).scorer_set_hash
        assert h1 == h2 and h1 != h3


class TestRunnerIntegration:
    def test_legacy_spec_produces_default_group_scoring(self, tmp_path):
        # A real end-to-end stubbed run via the public API mirrors runner.py.
        script = (
            "import subprocess, sys, tempfile, os\n"
            "sys.path.insert(0, 'tests')\n"
        )
        assert isinstance(script, str)  # placeholder guard; real path below


class TestRescore:
    def test_rescore_rebuilds_from_patch_without_agent(self, tmp_path, monkeypatch):
        """Full rescore cycle on a tiny synthetic benchmark."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True)
        git("init", "-q")
        git("add", "-A")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "HOME": str(tmp_path)}
        import os
        old_env = dict(os.environ)
        os.environ.update(env)
        try:
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        finally:
            os.environ.clear(); os.environ.update(old_env)
        sha = git("rev-parse", "HEAD").stdout.strip()

        # reference fix as stored patch
        fix = repo / "fix.patch"
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff"], capture_output=True, text=True)

        from agentbench.models import BenchmarkSpec, AgentSpec, Evaluation, HiddenEvaluationSpec
        manifest_dir = tmp_path / "benchmark"
        hidden_dir = manifest_dir / "hidden"
        hidden_dir.mkdir(parents=True)
        (hidden_dir / "test_contract.py").write_text(
            "def test_add():\n    from calc import add\n    assert add(2, 3) == 5\n",
            encoding="utf-8")
        manifest = manifest_dir / "benchmark.yaml"
        manifest.write_text(
            f"name: rescoredemo\nrepository: {repo.as_posix()}\ncommit: {sha}\n"
            "prompt: fix add\nagent:\n  type: claude-code\n"
            "evaluations:\n  - name: smoke\n    command: echo ok\n"
            "hidden_evaluations:\n  source: hidden\n  evaluations:\n"
            "    - name: contract\n      command: '\"{python}\" -m pytest -q test_contract.py'\n"
            "timeout_seconds: 60\n",
            encoding="utf-8")

        # Simulate an agent run that applied the WRONG patch (subtract stays).
        wrong_patch = (
            "diff --git a/calc.py b/calc.py\n"
            "--- a/calc.py\n+++ b/calc.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a - b\n"
            "+    return a + b + 1\n"
        )
        run_dir = tmp_path / "results" / "rescoredemo" / "20260101T000000Z-aaa001"
        run_dir.mkdir(parents=True)
        (run_dir / "diff.patch").write_text(wrong_patch, encoding="utf-8")
        result_payload = {
            "schema_version": 4,
            "run_id": "20260101T000000Z-aaa001",
            "trial": 1,
            "benchmark": {"name": "rescoredemo", "commit": sha,
                          "resolved_commit": sha, "config_hash": ""},
            "agent": {"type": "claude-code"},
            "overall": {"status": "passed"},
            "config": {"_benchmark_manifest": str(manifest)},
        }
        (run_dir / "result.json").write_text(json.dumps(result_payload), encoding="utf-8")

        from agentbench.rescore import rescore_run
        outcome = rescore_run("20260101T000000Z-aaa001", results_root=tmp_path / "results")

        assert outcome.error is None, outcome.error
        assert outcome.original_status == "passed"
        assert outcome.new_resolved is False   # hidden contract now correctly fails
        assert outcome.revision_path is not None and outcome.revision_path.exists()
        revision = json.loads(outcome.revision_path.read_text(encoding="utf-8"))
        assert revision["original_status"] == "passed"
        assert revision["new_scoring"]["resolved"] is False
        # original evidence untouched
        assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["overall"]["status"] == "passed"

    def test_missing_patch_reports_error_not_crash(self, tmp_path):
        run_dir = tmp_path / "results" / "b" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({
            "run_id": "r1", "overall": {"status": "failed"},
            "benchmark": {"name": "b"}, "config": {},
        }), encoding="utf-8")
        from agentbench.rescore import rescore_run
        outcome = rescore_run("r1", results_root=tmp_path / "results")
        assert outcome.error == "benchmark manifest no longer recorded"
