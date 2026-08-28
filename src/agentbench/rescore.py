"""Offline rescoring (P10): re-run scorers against preserved evidence.

The agent is never invoked. The stored ``diff.patch`` is applied to a fresh
clone at the pinned commit, scorers execute, and the result lands in a
``scoring_revisions/`` sidecar beside the ORIGINAL evidence — which stays
byte-immutable.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentbench.diffs import GIT_EXECUTABLE
from agentbench.scoring import compute_scoring
from agentbench.loader import load_benchmark
from agentbench.models import BenchmarkSpec
from agentbench.process import run_command


@dataclass(frozen=True)
class RescoreOutcome:
    run_id: str
    original_status: str | None
    new_resolved: bool
    partial_score: float | None
    revision_path: Path | None = None
    error: str | None = None


def _apply_patch(workspace: Path, patch_text: str) -> None:
    result = run_command(
        [str(GIT_EXECUTABLE), "apply", "--whitespace=nowarn", "-"],
        cwd=workspace,
        input_text=patch_text,
        timeout=60.0,
    )
    if result.exit_code != 0:
        raise ValueError(
            f"stored patch does not apply cleanly: {(result.stderr or '')[:300]}"
        )


def resolve_repository(spec: BenchmarkSpec, manifest_path: Path) -> Path:
    from agentbench.loader import resolve_repository_path

    base = manifest_path.parent if manifest_path.suffix != ".yaml" else manifest_path.parent
    return resolve_repository_path(spec.repository, base_dir=base)


def _locate_run_dir(results_root: Path, run_id: str) -> Path | None:
    for candidate in sorted(Path(results_root).glob("*/*/result.json")):
        if candidate.parent.name == run_id:
            return candidate.parent
    return None


def _version() -> str:
    from agentbench import __version__

    return __version__


def rescore_run(run_id: str, *, results_root: Path) -> RescoreOutcome:
    from agentbench.evaluation import Evaluation, run_evaluation, run_hidden_evaluation
    from agentbench.runner import effective_public_scorers
    from agentbench.workspace import create_workspace

    def fail(reason: str, status: str | None = None) -> RescoreOutcome:
        return RescoreOutcome(run_id=run_id, original_status=status,
                              new_resolved=False, partial_score=None, error=reason)

    run_dir = _locate_run_dir(results_root, run_id)
    original_status: str | None = None
    if run_dir is None:
        return fail("run not found")
    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    config = payload.get("config") or {}
    original_status = (payload.get("overall") or {}).get("status")
    # Nothing to re-grade: these outcomes carry no agent solution evidence,
    # so any "rescored" verdict would be fabrication, not measurement.
    if original_status == "setup_failed":
        return fail("setup-failed run has no agent evidence to rescore",
                    original_status)

    manifest_hint = config.get("_benchmark_manifest")
    if not manifest_hint or not Path(manifest_hint).is_file():
        return fail("benchmark manifest no longer recorded", original_status)
    spec: BenchmarkSpec = load_benchmark(manifest_hint)

    patch_file = run_dir / "diff.patch"
    if not patch_file.exists():
        return fail("no stored diff.patch; cannot rebuild the workspace state",
                    original_status)
    patch_text = patch_file.read_text(encoding="utf-8")
    if not patch_text.strip():
        # Failed/no-op agents legitimately record an empty patch; there is
        # nothing to replay, so refuse clearly instead of crashing.
        return fail("run recorded an empty diff; no agent changes to replay",
                    original_status)

    repository = resolve_repository(spec, Path(manifest_hint))
    started = time.monotonic()
    try:
        workspace = create_workspace(str(repository),
                                     payload["benchmark"]["resolved_commit"])
    except Exception as exc:  # noqa: BLE001 - reported as a rescore failure
        return fail(f"workspace preparation failed: {exc}", original_status)

    try:
        try:
            _apply_patch(workspace.path, patch_text)
        except ValueError as exc:
            return fail(str(exc), original_status)

        scorer_views = effective_public_scorers(spec)
        outcomes = [
            run_evaluation(
                Evaluation(name=view.id, command=view.command),
                workspace=workspace.path,
                timeout=spec.timeout_seconds,
            )
            for view in scorer_views
        ]
        hidden_outcomes: list = []
        if spec.hidden_evaluations is not None:
            hidden_dir = (Path(manifest_hint).parent
                          / spec.hidden_evaluations.source).resolve()
            hidden_outcomes = [
                run_hidden_evaluation(evaluation, workspace=workspace.path,
                                      hidden_dir=hidden_dir,
                                      timeout=spec.timeout_seconds)
                for evaluation in spec.hidden_evaluations.evaluations
            ]

        summary = compute_scoring(
            scorer_views, outcomes, declared_groups=spec.scoring_groups,
        )
        hidden_all_passed = all(o.passed for o in hidden_outcomes)
        summary.resolved = bool(summary.resolved and hidden_all_passed)

        revision_dir = run_dir / "scoring_revisions"
        revision_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        revision = {
            "schema_version": 1,
            "rescored_at": stamp.isoformat(),
            "original_run_id": run_id,
            "original_status": original_status,
            "original_scoring": payload.get("scoring"),
            "new_scoring": summary.to_dict(),
            "agentbench_version": _version(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "benchmark": {
                "name": (payload.get("benchmark") or {}).get("name"),
                "resolved_commit": (payload.get("benchmark") or {}).get(
                    "resolved_commit"),
                "config_hash": spec.config_hash(),
            },
            "scorer_set_hash": summary.scorer_set_hash,
            "uncovered_groups": list(summary.uncovered_groups),
            "new_resolved": bool(summary.resolved),
            "old_partial_score": (
                payload.get("scoring") or {}).get("partial_score")
                if isinstance(payload.get("scoring"), dict) else None,
            "new_partial_score": summary.partial_score,
        }
        target = revision_dir / (
            f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.json"
        )
        target.write_text(json.dumps(revision, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
        return RescoreOutcome(
            run_id=run_id,
            original_status=original_status,
            new_resolved=summary.resolved,
            partial_score=summary.partial_score,
            revision_path=target,
        )
    finally:
        try:
            workspace.cleanup()
        except Exception:  # noqa: BLE001 - temp dir best effort
            pass


__all__ = ["RescoreOutcome", "rescore_run"]
