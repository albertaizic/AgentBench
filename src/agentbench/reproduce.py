"""Reproduce a stored run under the same experimental conditions.

Reproduction reconstructs the benchmark configuration from persisted
evidence and creates a NEW run. AgentBench reproduces *conditions* — fixed
repo, commit, config, backend — never deterministic LLM output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentbench.loader import load_benchmark, resolve_repository_path
from agentbench.models import ExecutionSpec


@dataclass
class ProvenanceComparison:
    original_run_id: str
    new_run_id: str | None = None
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    blocked_reason: str | None = None

    def add(self, name: str, same: bool, detail: str = "") -> None:
        self.checks.append((name, same, detail))

    @property
    def identical_conditions(self) -> bool:
        return all(same for _, same, _ in self.checks)


def preflight(original: dict, *, results_root: Path) -> ProvenanceComparison:
    """Static checks that reproduction is possible before spending tokens."""
    comparison = ProvenanceComparison(original_run_id=original.get("run_id", "?"))
    benchmark = original.get("benchmark") or {}
    config: dict = original.get("config") or {}

    manifest_hint = config.get("_benchmark_manifest")
    if not manifest_hint:
        comparison.blocked_reason = (
            "original result predates provenance storage (no benchmark manifest recorded)"
        )
        return comparison
    if not Path(manifest_hint).is_file():
        comparison.blocked_reason = f"benchmark manifest no longer exists: {manifest_hint}"
        return comparison

    try:
        spec = load_benchmark(manifest_hint)
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        comparison.blocked_reason = f"stored manifest failed to load: {exc}"
        return comparison

    if spec.name != benchmark.get("name"):
        comparison.blocked_reason = "benchmark name changed"
        return comparison

    current_hash = spec.config_hash()
    # Recompute the identity of the STORED snapshot under current rules:
    # trusting the recorded digest would block reproduction whenever the
    # hash inputs evolve (e.g. metadata leaving identity), even when the
    # evaluation-relevant configuration is byte-identical.
    from agentbench.models import benchmark_hash_from_snapshot

    stored_hash = benchmark_hash_from_snapshot(config)
    comparison.add("benchmark identity (config hash)", current_hash == stored_hash,
                   current_hash)
    if current_hash != stored_hash:
        comparison.blocked_reason = (
            "benchmark configuration changed since the original run "
            "(config hash differs) — refusing to silently mix conditions"
        )
        return comparison

    repository = resolve_repository_path(spec.repository, base_dir=Path(manifest_hint).parent)
    if not Path(repository).exists():
        comparison.blocked_reason = f"repository/fixture unavailable: {repository}"
        return comparison

    execution = original.get("execution") or {}
    if execution.get("backend") == "docker":
        from agentbench.backends.docker import docker_available

        if not docker_available():
            comparison.blocked_reason = (
                "original run used the Docker backend but Docker is currently unavailable"
            )
            return comparison
        spec.execution = execution_spec_from_provenance(execution)
    return comparison


def execution_spec_from_provenance(payload: dict) -> ExecutionSpec | None:
    """Rebuild an :class:`ExecutionSpec` from a stored execution-provenance block.

    Provenance records evidence under different names than the spec itself
    (``image_requested`` vs ``image``, ``memory_limit`` vs ``memory``) and adds
    evidence-only keys (``docker_version``, ``pass_env_evidence``, ...), so it
    cannot be passed to the spec constructor directly: ``ExecutionSpec``
    forbids unknown fields and would reject every real stored block.

    ``pass_env`` is reconstructed from ``passed_env_names`` — provenance
    records which allowlisted variables were actually present, so restoring
    exactly those names reproduces the original forwarding behavior.
    """
    if not payload:
        return None
    return ExecutionSpec(
        backend=payload.get("backend", "host"),
        image=payload.get("image_requested"),
        network=payload.get("network", "enabled"),
        memory=payload.get("memory_limit"),
        cpus=payload.get("cpus_limit"),
        pids_limit=payload.get("pids_limit"),
        pass_env=list(payload.get("passed_env_names") or []),
    )


def condition_checks(original: dict, rerun: dict) -> list[tuple[str, bool, str]]:
    """Post-run comparison of persisted identities."""
    checks = []

    def pair(name: str, section: str, key: str):
        old = (original.get(section) or {}).get(key)
        new = (rerun.get(section) or {}).get(key)
        checks.append((name, old == new and old is not None, f"{old} vs {new}"))

    # Identity is compared by RECOMPUTING both snapshots under current rules,
    # so evidence recorded before an identity-rule change still compares fairly.
    from agentbench.models import benchmark_hash_from_snapshot

    old_id = benchmark_hash_from_snapshot(original.get("config") or {})
    new_id = benchmark_hash_from_snapshot(rerun.get("config") or {})
    checks.append(("same benchmark identity", old_id == new_id, f"{old_id} vs {new_id}"))
    pair("same resolved commit", "benchmark", "resolved_commit")
    pair("same agent config", "config", "agent")
    old_backend = (original.get("execution") or {}).get("backend")
    new_backend = (rerun.get("execution") or {}).get("backend")
    checks.append(("same execution backend", old_backend == new_backend,
                   f"{old_backend} vs {new_backend}"))
    old_digest = ((original.get("execution") or {}).get("image_digests") or [None])[0]
    new_digest = ((rerun.get("execution") or {}).get("image_digests") or [None])[0]
    if old_digest or new_digest:
        checks.append(("same docker image digest", old_digest == new_digest,
                       f"{old_digest} vs {new_digest}"))
    else:
        checks.append(("same docker image digest", True, "n/a (host backend)"))
    return checks


def load_original_evidence(result_dir: Path) -> dict:
    import json

    return json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
