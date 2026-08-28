"""Task quality audit tests (P11-P16): patch-free oracle handling, rollup."""

from __future__ import annotations

import pytest

from agentbench import audit as audit_mod

from agentbench.audit import (
    QUALITY_INVALID,
    QUALITY_NEEDS_REVIEW,
    QUALITY_RELEASE,
    audit_benchmark,
)
class _Ref:
    patch = "reference/fix.patch"


class _Spec:
    """Minimal stand-in for BenchmarkSpec (audit only reads these)."""

    def __init__(self, reference=True):
        self.name = "demo"
        self.reference_solution = _Ref() if reference else None
        self.hidden_evaluations = None
        self.protected_paths = ["tests/**"]
        self.change_policies = []
        self.prompt_requirements = [{"id": "r1", "text": "t"}]
        self.requirement_mappings = [{"requirement": "r1", "scored_by": ["core"]}]
        self.scoring_groups = {"core": {"weight": 1.0, "required": True}}
        self.source_kind = "authored"
        self.task_created_at = "2026-08-25"
        self.contamination_risk = "low"
        self.platforms = ["any"]
        self.human_time = {"expert_time_estimate_minutes": 30,
                           "estimate_method": "author_estimate"}
        self.canary = None

    def model_copy(self, deep: bool = True):
        return _Spec(reference=self.reference_solution is not None)


class _Validation:
    ok = True
    checks = []


def _install_stubs(monkeypatch, *, oracle_passes=5, oracle_runs=5, nop_fails=5,
                   nop_runs=5, reference=True, validate_ok=True):
    monkeypatch.setattr(audit_mod, "load_benchmark", lambda p: _Spec(reference))
    monkeypatch.setattr(audit_mod, "validate_benchmark",
                        lambda p: _Validation())
    monkeypatch.setattr(audit_mod, "check_regeneration_determinism",
                        lambda p: (True, "deterministic"))

    def fake_evals(spec, bench_dir, times):
        is_nop = spec.reference_solution is None and not reference is False
        # For the oracle call the caller passes the real spec (reference kept);
        # for nop the caller passes a clone with reference stripped.
        if spec.reference_solution is not None:
            return {"runs_requested": times, "passes": oracle_passes,
                    "fails": times - oracle_passes if oracle_passes < times else 0,
                    "errors": 0, "durations": [1.0], "duration_median_s": 1.0,
                    "last_detail": ""}
        return {"runs_requested": times, "passes": times - nop_fails,
                "fails": nop_fails, "errors": 0, "durations": [1.0],
                "duration_median_s": 1.0, "last_detail": ""}

    monkeypatch.setattr(audit_mod, "_run_evals_n_times", fake_evals)


def test_patch_free_oracle_is_warn_not_fail(tmp_path, monkeypatch):
    _install_stubs(monkeypatch, reference=False)
    report = audit_benchmark(tmp_path / "benchmark.yaml", oracle_runs=2, nop_runs=2)
    oracle = next(d for d in report.dimensions if d.name == "oracle_stability")
    assert oracle.verdict == "WARN"
    assert "not applicable" in oracle.detail
    assert report.quality_status != QUALITY_INVALID


def test_release_grade_requires_full_stability(tmp_path, monkeypatch):
    _install_stubs(monkeypatch, reference=True, oracle_passes=5, oracle_runs=5,
                   nop_fails=5, nop_runs=5)
    report = audit_benchmark(tmp_path / "benchmark.yaml", oracle_runs=5, nop_runs=5)
    assert report.quality_status == QUALITY_RELEASE


def test_flaky_oracle_blocks_release(tmp_path, monkeypatch):
    _install_stubs(monkeypatch, reference=True, oracle_passes=3, oracle_runs=5,
                   nop_fails=5, nop_runs=5)
    report = audit_benchmark(tmp_path / "benchmark.yaml", oracle_runs=5, nop_runs=5)
    assert report.quality_status in (QUALITY_NEEDS_REVIEW, QUALITY_INVALID)


def test_flaky_baseline_is_hard_signal(tmp_path, monkeypatch):
    _install_stubs(monkeypatch, reference=True, oracle_passes=5, oracle_runs=5,
                   nop_fails=2, nop_runs=5)  # baseline sometimes PASSES
    report = audit_benchmark(tmp_path / "benchmark.yaml", oracle_runs=5, nop_runs=5)
    nop = next(d for d in report.dimensions if d.name == "nop_stability")
    assert nop.verdict == "FAIL"
