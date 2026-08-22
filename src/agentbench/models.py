"""Pydantic schemas describing a benchmark run.

Validation is deliberately strict (`extra="forbid"` everywhere): benchmark
files must fail loudly on typos rather than silently run something else.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Abbreviated or full hex commit; git itself resolves it to an exact sha.
_COMMIT_PATTERN = r"^[0-9a-fA-F]{7,64}$"
# The benchmark name becomes a directory under results/, so no separators,
# no traversal, nothing hidden — and no trailing dot, which Windows strips
# from directory names.
_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?$"


class AgentSpec(BaseModel):
    """How the coding agent should be executed."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["claude-code"]
    # Optional override of the binary to execute (e.g. a wrapper script);
    # defaults to whatever the adapter considers its natural binary.
    command: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class Evaluation(BaseModel):
    """A single shell command whose exit code decides pass/fail."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)


class BenchmarkSpec(BaseModel):
    """A complete, self-contained benchmark definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    repository: str = Field(min_length=1)
    commit: str = Field(pattern=_COMMIT_PATTERN)
    prompt: str = Field(min_length=1)
    agent: AgentSpec
    evaluations: list[Evaluation] = Field(min_length=1)
    timeout_seconds: float = Field(default=900.0, gt=0)
    results_dir: str = "results"

    @field_validator("results_dir")
    @classmethod
    def _results_dir_must_be_safe_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        windows = PureWindowsPath(normalized)
        if PurePosixPath(normalized).is_absolute() or windows.is_absolute():
            raise ValueError(f"results_dir must be a relative path, got {value!r}")
        if windows.drive:
            # Drive-relative paths like 'C:x' are not is_absolute() but still
            # escape the results root.
            raise ValueError(f"results_dir must not contain a drive component, got {value!r}")
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise ValueError(f"results_dir must stay inside the results root, got {value!r}")
        return value

    @model_validator(mode="after")
    def _evaluation_names_must_be_unique(self) -> "BenchmarkSpec":
        # Sidecar logs are keyed by evaluation identity; duplicates would
        # silently overwrite each other's captured output.
        names = [evaluation.name for evaluation in self.evaluations]
        if len(names) != len(set(names)):
            raise ValueError("evaluation names must be unique")
        return self
