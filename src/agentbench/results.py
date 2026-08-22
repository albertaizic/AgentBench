"""Serialize run results: one JSON summary plus sidecar logs for raw output."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA_VERSION = 1


class RunResult(BaseModel):
    """Structured, JSON-safe summary of a single benchmark run.

    Bulky raw output (agent streams, eval logs, the patch) lives in sidecar
    files next to ``result.json`` and is referenced by path, keeping the JSON
    machine-readable at a glance.
    """

    schema_version: int = SCHEMA_VERSION
    benchmark: dict[str, Any]
    agent: dict[str, Any]
    diff: dict[str, Any]
    evaluations: list[dict[str, Any]]
    overall: dict[str, Any]
    environment: dict[str, Any]
    workspace_kept: bool = False
    workspace_path: str | None = None


@dataclass(frozen=True)
class RunArtifacts:
    """Raw outputs produced during a run, destined for sidecar files."""

    agent_stdout: str
    agent_stderr: str
    patch: str
    eval_outputs: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (stdout, stderr)


def _eval_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def eval_artifact_stem(index: int, name: str) -> str:
    """Injective sidecar filename stem for an evaluation.

    The index prefix keeps distinct evaluations from colliding even when
    their names slug to the same string ("lint check" vs "lint_check").
    """
    return f"{index:03d}-{_eval_slug(name)}"


def write_run(result: RunResult, artifacts: RunArtifacts, *, results_root: Path) -> Path:
    """Write ``result.json`` and sidecars under a fresh timestamped run dir."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = results_root / result.benchmark["name"] / f"{stamp}-{uuid.uuid4().hex[:6]}"
    (run_dir / "evals").mkdir(parents=True, exist_ok=True)

    (run_dir / "agent.stdout.log").write_text(artifacts.agent_stdout, encoding="utf-8")
    (run_dir / "agent.stderr.log").write_text(artifacts.agent_stderr, encoding="utf-8")
    (run_dir / "diff.patch").write_text(artifacts.patch, encoding="utf-8")

    for name, (stdout, stderr) in artifacts.eval_outputs.items():
        slug = _eval_slug(name)
        (run_dir / "evals" / f"{slug}.stdout.log").write_text(stdout, encoding="utf-8")
        (run_dir / "evals" / f"{slug}.stderr.log").write_text(stderr, encoding="utf-8")

    payload = result.model_dump(mode="json")
    (run_dir / "result.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return run_dir
