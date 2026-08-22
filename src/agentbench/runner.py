"""Orchestrate one benchmark run end to end.

Pipeline: clone+checkout workspace → run the agent → capture the diff → run
evaluations → decide PASS/FAIL from evaluation exit codes → serialize. Results
are persisted *before* workspace cleanup so that a cleanup failure (e.g. a
transient Windows file lock) cannot destroy the evidence of a completed run.
The adapter is injectable so tests can exercise the whole pipeline without a
real coding-agent session.
"""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentbench import __version__
from agentbench.adapters import AgentAdapter, get_adapter
from agentbench.diffs import capture_diff
from agentbench.evaluation import overall_status, run_evaluation
from agentbench.models import BenchmarkSpec
from agentbench.process import run_command
from agentbench.results import RunArtifacts, RunResult, eval_artifact_stem, write_run
from agentbench.workspace import create_workspace


@dataclass(frozen=True)
class RunOutcome:
    result: RunResult
    run_dir: Path
    workspace_path: Path | None  # set only when the workspace was kept


def run_benchmark(
    spec: BenchmarkSpec,
    *,
    adapter: AgentAdapter | None = None,
    results_root: Path | None = None,
    workspace_parent: Path | None = None,
    keep_workspace: bool = False,
    timeout_seconds: float | None = None,
) -> RunOutcome:
    """Execute *spec* once and persist the result; returns where it landed."""
    adapter = adapter if adapter is not None else get_adapter(spec.agent.type)
    timeout = timeout_seconds if timeout_seconds is not None else spec.timeout_seconds
    started_at = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()

    with create_workspace(
        spec.repository, spec.commit, parent=workspace_parent, keep=keep_workspace
    ) as workspace:
        invocation = adapter.build_invocation(
            workspace=workspace.path, prompt=spec.prompt, agent_spec=spec.agent
        )
        agent_run = run_command(
            invocation.argv,
            cwd=workspace.path,
            timeout=timeout,
            input_text=invocation.input_text,
        )
        # Diff against the pinned pre-agent commit: HEAD moves if the agent
        # commits its own work, and a mutable reference would report a
        # no-change run behind a passing one.
        diff = capture_diff(workspace.path, base=workspace.head_commit)
        outcomes = [
            run_evaluation(evaluation, workspace=workspace.path, timeout=timeout)
            for evaluation in spec.evaluations
        ]

        result = RunResult(
            benchmark={
                "name": spec.name,
                "repository": spec.repository,
                "commit": spec.commit,
                "resolved_commit": workspace.head_commit,
            },
            agent={
                "type": spec.agent.type,
                "exit_code": agent_run.exit_code,
                "timed_out": agent_run.timed_out,
                "duration_seconds": round(agent_run.duration_seconds, 3),
            },
            diff={
                "files_changed": diff.stats.files_changed,
                "insertions": diff.stats.insertions,
                "deletions": diff.stats.deletions,
                "patch_file": "diff.patch",
            },
            evaluations=[
                {
                    "name": outcome.name,
                    "command": outcome.command,
                    "exit_code": outcome.exit_code,
                    "passed": outcome.passed,
                    "timed_out": outcome.timed_out,
                    "duration_seconds": round(outcome.duration_seconds, 3),
                    "stdout_file": f"evals/{stem}.stdout.log",
                    "stderr_file": f"evals/{stem}.stderr.log",
                }
                for outcome, stem in (
                    (outcome, eval_artifact_stem(index, outcome.name))
                    for index, outcome in enumerate(outcomes)
                )
            ],
            overall={
                "status": overall_status(outcomes),
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(time.monotonic() - start_monotonic, 3),
            },
            environment={
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "agentbench_version": __version__,
            },
            workspace_kept=keep_workspace,
            workspace_path=str(workspace.path) if keep_workspace else None,
        )
        artifacts = RunArtifacts(
            agent_stdout=agent_run.stdout,
            agent_stderr=agent_run.stderr,
            patch=diff.patch,
            eval_outputs={
                eval_artifact_stem(index, outcome.name): (outcome.stdout, outcome.stderr)
                for index, outcome in enumerate(outcomes)
            },
        )
        # Persist while still inside the with-block: cleanup runs on exit and
        # must never be able to take the results down with it.
        root = results_root if results_root is not None else Path(spec.results_dir)
        run_dir = write_run(result, artifacts, results_root=root)

    return RunOutcome(
        result=result,
        run_dir=run_dir,
        workspace_path=workspace.path if keep_workspace else None,
    )
