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

    Three adapter types exist:

    * ``claude-code`` – the Claude Code CLI (primary real adapter);
    * ``hermes``      – the Hermes agent CLI, an OpenRouter-backed coding
      agent with a real tool loop, run in one-shot mode;
    * ``command``     – a generic argv-based adapter so any non-interactive
      coding agent can be benchmarked without touching AgentBench core. The
      prompt is delivered on stdin by default, or via a ``{prompt}``
      placeholder in one argv element when ``prompt_mode="arg"``. Never a
      shell string.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["claude-code", "command", "hermes"]
    # claude-code: optional override of the binary to execute (wrapper scripts).
    command: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    # Model selector honored by adapters that support it (claude-code, hermes).
    model: str | None = None
    # hermes adapter: inference provider and reasoning-effort overrides; both
    # materially change behavior, so both are part of the config identity.
    provider: str | None = None
    reasoning: str | None = None
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
    # Host-backend environment policy. "inherit" (default) passes the full
    # parent environment through — required when an agent authenticates from
    # ambient state. "restricted" starts from a minimal OS base plus the
    # ``pass_env`` allowlist and points HOME/USERPROFILE at a throwaway
    # directory, so agents that do not need ambient config cannot read it.
    env_policy: Literal["inherit", "restricted"] = "inherit"

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
        fields["env_policy"] = override.env_policy if override.env_policy != "inherit" else self.env_policy
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


# Descriptive metadata: documentation for humans and corpus tooling. It never
# influences how a run executes or scores, so it is excluded from config
# identity — editing a description must not invalidate evidence produced
# before the edit (and vice versa).
_BENCHMARK_METADATA_FIELDS = (
    "description", "category", "tags", "suites", "language",
    "difficulty", "reference_solution", "expect_broken_baseline",
)

_NON_IDENTITY_FIELDS = ("results_dir", "execution", "_benchmark_manifest",
                        *_BENCHMARK_METADATA_FIELDS)


def benchmark_hash_from_snapshot(snapshot: dict) -> str:
    """Hash a stored config snapshot under the CURRENT identity rules.

    Used by ``reproduce`` so old evidence is compared against the live
    manifest with today's semantics instead of trusting a digest computed
    under whatever rules existed when the run was recorded.
    """
    normalized = {
        key: value for key, value in snapshot.items()
        if key not in _NON_IDENTITY_FIELDS
    }
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def benchmark_task_hash_from_snapshot(snapshot: dict) -> str:
    """Task-only identity: everything except the agent block and metadata.

    Experiment runs inject per-config agent overrides (model/provider/
    reasoning) into the effective snapshot, while the manifest on disk may
    leave ``agent`` unpinned. Task identity must be agent-independent so
    reproducing an experiment-launched run compares like with like; the
    effective agent configuration is replayed from the stored evidence
    instead of being re-derived from the manifest.
    """
    normalized = {
        key: value for key, value in snapshot.items()
        if key not in _NON_IDENTITY_FIELDS and key != "agent"
    }
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


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
    # Declarative suite membership (e.g. smoke, python-core, performance);
    # suites are corpus metadata, never hardcoded CLI lists.
    suites: list[str] = Field(default_factory=list)
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

    # Descriptive metadata: documentation for humans and corpus tooling. It
    # never influences how a run executes or scores, so it stays OUT of
    # config identity — editing a description must not invalidate evidence
    # produced before the edit (and vice versa).
    def config_snapshot(self) -> dict:
        """JSON-safe snapshot of the configuration used for a run.

        Excluded: ``results_dir`` (machine-local), ``execution`` (describes
        *where* an experiment runs — provenance, compared separately), and
        the descriptive metadata block (never evaluation-relevant).
        """
        payload = self.model_dump(mode="json")
        payload.pop("results_dir", None)
        payload.pop("execution", None)
        for name in _BENCHMARK_METADATA_FIELDS:
            payload.pop(name, None)
        return payload

    def config_hash(self) -> str:
        """Stable short identity of this benchmark configuration."""
        return benchmark_hash_from_snapshot(self.config_snapshot())


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


class BenchmarkSelection(BaseModel):
    """Select benchmarks by corpus metadata instead of explicit names.

    Exactly one criterion is required; resolution to concrete benchmark
    names happens once, before execution, and the resolved list is persisted
    into the experiment manifest so later corpus changes never silently
    alter an already-created experiment.
    """

    model_config = ConfigDict(extra="forbid")

    suite: str | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None

    @model_validator(mode="after")
    def _exactly_one_criterion(self) -> "BenchmarkSelection":
        provided = [name for name in ("suite", "category") if getattr(self, name)]
        if self.tags:
            provided.append("tags")
        if len(provided) != 1:
            raise ValueError(
                "benchmark selection requires exactly one of suite / tags / category,"
                f" got {provided or 'none'}"
            )
        return self


class ExperimentSpec(BaseModel):
    """A benchmark × config × repeat matrix."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    # Explicit benchmark names, or a metadata selector (suite/tags/category).
    benchmarks: list[str] | BenchmarkSelection
    configs: list[ConfigSpec] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=100)
    execution: ExecutionSpec | None = None  # experiment-level default
    results_dir: str = "results"

    @model_validator(mode="after")
    def _validate_shape(self) -> "ExperimentSpec":
        names = [config.name for config in self.configs]
        if len(names) != len(set(names)):
            raise ValueError("experiment config names must be unique")
        if isinstance(self.benchmarks, list):
            if not self.benchmarks:
                raise ValueError("experiment needs at least one benchmark")
            if len(set(self.benchmarks)) != len(self.benchmarks):
                raise ValueError("experiment benchmark names must be unique")
        return self

    @property
    def cell_count(self) -> int:
        count = len(self.benchmarks) if isinstance(self.benchmarks, list) else 0
        return count * len(self.configs) * self.repeat
