"""Hatchling build hook: package the benchmark corpus without build artifacts.

The shipped corpus is part of the product (see pyproject dependencies note).
Generated ``fixture/`` repositories are .gitignored build artifacts that are
reprovisioned deterministically from each benchmark's ``create_fixture.py``
at validate/run time, so they must never enter the wheel — including via
direct-wheel builds (``pip install .``, ``python -m build --wheel``) where
VCS-based filtering does not apply.
"""

from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # build-time-only dep; absent in plain test envs
    BuildHookInterface = object  # type: ignore[misc,assignment]

CORPUS_DIR = "benchmarks"

SKIP_PATH_PARTS = {"fixture", "__pycache__"}


def corpus_wheel_files(project_root: Path) -> dict[str, str]:
    """Map every shippable corpus file to its location inside the wheel."""
    mapping: dict[str, str] = {}
    root = project_root / CORPUS_DIR
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith((".pyc", ".pyo")):
            continue
        rel_parts = path.relative_to(project_root).parts
        if any(part in SKIP_PATH_PARTS for part in rel_parts):
            continue
        wheel_path = "src/agentbench/" + "/".join(rel_parts)
        mapping[path.as_posix()] = wheel_path
    return mapping


class CorpusPackagingHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        build_data["force_include"].update(
            corpus_wheel_files(Path(self.root))
        )
