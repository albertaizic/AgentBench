"""Tests for evaluation execution and PASS/FAIL determination (agentbench.evaluation)."""

from __future__ import annotations

import sys
import textwrap

from agentbench.evaluation import (
    EvaluationOutcome,
    overall_status,
    run_evaluation,
    run_hidden_evaluation,
    substitute_placeholders,
)
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


class TestPlaceholders:
    def test_workspace_python_and_hidden_dir_substituted(self, tmp_path):
        hidden = tmp_path / "hidden"
        command = '"{python}" -m pytest "{hidden_dir}" --rootdir={workspace}'

        resolved = substitute_placeholders(
            command,
            workspace=tmp_path / "ws",
            hidden_dir=hidden,
            python_executable="py.exe",
        )

        assert str(tmp_path / "ws") in resolved
        assert str(hidden) in resolved
        assert "py.exe" in resolved
        assert "{" not in resolved

    def test_plain_commands_pass_through_unchanged(self, tmp_path):
        assert substitute_placeholders("pytest -q", workspace=tmp_path) == "pytest -q"


class TestHiddenEvaluationIsolation:
    def test_runs_in_hidden_dir_and_imports_workspace_code(self, tmp_path):
        # Workspace contains the package under test; the hidden evaluator
        # lives outside it and imports it via PYTHONPATH.
        package_dir = tmp_path / "workspace"
        (package_dir / "stockflow").mkdir(parents=True)
        (package_dir / "stockflow" / "__init__.py").write_text("VALUE = 41\n", encoding="utf-8")
        hidden_dir = tmp_path / "hidden"
        hidden_dir.mkdir()
        (hidden_dir / "test_behavior.py").write_text(
            textwrap.dedent(
                """
                import os
                from pathlib import Path

                from stockflow import VALUE


                def test_value():
                    assert VALUE == 41


                def test_cwd_is_hidden_dir():
                    Path("seen_from").write_text(os.getcwd())
                """
            ),
            encoding="utf-8",
        )
        evaluation = Evaluation(name="behavioral", command='"{python}" -m pytest -q')

        outcome = run_hidden_evaluation(evaluation, workspace=package_dir, hidden_dir=hidden_dir, timeout=60)

        assert outcome.passed is True, outcome.stdout + outcome.stderr
        # Proof the evaluator ran from OUTSIDE the agent workspace:
        assert (hidden_dir / "seen_from").exists()

    def test_hidden_sources_never_appear_in_the_workspace(self, tmp_path):
        package_dir = tmp_path / "workspace"
        package_dir.mkdir()
        hidden_dir = tmp_path / "hidden"
        hidden_dir.mkdir()
        (hidden_dir / "test_secret.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        evaluation = Evaluation(name="hidden-check", command='"{python}" -m pytest -q')

        run_hidden_evaluation(evaluation, workspace=package_dir, hidden_dir=hidden_dir, timeout=60)

        assert list(package_dir.iterdir()) == []  # nothing copied into the workspace
