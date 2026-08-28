"""Serialize run results: one JSON summary plus sidecar logs for raw output.

``result.json`` is schema-versioned evidence (``schema_version`` field); the
SQLite index in :mod:`agentbench.storage` is a derived query layer over these
files, never a replacement for them.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA_VERSION = 4


class RunResult(BaseModel):
    """Structured, JSON-safe summary of a single benchmark run.

    Bulky raw output (agent streams, eval logs, the patch) lives in sidecar
    files next to ``result.json`` and is referenced by path, keeping the JSON
    machine-readable at a glance. Sections hold plain dicts so the format
    stays agent-independent.
    """

    schema_version: int = SCHEMA_VERSION
    run_id: str
    trial: int | None = None
    benchmark: dict[str, Any]  # name/repository/commit/resolved_commit/config_hash
    agent: dict[str, Any]  # type/exit_code/timed_out/duration_seconds/model/capabilities
    usage: dict[str, Any] | None = None  # tokens/cost/turns/session — None when unavailable
    diff: dict[str, Any]
    evaluations: list[dict[str, Any]]
    hidden_evaluations: list[dict[str, Any]] = []
    protected_paths: dict[str, Any] | None = None
    overall: dict[str, Any]
    # status/failure_reason/failure_stage/started_at/finished_at/duration_seconds;
    # failure_stage names WHERE a failure happened (see agentbench.stages).
    execution: dict[str, Any] | None = None  # backend provenance (host/docker)
    environment: dict[str, Any]
    config: dict[str, Any]  # snapshot of the BenchmarkSpec used
    experiment_id: str | None = None
    config_name: str | None = None
    workspace_kept: bool = False
    workspace_path: str | None = None
    stage_timings: dict[str, float] | None = None  # per-phase wall-clock seconds
    # v0.6 P8/P9: structured scorer breakdown + partial credit. Absent on
    # historical runs, which remain valid without it.
    scoring: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunArtifacts:
    """Raw outputs produced during a run, destined for sidecar files."""

    agent_stdout: str
    agent_stderr: str
    patch: str
    eval_outputs: dict[str, tuple[str, str]] = field(default_factory=dict)  # stem -> (stdout, stderr)


def _eval_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def eval_artifact_stem(index: int, name: str) -> str:
    """Injective sidecar filename stem for an evaluation.

    The index prefix keeps distinct evaluations from colliding even when
    their names slug to the same string ("lint check" vs "lint_check").
    """
    return f"{index:03d}-{_eval_slug(name)}"


def write_run(
    result: RunResult,
    artifacts: RunArtifacts,
    *,
    results_root: Path,
    run_dir_name: str | None = None,
) -> Path:
    """Write ``result.json`` and sidecars under a fresh timestamped run dir.

    ``run_dir_name`` pins the directory name to the result's own run id;
    when omitted, a fresh timestamped name is generated (standalone use).
    """
    if run_dir_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir_name = f"{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir = results_root / result.benchmark["name"] / run_dir_name
    (run_dir / "evals").mkdir(parents=True, exist_ok=True)

    (run_dir / "agent.stdout.log").write_text(artifacts.agent_stdout, encoding="utf-8")
    (run_dir / "agent.stderr.log").write_text(artifacts.agent_stderr, encoding="utf-8")
    (run_dir / "diff.patch").write_text(artifacts.patch, encoding="utf-8")

    for stem, (stdout, stderr) in artifacts.eval_outputs.items():
        (run_dir / "evals" / f"{stem}.stdout.log").write_text(stdout, encoding="utf-8")
        (run_dir / "evals" / f"{stem}.stderr.log").write_text(stderr, encoding="utf-8")

    payload = result.model_dump(mode="json")
    (run_dir / "result.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return run_dir
