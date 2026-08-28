"""Task quality audit (v0.6 P11-P16, P24-P29).

``validate`` proves a benchmark is *solvable*; ``audit`` asks whether it is
*trustworthy*: oracle/nop stability across repeated fresh environments,
requirement-to-scorer mapping completeness, provenance metadata, and the
absence of trivial answer leakage. Every dimension yields PASS/WARN/FAIL
with evidence; the rollup quality_status is deliberately conservative:

    invalid        any FAIL on correctness-critical dimensions
    needs-review   any FAIL on metadata dimensions, or repeated instability
    provisional    WARNs only (default for new tasks until calibrated)
    release-grade  everything green AND oracle/nop stability at requested N

Audits never modify benchmarks and never invoke coding agents.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agentbench.loader import load_benchmark, resolve_repository_path
from agentbench.models import BenchmarkSpec
from agentbench.validation import (
    check_regeneration_determinism,
    validate_benchmark,
)

Verdict = Literal["PASS", "WARN", "FAIL"]
QUALITY_RELEASE = "release-grade"
QUALITY_PROVISIONAL = "provisional"
QUALITY_NEEDS_REVIEW = "needs-review"
QUALITY_INVALID = "invalid"


@dataclass
class Dimension:
    name: str
    verdict: Verdict
    detail: str


@dataclass
class AuditReport:
    name: str
    dimensions: list[Dimension] = field(default_factory=list)
    oracle: dict[str, Any] = field(default_factory=dict)
    nop: dict[str, Any] = field(default_factory=dict)
    quality_status: str = QUALITY_PROVISIONAL

    def add(self, name: str, verdict: Verdict, detail: str) -> None:
        self.dimensions.append(Dimension(name, verdict, detail))

    @property
    def has_fail(self) -> bool:
        return any(d.verdict == "FAIL" for d in self.dimensions)

    @property
    def has_warn(self) -> bool:
        return any(d.verdict == "WARN" for d in self.dimensions)


def _run_evals_n_times(spec: BenchmarkSpec, benchmark_dir: Path, times: int) -> dict[str, Any]:
    """Run public+hidden evaluators against a fresh workspace N times.

    Returns pass/fail counts plus duration stats. Used for BOTH oracle
    (reference applied) and nop (untouched baseline) stability.
    """
    from agentbench.evaluation import run_evaluation, run_hidden_evaluation
    from agentbench.workspace import create_workspace

    manifest_path = benchmark_dir / "benchmark.yaml"
    passes = fails = errors = 0
    durations: list[float] = []
    last_detail = ""
    for _ in range(times):
        try:
            workspace = create_workspace(str(
                resolve_repository_path(spec.repository, base_dir=benchmark_dir)),
                spec.commit)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_detail = f"workspace: {exc}"
            continue
        try:
            if spec.reference_solution is not None and spec.reference_solution.patch:
                from agentbench.diffs import GIT_EXECUTABLE
                patch_text = (benchmark_dir / spec.reference_solution.patch).read_text(
                    encoding="utf-8")
                apply_result = _git_apply(workspace.path, patch_text)
                if apply_result != "":
                    errors += 1
                    last_detail = apply_result[:200]
                    continue
            all_ok = True
            for evaluation in spec.evaluations:
                outcome = run_evaluation(evaluation, workspace=workspace.path,
                                         timeout=spec.timeout_seconds)
                durations.append(outcome.duration_seconds)
                if not outcome.passed:
                    all_ok = False
                    last_detail = f"public/{evaluation.name} exit={outcome.exit_code}"
            if spec.hidden_evaluations is not None:
                hidden_dir = (benchmark_dir / spec.hidden_evaluations.source).resolve()
                for evaluation in spec.hidden_evaluations.evaluations:
                    outcome = run_hidden_evaluation(
                        evaluation, workspace=workspace.path, hidden_dir=hidden_dir,
                        timeout=spec.timeout_seconds)
                    durations.append(outcome.duration_seconds)
                    if not outcome.passed:
                        all_ok = False
                        last_detail = f"hidden/{evaluation.name} exit={outcome.exit_code}"
            if all_ok:
                passes += 1
            else:
                fails += 1
        finally:
            try:
                workspace.cleanup()
            except Exception:  # noqa: BLE001
                pass
    return {
        "runs_requested": times,
        "passes": passes,
        "fails": fails,
        "errors": errors,
        "durations": durations,
        "duration_median_s": round(statistics.median(durations), 3) if durations else None,
        "last_detail": last_detail,
    }


def _git_apply(workspace: Path, patch_text: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(workspace), "apply", "--whitespace=nowarn", "-"],
        input=patch_text, capture_output=True, text=True, timeout=60,
    )
    return "" if result.returncode == 0 else (result.stderr or "patch failed")


def audit_benchmark(
    manifest_path: Path,
    *,
    oracle_runs: int = 1,
    nop_runs: int = 1,
    skip_stability: bool = False,
) -> AuditReport:
    spec = load_benchmark(manifest_path)
    report = AuditReport(name=spec.name)
    bench_dir = manifest_path.parent

    # 1) foundation: the full validate pipeline
    validation = validate_benchmark(manifest_path)
    report.add("manifest_and_solvable",
               "PASS" if validation.ok else "FAIL",
               "; ".join(detail for _, ok, detail in validation.checks if not ok)
               or "all validate checks green")

    # 2) deterministic fixture regeneration
    regen_ok, regen_detail = check_regeneration_determinism(manifest_path)
    report.add("regeneration_deterministic", "PASS" if regen_ok else "FAIL", regen_detail)

    # 3) isolation: hidden/reference outside fixture tree; protected paths set
    hidden_ok = True
    hidden_detail = "no hidden evaluators declared"
    if spec.hidden_evaluations is not None:
        hidden_src = (bench_dir / spec.hidden_evaluations.source).resolve()
        inside_fixture = False
        try:
            repo = resolve_repository_path(spec.repository, base_dir=bench_dir)
            inside_fixture = hidden_src.resolve().is_relative_to(Path(repo).resolve())
        except Exception:  # noqa: BLE001
            inside_fixture = False
        hidden_ok = not inside_fixture
        hidden_detail = ("hidden source INSIDE fixture tree — leakage risk"
                         if inside_fixture else
                         f"hidden source outside fixture ({hidden_src.name}/)")
    report.add("hidden_isolation", "PASS" if hidden_ok else "FAIL", hidden_detail)

    ref_declared = spec.reference_solution is not None
    ref_inside = False
    if ref_declared and spec.reference_solution and spec.reference_solution.patch:
        ref_file = (bench_dir / spec.reference_solution.patch).resolve()
        try:
            repo = resolve_repository_path(spec.repository, base_dir=bench_dir)
            ref_inside = ref_file.is_relative_to(Path(repo).resolve())
        except Exception:  # noqa: BLE001
            ref_inside = False
    report.add("reference_isolation",
               "PASS" if ref_declared and not ref_inside else
               ("WARN" if not ref_declared else "FAIL"),
               "no reference declared (patch-free task)" if not ref_declared
               else ("reference INSIDE fixture tree" if ref_inside else "reference kept beside fixture"))

    protected_ok = bool(spec.protected_paths) or bool(spec.change_policies)
    report.add("protected_paths_meaningful",
               "PASS" if protected_ok else "WARN",
               ", ".join(spec.protected_paths) or "none declared — test tampering undetected")

    # 4) requirement mapping lint (P14)
    req_ids = [r.get("id") for r in spec.prompt_requirements]
    mapped = {m.get("requirement") for m in spec.requirement_mappings}
    unscored = [r for r in req_ids if r not in mapped]
    groups_referenced = {
        g for m in spec.requirement_mappings for g in (m.get("scored_by") or [])
    }
    orphan_groups = [
        g for g in (spec.scoring_groups or {}) if g not in groups_referenced
        and g != "default"
    ]
    if not spec.prompt_requirements:
        report.add("requirement_mapping", "WARN",
                   "no prompt_requirements declared — alignment review is manual")
    elif unscored or orphan_groups:
        report.add("requirement_mapping", "WARN",
                   f"unscored requirements: {unscored}; unmapped groups: {orphan_groups}")
    else:
        report.add("requirement_mapping", "PASS",
                   f"{len(req_ids)} requirement(s) fully mapped")

    # 5) provenance / contamination / human-time / platforms (P16, P24, P43)
    meta_problems = []
    if spec.source_kind == "authored" and not spec.task_created_at:
        meta_problems.append("task_created_at missing")
    if spec.contamination_risk == "unknown":
        meta_problems.append("contamination_risk unknown")
    if not spec.platforms:
        meta_problems.append("platforms unset")
    report.add("provenance_metadata",
               "FAIL" if len(meta_problems) >= 2 else ("WARN" if meta_problems else "PASS"),
               "; ".join(meta_problems) or "source/contamination/platforms recorded")

    ht = spec.human_time or {}
    if ht.get("expert_time_estimate_minutes"):
        report.add("human_time_metadata", "PASS",
                   f"{ht['expert_time_estimate_minutes']} min "
                   f"({ht.get('estimate_method', 'unknown method')})")
    else:
        report.add("human_time_metadata", "WARN", "not estimated (allowed)")

    # 6) partial-score support (P9): groups declared => partial available
    report.add("partial_score_support",
               "PASS" if spec.scoring_groups else "WARN",
               f"{len(spec.scoring_groups)} group(s)" if spec.scoring_groups
               else "binary only — no partial credit dimension")

    # 7) canary placement sanity (P25): must never appear in evaluators
    if spec.canary and spec.canary.get("string"):
        canary_text = spec.canary["string"]
        eval_blob = json_blob_of_evaluators(spec)
        leaked = canary_text in eval_blob
        report.add("canary_placement", "FAIL" if leaked else "PASS",
                   "canary string appears in evaluator commands!" if leaked
                   else f"canary recorded ({spec.canary.get('placement')})")

    # 8) oracle / nop stability (P12, P13)
    if not skip_stability:
        if spec.reference_solution is None:
            # Patch-free grading (e.g. mutation-scored test writing): there
            # is no reference to apply, so repeated-oracle checks are not
            # applicable. Reported as WARN, never as an oracle failure.
            report.oracle = {"skipped": True, "reason": "patch-free grading"}
            report.add("oracle_stability", "WARN",
                       "not applicable: patch-free task declares no reference")
        else:
            oracle = _run_evals_n_times(spec, bench_dir, oracle_runs)
            report.oracle = oracle
            stable = oracle["errors"] == 0 and oracle["passes"] == oracle_runs
            report.add("oracle_stability",
                       "PASS" if stable else ("WARN" if oracle["passes"] > 0 else "FAIL"),
                       f"{oracle['passes']}/{oracle_runs} passed"
                       + (f"; last: {oracle['last_detail']}" if oracle["last_detail"] else ""))

        nop = _run_evals_n_times(_nop_spec(spec), bench_dir, nop_runs)
        report.nop = nop
        nop_fails_every_time = nop["errors"] == 0 and nop["fails"] == nop_runs
        report.add("nop_stability",
                   "PASS" if nop_fails_every_time else
                   ("FAIL" if nop["passes"] > 0 else "WARN"),
                   f"baseline failed {nop['fails']}/{nop_runs}"
                   + (" — FLAKY BASELINE" if nop["passes"] else ""))
    else:
        report.oracle = {"skipped": True}
        report.nop = {"skipped": True}
        report.add("oracle_nop_stability", "WARN", "skipped (--skip-stability)")

    report.quality_status = _rollup(report, oracle_runs if not skip_stability else 0)
    return report


def json_blob_of_evaluators(spec: BenchmarkSpec) -> str:
    blob = [e.command for e in spec.evaluations]
    if spec.hidden_evaluations is not None:
        blob.extend(e.command for e in spec.hidden_evaluations.evaluations)
    return "\n".join(blob)


def _nop_spec(spec: BenchmarkSpec) -> BenchmarkSpec:
    """A spec whose 'solution' is nothing: same checks, no reference."""
    clone = spec.model_copy(deep=True)
    clone.reference_solution = None
    return clone


_NON_BLOCKING_WARN_DIMENSIONS = {
    # Explicitly optional dimensions: their WARN means "not provided, which
    # is allowed" and must not permanently block promotion.
    "human_time_metadata",       # human-time estimates are optional
    "partial_score_support",     # binary-only grading is a capability note
}


def _blocking_warns(report: AuditReport) -> list[str]:
    return [d.name for d in report.dimensions
            if d.verdict == "WARN"
            and d.name not in _NON_BLOCKING_WARN_DIMENSIONS]


def _rollup(report: AuditReport, stability_runs: int) -> str:
    if report.has_fail:
        hard_fail_dimensions = {
            d.name for d in report.dimensions if d.verdict == "FAIL"
        }
        metadata_only_fails = hard_fail_dimensions <= {"provenance_metadata"}
        return QUALITY_NEEDS_REVIEW if metadata_only_fails else QUALITY_INVALID
    blocking = _blocking_warns(report)
    if stability_runs:
        oracle = report.oracle or {}
        nop = report.nop or {}
        if oracle.get("runs_requested") and oracle.get("passes") != stability_runs:
            # Repeated-oracle instability is exactly what needs human eyes.
            return QUALITY_NEEDS_REVIEW
        if nop.get("runs_requested") and nop.get("passes", 0) > 0:
            return QUALITY_NEEDS_REVIEW
        if (oracle.get("passes") == stability_runs
                and not report.has_warn):
            # Perfect run AND no advisory warnings of any kind.
            return QUALITY_RELEASE
        if (oracle.get("passes") == stability_runs and nop.get("runs_requested")
                and nop.get("passes") == 0 and not blocking):
            return QUALITY_RELEASE
    return QUALITY_PROVISIONAL
