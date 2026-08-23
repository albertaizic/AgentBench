"""Benchmark discovery: find manifests by name without hardcoding a list.

Two roots are searched, in order:

1. ``<cwd>/benchmarks/`` — user-local benchmark packs;
2. the built-in corpus shipped with AgentBench (``<repo>/benchmarks/``).

A benchmark is any directory containing ``benchmark.yaml``.
"""

from __future__ import annotations

from pathlib import Path

MANIFEST_NAME = "benchmark.yaml"


def builtin_root() -> Path:
    return Path(__file__).resolve().parents[2] / "benchmarks"


def discovery_roots(extra: Path | None = None) -> list[Path]:
    roots = [Path.cwd() / "benchmarks", builtin_root()]
    if extra is not None:
        roots.insert(0, Path(extra))
    seen: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def discover(extra: Path | None = None) -> list[Path]:
    """All manifest paths found across discovery roots (deduplicated, sorted)."""
    manifests: dict[str, Path] = {}
    for root in discovery_roots(extra):
        if not root.is_dir():
            continue
        for manifest in sorted(root.glob(f"*/{MANIFEST_NAME}")):
            manifests.setdefault(manifest.parent.name, manifest)
    return [manifests[name] for name in sorted(manifests)]


def find_manifest(name_or_path: str, extra: Path | None = None) -> Path:
    """Resolve a benchmark given either a manifest path or a corpus name."""
    direct = Path(name_or_path)
    if direct.suffix in (".yaml", ".yml") and direct.is_file():
        return direct.resolve()
    for root in discovery_roots(extra):
        candidate = root / name_or_path / MANIFEST_NAME
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"no benchmark named {name_or_path!r} (searched: "
        f"{', '.join(str(r) for r in discovery_roots(extra))})"
    )
