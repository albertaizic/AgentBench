"""Orchestrate one benchmark run end to end.

Pipeline: clone+checkout workspace → execute the agent through the configured
execution backend → parse optional metrics → capture the diff → evaluate
change policies → run public evaluations → run hidden evaluations (always
host-side) → classify the outcome → persist evidence → return. Results are
written *before* workspace cleanup so a cleanup failure can never destroy the
evidence of a completed run.

The adapter (how to invoke an agent) and the execution backend (where that
invocation runs) are independent injectables, so tests can exercise the whole
pipeline without a real coding agent or Docker.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentbench.adapters import AgentAdapter, get_adapter
from agentbench.backends import make_backend
from agentbench.backends.base import ExecutionBackend, credential_env
from agentbench.diffs import capture_diff
from agentbench.envmeta import capture_environment
from agentbench.evaluation import (
    EvaluationOutcome,
    run_evaluation,
    run_hidden_evaluation,
    substitute_placeholders,
)
from agentbench.models import AgentSpec, BenchmarkSpec, ExecutionSpec
from agentbench.process import run_command
from agentbench.protected import find_policy_violations
from agentbench.results import SCHEMA_VERSION, RunArtifacts, RunResult, eval_artifact_stem, write_run
from agentbench.taxonomy import Classification, classify_run
from agentbench.workspace import Workspace, create_workspace


@dataclass(frozen=True)
class RunOutcome:
    result: RunResult
    run_dir: Path
    workspace_path: Path | None  # set only when the workspace was kept


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _resolve_hidden_dir(spec: BenchmarkSpec, benchmark_dir: Path | None) -> Path:
    hidden = spec.hidden_evaluations
    if hidden is None:
        raise ValueError("no hidden_evaluations configured")
    if benchmark_dir is None:
        raise ValueError("hidden_evaluations require the benchmark file's directory")
    resolved = (benchmark_dir / hidden.source).resolve()
    base = benchmark_dir.resolve()
    if base not in resolved.parents:
        raise ValueError(
            f"hidden_evaluations source {hidden.source!r} escapes the benchmark directory"
        )
    if not resolved.is_dir():
        raise ValueError(f"hidden_evaluations source not found: {resolved}")
    return resolved


def _effective_policies(spec: BenchmarkSpec) -> list[tuple[list[str], str]]:
    """Merge v0.2-style protected paths with declarative change policies."""
    policies: list[tuple[list[str], str]] = []
    if spec.protected_paths:
        grade = "fail" if spec.fail_on_protected_path_violation else "warn"
        policies.append((list(spec.protected_paths), grade))
    for change_policy in spec.change_policies:
        policies.append((list(change_policy.patterns), change_policy.policy))
    return policies


def run_benchmark(
    spec: BenchmarkSpec,
    *,
    adapter: AgentAdapter | None = None,
    results_root: Path | None = None,
    workspace_parent: Path | None = None,
    keep_workspace: bool = False,
    timeout_seconds: float | None = None,
    trial: int | None = None,
    repository: str | None = None,
    benchmark_dir: Path | None = None,
    manifest_path: Path | None = None,
    execution: ExecutionSpec | None = None,
    agent_override: AgentSpec | None = None,
    experiment_id: str | None = None,
    config_name: str | None = None,
) -> RunOutcome:
    """Execute *spec* once and persist the result; returns where it landed.

    ``repository`` lets the caller hand over an already-resolved clone target;
    ``execution`` overrides the benchmark's execution block (used by the CLI's
    ``--execution`` plumbing and by experiment configurations).
    """
    exec_spec = execution if execution is not None else (spec.execution or ExecutionSpec())
    # Experiment configurations may replace the benchmark's agent entirely;
    # the effective spec drives identity, snapshots, and adapter resolution
    # so persisted evidence always reflects the conditions actually used.
    if agent_override is not None:
        spec = spec.model_copy(deep=True)
        spec.agent = agent_override
    adapter = adapter if adapter is not None else get_adapter(spec.agent.type)
    timeout = timeout_seconds if timeout_seconds is not None else spec.timeout_seconds
    started_at = datetime.now(timezone.utc)
    run_id = _new_run_id()
    start_monotonic = time.monotonic()

    backend = make_backend(exec_spec, workspace_parent=workspace_parent)
    try:
        with create_workspace(
            repository or spec.repository, spec.commit, parent=workspace_parent, keep=keep_workspace
        ) as workspace:
            invocation = adapter.build_invocation(
                workspace=workspace.path, prompt=spec.prompt, agent_spec=spec.agent
            )
            _, credential_evidence = credential_env(exec_spec.pass_env)
            agent_run = backend.run_agent(
                invocation,
                workspace=workspace.path,
                timeout=timeout,
                env=None,
            )
            agent_output = None
            try:
                agent_output = adapter.parse_output(agent_run.stdout)
            except Exception:  # noqa: BLE001 - optional metrics must never fail a run
                agent_output = None

            # Diff against the pinned pre-agent commit: HEAD moves if the agent
            # commits its own work, and a mutable reference would report a
            # no-change run behind a passing one.
            diff = capture_diff(workspace.path, base=workspace.head_commit)

            hidden_dir: Path | None = None
            try:
                if spec.hidden_evaluations is not None:
                    hidden_dir = _resolve_hidden_dir(spec, benchmark_dir)
            except ValueError as exc:
                hidden_dir = None
                hidden_error = str(exc)
            else:
                hidden_error = None

            public_outcomes: list[EvaluationOutcome] = []
            hidden_outcomes: list[EvaluationOutcome] = []
            evaluation_error: str | None = evaluation_error_from(hidden_error)
            try:
                placeholders = backend.placeholders(workspace=workspace.path, hidden_dir=hidden_dir)
                for evaluation in spec.evaluations:
                    command = substitute_placeholders(
                        evaluation.command,
                        workspace=Path(placeholders["workspace"]),
                        python_executable=placeholders["python"],
                        hidden_dir=Path(placeholders["hidden_dir"]) if placeholders["hidden_dir"] else None,
                    )
                    outcome = EvaluationOutcome(
                        name=evaluation.name,
                        command=command,
                        **_public_eval_fields(backend, command, workspace.path, timeout),
                    )
                    public_outcomes.append(outcome)
                if spec.hidden_evaluations is not None and hidden_dir is not None:
                    hidden_outcomes = [
                        run_hidden_evaluation(
                            evaluation,
                            workspace=workspace.path,
                            hidden_dir=hidden_dir,
                            timeout=timeout,
                        )
                        for evaluation in spec.hidden_evaluations.evaluations
                    ]
            except Exception as exc:  # noqa: BLE001 - harness anomaly, not benchmark failure
                evaluation_error = evaluation_error or f"{type(exc).__name__}: {exc}"

            all_outcomes = [*public_outcomes, *hidden_outcomes]
            violations = find_policy_violations(
                list(diff.changed_paths), _effective_policies(spec)
            )
            classification = _classify(
                agent_timed_out=agent_run.timed_out,
                agent_exit_code=agent_run.exit_code,
                outcomes=all_outcomes,
                violations=violations,
                evaluation_error=evaluation_error,
            )

            result = _build_result(
                spec,
                run_id=run_id,
                trial=trial,
                workspace=workspace,
                backend=backend,
                credential_evidence=credential_evidence,
                agent_run=agent_run,
                agent_output=agent_output,
                adapter=adapter,
                diff=diff,
                public_outcomes=public_outcomes,
                hidden_outcomes=hidden_outcomes,
                violations=violations,
                classification=classification,
                started_at=started_at,
                duration=time.monotonic() - start_monotonic,
                keep_workspace=keep_workspace,
                experiment_id=experiment_id,
                config_name=config_name,
                manifest_path=manifest_path,
            )
            artifacts = RunArtifacts(
                agent_stdout=agent_run.stdout,
                agent_stderr=agent_run.stderr,
                patch=diff.patch,
                eval_outputs={
                    eval_artifact_stem(index, outcome.name): (outcome.stdout, outcome.stderr)
                    for index, outcome in enumerate(all_outcomes)
                },
            )
            # Persist while still inside the with-block: cleanup runs on exit and
            # must never be able to take the results down with it.
            root = results_root if results_root is not None else Path(spec.results_dir)
            run_dir = write_run(result, artifacts, results_root=root, run_dir_name=run_id)
    finally:
        backend.cleanup()

    return RunOutcome(
        result=result,
        run_dir=run_dir,
        workspace_path=workspace.path if keep_workspace else None,
    )


def evaluation_error_from(hidden_error: str | None) -> str | None:
    return hidden_error


def _public_eval_fields(backend: ExecutionBackend, command: str, workspace: Path, timeout: float) -> dict:
    result = backend.run_public_evaluation(command, workspace=workspace, timeout=timeout, env=None)
    return {
        "exit_code": result.exit_code,
        "passed": result.exit_code == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
    }


def _classify(
    *,
    agent_timed_out: bool,
    agent_exit_code: int | None,
    outcomes: list[EvaluationOutcome],
    violations,
    evaluation_error: str | None,
) -> Classification:
    if evaluation_error is not None:
        return Classification("invalid_result", evaluation_error)
    return classify_run(
        agent_timed_out=agent_timed_out,
        agent_exit_code=agent_exit_code,
        evaluations_passed=bool(outcomes) and all(o.passed for o in outcomes),
        has_evaluation_results=bool(outcomes),
        protected_violation=any(v.policy == "fail" for v in violations),
    )


def _build_result(
    spec: BenchmarkSpec,
    *,
    run_id: str,
    trial: int | None,
    workspace: Workspace,
    backend: ExecutionBackend,
    credential_evidence: list[dict],
    agent_run,
    agent_output,
    adapter: AgentAdapter,
    diff,
    public_outcomes: list[EvaluationOutcome],
    hidden_outcomes: list[EvaluationOutcome],
    violations,
    classification: Classification,
    started_at: datetime,
    duration: float,
    keep_workspace: bool,
    experiment_id: str | None,
    config_name: str | None,
    manifest_path: Path | None,
) -> RunResult:
    usage = agent_output.usage if agent_output is not None else None
    model = agent_output.model if agent_output is not None else None

    try:
        agent_cli_version = adapter.cli_version()
    except Exception:  # noqa: BLE001 - optional metadata must never fail a run
        agent_cli_version = None

    def outcome_rows(outcomes: list[EvaluationOutcome], index_offset: int) -> list[dict]:
        return [
            {
                "name": outcome.name,
                "command": outcome.command,
                "exit_code": outcome.exit_code,
                "passed": outcome.passed,
                "timed_out": outcome.timed_out,
                "duration_seconds": round(outcome.duration_seconds, 3),
                "stdout_file": f"evals/{eval_artifact_stem(index_offset + index, outcome.name)}.stdout.log",
                "stderr_file": f"evals/{eval_artifact_stem(index_offset + index, outcome.name)}.stderr.log",
            }
            for index, outcome in enumerate(outcomes)
        ]

    execution_provenance = backend.provenance()
    execution_provenance["pass_env_evidence"] = credential_evidence

    return RunResult(
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
            "type": spec.agent.type,
            "exit_code": agent_run.exit_code,
            "timed_out": agent_run.timed_out,
            "duration_seconds": round(agent_run.duration_seconds, 3),
            "model": model,
            "capabilities": sorted(adapter.capabilities()),
        },
        usage=(
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "cost_usd": usage.cost_usd,
                "tool_calls": usage.tool_calls,
                "num_turns": usage.num_turns,
                "session_id": usage.session_id,
            }
            if usage is not None
            else None
        ),
        diff={
            "files_changed": diff.stats.files_changed,
            "insertions": diff.stats.insertions,
            "deletions": diff.stats.deletions,
            "patch_file": "diff.patch",
            "changed_paths": list(diff.changed_paths),
            "added_files": list(diff.added_paths),
            "deleted_files": list(diff.deleted_paths),
            "renamed_files": list(diff.renamed_paths),
            "binary_files": list(diff.binary_paths),
        },
        evaluations=outcome_rows(public_outcomes, 0),
        hidden_evaluations=outcome_rows(hidden_outcomes, len(public_outcomes)),
        protected_paths=(
            {
                "patterns": list(spec.protected_paths),
                "fail_on_violation": spec.fail_on_protected_path_violation,
                "policies": [
                    {"patterns": p.patterns, "policy": p.policy}
                    for p in spec.change_policies
                ],
                "violations": [
                    {"path": v.path, "pattern": v.pattern, "policy": v.policy}
                    for v in violations
                ],
            }
            if (spec.protected_paths or spec.change_policies)
            else None
        ),
        overall={
            "status": classification.status,
            "failure_reason": classification.reason,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(duration, 3),
        },
        execution=execution_provenance,
        environment=capture_environment(agent_cli_version=agent_cli_version),
        config={
            **spec.config_snapshot(),
            # Provenance only — added after hashing, never part of identity.
            "_benchmark_manifest": str(manifest_path) if manifest_path else None,
        },
        experiment_id=experiment_id,
        config_name=config_name,
        workspace_kept=keep_workspace,
        workspace_path=str(workspace.path) if keep_workspace else None,
    )


__all__ = ["RunOutcome", "run_benchmark"]
