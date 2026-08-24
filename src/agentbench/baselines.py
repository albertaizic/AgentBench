"""Reference-patch baseline: prove a benchmark is solvable without an agent.

Maintenance-only mode. The known-good patch is applied to a fresh checkout,
evaluations run exactly as they would for an agent, and the outcome is
persisted as ordinary evidence whose ``agent.type`` is ``reference-baseline``
— never represented as an AI coding agent anywhere it is displayed.

The patch exists ONLY here and inside ``benchmark validate``. It is never
mounted into, copied to, or otherwise exposed to an agent-visible workspace;
real runs go through :func:`agentbench.runner.run_benchmark`, which never
touches ``reference_solution``.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from agentbench.backends import make_backend
from agentbench.backends.base import credential_env
from agentbench.diffs import capture_diff
from agentbench.evaluation import EvaluationOutcome
from agentbench.models import BenchmarkSpec, ExecutionSpec
from agentbench.results import SCHEMA_VERSION, RunArtifacts, RunResult, eval_artifact_stem, write_run
from agentbench.runner import _new_run_id, eval_outcome_rows
from agentbench.taxonomy import Classification
from agentbench.workspace import create_workspace


class BaselineError(RuntimeError):
    pass


def run_reference_baseline(
    spec: BenchmarkSpec,
    *,
    repository: str | None = None,
    benchmark_dir: Path,
    manifest_path: Path | None = None,
    results_root: Path,
    timeout_seconds: float | None = None,
    trial: int | None = None,
    experiment_id: str | None = None,
    keep_workspace: bool = False,
) -> tuple[RunResult, Path]:
    """Apply the declared reference patch and evaluate it like any run."""
    if spec.reference_solution is None:
        raise BaselineError(f"benchmark '{spec.name}' declares no reference_solution")
    patch_path = (benchmark_dir / spec.reference_solution.patch).resolve()
    if benchmark_dir not in patch_path.parents or not patch_path.is_file():
        raise BaselineError(f"reference patch missing: {patch_path}")

    exec_spec = spec.execution or ExecutionSpec()
    timeout = timeout_seconds if timeout_seconds is not None else spec.timeout_seconds
    started_at = datetime.now(timezone.utc)
    run_id = _new_run_id()
    start_monotonic = time.monotonic()
    backend = make_backend(exec_spec, workspace_parent=None)

    workspace = create_workspace(
        repository or spec.repository, spec.commit, keep=keep_workspace
    )
    try:
        apply = subprocess.run(
            ["git", "apply", str(patch_path)], cwd=workspace.path,
            capture_output=True, text=True,
        )
        if apply.returncode != 0:
            raise BaselineError(
                f"reference patch does not apply cleanly: {apply.stderr.strip()}"
            )

        _, credential_evidence = credential_env(exec_spec.pass_env)
        public_outcomes: list[EvaluationOutcome] = []
        hidden_outcomes: list[EvaluationOutcome] = []
        try:
            placeholders = backend.placeholders(workspace=workspace.path, hidden_dir=None)
            for evaluation in spec.evaluations:
                from agentbench.evaluation import substitute_placeholders

                command = substitute_placeholders(
                    evaluation.command,
                    workspace=Path(placeholders["workspace"]),
                    python_executable=placeholders["python"],
                    hidden_dir=None,
                )
                public_outcomes.append(EvaluationOutcome(
                    name=evaluation.name,
                    command=command,
                    exit_code=(result := backend.run_public_evaluation(
                        command, workspace=workspace.path, timeout=timeout, env=None,
                    )).exit_code,
                    passed=result.exit_code == 0,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    duration_seconds=result.duration_seconds,
                    timed_out=result.timed_out,
                ))
            if spec.hidden_evaluations is not None:
                from agentbench.evaluation import run_hidden_evaluation

                hidden_dir = (benchmark_dir / spec.hidden_evaluations.source).resolve()
                hidden_outcomes = [
                    run_hidden_evaluation(evaluation, workspace=workspace.path,
                                          hidden_dir=hidden_dir, timeout=timeout)
                    for evaluation in spec.hidden_evaluations.evaluations
                ]
        except Exception as exc:  # noqa: BLE001 - harness anomaly surfaces honestly
            classification = Classification("invalid_result", f"{type(exc).__name__}: {exc}")
        else:
            all_outcomes = [*public_outcomes, *hidden_outcomes]
            classification = (
                Classification("passed", None)
                if all(o.passed for o in all_outcomes)
                else Classification("evaluation_failed", "reference patch failed evaluators")
            )

        diff = capture_diff(workspace.path, base=workspace.head_commit)
        result = RunResult(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            trial=trial,
            benchmark={
                "name": spec.name,
                "repository": spec.repository,
                "commit": spec.commit,
                "resolved_commit": workspace.head_commit,
                "config_hash": spec.config_hash(),
            },
            agent={
                "type": "reference-baseline",
                "exit_code": 0,
                "timed_out": False,
                "duration_seconds": round(time.monotonic() - start_monotonic, 3),
                "model": None,
                "capabilities": [],
            },
            usage=None,
            diff={
                "files_changed": diff.stats.files_changed,
                "insertions": diff.stats.insertions,
                "deletions": diff.stats.deletions,
                "patch_file": "diff.patch",
                "changed_paths": list(diff.changed_paths),
                "added_files": [], "deleted_files": [], "renamed_files": [], "binary_files": [],
            },
            evaluations=eval_outcome_rows(public_outcomes, 0),
            hidden_evaluations=eval_outcome_rows(hidden_outcomes, len(public_outcomes)),
            protected_paths=None,
            overall={
                "status": classification.status,
                "failure_reason": classification.reason,
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(time.monotonic() - start_monotonic, 3),
            },
            execution={
                **backend.provenance(),
                "pass_env_evidence": credential_evidence,
                "baseline_kind": "reference_patch",
            },
            environment={},
            config={
                **spec.config_snapshot(),
                "_baseline": "reference_patch",
                "_benchmark_manifest": str(manifest_path) if manifest_path else None,
            },
            experiment_id=experiment_id,
            config_name=None,
            workspace_kept=keep_workspace,
            workspace_path=str(workspace.path) if keep_workspace else None,
            stage_timings=None,
        )
        artifacts = RunArtifacts(
            agent_stdout="",
            agent_stderr="",
            patch=diff.patch,
            eval_outputs={
                eval_artifact_stem(index, o.name): (o.stdout, o.stderr)
                for index, o in enumerate([*public_outcomes, *hidden_outcomes])
            },
        )
        run_dir = write_run(result, artifacts, results_root=results_root, run_dir_name=run_id)
        return result, run_dir
    finally:
        backend.cleanup()
        workspace.cleanup()