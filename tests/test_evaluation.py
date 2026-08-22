"""Tests for evaluation execution and PASS/FAIL determination (agentbench.evaluation)."""

from __future__ import annotations

import sys

from agentbench.evaluation import EvaluationOutcome, overall_status, run_evaluation
from agentbench.models import Evaluation


def make_eval(name: str = "check", command: str | None = None) -> Evaluation:
    return Evaluation(
        name=name,
        command=command or f'"{sys.executable}" -c "raise SystemExit(0)"',
    )


class TestRunEvaluation:
    def test_zero_exit_code_means_pass(self, tmp_path):
        outcome = run_evaluation(make_eval(), workspace=tmp_path, timeout=60.0)

        assert isinstance(outcome, EvaluationOutcome)
        assert outcome.passed is True
        assert outcome.exit_code == 0
        assert outcome.timed_out is False

    def test_nonzero_exit_code_means_fail(self, tmp_path):
        failing = make_eval(command=f'"{sys.executable}" -c "print(\'nope\'); raise SystemExit(2)"')

        outcome = run_evaluation(failing, workspace=tmp_path, timeout=60.0)

        assert outcome.passed is False
        assert outcome.exit_code == 2
        assert "nope" in outcome.stdout  # output preserved for debugging

    def test_command_runs_inside_workspace(self, local_repo):
        repo_path, _ = local_repo
        inner = "import os; print(sorted(os.listdir('.')))"
        listing = make_eval(command=f'"{sys.executable}" -c "{inner}"')

        outcome = run_evaluation(listing, workspace=repo_path, timeout=60.0)

        assert "'src'" in outcome.stdout

    def test_timeout_fails_the_evaluation(self, tmp_path):
        slow = make_eval(command=f'"{sys.executable}" -c "import time; time.sleep(30)"')

        outcome = run_evaluation(slow, workspace=tmp_path, timeout=2.0)

        assert outcome.timed_out is True
        assert outcome.passed is False


class TestOverallStatus:
    def test_all_passing_evaluations_pass(self):
        outcomes = [
            EvaluationOutcome(name="a", command="x", exit_code=0, passed=True, stdout="", stderr="", duration_seconds=1.0, timed_out=False),
            EvaluationOutcome(name="b", command="y", exit_code=0, passed=True, stdout="", stderr="", duration_seconds=1.0, timed_out=False),
        ]

        assert overall_status(outcomes) == "passed"

    def test_any_failing_evaluation_fails_the_run(self):
        outcomes = [
            EvaluationOutcome(name="a", command="x", exit_code=0, passed=True, stdout="", stderr="", duration_seconds=1.0, timed_out=False),
            EvaluationOutcome(name="b", command="y", exit_code=1, passed=False, stdout="", stderr="", duration_seconds=1.0, timed_out=False),
        ]

        assert overall_status(outcomes) == "failed"

    def test_no_evaluations_cannot_pass(self):
        assert overall_status([]) == "failed"
