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
