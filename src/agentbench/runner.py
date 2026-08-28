"""Orchestrate one benchmark run end to end.

Pipeline: clone+checkout workspace → execute the agent through the configured
execution backend → parse optional metrics → capture the diff → evaluate
change policies → run public evaluations → run hidden evaluations (always
host-side) → classify the outcome → persist evidence → return. Results are
written *before* workspace cleanup so a cleanup failure can never destroy the
evidence of a completed run.

Setup problems that occur once benchmark identity exists (unclonable
repository, missing commit, unavailable Docker image, missing agent binary)
are persisted as ``setup_failed`` runs with a structured ``failure_stage``
instead of vanishing as unrecorded exit-code-2 errors. Only errors so early
that no identity exists (invalid manifest YAML) remain unpersisted.

The adapter (how to invoke an agent) and the execution backend (where that
invocation runs) are independent injectables, so tests can exercise the whole
pipeline without a real coding agent or Docker.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentbench.adapters import AgentAdapter, UnknownAgentError, get_adapter
from agentbench.backends import make_backend
from agentbench.backends.base import ExecutionBackend, credential_env
from agentbench.backends.docker import DockerExecutionBackend, is_infrastructure_failure
from agentbench.diffs import capture_diff
from agentbench.envmeta import capture_environment
from agentbench.evaluation import (
    EvaluationOutcome,
    substitute_placeholders,
    run_hidden_evaluation,
    run_evaluation,
)
from agentbench.models import AgentSpec, BenchmarkSpec, ExecutionSpec
from agentbench.process import ProcessResult, run_command
from agentbench.protected import find_policy_violations
from agentbench.scoring import ScorerSpecView, compute_scoring
from agentbench.results import SCHEMA_VERSION, RunArtifacts, RunResult, eval_artifact_stem, write_run
from agentbench.stages import (
    STAGE_AGENT,
    STAGE_BACKEND_PREPARE,
    STAGE_CLEANUP,
    STAGE_EVIDENCE,
    STAGE_EVALUATION,
    STAGE_PERSISTENCE,
    STAGE_WORKSPACE,
    StageTimer,
)
from agentbench.taxonomy import SETUP_FAILED, Classification, classify_run, classify_validity
from agentbench.workspace import Workspace, WorkspaceError, create_workspace


@dataclass(frozen=True)
class RunOutcome:
    result: RunResult
    run_dir: Path
    workspace_path: Path | None  # set only when the workspace was kept


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


_ERROR_SNIPPET_LIMIT = 400


def _safe_error_summary(
    exc: BaseException | str, *, spec: BenchmarkSpec | None = None
) -> str:
    """Error text safe to persist: secret values masked, length bounded."""
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    if spec is not None:
        for name in spec.execution.pass_env if spec.execution else []:
            value = os.environ.get(name)
            if value:
                text = text.replace(value, "***")
    # Well-known credential variables never belong in evidence, even when the
    # caller had no spec to derive an allowlist from.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "***")
    if len(text) > _ERROR_SNIPPET_LIMIT:
        text = text[:_ERROR_SNIPPET_LIMIT] + "…[truncated]"
    return text


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


def effective_public_scorers(spec: BenchmarkSpec) -> list[ScorerSpecView]:
    """Public scorers for a spec: explicit declarations or legacy fallback.

    Legacy ``evaluations`` entries become required binary scorers in the
    "default" group, preserving v0.1 semantics exactly.
    """
    if spec.scorers:
        return [
            ScorerSpecView(
                id=s.id, command=s.command, score_type=s.score_type,
                groups=tuple(s.groups), required=s.required, max_count=s.max_count,
            )
            for s in spec.scorers
        ]
    return [
        ScorerSpecView(id=e.name, command=e.command) for e in spec.evaluations
    ]


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

    Setup failures (unclonable source, missing commit/image/binary) are
    persisted as ``setup_failed`` evidence and returned as a normal outcome —
    callers check ``result.overall["status"]`` rather than catching exceptions.
    Only harness anomalies (e.g. an adapter crashing while building its own
    invocation) propagate.
    """
    exec_spec = execution if execution is not None else (spec.execution or ExecutionSpec())
    # Experiment configurations may replace the benchmark's agent entirely;
    # the effective spec drives identity, snapshots, and adapter resolution
    # so persisted evidence always reflects the conditions actually used.
    if agent_override is not None:
        spec = spec.model_copy(deep=True)
        spec.agent = agent_override
    timeout = timeout_seconds if timeout_seconds is not None else spec.timeout_seconds
    started_at = datetime.now(timezone.utc)
    run_id = _new_run_id()
    start_monotonic = time.monotonic()
    root = results_root if results_root is not None else Path(spec.results_dir)
    timer = StageTimer()
    backend = make_backend(exec_spec, workspace_parent=workspace_parent)

    def finish_setup_failure(stage: str, error: BaseException | str, *, artifacts=None) -> RunOutcome:
        result, run_dir = _persist_setup_failure(
            spec,
            run_id=run_id,
            stage=stage,
            error=error,
            backend=backend,
            results_root=root,
            started_at=started_at,
            duration=time.monotonic() - start_monotonic,
            trial=trial,
            experiment_id=experiment_id,
            config_name=config_name,
            manifest_path=manifest_path,
            stage_timings=timer.snapshot(),
            artifacts=artifacts,
        )
        return RunOutcome(result=result, run_dir=run_dir, workspace_path=None)

    try:
        try:
            resolved_adapter = adapter if adapter is not None else get_adapter(spec.agent.type)
        except UnknownAgentError as exc:
            return finish_setup_failure(STAGE_BACKEND_PREPARE, exc)

        workspace: Workspace | None = None
        with timer.stage(STAGE_WORKSPACE):
            try:
                workspace = create_workspace(
                    repository or spec.repository,
                    spec.commit,
                    parent=workspace_parent,
                    keep=keep_workspace,
                )
            except WorkspaceError as exc:
                return finish_setup_failure(STAGE_WORKSPACE, exc)

        setup_artifacts = None
        try:
            with timer.stage(STAGE_BACKEND_PREPARE):
                invocation = resolved_adapter.build_invocation(
                    workspace=workspace.path, prompt=spec.prompt, agent_spec=spec.agent
                )
                _, credential_evidence = credential_env(exec_spec.pass_env)

            with timer.stage(STAGE_AGENT):
                try:
                    agent_run = backend.run_agent(
                        invocation,
                        workspace=workspace.path,
                        timeout=timeout,
                        env=None,
                    )
                except OSError as exc:
                    # Unresolvable/non-executable agent binary: the environment
                    # failed before the agent could meaningfully start.
                    setup_artifacts = RunArtifacts(agent_stdout="", agent_stderr="", patch="")
                    return finish_setup_failure(STAGE_BACKEND_PREPARE, exc, artifacts=setup_artifacts)

            if isinstance(backend, DockerExecutionBackend) and is_infrastructure_failure(agent_run):
                # Docker-level failure (missing image, dead daemon): never the
                # agent's fault. Keep the docker CLI output as evidence.
                setup_artifacts = RunArtifacts(
                    agent_stdout=agent_run.stdout, agent_stderr=agent_run.stderr, patch=""
                )
                return finish_setup_failure(
                    STAGE_BACKEND_PREPARE,
                    f"docker infrastructure failure (exit {agent_run.exit_code}): "
                    + _safe_error_summary(
                        (agent_run.stderr or agent_run.stdout).strip() or "no output",
                        spec=spec,
                    ),
                    artifacts=setup_artifacts,
                )

            with timer.stage(STAGE_EVIDENCE):
                agent_output = None
                try:
                    agent_output = resolved_adapter.parse_output(agent_run.stdout)
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
            with timer.stage(STAGE_EVALUATION):
                try:
                    placeholders = backend.placeholders(workspace=workspace.path, hidden_dir=hidden_dir)
                    for scorer_view in effective_public_scorers(spec):
                        command = substitute_placeholders(
                            scorer_view.command,
                            workspace=Path(placeholders["workspace"]),
                            python_executable=placeholders["python"],
                            hidden_dir=Path(placeholders["hidden_dir"]) if placeholders["hidden_dir"] else None,
                        )
                        outcome = EvaluationOutcome(
                            name=scorer_view.id,
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
            # P8/P9: structured scorer breakdown + partial credit, computed
            # over PUBLIC scorers; hidden evaluators gate resolution too.
            scoring_summary = compute_scoring(
                effective_public_scorers(spec),
                public_outcomes,
                declared_groups=spec.scoring_groups,
            )
            hidden_all_passed = all(o.passed for o in hidden_outcomes)
            resolved = bool(scoring_summary.resolved and hidden_all_passed)

            with timer.stage(STAGE_EVIDENCE):
                violations = find_policy_violations(
                    list(diff.changed_paths), _effective_policies(spec)
                )
                classification = _classify(
                    agent_timed_out=agent_run.timed_out,
                    agent_exit_code=agent_run.exit_code,
                    outcomes=all_outcomes,
                    violations=violations,
                    evaluation_error=evaluation_error,
                    resolved_override=(
                        resolved if (spec.scorers or spec.scoring_groups) else None
                    ),
                )

            result = _build_result(
                spec,
                scoring=scoring_summary,
                run_id=run_id,
                trial=trial,
                workspace=workspace,
                backend=backend,
                credential_evidence=credential_evidence,
                agent_run=agent_run,
                agent_output=agent_output,
                adapter=resolved_adapter,
                diff=diff,
                public_outcomes=public_outcomes,
                hidden_outcomes=hidden_outcomes,
                violations=violations,
                classification=classification,
                started_at=started_at,
                timeout_seconds=timeout,
                duration=time.monotonic() - start_monotonic,
                keep_workspace=keep_workspace,
                experiment_id=experiment_id,
                config_name=config_name,
                manifest_path=manifest_path,
                stage_timings=timer.snapshot(),
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
            # Persist before any teardown: cleanup must never be able to take
            # the evidence of a completed run down with it.
            with timer.stage(STAGE_PERSISTENCE):
                run_dir = write_run(result, artifacts, results_root=root, run_dir_name=run_id)
                _persist_trajectory(
                    run_dir=run_dir,
                    agent_type=result.agent.get("type", ""),
                    stdout_path=run_dir / "agent.stdout.log",
                    session_id=(
                        agent_output.usage.session_id
                        if agent_output is not None and agent_output.usage is not None
                        else None
                    ),
                )
        finally:
            with timer.stage(STAGE_CLEANUP):
                backend.cleanup()
                if workspace is not None:
                    workspace.cleanup()

    except Exception:
        # Setup-failure paths above already persisted their evidence; anything
        # reaching here is an unexpected harness bug and stays loud.
        raise

    # Persistence/cleanup durations only exist after those stages ran; refresh
    # the snapshot and re-write the (small) summary. If this fails the original
    # evidence file remains intact — timings are provenance, not the outcome.
    result.stage_timings = timer.snapshot()
    try:
        (run_dir / "result.json").write_text(
            json.dumps(result.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass

    # If trajectory extraction ran inside the persistence stage it already
    # wrote trajectory.jsonl; nothing further is required here.
    return RunOutcome(
        result=result,
        run_dir=run_dir,
        workspace_path=workspace.path if keep_workspace else None,
    )


def _export_hermes_session(session_id: str | None) -> str | None:
    if not session_id:
        return None
    from agentbench.process import resolve_executable

    result = run_command(
        [resolve_executable("hermes"), "sessions", "export", "--format", "jsonl",
         "--session-id", str(session_id), "-"],
        timeout=60.0,
        cwd=Path.cwd(),
    )
    if result.exit_code == 0 and result.stdout and result.stdout.strip().startswith("{"):
        return result.stdout
    return None


def _persist_trajectory(
    *,
    run_dir: Path,
    agent_type: str,
    stdout_path: Path,
    session_id: str | None,
) -> None:
    """Write ``trajectory.jsonl`` beside the raw logs. Never raises."""
    from agentbench import trajectories as traj

    stdout_text = ""
    try:
        stdout_text = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    claude_stdout = omp_stdout = hermes_export = None
    if agent_type == "claude-code":
        claude_stdout = stdout_text
    elif agent_type == "omp":
        omp_stdout = stdout_text
    elif agent_type == "hermes":
        hermes_export = _export_hermes_session(session_id)

    builder = traj.extract_trajectory(
        agent_type,
        claude_stdout=claude_stdout,
        hermes_export=hermes_export,
        omp_stdout=omp_stdout,
        run_id=run_dir.name,
    )
    if builder.events:
        traj.write_trajectory(run_dir, builder)



def _persist_setup_failure(
    spec: BenchmarkSpec,
    *,
    run_id: str,
    stage: str,
    error: BaseException | str,
    backend: ExecutionBackend,
    results_root: Path,
    started_at: datetime,
    duration: float,
    trial: int | None,
    experiment_id: str | None,
    config_name: str | None,
    manifest_path: Path | None,
    stage_timings: dict[str, float] | None,
    artifacts: RunArtifacts | None = None,
) -> tuple[RunResult, Path]:
    """Persist structured ``setup_failed`` evidence for a run that never got
    far enough to produce evaluations.

    Enough identity exists by definition once the manifest loaded, so these
    records are first-class runs: queryable in history/show/compare/dashboard.
    """
    summary = _safe_error_summary(error, spec=spec)
    finished_at = datetime.now(timezone.utc)
    try:
        provenance = backend.provenance()
    except Exception:  # noqa: BLE001 - provenance must not mask the failure itself
        provenance = {"backend": backend.name}
    provenance.setdefault("backend", backend.name)

    result = RunResult(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        trial=trial,
        benchmark={
            "name": spec.name,
            "repository": spec.repository,
            "commit": spec.commit,
            "resolved_commit": "",
            "config_hash": spec.config_hash(),
        },
        agent={
            "type": spec.agent.type,
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": None,
            "model": None,
            "capabilities": [],
        },
        usage=None,
        diff={
            "files_changed": 0,
            "insertions": 0,
            "deletions": 0,
            "patch_file": "diff.patch",
            "changed_paths": [],
            "added_files": [],
            "deleted_files": [],
            "renamed_files": [],
            "binary_files": [],
        },
        evaluations=[],
        hidden_evaluations=[],
        protected_paths=None,
        overall={
            "status": SETUP_FAILED,
            "failure_reason": summary,
            "failure_stage": stage,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round(duration, 3),
        },
        execution=provenance,
        environment=capture_environment(agent_cli_version=None),
        config={
            **spec.config_snapshot(),
            "_benchmark_manifest": str(manifest_path) if manifest_path else None,
        },
        experiment_id=experiment_id,
        config_name=config_name,
        workspace_kept=False,
        workspace_path=None,
        stage_timings=stage_timings,
    )
    safe_artifacts = artifacts or RunArtifacts(agent_stdout="", agent_stderr="", patch="")
    run_dir = write_run(result, safe_artifacts, results_root=results_root, run_dir_name=run_id)
    return result, run_dir


def evaluation_error_from(hidden_error: str | None) -> str | None:
    return hidden_error


def eval_outcome_rows(outcomes: list[EvaluationOutcome], index_offset: int) -> list[dict]:
    """JSON rows for persisted evaluation summaries (shared with baselines)."""
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
    resolved_override: bool | None = None,
) -> Classification:
    if evaluation_error is not None:
            return Classification("invalid_result", evaluation_error)
    if resolved_override is not None:
        # P8/P9 scorer-aware resolution: only REQUIRED scorers gate the
        # binary outcome; non-required groups contribute partial credit.
        return classify_run(
            agent_timed_out=agent_timed_out,
            agent_exit_code=agent_exit_code,
            evaluations_passed=bool(outcomes) and resolved_override,
            has_evaluation_results=bool(outcomes),
            protected_violation=any(v.policy == "fail" for v in violations),
        )
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
    scoring=None,
    started_at: datetime,
    duration: float,
    keep_workspace: bool,
    timeout_seconds: float | None,
    experiment_id: str | None,
    config_name: str | None,
    manifest_path: Path | None,
    stage_timings: dict[str, float] | None = None,
) -> RunResult:
    usage = agent_output.usage if agent_output is not None else None
    model = agent_output.model if agent_output is not None else None

    try:
        agent_cli_version = adapter.cli_version()
    except Exception:  # noqa: BLE001 - optional metadata must never fail a run
        agent_cli_version = None

    def outcome_rows(outcomes: list[EvaluationOutcome], index_offset: int) -> list[dict]:
        return eval_outcome_rows(outcomes, index_offset)

    execution_provenance = backend.provenance()
    execution_provenance["pass_env_evidence"] = credential_evidence
    if getattr(workspace, "prep_info", None):
        # How the source was obtained (cache hit/miss + preparation seconds).
        execution_provenance["source_preparation"] = dict(workspace.prep_info)

    caps = adapter.capabilities()
    execution_provenance["limits"] = {
        "wall_time_seconds": timeout_seconds,
        "token_budget": spec.execution.token_budget if spec.execution else None,
        "cost_budget_usd": spec.execution.cost_budget_usd if spec.execution else None,
        "enforced": ["wall_time"],
        "token_budget_enforced": AgentAdapter.CAP_TOKEN_BUDGET_ENFORCEMENT in caps,
        "cost_budget_enforced": AgentAdapter.CAP_COST_BUDGET_ENFORCEMENT in caps,
    }

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
                "cost_provenance": usage.cost_provenance,
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
            "failure_stage": classification.stage,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(duration, 3),
            # Orthogonal to status (P41): can the evidence be graded as a
            # capability measurement at all? Provider outages yield
            # infra_invalid while staying visible in the taxonomy.
            "validity": classify_validity(
                agent_stdout=agent_run.stdout,
                agent_stderr=agent_run.stderr,
                total_tokens=usage.total_tokens if usage is not None else None,
            ),
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
        stage_timings=stage_timings,
        scoring=(
            scoring.to_dict()
            if scoring is not None and (scoring.scorers or scoring.partial_score is not None)
            else None
        ),
    )


__all__ = ["RunOutcome", "run_benchmark"]
