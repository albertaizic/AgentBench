"""Run evaluation commands; exit codes decide PASS/FAIL.

Public evaluations execute inside the agent workspace. Hidden evaluations
execute from their own source directory — outside the workspace, which never
receives their files — with the workspace prepended to ``PYTHONPATH`` so they
can import the package the agent worked on.

Commands may reference these placeholders (plain substitution, no shell):

* ``{python}``    – the AgentBench interpreter running this process
* ``{workspace}`` – the cloned agent workspace
* ``{hidden_dir}``– the hidden-evaluator source directory
"""

from __future__ import annotations

import os
import sys
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


def substitute_placeholders(
    command: str,
    *,
    workspace: Path,
    hidden_dir: Path | None = None,
    python_executable: str | None = None,
) -> str:
    resolved = command
    replacements: dict[str, str] = {
        "{workspace}": str(workspace),
        "{python}": python_executable or sys.executable,
        "{hidden_dir}": str(hidden_dir or ""),
    }
    for placeholder, value in replacements.items():
        resolved = resolved.replace(placeholder, value)
    return resolved


def run_evaluation(evaluation: Evaluation, *, workspace: Path, timeout: float) -> EvaluationOutcome:
    """Run one public evaluation command inside *workspace*."""
    command = substitute_placeholders(evaluation.command, workspace=workspace)
    result = run_shell_command(command, cwd=workspace, timeout=timeout)

    return EvaluationOutcome(
        name=evaluation.name,
        command=command,
        exit_code=result.exit_code,
        passed=result.exit_code == 0,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
        timed_out=result.timed_out,
    )


def run_hidden_evaluation(
    evaluation: Evaluation,
    *,
    workspace: Path,
    hidden_dir: Path,
    timeout: float,
) -> EvaluationOutcome:
    """Run one hidden evaluation from *hidden_dir*, importing code from the workspace."""
    command = substitute_placeholders(evaluation.command, workspace=workspace, hidden_dir=hidden_dir)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(workspace) + (os.pathsep + existing if existing else "")

    result = run_shell_command(command, cwd=hidden_dir, timeout=timeout, env=env)

    return EvaluationOutcome(
        name=evaluation.name,
        command=command,
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
