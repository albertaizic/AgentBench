"""Pydantic schemas describing benchmarks, execution, and experiments.

Validation is deliberately strict (`extra="forbid"` everywhere): benchmark
files must fail loudly on typos rather than silently run something else.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Abbreviated or full hex commit; git itself resolves it to an exact sha.
_COMMIT_PATTERN = r"^[0-9a-fA-F]{7,64}$"
# The benchmark name becomes a directory under results/, so no separators,
# no traversal, nothing hidden — and no trailing dot, which Windows strips
# from directory names.
_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?$"
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _reject_unsafe_relative(value: str, field: str) -> str:
    """Reject absolute paths, drive components, and '..' traversal."""
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(normalized)
    if PurePosixPath(normalized).is_absolute() or windows.is_absolute():
        raise ValueError(f"{field} must be a relative path, got {value!r}")
    if windows.drive:
        # Drive-relative paths like 'C:x' are not is_absolute() but still
        # escape their base directory.
        raise ValueError(f"{field} must not contain a drive component, got {value!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"{field} must stay inside its base directory, got {value!r}")
    return value


class AgentSpec(BaseModel):
    """How the coding agent should be executed.

    Two adapter types exist:

    * ``claude-code`` – the Claude Code CLI (primary real adapter);
    * ``command``     – a generic argv-based adapter so any non-interactive
      coding agent can be benchmarked without touching AgentBench core. The
      prompt is delivered on stdin by default, or via a ``{prompt}``
      placeholder in one argv element when ``prompt_mode="arg"``. Never a
      shell string.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["claude-code", "command"]
    # claude-code: optional override of the binary to execute (wrapper scripts).
    command: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    # Model selector honored by adapters that support it (claude-code).
    model: str | None = None
    # command adapter:
    argv: list[str] | None = None
    prompt_mode: Literal["stdin", "arg"] = "stdin"

    @model_validator(mode="after")
    def _validate_type_specific_fields(self) -> "AgentSpec":
        if self.type == "command":
            if not self.argv or not all(isinstance(a, str) and a.strip() for a in self.argv):
                raise ValueError("agent type 'command' requires a non-empty 'argv' list")
            if self.prompt_mode == "arg" and not any("{prompt}" in a for a in self.argv):
                raise ValueError(
                    "prompt_mode='arg' requires exactly the {prompt} placeholder in argv"
                )
        return self


class Evaluation(BaseModel):
    """A single shell command whose exit code decides pass/fail."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)


class HiddenEvaluationSpec(BaseModel):
    """Evaluators kept outside the agent-visible workspace.

    ``source`` is a directory relative to the benchmark file; it is never
    copied into the cloned workspace. Its commands run with that directory
    as cwd (host-side, regardless of execution backend) and the agent's
    workspace prepended to ``PYTHONPATH``, so hidden tests can import the
    package the agent worked on without the agent ever seeing them.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    evaluations: list[Evaluation] = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def _source_must_be_safe_relative(cls, value: str) -> str:
        return _reject_unsafe_relative(value, "hidden_evaluations.source")


class ExecutionSpec(BaseModel):
    """Where and how the agent (and public evaluations) execute.

    ``host`` runs everything as local subprocesses (v0.1/v0.2 behavior).
    ``docker`` executes the agent inside a container while AgentBench keeps
    workspace management, diff capture, hidden evaluations, and persistence
    on the host.

    Isolation is explicit, never overstated:

    * only the workspace is mounted — never AgentBench source, hidden
      evaluators, results storage, host home, or the Docker socket;
    * container environment is EMPTY except variables explicitly allowlisted
      via ``pass_env`` (names only; values are read from the host at run time
      and never persisted);
    * ``network`` controls container network access ("enabled" is required
      for agents that must reach model APIs; "disabled" uses Docker's
      ``--network none``);
    * resource limits map to Docker's own flags and are recorded in results.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["host", "docker"] = "host"
    image: str | None = None  # docker backend; default image chosen by the backend
    network: Literal["enabled", "disabled"] = "enabled"
    memory: str | None = None  # e.g. "2g" → docker --memory
    cpus: float | None = Field(default=None, gt=0)  # docker --cpus
    pids_limit: int | None = Field(default=None, gt=0)  # docker --pids-limit
    pass_env: list[str] = Field(default_factory=list)

    @field_validator("pass_env")
    @classmethod
    def _env_names_must_be_valid(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _ENV_NAME_PATTERN.match(name):
                raise ValueError(f"pass_env entries must be valid env var names, got {name!r}")
        return value

    @field_validator("memory")
    @classmethod
    def _memory_must_look_like_docker_size(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"\d+[bkmgt]", value.lower()):
            raise ValueError(f"memory must look like '<number><b|k|m|g>', got {value!r}")
        return value.lower()

    def merged_with(self, override: "ExecutionSpec | None") -> "ExecutionSpec":
        """Merge an overriding spec on top of this one (override wins)."""
        if override is None:
            return self
        fields = {}
        for name in ("backend", "image", "network", "memory", "cpus", "pids_limit"):
            override_value = getattr(override, name)
            fields[name] = (
                override_value if override_value is not None else getattr(self, name)
            )
        fields["pass_env"] = sorted(set(self.pass_env) | set(override.pass_env))
        return ExecutionSpec(**fields)


class ChangePolicy(BaseModel):
    """Declarative policy for changes to groups of paths.

    ``fail`` classifies the run as protected_path_violation; ``warn`` records
    evidence prominently; ``allowed`` records nothing beyond the diff itself.
    """

    model_config = ConfigDict(extra="forbid")

    patterns: list[str] = Field(min_length=1)
    policy: Literal["warn", "fail", "allowed"] = "warn"
    description: str | None = None

    @field_validator("patterns")
    @classmethod
    def _patterns_must_be_sane_globs(cls, value: list[str]) -> list[str]:
        for pattern in value:
            stripped = pattern.strip()
            if not stripped:
                raise ValueError("policy patterns must not be empty")
            _reject_unsafe_relative(stripped, "policy pattern")
        return value


class ReferenceSolution(BaseModel):
    """Maintenance-only pointer to a known-good patch.

    Used exclusively by ``benchmark validate``; never mounted into or copied
    to the agent-visible workspace.
    """

    model_config = ConfigDict(extra="forbid")

    patch: str  # path relative to the benchmark file

    @field_validator("patch")
    @classmethod
    def _patch_must_be_safe_relative(cls, value: str) -> str:
        return _reject_unsafe_relative(value, "reference_solution.patch")


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
    hidden_evaluations: HiddenEvaluationSpec | None = None
    protected_paths: list[str] = Field(default_factory=list)
    fail_on_protected_path_violation: bool = False
    change_policies: list[ChangePolicy] = Field(default_factory=list)
    execution: ExecutionSpec | None = None
    # Optional descriptive metadata (never exposed as evaluation criteria).
    description: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    reference_solution: ReferenceSolution | None = None
    expect_broken_baseline: bool = False

    @field_validator("results_dir")
    @classmethod
    def _results_dir_must_be_safe_relative(cls, value: str) -> str:
        return _reject_unsafe_relative(value, "results_dir")

    @field_validator("protected_paths")
    @classmethod
    def _protected_paths_must_be_sane_globs(cls, value: list[str]) -> list[str]:
        for pattern in value:
            stripped = pattern.strip()
            if not stripped:
                raise ValueError("protected_paths entries must not be empty")
            _reject_unsafe_relative(stripped, "protected_paths entry")
        return value

    @model_validator(mode="after")
    def _evaluation_names_must_be_unique(self) -> "BenchmarkSpec":
        # Sidecar logs are keyed by evaluation identity; duplicates would
        # silently overwrite each other's captured output.
        names = [evaluation.name for evaluation in self.all_evaluations]
        if len(names) != len(set(names)):
            raise ValueError("evaluation names must be unique across public and hidden evaluations")
        return self

    @property
    def all_evaluations(self) -> list[Evaluation]:
        """Public evaluations first, then hidden ones."""
        hidden = self.hidden_evaluations.evaluations if self.hidden_evaluations else []
        return [*self.evaluations, *hidden]

    def config_snapshot(self) -> dict:
        """JSON-safe snapshot of the configuration used for a run.

        The execution block is excluded: it describes *where* an experiment
        runs (provenance, compared separately), not what the experiment is.
        """
        payload = self.model_dump(mode="json")
        payload.pop("results_dir", None)
        payload.pop("execution", None)
        return payload

    def config_hash(self) -> str:
        """Stable short identity of this benchmark configuration."""
        canonical = json.dumps(self.config_snapshot(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# -- experiment schemas -------------------------------------------------------


class ConfigSpec(BaseModel):
    """One named agent/execution configuration inside an experiment."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    agent: AgentSpec
    execution: ExecutionSpec | None = None

    def config_hash(self) -> str:
        canonical = json.dumps(
            {"agent": self.agent.model_dump(mode="json"),
             "execution": self.execution.model_dump(mode="json") if self.execution else None},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class ExperimentSpec(BaseModel):
    """A benchmark × config × repeat matrix."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    benchmarks: list[str] = Field(min_length=1)
    configs: list[ConfigSpec] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=100)
    execution: ExecutionSpec | None = None  # experiment-level default
    results_dir: str = "results"

    @model_validator(mode="after")
    def _config_names_must_be_unique(self) -> "ExperimentSpec":
        names = [config.name for config in self.configs]
        if len(names) != len(set(names)):
            raise ValueError("experiment config names must be unique")
        if len(set(self.benchmarks)) != len(self.benchmarks):
            raise ValueError("experiment benchmark names must be unique")
        return self

    @property
    def cell_count(self) -> int:
        return len(self.benchmarks) * len(self.configs) * self.repeat
