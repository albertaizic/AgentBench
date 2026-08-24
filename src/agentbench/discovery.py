"""Benchmark discovery: find manifests by name without hardcoding a list.

Two roots are searched, in order:

1. ``<cwd>/benchmarks/`` — user-local benchmark packs;
2. the built-in corpus shipped with AgentBench (``<repo>/benchmarks/``).

A benchmark is any directory containing ``benchmark.yaml``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

MANIFEST_NAME = "benchmark.yaml"


def builtin_root() -> Path:
    """The shipped corpus: packaged copy first, source checkout fallback."""
    packaged = Path(__file__).resolve().parent / "benchmarks"
    if packaged.is_dir():
        return packaged
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


def select_benchmarks(selection, extra: Path | None = None) -> list[str]:
    """Resolve an experiment benchmark selector to concrete corpus names.

    * ``list[str]``  — explicit names pass through untouched;
    * ``{"suite": s}`` / ``{"tags": [...]}`` / ``{"category": c}`` — every
      loaded corpus benchmark whose metadata matches.

    Invalid manifests are skipped (they cannot match anything reliably).
    Raises FileNotFoundError when a metadata selector matches nothing —
    silently running zero cells would be worse than failing loudly.
    """
    if isinstance(selection, list):
        return list(selection)

    from agentbench.loader import LoaderError, load_benchmark

    matches: list[str] = []
    for manifest in discover(extra):
        try:
            spec = load_benchmark(manifest)
        except (LoaderError, ValidationError):
            continue
        if selection.suite and selection.suite not in spec.suites:
            continue
        if selection.tags and not set(selection.tags) <= set(spec.tags):
            continue
        if selection.category and selection.category != spec.category:
            continue
        matches.append(spec.name)
    if not matches:
        criterion = (
            f"suite={selection.suite!r}" if selection.suite
            else f"tags={selection.tags!r}" if selection.tags
            else f"category={selection.category!r}"
        )
        raise FileNotFoundError(f"no corpus benchmarks match {criterion}")
    return matches


def matching_suites(suite: str | None = None, extra: Path | None = None) -> list[str]:
    """Distinct suites in the corpus; used by `benchmark list --suite` filtering."""
    from agentbench.loader import LoaderError, load_benchmark

    names: set[str] = set()
    for manifest in discover(extra):
        try:
            spec = load_benchmark(manifest)
        except (LoaderError, ValidationError):
            continue
        if suite is None or suite in spec.suites:
            names.update(spec.suites)
    return sorted(names)
