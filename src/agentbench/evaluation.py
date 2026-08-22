"""Run evaluation commands inside the workspace; exit codes decide PASS/FAIL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentbench.models import Evaluation
from agentbench.process import run_shell_command


@dataclass(frozen=True)
class EvaluationOutcome:
    name: str
    command: str
    exit_code: int | None
    passed: bool
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


def run_evaluation(evaluation: Evaluation, *, workspace: Path, timeout: float) -> EvaluationOutcome:
    """Run one evaluation command with *workspace* as its working directory."""
    result = run_shell_command(evaluation.command, cwd=workspace, timeout=timeout)

    return EvaluationOutcome(
        name=evaluation.name,
        command=evaluation.command,
        exit_code=result.exit_code,
        passed=result.exit_code == 0,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
        timed_out=result.timed_out,
    )


def overall_status(outcomes: list[EvaluationOutcome]) -> str:
    """A run passes only if every evaluation passed; nothing to run means fail."""
    return "passed" if outcomes and all(outcome.passed for outcome in outcomes) else "failed"
