"""The packaged corpus must ship curated assets, never generated fixtures."""

from __future__ import annotations

from pathlib import Path

from hatch_build import SKIP_PATH_PARTS, corpus_wheel_files


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
