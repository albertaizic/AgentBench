"""Command line entry point.

Commands: ``run``, ``experiment``, ``history``, ``show``, ``compare``,
``reproduce``, ``export``, ``benchmark list/validate``, ``dashboard``,
``doctor``, ``cleanup``.

Exit codes for ``run``: 0 all evaluations passed · 1 at least one trial did
not pass · 130 interrupted · everything else is an AgentBench/setup error
(exit 2). Query commands exit 2 only on AgentBench-level errors (unknown run,
corrupted index), never because a benchmark failed.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from agentbench.aggregate import (
    aggregate_by_config,
    format_count,
    format_duration,
    format_percent,
)
from agentbench.adapters import UnknownAgentError, get_adapter
from agentbench.dashboard import make_dashboard
from agentbench.discovery import discover, find_manifest, select_benchmarks
from agentbench.experiments import (
    ExperimentError,
    experiment_id_for,
    load_manifest,
    new_manifest,
    plan_cells,
    save_manifest,
)
from agentbench.loader import LoaderError, load_benchmark, resolve_repository_path
from agentbench.models import BenchmarkSpec, ExecutionSpec
from agentbench.results import RunResult
from agentbench.runner import RunOutcome, run_benchmark
from agentbench.scheduler import CANCELLED, MAX_JOBS, Scheduler
from agentbench.storage import ResultIndex, default_db_path
from agentbench.taxonomy import AGENT_TIMEOUT
from agentbench.workspace import WorkspaceError

DEFAULT_RESULTS_DIR = "results"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130
REPEAT_MIN = 1
REPEAT_MAX = 100

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Reproducible evaluation framework for coding agents.",
)
console = Console()


def _load_spec_or_exit(benchmark: Path) -> BenchmarkSpec:
    try:
        return load_benchmark(benchmark)
    except (LoaderError, ValidationError) as exc:
        console.print(f"[red]Invalid benchmark file ({benchmark}):[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc


def _results_root(spec: BenchmarkSpec, override: Path | None) -> Path:
    return Path(override) if override is not None else Path(spec.results_dir)


def _index_outcome(results_root: Path, outcome: RunOutcome) -> None:
    """Best-effort indexing: DB problems never destroy JSON evidence."""
    try:
        index = ResultIndex(default_db_path(results_root))
        index.index_result(outcome.result.model_dump(mode="json"), result_dir=outcome.run_dir)
    except (sqlite3.DatabaseError, OSError) as exc:
        console.print(f"[yellow]Warning: could not update result index:[/] {exc}")


def _open_index(results_dir: str) -> ResultIndex:
    try:
        index = ResultIndex(default_db_path(Path(results_dir)))
        _indexed, skipped = index.scan_results(Path(results_dir))
        if skipped:
            console.print(
                f"[yellow]Warning: {skipped} result.json file(s) were unreadable"
                " and were skipped from the index; the files themselves are"
                " untouched.[/]"
            )
        return index
    except (sqlite3.DatabaseError, OSError) as exc:
        console.print(f"[red]Result index unusable ({exc}); JSON evidence is unaffected.[/]")
        raise typer.Exit(code=EXIT_ERROR) from exc


def _print_single_run_summary(spec: BenchmarkSpec, result: RunResult, run_dir: Path) -> None:
    agent = result.agent
    diff = result.diff
    overall = result.overall

    console.print()
    console.print(f"[bold]Benchmark:[/] {spec.name}   [bold]Run:[/] {result.run_id}")
    console.print(
        f"[bold]Repository:[/] {result.benchmark['repository']}"
        f" @ {str(result.benchmark['resolved_commit'])[:12]}"
    )
    console.print(
        f"[bold]Agent ({agent['type']}):[/] exit {agent['exit_code']}"
        f" in {agent['duration_seconds']}s"
        + (" [yellow](timed out)[/]" if agent["timed_out"] else "")
    )
    usage = result.usage
    if usage and usage.get("total_tokens") is not None:
        cost = f", ${usage['cost_usd']:.4f}" if usage.get("cost_usd") is not None else ""
        console.print(
            f"[bold]Usage:[/] {usage['input_tokens']} in / {usage['output_tokens']} out"
            f" ({usage['total_tokens']} total{cost})"
        )
    console.print(
        f"[bold]Diff:[/] {diff['files_changed']} file(s) changed,"
        f" +{diff['insertions']}/-{diff['deletions']}"
    )

    table = Table(title="Evaluations", show_lines=False, expand=False)
    table.add_column("Kind", style="dim")
    table.add_column("Evaluation", style="bold")
    table.add_column("Exit")
    table.add_column("Duration")
    table.add_column("Result", justify="right")

    for kind, evaluations in (("public", result.evaluations), ("hidden", result.hidden_evaluations)):
        for evaluation in evaluations:
            passed: bool = evaluation["passed"]
            table.add_row(
                kind,
                evaluation["name"],
                str(evaluation["exit_code"]),
                format_duration(evaluation["duration_seconds"]),
                "[green]PASS[/]" if passed else "[red]FAIL[/]",
            )
    console.print(table)

    status = overall["status"]
    if status == "passed":
        console.print("[bold green]Overall: PASSED[/]")
    else:
        reason = overall.get("failure_reason")
        stage = overall.get("failure_stage")
        suffix = f" — {reason}" if reason else ""
        if stage:
            suffix += f" [dim](stage: {stage})[/]"
        console.print(f"[bold red]Overall: {status.upper()}{suffix}[/]")
    protected = result.protected_paths
    if protected and protected.get("violations"):
        console.print("[bold red]Protected path violations:[/]")
        for violation in protected["violations"]:
            console.print(f"  [red]{violation['path']}[/] matched {violation['pattern']}")
    console.print(f"Results saved to: [underline]{run_dir}[/]")


def _outcome_row(outcome: RunOutcome) -> dict:
    payload = outcome.result
    usage = payload.usage or {}
    return {
        "run_id": payload.run_id,
        "config_hash": payload.benchmark.get("config_hash"),
        "resolved_commit": payload.benchmark.get("resolved_commit"),
        "agent": payload.agent.get("type"),
        "model": payload.agent.get("model"),
        "status": payload.overall.get("status"),
        "duration_seconds": payload.overall.get("duration_seconds"),
        "files_changed": payload.diff.get("files_changed"),
        "insertions": payload.diff.get("insertions"),
        "deletions": payload.diff.get("deletions"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": usage.get("cost_usd"),
    }


def _print_repeat_summary(outcomes: list[RunOutcome]) -> None:
    rows = [_outcome_row(outcome) for outcome in outcomes]
    groups = aggregate_by_config(rows)

    table = Table(title=f"Repeat summary ({len(rows)} trials)", show_lines=False)
    table.add_column("Run ID", style="bold")
    table.add_column("Trial")
    table.add_column("Status", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Lines", justify="right")
    table.add_column("Tokens", justify="right")
    for outcome in outcomes:
        payload = outcome.result
        usage = payload.usage or {}
        diff = payload.diff
        table.add_row(
            payload.run_id,
            str(payload.trial),
            status_markup(payload.overall.get("status")),
            format_duration(payload.overall.get("duration_seconds")),
            str(diff.get("files_changed")),
            format_count(
                (diff.get("insertions") or 0) + (diff.get("deletions") or 0)
            ),
            format_count(usage.get("total_tokens")),
        )
    console.print(table)

    for group in groups:
        heterogeneity = ""
        if len(group.resolved_commits) > 1:
            heterogeneity = " [yellow](mixed commits!)[/]"
        console.print(
            f"{group.label}{heterogeneity}: {group.passes}/{group.runs} passed"
            f" ({format_percent(group.pass_rate)}),"
            f" median time {format_duration(group.median_duration)},"
            f" median tokens {format_count(group.median_total_tokens)}"
        )


def status_markup(status: str | None) -> str:
    if status == "passed":
        return "[green]PASS[/]"
    if status == AGENT_TIMEOUT:
        return "[yellow]TIMEOUT[/]"
    return f"[red]{(status or '?').upper()}[/]"


def _print_baseline_summary(result: RunResult, run_dir: Path) -> None:
    """Summary for reference-baseline runs (never styled as an AI agent)."""
    overall = result.overall
    console.print()
    console.print(
        f"[bold]Reference-patch baseline[/] (maintenance check, not an agent)"
        f" — Benchmark: [bold]{result.benchmark['name']}[/]   Run: {result.run_id}"
    )
    console.print(
        f"Resolved commit: {str(result.benchmark.get('resolved_commit'))[:12]}"
        f"   Patch applied via git apply"
    )
    table = Table(title="Evaluations")
    for column in ("Kind", "Evaluation", "Exit", "Duration", "Result"):
        table.add_column(column)
    for kind, evaluations in (
        ("public", result.evaluations),
        ("hidden", result.hidden_evaluations),
    ):
        for evaluation in evaluations:
            passed: bool = evaluation["passed"]
            table.add_row(
                kind,
                evaluation["name"],
                str(evaluation["exit_code"]),
                format_duration(evaluation["duration_seconds"]),
                "[green]PASS[/]" if passed else "[red]FAIL[/]",
            )
    console.print(table)
    status = overall["status"]
    if status == "passed":
        console.print("[bold green]Overall: PASSED — benchmark is solvable.[/]")
    else:
        reason = overall.get("failure_reason") or ""
        console.print(f"[bold red]Overall: {status.upper()} — {reason}[/]")
    console.print(f"Results saved to: [underline]{run_dir}[/]")


@app.callback()
def _root() -> None:
    """AgentBench: reproducible evaluation framework for coding agents."""


@app.command()
def run(
    benchmark: Path = typer.Argument(..., exists=True, readable=True, help="Path to the benchmark YAML file."),
    repeat: int = typer.Option(1, "--repeat", min=REPEAT_MIN, max=REPEAT_MAX, help="Independent trials (fresh workspace each)."),
    results_dir: Path | None = typer.Option(None, "--results-dir", help="Directory for run results."),
    keep_workspace: bool = typer.Option(False, "--keep-workspace", help="Keep the temporary workspace for debugging."),
    timeout_seconds: float | None = typer.Option(None, "--timeout-seconds", min=0.1, help="Override the per-step timeout."),
    backend: str | None = typer.Option(None, "--backend", help="Execution backend override: host or docker."),
    baseline: str | None = typer.Option(None, "--baseline", help="Deterministic baseline instead of an agent: 'reference' applies the benchmark's reference patch (maintenance-only)."),
) -> None:
    """Parse BENCHMARK, run the configured agent, evaluate, persist, report."""
    spec = _load_spec_or_exit(benchmark)

    if baseline is not None:
        if baseline != "reference":
            console.print(f"[red]Unknown baseline:[/] {baseline} (supported: reference)")
            raise typer.Exit(code=EXIT_ERROR)
        from agentbench.baselines import BaselineError, run_reference_baseline

        repository_ref = resolve_repository_path(spec.repository, base_dir=benchmark.parent)
        root = _results_root(spec, results_dir)
        try:
            result, run_dir = run_reference_baseline(
                spec,
                repository=repository_ref,
                benchmark_dir=benchmark.parent.resolve(),
                manifest_path=benchmark.resolve(),
                results_root=root,
                timeout_seconds=timeout_seconds,
            )
        except BaselineError as exc:
            console.print(f"[red]Reference baseline failed:[/] {exc}")
            raise typer.Exit(code=EXIT_ERROR) from exc
        except WorkspaceError as exc:
            console.print(f"[red]Reference baseline failed:[/] {exc}")
            raise typer.Exit(code=EXIT_ERROR) from exc
        _index_outcome(root, RunOutcome(result=result, run_dir=run_dir, workspace_path=None))
        _print_baseline_summary(result, run_dir)
        status = result.overall["status"]
        raise typer.Exit(code=EXIT_PASS if status == "passed" else EXIT_FAIL)
    repository = resolve_repository_path(spec.repository, base_dir=benchmark.parent)
    execution = ExecutionSpec(backend=backend) if backend else None

    try:
        adapter = get_adapter(spec.agent.type)
    except UnknownAgentError as exc:
        console.print(f"[red]Run failed:[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    root = _results_root(spec, results_dir)
    outcomes: list[RunOutcome] = []
    interrupted = False
    setup_failed = False
    try:
        for trial in range(1, repeat + 1):
            if repeat > 1:
                console.print(f"[bold]Trial {trial}/{repeat}[/] starting…")
            outcome = run_benchmark(
                spec,
                adapter=adapter,
                results_root=root,
                keep_workspace=keep_workspace,
                timeout_seconds=timeout_seconds,
                trial=trial if repeat > 1 else None,
                repository=repository,
                benchmark_dir=benchmark.parent,
                manifest_path=benchmark.resolve(),
                execution=execution,
            )
            _index_outcome(root, outcome)
            if outcome.result.overall.get("status") == "setup_failed":
                # The environment is broken; remaining trials would fail
                # identically. Evidence is already persisted.
                setup_failed = True
                _print_single_run_summary(spec, outcome.result, outcome.run_dir)
                break
            if repeat > 1:
                console.print(
                    f"Trial {trial}/{repeat}  {status_markup(outcome.result.overall.get('status'))}"
                    f"  ({format_duration(outcome.result.overall.get('duration_seconds'))})"
                )
            else:
                _print_single_run_summary(spec, outcome.result, outcome.run_dir)
            outcomes.append(outcome)
    except KeyboardInterrupt:
        interrupted = True
        console.print(
            f"\n[yellow]Interrupted — {len(outcomes)} completed trial(s) preserved.[/]"
        )
    except (UnknownAgentError, WorkspaceError, RuntimeError, OSError) as exc:
        # Last-resort guard: setup failures normally persist as evidence and
        # return as outcomes; anything still raising here is AgentBench-level.
        console.print(f"[red]Run failed:[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    if setup_failed:
        raise typer.Exit(code=EXIT_ERROR)

    if repeat > 1 and outcomes:
        _print_repeat_summary(outcomes)

    if interrupted:
        raise typer.Exit(code=EXIT_INTERRUPTED)
    all_passed = bool(outcomes) and all(
        o.result.overall.get("status") == "passed" for o in outcomes
    )
    raise typer.Exit(code=EXIT_PASS if all_passed else EXIT_FAIL)


@app.command()
def history(
    benchmark: str | None = typer.Option(None, "--benchmark"),
    agent: str | None = typer.Option(None, "--agent"),
    model: str | None = typer.Option(None, "--model"),
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """List persisted runs, newest first."""
    index = _open_index(results_dir)
    rows = index.query(
        benchmark=benchmark, agent=agent, model=model, status=status, limit=limit
    )

    table = Table(title="Run history")
    for column in ("RUN ID", "BENCHMARK", "AGENT", "MODEL", "STATUS", "DURATION", "TIME"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["run_id"],
            row["benchmark"],
            row["agent"],
            row["model"] or "—",
            status_markup(row["status"]),
            format_duration(row["duration_seconds"]),
            (row["created_at"] or "")[:19],
        )
    console.print(table)
    console.print(f"{len(rows)} run(s)")


@app.command()
def show(
    run_id: str = typer.Argument(..., help="A run id as printed by run/history."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Show one run's evidence: config, evaluations, diff stats, artifact paths."""
    index = _open_index(results_dir)
    row = index.get_run(run_id)
    if row is None:
        console.print(f"[red]Unknown run id:[/] {run_id}")
        raise typer.Exit(code=EXIT_ERROR)

    result_file = Path(row["result_dir"]) / "result.json"
    try:
        payload: dict = RunResult.model_validate_json(result_file.read_text(encoding="utf-8")).model_dump()
    except (OSError, ValidationError):
        payload = {}

    benchmark = payload.get("benchmark") or row
    agent = payload.get("agent") or {}
    diff = payload.get("diff") or {}
    overall = payload.get("overall") or {}
    usage = payload.get("usage") or {}
    environment = payload.get("environment") or {}

    def section(title: str, pairs: list[tuple[str, object]]) -> None:
        console.print(f"[bold]{title}[/]")
        for name, value in pairs:
            console.print(f"  {name}: {value if value is not None else '—'}")

    section("Benchmark", [
        ("Name", benchmark.get("name")),
        ("Repository", benchmark.get("repository")),
        ("Requested commit", benchmark.get("commit")),
        ("Resolved commit", benchmark.get("resolved_commit")),
        ("Config hash", benchmark.get("config_hash")),
        ("Trial", payload.get("trial")),
    ])
    section("Agent", [
        ("Type", agent.get("type") or row.get("agent")),
        ("Model", agent.get("model")),
        ("Exit code", agent.get("exit_code")),
        ("Timed out", agent.get("timed_out")),
        ("Duration", format_duration(agent.get("duration_seconds"))),
    ])
    section("Outcome", [
        ("Status", overall.get("status") or row.get("status")),
        ("Failure reason", overall.get("failure_reason")),
        ("Failure stage", overall.get("failure_stage")),
        ("Started", overall.get("started_at")),
        ("Duration", format_duration(overall.get("duration_seconds"))),
    ])

    stage_timings = payload.get("stage_timings")
    if stage_timings:
        console.print("[bold]Stage timings[/]")
        for name, seconds in sorted(stage_timings.items()):
            console.print(f"  {name}: {format_duration(seconds)}")

    for title, evaluations in (
        ("Evaluations", payload.get("evaluations")),
        ("Hidden evaluations", payload.get("hidden_evaluations")),
    ):
        if evaluations:
            console.print(f"[bold]{title}[/]")
            for evaluation in evaluations:
                marker = "[green]PASS[/]" if evaluation.get("passed") else "[red]FAIL[/]"
                console.print(
                    f"  {marker} {evaluation['name']}"
                    f" (exit {evaluation.get('exit_code')},"
                    f" {format_duration(evaluation.get('duration_seconds'))})"
                )

    section("Diff statistics", [
        ("Files changed", diff.get("files_changed")),
        ("Insertions", diff.get("insertions")),
        ("Deletions", diff.get("deletions")),
        ("Changed paths", ", ".join(diff.get("changed_paths") or []) or "—"),
    ])

    protected = payload.get("protected_paths")
    if protected:
        section("Protected paths", [
            ("Patterns", ", ".join(protected.get("patterns") or [])),
            ("Fail on violation", protected.get("fail_on_violation")),
            ("Violations", len(protected.get("violations") or [])),
        ])

    if usage:
        section("Usage", [
            ("Input tokens", usage.get("input_tokens")),
            ("Output tokens", usage.get("output_tokens")),
            ("Total tokens", usage.get("total_tokens")),
            ("Cost (USD)", usage.get("cost_usd")),
            ("Turns", usage.get("num_turns")),
            ("Session", usage.get("session_id")),
        ])

    section("Environment", [(k, environment.get(k)) for k in (
        "agentbench_version", "python_version", "platform", "git_version", "agent_cli_version",
    )])

    console.print("[bold]Artifacts[/]")
    if row.get("result_dir"):
        result_dir = Path(row["result_dir"])
        for name in ["result.json", "diff.patch", "agent.stdout.log", "agent.stderr.log"]:
            console.print(f"  {result_dir / name}")
        for log_file in sorted((result_dir / 'evals').glob('*')):
            console.print(f"  {log_file}")


@app.command()
def compare(
    benchmark: str = typer.Argument(..., help="Benchmark name to aggregate."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Aggregate completed runs of a benchmark by configuration."""
    index = _open_index(results_dir)
    rows = index.query(benchmark=benchmark, limit=None)
    if not rows:
        console.print(f"No runs recorded for benchmark '[bold]{benchmark}[/]'.")
        return

    distinct_commits = {row.get("resolved_commit") for row in rows}
    if len(distinct_commits) > 1:
        console.print(
            f"[yellow]Warning:[/] runs span {len(distinct_commits)} different resolved"
            " commits — groups below evaluated materially different code."
        )

    table = Table(title=f"Comparison: {benchmark}")
    for column in ("CONFIG", "RUNS", "PASS RATE", "MEDIAN TIME", "MEDIAN FILES",
                   "MEDIAN LINES", "MEDIAN TOKENS", "AVG COST"):
        table.add_column(column, justify="right" if column != "CONFIG" else "left")
    for group in aggregate_by_config(rows):
        if len(group.resolved_commits) > 1:
            console.print(
                f"[yellow]Warning:[/] {group.label} spans {len(group.resolved_commits)}"
                " different resolved commits — these runs evaluated different code."
            )
        table.add_row(
            group.label,
            str(group.runs),
            format_percent(group.pass_rate),
            format_duration(group.median_duration),
            format_count(group.median_files_changed),
            format_count(group.median_lines_changed),
            format_count(group.median_total_tokens),
            f"${statistics.fmean(group.costs):.4f}" if group.costs else "—",
        )
    console.print(table)


@app.command()
def dashboard(
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (localhost by default)."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Serve a read-only local web UI over stored results."""
    server = make_dashboard(Path(results_dir), port=port, host=host)
    url = f"http://{host}:{server.server_address[1]}"
    console.print("[bold]AgentBench dashboard[/]")
    console.print(url)
    console.print("[dim]Ctrl+C to stop.[/]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("Dashboard stopped.")
    finally:
        server.server_close()


benchmark_app = typer.Typer(help="Inspect the benchmark corpus.")
app.add_typer(benchmark_app, name="benchmark")


@benchmark_app.command("list")
def benchmark_list(
    suite: str | None = typer.Option(None, "--suite", help="Only benchmarks in this suite."),
) -> None:
    """List benchmarks discovered in the corpus and ./benchmarks."""
    table = Table(title="Benchmark corpus" + (f" [suite={suite}]" if suite else ""))
    for column in ("NAME", "CATEGORY", "LANGUAGE", "SUITES", "DIFFICULTY"):
        table.add_column(column)
    count = 0
    for manifest in discover():
        try:
            spec = load_benchmark(manifest)
        except (LoaderError, ValidationError) as exc:
            console.print(f"[red]{manifest.parent.name}: invalid manifest[/] {exc}")
            continue
        if suite is not None and suite not in spec.suites:
            continue
        table.add_row(
            spec.name,
            spec.category or "—",
            spec.language or "—",
            ", ".join(spec.suites) or "—",
            spec.difficulty or "—",
        )
        count += 1
    console.print(table)
    console.print(f"{count} benchmark(s)")


@benchmark_app.command("validate")
def benchmark_validate(
    benchmark: str | None = typer.Argument(None, help="Corpus name or path to a benchmark.yaml."),
    all_benchmarks: bool = typer.Option(False, "--all", help="Validate the entire corpus."),
    extra_root: Path | None = typer.Option(None, "--path", help="Additional discovery root."),
) -> None:
    """Validate solvability/structure without running an agent; --all sweeps the corpus."""
    from agentbench.validation import validate_benchmark

    if all_benchmarks:
        from agentbench.validation import validate_corpus

        reports = validate_corpus(extra_root=extra_root)
        if not reports:
            console.print("[yellow]No benchmarks discovered.[/]")
            raise typer.Exit(code=EXIT_ERROR)

        failed = [report for report in reports if not report.ok]
        total = len(reports)

        summary_table = Table(title=f"Corpus validation ({total} benchmarks)")
        summary_table.add_column("BENCHMARK")
        summary_table.add_column("RESULT", justify="right")
        for report in reports:
            summary_table.add_row(
                report.name,
                "[green]PASS[/]" if report.ok else "[red]FAIL[/]",
            )
        console.print(summary_table)

        # Spec-style rollup over the check families that matter.
        def count(check_name: str) -> str:
            matching = [
                any(name == check_name and ok for name, ok, _ in report.checks)
                for report in reports
            ]
            return f"{sum(matching)}/{total}"

        def baseline_ok(report) -> bool:
            for name, ok, _ in report.checks:
                if name == "baseline is broken as declared":
                    if not ok:
                        return False
                elif name == "baseline evaluations pass" and not ok:
                    return False
            return True

        patch_free = sum(
            1 for r in reports
            if any(n == "reference solution present" and ok for n, ok, _ in r.checks)
        )
        console.print(f"{count('manifest loads')} manifests load")
        console.print(f"{count('commit resolves')} commits resolve")
        console.print(f"{sum(1 for r in reports if baseline_ok(r))}/{total} baselines match declared state")
        console.print(
            f"{count('reference fix passes all evaluators')} reference fixes pass"
            f" ({patch_free} patch-free)"
        )
        console.print(f"{count('fixture regeneration deterministic')} fixture regenerations deterministic")

        for report in failed:
            console.print(f"[bold red]{report.name}: failures[/]")
            for check_name, ok, detail in report.checks:
                if not ok:
                    console.print(f"  [red]x[/] {check_name}: {detail}")

        if failed:
            raise typer.Exit(code=EXIT_FAIL)
        console.print("[green]All corpus benchmarks are valid.[/]")
        return

    if benchmark is None:
        console.print("[red]Provide a benchmark name/path or pass --all.[/]")
        raise typer.Exit(code=EXIT_ERROR)
    try:
        manifest = find_manifest(benchmark, extra_root)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=EXIT_ERROR) from exc

    report = validate_benchmark(manifest)

    table = Table(title=f"Validation: {report.name}")
    table.add_column("Check")
    table.add_column("Result", justify="right")
    table.add_column("Detail")
    for check, passed, detail in report.checks:
        marker = "[green]PASS[/]" if passed else "[red]FAIL[/]"
        table.add_row(check, marker, detail)
    console.print(table)

    if not report.ok:
        raise typer.Exit(code=EXIT_FAIL)


@benchmark_app.command("init")
def benchmark_init(
    name: str = typer.Argument(..., help="New benchmark name (lowercase)."),
    language: str = typer.Option("python", "--language"),
    suite: str = typer.Option("provisional", "--suite", help="Suite tag for the new task."),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Scaffold a new benchmark with the full v0.6 metadata skeleton.

    Creates the directory layout and a manifest containing every quality
    field (requirements map, provenance, human-time placeholder) as TODO
    markers an author fills in. Nothing here runs agents.
    """
    import re as _re

    if not _re.fullmatch(r"[a-z][a-z0-9-]{2,30}", name):
        console.print("[red]Name must be lowercase letters/digits/hyphens (3-31 chars).[/]")
        raise typer.Exit(code=EXIT_ERROR)
    bench_dir = Path("benchmarks") / name
    if bench_dir.exists() and not force:
        console.print(f"[red]benchmarks/{name} already exists (use --force to overwrite).[/]")
        raise typer.Exit(code=EXIT_ERROR)

    today = "1970-01-01"  # author replaces with the actual authoring date
    (bench_dir / "hidden").mkdir(parents=True, exist_ok=True)
    (bench_dir / "reference").mkdir(parents=True, exist_ok=True)
    if language == "typescript":
        eval_cmd = "node run_tests.mjs"
        fixture_files = "// TODO: fixture sources\n"
        fixture_main = "index.js"
    else:
        eval_cmd = '"{python}" -m pytest -q'
        fixture_files = "# TODO: fixture sources\n"
        fixture_main = "package_stub.py"

    (bench_dir / "benchmark.yaml").write_text(f"""name: {name}
repository: fixture
commit: TODO_RUN_create_fixture_py_AND_PASTE_SHA
description: |
  TODO: one paragraph on the seeded defect(s).
prompt: |
  TODO: realistic maintainer task. Observable behavior only.
agent:
  type: claude-code
evaluations:
  - name: smoke
    command: '{eval_cmd}'
scoring_groups:
  core_behavior: {{weight: 0.5, required: true}}
  edge_cases: {{weight: 0.2, required: false}}
hidden_evaluations:
  source: hidden
  evaluations:
    - name: contract
      command: '{eval_cmd}'
protected_paths: [tests/**]
fail_on_protected_path_violation: true
category: TODO
tags: []
suites: [{suite}]
language: {language}
difficulty: medium
expect_broken_baseline: true
reference_solution:
  patch: reference/fix.patch
timeout_seconds: 900
prompt_requirements:
  - {{id: req-1, text: TODO}}
requirement_mappings:
  - {{requirement: req-1, scored_by: [core_behavior]}}
source_kind: authored
task_created_at: "{today}"
contamination_risk: low
platforms: [any]
instruction_style: explicit-task
quality_status: provisional
human_time:
  expert_time_estimate_minutes: null   # fill when justified; method required too
""", encoding="utf-8", newline="\n")
    (bench_dir / "create_fixture.py").write_text(
        f'"""Deterministic fixture generator for {name}. Prints head sha."""\n'
        '# TODO: build the fixture repository exactly like benchmarks/csvroll/create_fixture.py\n'
        f'repo_files = {{"{fixture_main}": {fixture_files!r}}}\n',
        encoding="utf-8", newline="\n")
    (bench_dir / "reference" / "NOTES.md").write_text(
        f"# {name} — maintainer notes\n\nTODO: defect mechanism, why it discriminates.\n",
        encoding="utf-8", newline="\n")
    console.print(
        f"[green]Scaffolded[/] benchmarks/{name}: manifest, hidden/, reference/.\n"
        "Next: implement create_fixture.py, paste the sha into the manifest,\n"
        "write evaluators + reference patch, then run "
        "`agentbench benchmark validate {name}`.".replace("{name}", name)
    )
    console.print("[green]Benchmark is valid.[/]")



def _strip_generation_stamp(markdown_text: str) -> str:
    """Drop the wall-clock generation line so verify compares content, not time."""
    return "\n".join(
        line for line in markdown_text.splitlines()
        if not line.startswith("- Generated:")
    ) + "\n"


@app.command()
def study(
    action: str = typer.Argument(..., help="Only 'verify' is supported."),
    study_dir: Path = typer.Argument(..., help="Directory containing report.md + experiment.json."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir",
                                    help="Evidence root used to recompute the report."),
) -> None:
    """Verify derived study artifacts against primary evidence (P37/P38).

    Recomputes report.md from result evidence and checks SHA-256 hashes in
    hashes.json. A public report must never depend on hand-typed numbers.
    """
    if action != "verify":
        console.print("[red]Unknown study action:[/] use 'verify'.")
        raise typer.Exit(code=EXIT_ERROR)

    import hashlib

    manifest_path = study_dir / "experiment.json"
    report_path = study_dir / "report.md"
    for required in (manifest_path, report_path):
        if not required.exists():
            console.print(f"[red]Missing artifact:[/] {required}")
            raise typer.Exit(code=EXIT_ERROR)

    from agentbench.experiments import load_manifest
    from agentbench.reporting import build_study, render_markdown

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Manifest unreadable:[/] {exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    rows = index_rows_safe(results_dir, manifest.experiment_id)
    recomputed = _strip_generation_stamp(
        render_markdown(build_study(manifest, rows)))
    stored = _strip_generation_stamp(report_path.read_text(encoding="utf-8"))

    ok = True
    if recomputed == stored:
        console.print("[green]report.md matches recomputation from evidence.[/]")
    else:
        ok = False
        console.print("[yellow]report.md differs from recomputation — either "
                      "evidence moved or the report was hand-edited.[/]")

    hashes_path = study_dir / "hashes.json"
    if hashes_path.exists():
        try:
            recorded = json.loads(hashes_path.read_text(encoding="utf-8"))
            if not isinstance(recorded, dict):
                raise ValueError("hashes.json must be a JSON object")
        except ValueError as exc:
            ok = False
            console.print(f"[red]Malformed hashes.json:[/] {exc}")
        else:
            mismatches = []
            for fname, expected in sorted(recorded.items()):
                f = study_dir / fname
                if not f.exists():
                    mismatches.append(f"{fname} missing")
                    continue
                actual = hashlib.sha256(f.read_bytes()).hexdigest()
                if actual != expected:
                    mismatches.append(fname)
            # Policy: extra unmanifested files are ALLOWED (bundles may be
            # extended locally) but never trusted — they are outside the
            # integrity manifest, so they are surfaced as a warning.
            extras = sorted(
                p.name for p in study_dir.iterdir()
                if p.is_file() and p.name != "hashes.json"
                and p.name not in recorded
            )
            if mismatches:
                ok = False
                console.print(f"[red]Hash mismatches:[/] {', '.join(mismatches)}")
            else:
                console.print(f"[green]All {len(recorded)} bundle hashes verified.[/]")
            if extras:
                console.print(f"[yellow]Unmanifested files (not integrity-checked):[/] "
                              f"{', '.join(extras)}")
    else:
        console.print("[yellow]No hashes.json present — integrity unverified.[/]")

    raise typer.Exit(code=0 if ok else EXIT_FAIL)

@benchmark_app.command("audit")
def benchmark_audit(
    benchmark: str | None = typer.Argument(None, help="Corpus name or manifest path."),
    all_benchmarks: bool = typer.Option(False, "--all"),
    oracle_runs: int = typer.Option(1, "--oracle-runs", min=1, max=20,
                                    help="Repeated reference-solution executions."),
    nop_runs: int = typer.Option(1, "--nop-runs", min=1, max=20),
    skip_stability: bool = typer.Option(False, "--skip-stability"),
    as_json: bool = typer.Option(False, "--json"),
    build_report: bool = typer.Option(False, "--report", help="Quality report table + statuses."),
) -> None:
    """Task quality audit: stability, isolation, alignment, provenance.

    Extends validate with repeated-oracle / repeated-nop stability and
    metadata dimensions. Never invokes a coding agent. New or unaudited
    tasks should expect provisional until real calibration.
    """
    from agentbench.audit import audit_benchmark

    names: list[Path] = []
    if all_benchmarks:
        from agentbench.discovery import discover
        for manifest in discover():
            names.append(manifest)
    else:
        if benchmark is None:
            console.print("[red]Provide a benchmark name/path or --all.[/]")
            raise typer.Exit(code=EXIT_ERROR)
        try:
            names.append(find_manifest(benchmark, None))
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=EXIT_ERROR) from exc

    reports = []
    for manifest in names:
        reports.append(audit_benchmark(
            manifest,
            oracle_runs=oracle_runs if not skip_stability else 0,
            nop_runs=nop_runs if not skip_stability else 0,
            skip_stability=skip_stability,
        ))

    if as_json:
        payload = []
        for r in reports:
            payload.append({
                "benchmark": r.name,
                "quality_status": r.quality_status,
                "dimensions": [
                    {"name": d.name, "verdict": d.verdict, "detail": d.detail}
                    for d in r.dimensions
                ],
                "oracle": r.oracle, "nop": r.nop,
            })
        console.print(json.dumps(payload, indent=2))
        return

    if build_report:
        table = Table(title=f"Benchmark quality report ({len(reports)} tasks)")
        table.add_column("BENCHMARK")
        table.add_column("ORACLE")
        table.add_column("NOP")
        table.add_column("MAPPING")
        table.add_column("ISOLATION")
        table.add_column("PARTIAL")
        table.add_column("STATUS")
        for r in reports:
            def dim(name: str) -> str:
                for d in r.dimensions:
                    if name in d.name:
                        return {"PASS": "[green]ok[/]", "WARN": "[yellow]warn[/]",
                                "FAIL": "[red]FAIL[/]"}[d.verdict]
                return "—"
            oracle_s = f"{r.oracle.get('passes', '—')}/{r.oracle.get('runs_requested', '—')}"
            nop_s = f"{r.nop.get('fails', '—')}/{r.nop.get('runs_requested', '—')} fail"
            status_color = {"release-grade": "[green]", "provisional": "[cyan]",
                            "needs-review": "[yellow]", "invalid": "[red]"}.get(
                                r.quality_status, "")
            table.add_row(r.name, oracle_s, nop_s,
                          dim("requirement_mapping"), dim("isolation"),
                          dim("partial_score_support"),
                          f"{status_color}{r.quality_status}")
        console.print(table)
        counts: dict[str, int] = {}
        for r in reports:
            counts[r.quality_status] = counts.get(r.quality_status, 0) + 1
        console.print(" · ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        # P28 review queue: deterministic triage of flagged tasks.
        flagged = [
            r for r in reports
            if r.quality_status in ("needs-review", "invalid")
            or any(d.verdict == "WARN" for d in r.dimensions)
        ]
        if flagged:
            console.print("[bold]Review queue[/]")
            for r in flagged:
                reasons = "; ".join(
                    f"{d.name}: {d.detail[:80]}" for d in r.dimensions
                    if d.verdict in ("WARN", "FAIL"))
                console.print(f"  [yellow]{r.name}[/] ({r.quality_status}) — {reasons}")
        return

    failed = False
    for r in reports:
        table = Table(title=f"Audit: {r.name} → {r.quality_status}")
        table.add_column("Dimension")
        table.add_column("Verdict", justify="right")
        table.add_column("Detail")
        for d in r.dimensions:
            color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[d.verdict]
            table.add_row(d.name, f"[{color}]{d.verdict}[/]", d.detail)
        console.print(table)
        if r.has_fail:
            failed = True
    if failed:
        raise typer.Exit(code=EXIT_FAIL)


@benchmark_app.command("report")
def benchmark_report(
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
    min_samples: int = typer.Option(5, "--min-samples", min=1,
                                    help="Runs per config before a calibration flag is issued."),
) -> None:
    """Evidence-informed corpus view: pass rates, durations, calibration flags.

    Difficulty labels stay provisional until real runs accumulate; this
    report never rewrites them automatically.
    """
    from agentbench.loader import load_benchmark

    index = _open_index(results_dir)
    rows = index.query(limit=None)

    metadata: dict[str, dict] = {}
    for manifest in discover():
        try:
            spec_meta = load_benchmark(manifest)
        except (LoaderError, ValidationError):
            continue
        metadata[spec_meta.name] = {
            "category": spec_meta.category,
            "difficulty": spec_meta.difficulty,
        }

    by_benchmark: dict[str, list[dict]] = {}
    for row in rows:
        by_benchmark.setdefault(row["benchmark"], []).append(row)

    table = Table(title="Corpus report (evidence-informed)")
    for column in ("BENCHMARK", "CATEGORY", "DIFFICULTY", "RUNS", "PASS RATE",
                   "MEDIAN TIME", "MEDIAN TOKENS", "CALIBRATION"):
        table.add_column(column)

    def calibration(rate: float | None, n: int) -> str:
        if n < min_samples or rate is None:
            return "[dim]uncalibrated[/]"
        if rate >= 0.90:
            return "[yellow]too easy?[/]"
        if rate <= 0.10:
            return "[yellow]too hard?[/]"
        return "[green]calibrated[/]"

    for name in sorted(set(metadata) | set(by_benchmark)):
        bench_rows = by_benchmark.get(name, [])
        groups = aggregate_by_config(bench_rows)
        runs = sum(g.runs for g in groups)
        passes = sum(g.passes for g in groups)
        rate = (passes / runs) if runs else None
        durations = [d for g in groups for d in g.durations]
        tokens = [t for g in groups for t in g.total_tokens]
        median_time = format_duration(statistics.median(durations)) if durations else "—"
        median_tokens = format_count(statistics.median(tokens)) if tokens else "—"
        meta = metadata.get(name, {})
        table.add_row(
            name,
            meta.get("category") or "—",
            meta.get("difficulty") or "—",
            str(runs),
            format_percent(rate),
            median_time,
            median_tokens,
            calibration(rate, runs),
        )
    console.print(table)
    console.print(
        f"[dim]Calibration flags need ≥{min_samples} run(s) per benchmark;"
        " difficulty labels are provisional metadata and never rewritten automatically.[/]"
    )


@app.command()
def experiment(
    experiment_file: Path = typer.Argument(..., exists=True, readable=True, help="Experiment YAML."),
    resume: str | None = typer.Option(None, "--resume", help="Resume this experiment id."),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    timeout_seconds: float | None = typer.Option(None, "--timeout-seconds", min=0.1),
    jobs: int = typer.Option(1, "--jobs", min=1, max=MAX_JOBS,
                             help=f"Cells run concurrently (each gets an independent workspace); 1–{MAX_JOBS}."),
    max_runs: int | None = typer.Option(None, "--max-runs", min=1,
                                        help="Hard cap on cells executed this invocation; stop cleanly when reached (resumable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the resolved plan and exit; no workspaces or runs."),
) -> None:
    """Run a benchmark × config × repeat matrix; resumable via --resume.

    Benchmarks come either as explicit names or a metadata selector
    (suite/tags/category); the resolved list is persisted at creation so
    later corpus changes cannot silently alter the experiment.

    Exit code 0 means the matrix ran to completion — cell outcomes (including
    failures) are results, not errors. Exit 130 on interrupt; 2 on setup
    problems. ``--jobs N`` runs up to N cells at once; Ctrl+C stops scheduling
    new cells and interrupts in-flight ones; each interrupted cell is persisted
    as a failed run so evidence stays complete and resumable.
    """
    from agentbench.models import ExperimentSpec

    try:
        spec = ExperimentSpec.model_validate(
            yaml.safe_load(Path(experiment_file).read_text(encoding="utf-8"))
        )
    except (OSError, ValidationError) as exc:
        console.print(f"[red]Invalid experiment file ({experiment_file}):[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    results_dir = Path(spec.results_dir)

    # Metadata selectors resolve exactly once; a resumed experiment reuses its
    # stored benchmark list so later corpus changes cannot alter it.
    stored_benchmarks: list[str] | None = None
    if resume:
        try:
            existing = load_manifest(results_dir / "experiments" / resume / "experiment.json")
        except ExperimentError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=EXIT_ERROR) from exc
        stored_benchmarks = list(existing.resolved_benchmarks) or None

    try:
        benchmark_names = select_benchmarks(spec.benchmarks) \
            if not isinstance(spec.benchmarks, list) else list(spec.benchmarks)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=EXIT_ERROR) from exc
    if stored_benchmarks is not None:
        # Resume always runs the ORIGINAL resolved list. An explicit list in
        # the yaml must still match it; a selector is simply ignored in favor
        # of what was persisted at creation time.
        if isinstance(spec.benchmarks, list) and sorted(spec.benchmarks) != sorted(stored_benchmarks):
            console.print(
                "[red]Cannot resume:[/] experiment's benchmarks were"
                f" {sorted(stored_benchmarks)}; the file now lists {sorted(spec.benchmarks)}."
            )
            raise typer.Exit(code=EXIT_ERROR)
        benchmark_names = stored_benchmarks

    total_cells = len(benchmark_names) * len(spec.configs) * spec.repeat

    manifests: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    specs: dict[str, BenchmarkSpec] = {}
    repositories: dict[str, str] = {}
    unavailable_adapters: list[str] = []
    for name in benchmark_names:
        try:
            manifest_path = find_manifest(name)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=EXIT_ERROR) from exc
        bench_spec = _load_spec_or_exit(manifest_path)
        manifests[name] = manifest_path
        specs[name] = bench_spec
        hashes[name] = bench_spec.config_hash()
        repositories[name] = resolve_repository_path(
            bench_spec.repository, base_dir=manifest_path.parent
        )
    for config in spec.configs:
        try:
            get_adapter(config.agent.type)
        except UnknownAgentError as exc:
            unavailable_adapters.append(f"{config.name}: {exc}")

    plans = plan_cells(spec, manifests, hashes, benchmarks=benchmark_names)

    # --dry-run: pure plan inspection. No manifest, no directories, no runs.
    if dry_run:
        console.print(f"[bold]Dry run[/] — nothing will be executed.")
        console.print(f"Experiment: {spec.name}")
        console.print(
            f"Benchmarks ({len(benchmark_names)}): {', '.join(benchmark_names)}"
        )
        for config in spec.configs:
            eff_execution = (
                spec.execution.merged_with(config.execution)
                if (spec.execution or config.execution)
                else None
            )
            backend_name = eff_execution.backend if eff_execution else (
                specs[benchmark_names[0]].execution.backend
                if specs[benchmark_names[0]].execution else "host"
            )
            limits = ", ".join(
                filter(None, [
                    f"memory={eff_execution.memory}" if eff_execution and eff_execution.memory else "",
                    f"cpus={eff_execution.cpus}" if eff_execution and eff_execution.cpus else "",
                    f"pids={eff_execution.pids_limit}" if eff_execution and eff_execution.pids_limit else "",
                ])
            ) or "unlimited"
            console.print(
                f"Config {config.name}: agent={config.agent.type}"
                + (f"/{config.agent.model}" if config.agent.model else "")
                + f", backend={backend_name}, resources={limits}"
            )
        console.print(
            f"Repeats: {spec.repeat}   Total cells: {total_cells}   "
            f"Max parallelism: {min(jobs, total_cells) if total_cells else 0}"
        )
        for note in unavailable_adapters:
            console.print(f"[yellow]Unavailable adapter:[/] {note}")
        if any(
            ((spec.execution.merged_with(c.execution) if (spec.execution or c.execution) else None)
             or ExecutionSpec()).backend == "docker"
            for c in spec.configs
        ):
            from agentbench.backends.docker import docker_available
            if not docker_available():
                console.print("[yellow]Docker daemon unreachable — docker configs would fail.[/]")
        return

    if resume:
        manifest = existing
        for name, stored_hash in manifest.benchmark_identities.items():
            if hashes.get(name) != stored_hash:
                console.print(
                    f"[red]Cannot resume:[/] benchmark '{name}' changed since the original"
                    f" run (config hash {stored_hash} -> {hashes.get(name)})."
                )
                raise typer.Exit(code=EXIT_ERROR)
        for config in spec.configs:
            stored = manifest.config_identities.get(config.name)
            if stored != config.config_hash():
                console.print(
                    f"[red]Cannot resume:[/] config '{config.name}' changed since the"
                    " original run."
                )
                raise typer.Exit(code=EXIT_ERROR)
        manifest.interrupted = False
        experiment_id = resume
    else:
        experiment_id = experiment_id_for(spec.name)
        manifest = new_manifest(spec, experiment_id, results_dir, resolved_benchmarks=benchmark_names)
        manifest.benchmark_identities = dict(hashes)

    console.print(f"[bold]Experiment {experiment_id}[/]: {spec.name}")
    console.print(
        f"Plan: {len(benchmark_names)} benchmark(s) × {len(spec.configs)} config(s)"
        f" × {spec.repeat} trial(s) = [bold]{total_cells}[/] runs"
    )

    # Resource-aware planning: Docker containers without resource limits are
    # effectively unbounded; refuse to start many of them at once.
    effective_jobs = jobs
    unbounded_docker = []
    for config in spec.configs:
        eff_execution = (
            spec.execution.merged_with(config.execution)
            if (spec.execution or config.execution)
            else None
        )
        has_limits = bool(
            eff_execution and (eff_execution.memory or eff_execution.cpus or eff_execution.pids_limit)
        )
        if eff_execution is not None and eff_execution.backend == "docker" and not has_limits:
            unbounded_docker.append(config.name)
    if unbounded_docker and effective_jobs > 4:
        effective_jobs = 4
        console.print(
            "[yellow]Note:[/] docker configs without memory/cpus/pids limits are"
            f" unbounded; parallelism clamped to 4 (configs: {', '.join(unbounded_docker)})."
            " Set execution resource limits to raise it."
        )
    console.print(f"Backend: {manifest.execution_backend or 'host'}   Jobs: {effective_jobs}")

    total = len(plans)
    todo: list[tuple] = []
    skipped = 0
    for index, plan in enumerate(plans, start=1):
        label = (
            f"[{index:02d}/{total:02d}] {plan.benchmark_name} / {plan.config_name} /"
            f" trial {plan.trial}"
        )
        if manifest.cell_done(plan.cell_key):
            skipped += 1
            console.print(f"{label}   [dim]already complete — skipping[/]")
        else:
            todo.append((plan, label))

    executed_count = {"n": 0}

    def run_cell(job):
        plan, _label = job
        cell_spec = specs[plan.benchmark_name]
        config = next(c for c in spec.configs if c.name == plan.config_name)
        execution_eff = (
            spec.execution.merged_with(config.execution)
            if (spec.execution or config.execution)
            else None
        )
        return run_benchmark(
            cell_spec,
            adapter=get_adapter(config.agent.type),
            results_root=results_dir,
            keep_workspace=keep_workspace,
            timeout_seconds=timeout_seconds,
            trial=plan.trial,
            repository=repositories[plan.benchmark_name],
            benchmark_dir=manifests[plan.benchmark_name].parent,
            manifest_path=manifests[plan.benchmark_name],
            execution=execution_eff,
            agent_override=config.agent,
            experiment_id=experiment_id,
            config_name=plan.config_name,
        )

    def record_cell(plan, **fields) -> None:
        manifest.record({
            "cell_key": plan.cell_key,
            "benchmark": plan.benchmark_name,
            "config": plan.config_name,
            "trial": plan.trial,
            **fields,
        })
        save_manifest(manifest, results_dir / "experiments" / experiment_id)

    def complete_cell(job, future) -> None:
        plan, label = job
        try:
            outcome = future.result()
        except Exception as exc:  # noqa: BLE001 - one broken cell never aborts the matrix
            detail = f"{type(exc).__name__}: {exc}"
            record_cell(plan, status="setup_failed", run_id=None, run_dir=None, error=detail)
            console.print(f"{label}   [red]ERROR[/]  {detail}")
            executed_count["n"] += 1
            return
        if outcome is CANCELLED:
            return  # never started; resume will pick these up
        if outcome.result.overall.get("status") == "setup_failed":
            # Persisted evidence exists; the matrix continues.
            _index_outcome(results_dir, outcome)
            record_cell(
                plan,
                status="setup_failed",
                run_id=outcome.result.run_id,
                run_dir=str(outcome.run_dir),
                error=outcome.result.overall.get("failure_reason"),
            )
            console.print(
                f"{label}   [red]SETUP FAILED[/]"
                f" [{outcome.result.overall.get('failure_stage')}]"
                f"  {outcome.result.overall.get('failure_reason')}"
            )
            executed_count["n"] += 1
            return
        _index_outcome(results_dir, outcome)
        record_cell(
            plan,
            status=outcome.result.overall["status"],
            run_id=outcome.result.run_id,
            run_dir=str(outcome.run_dir),
        )
        console.print(
            f"{label}   {status_markup(outcome.result.overall.get('status'))}"
            f"  ({format_duration(outcome.result.overall.get('duration_seconds'))})"
        )
        executed_count["n"] += 1

    scheduler = Scheduler(effective_jobs)

    def mark_interrupted() -> None:
        manifest.interrupted = True
        save_manifest(manifest, results_dir / "experiments" / experiment_id)

    # --max-runs is enforced by the scheduler at submission time: no over-budget
    # cell is ever launched. Cells already complete on resume were filtered out
    # of `todo` above, so they cannot consume the budget.
    was_interrupted = scheduler.run(
        todo, run_cell, complete_cell,
        on_interrupt=mark_interrupted,
        max_starts=max_runs,
    )
    stopped_by_budget = scheduler.budget_exhausted

    if scheduler.stop_requested and not manifest.interrupted:
        mark_interrupted()

    if was_interrupted and not stopped_by_budget:
        console.print(
            "\n[yellow]Interrupted — completed runs preserved; experiment marked"
            " incomplete. Resume with:[/] agentbench experiment"
            f" {experiment_file} --resume {experiment_id}"
        )
    elif stopped_by_budget:
        console.print(
            f"\n[yellow]Stopped after {executed_count['n']} executed run(s) (--max-runs)."
            f" Resume with:[/] agentbench experiment {experiment_file}"
            f" --resume {experiment_id}"
        )

    console.print(
        f"\nExperiment {experiment_id}: {len(manifest.completed)}/{manifest.planned_cells}"
        f" cells complete ({skipped} skipped on resume)"
    )
    if outcomes_summary := [
        r for r in index_rows_safe(results_dir, experiment_id)
    ]:
        groups = aggregate_by_config(outcomes_summary)
        for group in groups:
            interval = group.pass_rate_interval
            bounds = (
                f" [{interval[0]*100:.0f}%–{interval[1]*100:.0f}%]" if interval else ""
            )
            # Experiment summaries span benchmarks: a config name alone is
            # ambiguous because task identity (and thus grouping) is per
            # benchmark. Scope the label unless exactly one benchmark ran.
            scope = ""
            if len(group.benchmarks) == 1:
                scope = f" @ {next(iter(group.benchmarks))}"
            console.print(
                f"{group.label}{scope}: {group.passes}/{group.runs} passed"
                f" ({format_percent(group.pass_rate)}{bounds}),"
                f" median time {format_duration(group.median_duration)},"
                f" median tokens {format_count(group.median_total_tokens)}"
            )

    if was_interrupted and not stopped_by_budget:
        raise typer.Exit(code=EXIT_INTERRUPTED)


def index_rows_safe(results_dir: str | Path, experiment_id: str) -> list[dict]:
    """Best-effort DB read for experiment summaries; empty on any problem."""
    try:
        return ResultIndex(default_db_path(Path(results_dir))).query(
            experiment_id=experiment_id, limit=None
        )
    except sqlite3.DatabaseError:
        return []


@app.command()
def report(
    experiment_id: str = typer.Argument(..., help="Experiment id shown after `agentbench experiment`."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
    out: Path | None = typer.Option(None, "--out",
                                     help="Directory for report files (default <results>/reports/<id>)."),
    bundle: Path | None = typer.Option(None, "--bundle",
                                       help="Also export a safe public study bundle into this directory."),
    no_html: bool = typer.Option(False, "--no-html", help="Skip the HTML variant."),
) -> None:
    """Generate a static Markdown/HTML benchmark-study report from persisted evidence.

    Mechanical and conservative: every number comes from the run database;
    conclusions are limited to paired counts, intervals, and failure tallies.
    """
    from agentbench.experiments import ExperimentError, load_manifest
    from agentbench.reporting import (
        SecretLeakError,
        build_study,
        export_bundle,
        render_html,
        render_markdown,
    )

    manifest_path = Path(results_dir) / "experiments" / experiment_id / "experiment.json"
    if not manifest_path.exists():
        console.print(f"[red]Unknown experiment:[/] no manifest at {manifest_path}")
        raise typer.Exit(code=EXIT_ERROR)
    try:
        manifest = load_manifest(manifest_path)
    except ExperimentError as exc:
        console.print(f"[red]Unreadable manifest:[/] {exc}")
        raise typer.Exit(code=EXIT_ERROR)

    study = build_study(manifest, index_rows_safe(results_dir, experiment_id))
    target = out if out is not None else Path(results_dir) / "reports" / experiment_id
    target.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(study)
    (target / "report.md").write_text(markdown, encoding="utf-8", newline="\n")
    written = [target / "report.md"]
    if not no_html:
        html_path = target / "report.html"
        html_path.write_text(render_html(study), encoding="utf-8", newline="\n")
        written.append(html_path)

    console.print(f"[bold]Report[/]: {', '.join(str(p) for p in written)}")
    for agg in study.aggregates:
        interval = agg.interval
        bounds = f" [{interval[0]*100:.0f}%–{interval[1]*100:.0f}%]" if interval else ""
        console.print(
            f"  {agg.name}: {agg.passes}/{agg.runs} passed"
            f" ({format_percent(agg.pass_rate)}{bounds})"
        )

    if bundle is not None:
        try:
            bundled = export_bundle(study, manifest, index_rows_safe(results_dir, experiment_id),
                                    bundle, markdown=markdown,
                                    html_text=None if no_html else render_html(study))
        except SecretLeakError as exc:
            console.print(f"[red]Bundle aborted — possible credential:[/] {exc}")
            raise typer.Exit(code=EXIT_ERROR)
        console.print(f"[bold]Bundle[/]: {bundle} ({len(bundled)} files, secret-scanned)")


@app.command()
def saturation(
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
    min_runs: int = typer.Option(6, "--min-runs", min=1,
                                 help="Minimum runs per benchmark before classifying."),
    experiment_id: list[str] = typer.Option([], "--experiment-id",
                                            help="Restrict evidence to these experiment ids."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Classify corpus difficulty/saturation from real run evidence.

    Verdicts are mechanical: uncalibrated / discriminating / likely_saturated /
    likely_too_hard, and require --min-runs evidence per benchmark.
    """
    import json

    from agentbench.saturation import analyze

    index = ResultIndex(default_db_path(Path(results_dir)))
    ids = experiment_id or [None]
    rows: list[dict] = []
    for one in ids:
        try:
            rows.extend(index.query(experiment_id=one, limit=None))
        except sqlite3.DatabaseError as exc:
            console.print(f"[red]Results database unreadable:[/] {exc}")
            raise typer.Exit(code=EXIT_ERROR)

    verdicts = analyze(rows, min_runs=min_runs)
    if as_json:
        payload = [
            {
                "benchmark": v.benchmark,
                "total_runs": v.total_runs,
                "classification": v.classification,
                "overall_pass_rate": v.overall_pass_rate,
                "reason": v.reason,
                "configs": [
                    {
                        "label": c.label, "runs": c.runs, "passes": c.passes,
                        "pass_rate": c.pass_rate, "median_duration": c.median_duration,
                        "median_tokens": c.median_tokens, "median_cost_usd": c.median_cost_usd,
                    }
                    for c in v.configs
                ],
            }
            for v in verdicts
        ]
        console.print(json.dumps(payload, indent=2))
        return

    if not verdicts:
        console.print("No indexed runs found — nothing to classify.")
        return
    console.print(f"[bold]Corpus difficulty[/] (min_runs={min_runs}, {len(rows)} indexed runs)")
    for v in verdicts:
        rate = format_percent(v.overall_pass_rate)
        console.print(f"  {v.benchmark}: [bold]{v.classification}[/] ({rate}) — {v.reason}")


@app.command()
def trajectory(
    run_id: str = typer.Argument(..., help="A persisted run id."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
    event_type: list[str] = typer.Option([], "--type", help="Filter by event type (repeatable)."),
    failures_only: bool = typer.Option(False, "--failures-only"),
    as_json: bool = typer.Option(False, "--json", help="Dump raw trajectory events."),
) -> None:
    """Show the normalized agent trajectory for one run.

    Chronological, privacy-aware: only observable tool/command activity —
    never model reasoning. Runs without a trajectory report unavailable.
    """
    from agentbench.trajectories import load_trajectory

    index = _open_index(results_dir)
    row = index.get_run(run_id)
    if row is None:
        console.print(f"[red]Unknown run id:[/] {run_id}")
        raise typer.Exit(code=EXIT_ERROR)
    header, events = load_trajectory(Path(row["result_dir"]))
    if not header:
        console.print(f"[yellow]No trajectory recorded for {run_id} "
                      f"(pre-v0.6 run or extraction unavailable).[/]")
        raise typer.Exit(code=0)

    if failures_only:
        events = [e for e in events if e.get("success") is False
                  or e.get("event_type") == "error"]
    if event_type:
        wanted = set()
        for t in event_type:
            alias = {
                "tool": ("shell_command", "file_read", "file_write", "file_edit",
                         "test_command", "git_command", "search", "tool_call"),
                "test": ("test_command",),
                "edit": ("file_edit", "file_write"),
            }.get(t.lower(), (t.lower(),))
            wanted.update(alias)
        events = [e for e in events if e.get("event_type") in wanted]

    console.print(
        f"[bold]Trajectory[/] {run_id} · agent={header.get('agent_type')} · "
        f"status={header.get('trajectory_status')} · events={header.get('event_count')}"
    )
    if as_json:
        console.print(json.dumps(events, indent=2))
        return
    for ev in events:
        rel = ev.get("relative_ms")
        stamp = f"{rel/1000:6.1f}s" if isinstance(rel, (int, float)) else "     —"
        etype = ev.get("event_type") or "?"
        tool = ev.get("tool")
        detail = ""
        md = ev.get("metadata") or {}
        if md.get("command"):
            detail = str(md["command"])[:80]
        elif ev.get("path"):
            detail = str(ev["path"])[:80]
        success = ev.get("success")
        mark = ""
        if success is False:
            mark = "[red]✗[/] "
        console.print(f"{stamp}  {mark}{etype:<14} {(tool or ''):<8} {detail}")


@app.command()
def rescore(
    run_id: str = typer.Argument(None, help="A persisted run id."),
    experiment: str | None = typer.Option(None, "--experiment", help="Rescore every run of an experiment."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Re-run scorers against stored evidence — no agent, no API spend.

    The original result.json stays immutable; a scoring revision is written
    to ``scoring_revisions/`` beside it and old-vs-new is printed.
    """
    from agentbench.rescore import rescore_run

    targets: list[str] = []
    if experiment:
        index = _open_index(results_dir)
        rows = index.query(experiment_id=experiment, limit=None)
        targets = [str(r["run_id"]) for r in rows]
        if not targets:
            console.print(f"[red]No runs indexed for experiment {experiment}[/]")
            raise typer.Exit(code=EXIT_ERROR)
    elif run_id:
        targets = [run_id]
    else:
        console.print("[red]Provide a RUN-ID or --experiment.[/]")
        raise typer.Exit(code=EXIT_ERROR)

    changed = 0
    for rid in targets:
        outcome = rescore_run(rid, results_root=Path(results_dir))
        if outcome.error:
            console.print(f"[yellow]{rid}: skipped[/] — {outcome.error}")
            continue
        same = (
            (outcome.original_status == "passed") == outcome.new_resolved
        )
        verdict = "[green]unchanged[/]" if same else "[bold yellow]CHANGED[/]"
        changed += 0 if same else 1
        partial = (
            f" · partial {outcome.partial_score:.3f}"
            if outcome.partial_score is not None else ""
        )
        console.print(
            f"{rid}: original={outcome.original_status} → "
            f"resolved={outcome.new_resolved}{partial} · {verdict}\n"
            f"  revision: {outcome.revision_path}"
        )
    if changed:
        console.print(
            f"[yellow]{changed} run(s) changed verdict under current scorers. "
            f"Original evidence untouched.[/]"
        )


@app.command()
def reproduce(
    run_id: str = typer.Argument(..., help="A persisted run id."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
) -> None:
    """Re-run RUN_ID under the same recorded conditions as a NEW run."""
    from agentbench.reproduce import (
        condition_checks,
        execution_spec_from_provenance,
        load_original_evidence,
        preflight,
    )

    index = _open_index(results_dir)
    row = index.get_run(run_id)
    if row is None:
        console.print(f"[red]Unknown run id:[/] {run_id}")
        raise typer.Exit(code=EXIT_ERROR)
    result_dir = Path(row["result_dir"])
    try:
        original = load_original_evidence(result_dir)
    except OSError as exc:
        console.print(f"[red]Original evidence missing:[/] {exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    comparison = preflight(original, results_root=Path(results_dir))
    if comparison.blocked_reason:
        console.print(f"[red]Cannot reproduce:[/] {comparison.blocked_reason}")
        raise typer.Exit(code=EXIT_ERROR)

    config_snapshot: dict = original.get("config") or {}
    manifest_hint = Path(config_snapshot["_benchmark_manifest"])
    spec = load_benchmark(manifest_hint)
    stored_agent = config_snapshot.get("agent")
    if isinstance(stored_agent, dict):
        # Experiments inject model/provider/reasoning per config; the bare
        # manifest may leave them unset. Replay the original effective agent
        # so the rerun measures the same configuration instead of whatever
        # default the manifest (or CLI) would pick today.
        from agentbench.models import AgentSpec

        spec = spec.model_copy(update={"agent": AgentSpec.model_validate(stored_agent)})
    repository = resolve_repository_path(spec.repository, base_dir=manifest_hint.parent)
    execution_spec = execution_spec_from_provenance(original.get("execution") or {})

    console.print(f"[bold]Reproducing[/] {run_id} under identical conditions…")
    outcome = run_benchmark(
        spec,
        results_root=Path(results_dir),
        keep_workspace=keep_workspace,
        repository=repository,
        benchmark_dir=manifest_hint.parent,
        manifest_path=manifest_hint,
        execution=execution_spec,
    )
    _index_outcome(Path(results_dir), outcome)

    checks = condition_checks(original, outcome.result.model_dump(mode="json"))
    table = Table(title=f"Provenance: {run_id} -> {outcome.result.run_id}")
    table.add_column("Condition")
    table.add_column("Match", justify="right")
    table.add_column("Values")
    for name, same, detail in list(comparison.checks) + checks:
        table.add_row(name, "[green]same[/]" if same else "[red]differs[/]", detail)
    console.print(table)
    console.print(f"New run id: [bold]{outcome.result.run_id}[/]")


@app.command()
def export(
    experiment: str | None = typer.Option(None, "--experiment", help="Export one experiment's runs."),
    benchmark_name: str | None = typer.Option(None, "--benchmark"),
    agent: str | None = typer.Option(None, "--agent", help="Filter by adapter type."),
    status: str | None = typer.Option(None, "--status", help="Filter by outcome status."),
    fmt: str = typer.Option("csv", "--format", help="csv or json"),
    output: Path | None = typer.Option(None, "--output", help="Write to file instead of stdout."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Export flattened run metrics as CSV or JSON (never logs/secrets)."""
    from agentbench.export import write_export

    index = _open_index(results_dir)
    rows = index.query(
        experiment_id=experiment, benchmark=benchmark_name,
        agent=agent, status=status, limit=None,
    )
    if not rows:
        console.print("No matching runs to export.")
        return
    rendered_or_path = write_export(rows, fmt=fmt, output=output)
    if output is None:
        # Machine-readable formats must never pass through the styled console:
        # Rich wraps long lines at terminal width and corrupts CSV/JSON.
        import sys as _sys

        # Binary write avoids Windows text-mode translating the CSV's \r\n
        # into \r\r\n (which parses as blank rows).
        _sys.stdout.buffer.write(rendered_or_path.encode("utf-8"))
        _sys.stdout.buffer.flush()
    else:
        console.print(f"Wrote {len(rows)} run(s) to [underline]{rendered_or_path}[/]")


@app.command()
def doctor(
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Check the local environment for AgentBench readiness."""
    from agentbench.doctor import run_checks, worst_state

    state_icons = {"ok": "[green]OK[/]", "warn": "[yellow]WARN[/]", "fail": "[red]FAIL[/]"}
    checks = run_checks(Path(results_dir))
    table = Table(title="AgentBench doctor")
    table.add_column("CHECK")
    table.add_column("STATE", justify="right")
    table.add_column("DETAIL")
    for check in checks:
        table.add_row(check.name, state_icons.get(check.state, check.state), check.detail)
    console.print(table)
    if worst_state(checks) == "fail":
        raise typer.Exit(code=1)


cleanup_app = typer.Typer(help="Remove stale AgentBench-owned resources.")
app.add_typer(cleanup_app, name="cleanup")


@cleanup_app.command("workspaces")
def cleanup_workspaces(
    apply: bool = typer.Option(False, "--apply", help="Actually delete (default: dry run)."),
) -> None:
    """Delete leftover agentbench-* temp workspaces owned by AgentBench."""
    import shutil
    import tempfile

    root = Path(tempfile.gettempdir())
    targets = sorted(root.glob("agentbench-*"))
    if not targets:
        console.print("No stale workspaces found.")
        return
    for target in targets:
        if apply:
            try:
                from agentbench.workspace import remove_tree

                remove_tree(target)
                console.print(f"removed {target.name}")
            except OSError as exc:
                console.print(f"[yellow]could not remove {target.name}: {exc}[/]")
        else:
            console.print(f"[dim]dry-run:[/] would remove {target}")
    if not apply:
        console.print("Dry run only — pass [bold]--apply[/] to delete.")


@cleanup_app.command("containers")
def cleanup_containers(
    apply: bool = typer.Option(False, "--apply", help="Actually remove containers."),
) -> None:
    """Remove stopped Docker containers labeled org.agentbench.run=true."""
    import subprocess as sp

    from agentbench.backends.docker import _docker_binary, docker_available

    if not docker_available():
        console.print("[yellow]Docker unavailable — nothing to clean.[/]")
        return
    listing = sp.run(
        [_docker_binary(), "ps", "-a", "--filter", "label=org.agentbench.run=true",
         "--format", "{{.ID}} {{.Names}}"],
        capture_output=True, text=True,
    )
    lines = [line for line in listing.stdout.splitlines() if line.strip()]
    if not lines:
        console.print("No AgentBench-labeled containers found.")
        return
    for line in lines:
        container_id = line.split()[0]
        if apply:
            sp.run([_docker_binary(), "rm", "-f", container_id], capture_output=True)
            console.print(f"removed container {container_id}")
        else:
            console.print(f"[dim]dry-run:[/] would remove container {line}")


if __name__ == "__main__":  # pragma: no cover
    app()
