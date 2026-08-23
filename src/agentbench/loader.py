"""Load benchmark YAML files into validated :mod:`agentbench.models` objects."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from agentbench.models import BenchmarkSpec


class LoaderError(RuntimeError):
    """Raised when a benchmark file cannot be found, read, or parsed."""


def load_benchmark(path: str | Path) -> BenchmarkSpec:
    """Parse and validate *path*; schema violations raise ``ValidationError``."""
    path = Path(path)
    if not path.is_file():
        raise LoaderError(f"Benchmark file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LoaderError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise LoaderError(f"Benchmark file must contain a YAML mapping, got {type(raw).__name__}: {path}")

    return BenchmarkSpec.model_validate(raw)


def resolve_repository_path(repository: str, *, base_dir: Path | None) -> str:
    """Resolve a repository reference to a cloneable location.

    URLs (anything with ``://`` or scp-like ``git@``) pass through untouched;
    relative paths resolve against *base_dir* — the benchmark file's
    directory — so committed benchmark fixtures work on any machine. The
    verbatim benchmark value remains what config identity is computed from.
    """
    if "://" in repository or repository.startswith("git@"):
        return repository
    candidate = Path(repository)
    if candidate.is_absolute():
        return str(candidate)
    if base_dir is None:
        return repository
    return str((base_dir / candidate).resolve())
