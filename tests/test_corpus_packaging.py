"""The packaged corpus must ship curated assets, never generated fixtures."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_hatch_build():
    """Load the repository-root ``hatch_build.py`` by absolute path.

    ``hatch_build.py`` is a Hatchling build hook at the repo root and is
    intentionally NOT a normal importable package module — placing its
    file path on ``sys.path`` would let other tests import arbitrary
    repository-root modules and silently mask real packaging bugs. Load
    it by filesystem location instead so this test exercises the actual
    build hook under any pytest invocation mode.
    """
    hook_path = _repo_root() / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("_hatch_build_under_test",
                                                  hook_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load build hook at {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HOOK = _load_hatch_build()
SKIP_PATH_PARTS = _HOOK.SKIP_PATH_PARTS
corpus_wheel_files = _HOOK.corpus_wheel_files


class TestCorpusPackagingHook:
    def test_all_benchmark_manifests_shipped(self):
        mapping = corpus_wheel_files(_repo_root())
        manifests = [k for k in mapping if k.endswith("/benchmark.yaml")]
        assert len(manifests) == 32

    def test_generated_fixtures_excluded(self):
        mapping = corpus_wheel_files(_repo_root())
        assert not any(f"{p}/fixture" in k for k in mapping for p in ["benchmarks"])
        # A known generated artifact path must never appear.
        assert not any("benchmarks/jobqueue/fixture/" in k for k in mapping)

    def test_hidden_and_reference_assets_shipped(self):
        mapping = corpus_wheel_files(_repo_root())
        assert any("benchmarks/jobqueue/hidden/" in k for k in mapping)
        assert any("benchmarks/ledgerpad/reference/fix.patch" in k for k in mapping)

    def test_skip_parts_documented(self):
        assert "fixture" in SKIP_PATH_PARTS
        assert "__pycache__" in SKIP_PATH_PARTS