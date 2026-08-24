"""Experiment planning, manifests, and resume semantics.

An experiment is a ``benchmark × config × trial`` matrix. Each cell is a
completely independent AgentBench run; the manifest records identities so
resume can skip completed cells and reject configurations that changed
materially between invocations.

Cell identity is content-derived — benchmark name + benchmark config hash +
config name + config hash + trial number — so resume matches on *what* is
being run, not on display names or ordering.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentbench.models import ExperimentSpec


class ExperimentError(RuntimeError):
    pass


class ExperimentManifest(BaseModel):
    """Persisted experiment state; updated incrementally after each cell."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    experiment_id: str
    name: str
    created_at: str
    results_dir: str
    planned_cells: int
    repeat: int
    # Identity snapshots at planning time (content-derived, not display names).
    benchmark_identities: dict[str, str]  # name -> config hash of the loaded spec
    config_identities: dict[str, str]  # config name -> hash
    execution_backend: str | None = None
    # Concrete benchmark names resolved at creation time. Metadata selectors
    # (suite/tags/category) are resolved once; later corpus changes never
    # silently alter an existing experiment.
    resolved_benchmarks: list[str] = Field(default_factory=list)
    completed: list[dict] = Field(default_factory=list)
    interrupted: bool = False

    def cell_done(self, key: str) -> bool:
        return any(record["cell_key"] == key for record in self.completed)

    def record(self, record: dict) -> None:
        self.completed.append(record)


@dataclass(frozen=True)
class CellPlan:
    benchmark_name: str
    manifest_path: Path
    config_name: str
    trial: int
    cell_key: str


def experiment_id_for(name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(
        f"{name}|{stamp}|{datetime.now(timezone.utc).timestamp()}".encode()
    ).hexdigest()[:8]
    return f"{stamp}-{digest}"


def plan_cells(
    spec: ExperimentSpec,
    manifests: dict[str, Path],
    benchmark_hashes: dict[str, str],
    benchmarks: list[str] | None = None,
) -> list[CellPlan]:
    """Build the full cell plan; every benchmark name must resolve.

    ``benchmarks`` overrides the spec's own selection (used once a metadata
    selector has been resolved to concrete names).
    """
    names = benchmarks if benchmarks is not None else (
        spec.benchmarks if isinstance(spec.benchmarks, list) else []
    )
    missing = [name for name in names if name not in manifests]
    if missing:
        raise ExperimentError(f"unknown benchmark(s): {', '.join(sorted(missing))}")

    plans: list[CellPlan] = []
    for benchmark_name in names:
        for config in spec.configs:
            for trial in range(1, spec.repeat + 1):
                key = cell_key(
                    benchmark_name,
                    config.name,
                    benchmark_hashes.get(benchmark_name, ""),
                    config.config_hash(),
                    trial,
                )
                plans.append(
                    CellPlan(
                        benchmark_name=benchmark_name,
                        manifest_path=manifests[benchmark_name],
                        config_name=config.name,
                        trial=trial,
                        cell_key=key,
                    )
                )
    return plans


def cell_key(
    benchmark_name: str,
    config_name: str,
    benchmark_hash: str,
    config_hash: str,
    trial: int,
) -> str:
    canonical = json.dumps(
        [benchmark_name, benchmark_hash, config_name, config_hash, trial], sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_manifest(path: Path) -> ExperimentManifest:
    try:
        return ExperimentManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExperimentError(f"experiment manifest not found: {path}") from exc
    except ValidationError as exc:
        raise ExperimentError(f"invalid experiment manifest {path}: {exc}") from exc


def save_manifest(manifest: ExperimentManifest, directory: Path) -> Path:
    """Write the manifest atomically (temp file + replace)."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "experiment.json"
    temp = directory / "experiment.json.tmp"
    temp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    temp.replace(target)
    return target


def new_manifest(
    spec: ExperimentSpec,
    experiment_id: str,
    results_dir: Path,
    resolved_benchmarks: list[str] | None = None,
) -> ExperimentManifest:
    benchmarks = resolved_benchmarks if resolved_benchmarks is not None else (
        spec.benchmarks if isinstance(spec.benchmarks, list) else []
    )
    return ExperimentManifest(
        experiment_id=experiment_id,
        name=spec.name,
        created_at=datetime.now(timezone.utc).isoformat(),
        results_dir=str(results_dir),
        planned_cells=len(benchmarks) * len(spec.configs) * spec.repeat,
        repeat=spec.repeat,
        benchmark_identities={},
        config_identities={c.name: c.config_hash() for c in spec.configs},
        execution_backend=(spec.execution.backend if spec.execution else None),
        resolved_benchmarks=list(benchmarks),
    )


__all__ = [
    "CellPlan",
    "ExperimentError",
    "ExperimentManifest",
    "cell_key",
    "experiment_id_for",
    "load_manifest",
    "new_manifest",
    "plan_cells",
    "save_manifest",
]
