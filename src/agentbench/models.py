"""Pydantic schemas describing benchmarks, execution, and experiments.

Validation is deliberately strict (`extra="forbid"` everywhere): benchmark
files must fail loudly on typos rather than silently run something else.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

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
    * ``omp``         – the OMP (Oh My Pi) coding-agent CLI, run headless in
      JSON-streaming print mode with user customization sources disabled;
    * ``command``     – a generic argv-based adapter so any non-interactive
      coding agent can be benchmarked without touching AgentBench core. The
      prompt is delivered on stdin by default, or via a ``{prompt}``
      placeholder in one argv element when ``prompt_mode="arg"``. Never a
      shell string.
    """

    type: Literal["claude-code", "command", "hermes", "omp"]
    command: str | None = None
    # Model selector honored by adapters that support it (claude-code,
    # hermes, omp). Part of the config identity.
    model: str | None = None
    # Optional adapter-argument escape hatch; never displaces isolation
    # guarantees and never carries the prompt.
    extra_args: list[str] = Field(default_factory=list)
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


class ScoringGroup(BaseModel):
    """One independently meaningful dimension of task success (P9).

    ``weight`` drives the partial score; ``required`` groups must fully pass
    for the binary resolution to stay ``passed``. Weights across all declared
    groups are normalized to 1.0.
    """

    model_config = ConfigDict(extra="forbid")

    weight: float = Field(default=1.0, gt=0)
    required: bool = False


class Scorer(BaseModel):
    """A deterministic, programmatic scorer (P8) — never an LLM judge.

    ``binary`` scorers decide pass/fail by exit code, exactly like the
    legacy ``evaluations`` list. ``fraction``/``continuous`` scorers
    additionally parse a final-line ``agentbench-score: <0..1>`` marker;
    ``count`` scorers report their raw number without contributing to the
    partial score unless ``max_count`` normalizes them.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    score_type: Literal["binary", "fraction", "count", "continuous"] = "binary"
    groups: list[str] = Field(default_factory=lambda: ["default"])
    required: bool = True
    max_count: int | None = Field(default=None, gt=0)


class Evaluation(BaseModel):
    """A single shell command whose exit code decides pass/fail.

    Legacy v0.1 surface kept verbatim; internally equivalent to a binary
    scorer in the ``default`` group.
    """

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
    # Soft budget REQUESTS (P39). AgentBench always enforces wall time via
    # ``timeout_seconds``. Token/cost budgets are recorded as requested and
    # marked enforced=False unless the adapter declares a matching
    # enforcement capability — a harness that cannot interrupt itself must
    # not pretend it can.
    token_budget: int | None = Field(default=None, gt=0)
    cost_budget_usd: float | None = Field(default=None, ge=0)
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
    # v0.6 quality/provenance block: documentation ABOUT the task. Editing
    # these records review work or provenance facts; none of them changes
    # what is executed or how success is decided, so they stay OUT of
    # benchmark identity (adding "needs-review" must never make old runs
    # unreproducible). Grading-relevant blocks — scorers, scoring_groups,
    # evaluations, protected_paths, prompt, commit — remain IN identity.
    "prompt_requirements", "requirement_mappings", "source_kind",
    "task_created_at", "task_public_since", "solution_public_since",
    "contamination_risk", "canary", "human_time", "platforms",
    "instruction_style", "quality_status",
)

_NON_IDENTITY_FIELDS = ("results_dir", "execution", "_benchmark_manifest",
                        "_baseline", *_BENCHMARK_METADATA_FIELDS)

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

    # -- v0.6: scoring / partial credit (P8, P9) ---------------------------
    # Declarative success dimensions. Absent => legacy behavior: every
    # evaluation is a required binary scorer in the "default" group.
    scoring_groups: dict[str, ScoringGroup] | None = None
    scorers: list[Scorer] = Field(default_factory=list)

    # -- v0.6: task provenance & quality metadata (P14, P16, P24, P25) -----
    prompt_requirements: list[dict[str, str]] = Field(default_factory=list)
    requirement_mappings: list[dict[str, Any]] = Field(default_factory=list)
    source_kind: Literal[
        "authored", "synthetic", "historical-public", "fresh-public", "cleanroom"
    ] = "authored"
    task_created_at: str | None = None
    task_public_since: str | None = None
    solution_public_since: str | None = None
    contamination_risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    canary: dict[str, str] | None = None  # {string, placement} — never graded
    human_time: dict[str, Any] | None = None
    platforms: list[Literal["windows", "linux", "any"]] = Field(default_factory=lambda: ["any"])
    instruction_style: Literal["explicit-task", "goal-oriented"] = "explicit-task"
    stages: list[dict[str, Any]] = Field(default_factory=list)
    quality_status: Literal[
        "unreviewed", "provisional", "release-grade", "needs-review", "invalid"
    ] = "unreviewed"

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

    @model_validator(mode="after")
    def _scorer_ids_must_be_unique(self) -> "BenchmarkSpec":
        # compute_scoring keys executed outcomes by scorer id; a duplicate
        # would silently shadow the first result and fabricate credit.
        ids = [scorer.id for scorer in self.scorers]
        if len(ids) != len(set(ids)):
            raise ValueError("scorer ids must be unique within scorers")
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


# Model-identity fields a ``model-controlled`` experiment is allowed to vary.
_MODEL_VARYING_FIELDS = ("model", "provider", "reasoning")


def _validate_comparison_mode(spec: "ExperimentSpec") -> list[str]:
    """Validate a declared comparison intent; return non-fatal warnings.

    * model-controlled: every config must share the same harness (type,
      command, extra_args, prompt_mode); only model/provider/reasoning may
      differ, and at least one MUST differ — otherwise nothing is compared.
    * scaffold-controlled: the author declares the model/provider side
      equivalent; AgentBench cannot verify provider-side equivalence, so any
      difference becomes a persisted warning instead of an error.
    * system-comparison: warn when configs share both harness and model
      (the experiment would compare nothing material).
    """
    import json as _json

    warnings: list[str] = []
    agents = [c.agent for c in spec.configs]
    names = [c.name for c in spec.configs]

    def same(field: str) -> bool:
        values = {_json.dumps(getattr(a, field), sort_keys=True) for a in agents}
        return len(values) == 1

    def differing(field: str) -> list[str]:
        return sorted({str(getattr(a, field)) for a in agents})

    if spec.comparison_mode == "model-controlled":
        for field in ("type", "command", "extra_args", "prompt_mode"):
            if not same(field):
                raise ValueError(
                    f"model-controlled experiment requires identical agent "
                    f"{field} across configs {names}; use "
                    f"'system-comparison' if the harness itself differs"
                )
        # Execution-level differences (timeout, backend, network policy) do
        # not change the model under test but CAN change outcomes; they are
        # allowed as an explicit override and persisted as a warning so no
        # reader mistakes the run for a pure model comparison.
        executions = [_json.dumps(c.execution.model_dump(mode="json"), sort_keys=True)
                      if c.execution else None for c in spec.configs]
        if len(set(executions)) > 1:
            warnings.append(
                f"model-controlled experiment has differing execution "
                f"settings across configs {names}; results may reflect "
                f"execution policy, not model capability alone"
            )
    elif spec.comparison_mode == "scaffold-controlled":
        for field in _MODEL_VARYING_FIELDS:
            values = differing(field)
            if len(values) > 1:
                warnings.append(
                    f"scaffold-controlled comparison: '{field}' differs across "
                    f"configs ({' vs '.join(values)}); author declares the "
                    f"model-side configuration equivalent — AgentBench cannot "
                    f"verify provider-side equivalence"
                )
        if len(spec.configs) > 1 and same("type"):
            warnings.append(
                f"scaffold-controlled comparison uses a single harness type "
                f"({agents[0].type!r}); no scaffold variation is present"
            )
    else:  # system-comparison
        if len(spec.configs) > 1 and all(
            same(field) for field in ("type", *_MODEL_VARYING_FIELDS)
        ):
            warnings.append(
                "system-comparison configs share harness and model settings; "
                "the experiment compares nothing material"
            )
    return warnings


def validate_comparison_mode(spec: "ExperimentSpec") -> list[str]:
    """Public alias of the load-time comparison-intent validator."""
    return _validate_comparison_mode(spec)


class ExperimentSpec(BaseModel):
    """A benchmark × config × repeat matrix.

    ``comparison_mode`` makes the intent of a comparison explicit so reports
    never conflate model capability with harness/scaffold capability:

    * ``system-comparison``   — complete systems differ (harness AND model);
      differences in harness behavior are part of the compared system.
    * ``model-controlled``    — same harness, tool policy, backend, timeout,
      prompt and environment; ONLY model-related configuration differs.
    * ``scaffold-controlled`` — scaffolds differ while the model/provider
      configuration is declared equivalent; equivalence cannot be verified
      automatically, so creation records an explicit warning instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    # Explicit benchmark names, or a metadata selector (suite/tags/category).
    benchmarks: list[str] | BenchmarkSelection
    configs: list[ConfigSpec] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=100)
    execution: ExecutionSpec | None = None  # experiment-level default
    results_dir: str = "results"
    comparison_mode: Literal[
        "system-comparison", "model-controlled", "scaffold-controlled"
    ] = "system-comparison"
    # Populated automatically at load time by _validate_comparison_mode.
    comparison_warnings: list[str] = Field(default_factory=list)

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
        # Comparison-intent consistency (P1). Hard errors fire here at YAML
        # load time; returned warnings are persisted with the manifest.
        self.comparison_warnings = _validate_comparison_mode(self)
        return self

    @property
    def cell_count(self) -> int:
        count = len(self.benchmarks) if isinstance(self.benchmarks, list) else 0
        return count * len(self.configs) * self.repeat
