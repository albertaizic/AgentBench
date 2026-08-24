"""Benchmark maintenance validation.

``validate`` answers: is this benchmark well-formed and solvable, WITHOUT
running an agent? Checks performed:

* manifest loads; fixture/repository exists; commit resolves;
* working tree starts clean at the pinned commit;
* public evaluators run against the baseline (expected broken when the
  benchmark declares ``expect_broken_baseline: true``);
* hidden evaluator sources resolve inside the benchmark directory;
* reference solution, when present, makes every evaluator pass on a fresh
  checkout — and is never touched during agent runs.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agentbench.loader import resolve_repository_path
from agentbench.models import BenchmarkSpec
from agentbench.process import run_shell_command


@dataclass
class ValidationReport:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, check: str, passed: bool, detail: str = "") -> None:
        self.checks.append((check, passed, detail))

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", "--no-pager", *args], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout


def _run_evals(spec: BenchmarkSpec, workspace: Path, benchmark_dir: Path) -> tuple[bool, str]:
    from agentbench.evaluation import run_evaluation, run_hidden_evaluation

    failures: list[str] = []
    for evaluation in spec.evaluations:
        outcome = run_evaluation(evaluation, workspace=workspace, timeout=spec.timeout_seconds)
        if not outcome.passed:
            failures.append(f"public/{evaluation.name} (exit {outcome.exit_code})")
    if spec.hidden_evaluations is not None:
        hidden_dir = (benchmark_dir / spec.hidden_evaluations.source).resolve()
        for evaluation in spec.hidden_evaluations.evaluations:
            outcome = run_hidden_evaluation(
                evaluation, workspace=workspace, hidden_dir=hidden_dir,
                timeout=spec.timeout_seconds,
            )
            if not outcome.passed:
                failures.append(f"hidden/{evaluation.name} (exit {outcome.exit_code})")
    return (not failures), "; ".join(failures) or "all evaluations pass"


def validate_benchmark(manifest_path: Path, *, work_root: Path | None = None) -> ValidationReport:
    import tempfile

    from agentbench.loader import load_benchmark
    from agentbench.workspace import create_workspace

    report = ValidationReport(name=manifest_path.parent.name)
    benchmark_dir = manifest_path.parent.resolve()

    try:
        spec = load_benchmark(manifest_path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a failed check
        report.add("manifest loads", False, str(exc))
        return report
    report.add("manifest loads", True, spec.name)

    # Repository/fixture presence.
    repository = resolve_repository_path(spec.repository, base_dir=benchmark_dir)
    repo_exists = Path(repository).exists()
    report.add("repository/fixture exists", repo_exists, repository)
    if not repo_exists:
        return report

    # Commit resolves.
    try:
        sha = subprocess.run(
            ["git", "-C", repository, "rev-parse", f"{spec.commit}^{{commit}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        report.add("commit resolves", True, sha[:12])
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        report.add("commit resolves", False, str(exc))
        return report

    # Fresh clone: clean tree, baseline behavior, reference fix.
    root = Path(work_root or tempfile.mkdtemp(prefix="agentbench-validate-"))
    root.mkdir(parents=True, exist_ok=True)
    try:
        with create_workspace(repository, sha, parent=root) as ws:
            dirty = _git(["status", "--porcelain"], ws.path)
            report.add("baseline tree starts clean", dirty == "", dirty[:200] or "clean")

            evals_pass, eval_detail = _run_evals(spec, ws.path, benchmark_dir)
            if spec.expect_broken_baseline:
                report.add(
                    "baseline is broken as declared",
                    not evals_pass,
                    eval_detail if not evals_pass else "evaluations unexpectedly pass",
                )
            else:
                report.add("baseline evaluations pass", evals_pass, eval_detail)

        if spec.reference_solution is not None:
            patch_path = (benchmark_dir / spec.reference_solution.patch).resolve()
            patch_ok = patch_path.is_file() and benchmark_dir in patch_path.parents
            report.add("reference patch resolves inside benchmark dir", patch_ok, str(patch_path))
            if patch_ok:
                with create_workspace(repository, sha, parent=root) as ws:
                    apply = subprocess.run(
                        ["git", "apply", str(patch_path)], cwd=ws.path,
                        capture_output=True, text=True,
                    )
                    if apply.returncode != 0:
                        report.add("reference patch applies", False, apply.stderr.strip())
                    else:
                        fixed_pass, fixed_detail = _run_evals(spec, ws.path, benchmark_dir)
                        report.add(
                            "reference fix passes all evaluators",
                            fixed_pass,
                            fixed_detail,
                        )
        else:
            # Patch-free grading (e.g. mutation-checked test-writing tasks)
            # declares no reference solution by design.
            report.add("reference solution present", True, "none declared (patch-free grading)")

    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    return report


def check_regeneration_determinism(manifest_path: Path) -> tuple[bool, str]:
    """Re-run the benchmark's fixture generator; the commit sha must not move.

    Generators are deterministic by contract: identical embedded contents,
    pinned authorship/dates → identical shas on every machine.
    """
    import subprocess as sp

    from agentbench.loader import load_benchmark

    benchmark_dir = manifest_path.parent.resolve()
    generator = benchmark_dir / "create_fixture.py"
    if not generator.is_file():
        return False, f"generator missing: {generator}"

    def head(repo: Path) -> str | None:
        result = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                        capture_output=True, text=True)
        return result.stdout.strip() or None

    try:
        before = head(benchmark_dir / "fixture")
    except Exception:  # noqa: BLE001 - unreadable fixture counts as a mismatch source
        before = None

    run = sp.run([sys.executable, str(generator)], capture_output=True, text=True)
    if run.returncode != 0:
        return False, f"generator failed: {(run.stderr or run.stdout).strip()[:200]}"

    fixture = benchmark_dir / "fixture"
    after = head(fixture)
    if after is None:
        return False, "fixture has no git repository after generation"

    try:
        spec = load_benchmark(manifest_path)
        pinned_matches = spec.commit.lower() == after.lower()
    except Exception as exc:  # noqa: BLE001
        return False, f"manifest unparsable after generation: {exc}"
    if not pinned_matches:
        return False, f"pinned commit {spec.commit[:12]} != generated {after[:12]}"
    if before is not None and before != after:
        return False, f"sha moved across regeneration ({before[:12]} → {after[:12]})"
    return True, f"deterministic at {after[:12]}"


def validate_corpus(*, extra_root: Path | None = None) -> list[ValidationReport]:
    """Validate every discovered benchmark, including regeneration checks."""
    from agentbench.discovery import discover

    reports: list[ValidationReport] = []
    for manifest in discover(extra_root):
        report = validate_benchmark(manifest)
        regen_ok, regen_detail = check_regeneration_determinism(manifest)
        report.add("fixture regeneration deterministic", regen_ok, regen_detail)
        reports.append(report)
    return reports
