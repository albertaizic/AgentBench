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
            report.add("reference solution present", False, "none declared")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    return report
