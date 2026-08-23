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
from agentbench.discovery import discover, find_manifest
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
        index.scan_results(Path(results_dir))
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
        suffix = f" — {reason}" if reason else ""
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
) -> None:
    """Parse BENCHMARK, run the configured agent, evaluate, persist, report."""
    spec = _load_spec_or_exit(benchmark)
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
        # OSError covers unresolvable/non-executable agent binaries — a setup
        # error (exit 2), never a benchmark FAIL (exit 1).
        console.print(f"[red]Run failed:[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

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
        ("Started", overall.get("started_at")),
        ("Duration", format_duration(overall.get("duration_seconds"))),
    ])

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
def benchmark_list() -> None:
    """List benchmarks discovered in the corpus and ./benchmarks."""
    table = Table(title="Benchmark corpus")
    for column in ("NAME", "CATEGORY", "LANGUAGE", "HIDDEN", "PROTECTED", "DIFFICULTY"):
        table.add_column(column)
    count = 0
    for manifest in discover():
        try:
            spec = load_benchmark(manifest)
        except (LoaderError, ValidationError) as exc:
            console.print(f"[red]{manifest.parent.name}: invalid manifest[/] {exc}")
            continue
        table.add_row(
            spec.name,
            spec.category or "—",
            spec.language or "—",
            "yes" if spec.hidden_evaluations else "no",
            "yes" if spec.protected_paths or spec.change_policies else "no",
            spec.difficulty or "—",
        )
        count += 1
    console.print(table)
    console.print(f"{count} benchmark(s)")


@benchmark_app.command("validate")
def benchmark_validate(
    benchmark: str = typer.Argument(..., help="Corpus name or path to a benchmark.yaml."),
    extra_root: Path | None = typer.Option(None, "--path", help="Additional discovery root."),
) -> None:
    """Validate solvability/structure of a benchmark without running an agent."""
    try:
        manifest = find_manifest(benchmark, extra_root)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=EXIT_ERROR) from exc

    from agentbench.validation import validate_benchmark

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
    console.print("[green]Benchmark is valid.[/]")


@app.command()
def experiment(
    experiment_file: Path = typer.Argument(..., exists=True, readable=True, help="Experiment YAML."),
    resume: str | None = typer.Option(None, "--resume", help="Resume this experiment id."),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    timeout_seconds: float | None = typer.Option(None, "--timeout-seconds", min=0.1),
) -> None:
    """Run a benchmark × config × repeat matrix; resumable via --resume.

    Exit code 0 means the matrix ran to completion — cell outcomes (including
    failures) are results, not errors. Exit 130 on interrupt; 2 on setup
    problems.
    """
    from agentbench.models import ExperimentSpec

    try:
        spec = ExperimentSpec.model_validate(
            yaml.safe_load(Path(experiment_file).read_text(encoding="utf-8"))
        )
    except (OSError, ValidationError) as exc:
        console.print(f"[red]Invalid experiment file ({experiment_file}):[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    manifests: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    specs: dict[str, BenchmarkSpec] = {}
    repositories: dict[str, str] = {}
    for name in spec.benchmarks:
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

    plans = plan_cells(spec, manifests, hashes)

    results_dir = Path(spec.results_dir)
    if resume:
        try:
            manifest = load_manifest(results_dir / "experiments" / resume / "experiment.json")
        except ExperimentError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=EXIT_ERROR) from exc
        for name, stored_hash in manifest.benchmark_identities.items():
            if hashes.get(name) != stored_hash:
                console.print(
                    f"[red]Cannot resume:[/] benchmark '{name}' changed since the original"
                    f" run (config hash {stored_hash} → {hashes.get(name)})."
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
        manifest = new_manifest(spec, experiment_id, results_dir)
        manifest.benchmark_identities = dict(hashes)

    console.print(f"[bold]Experiment {experiment_id}[/]: {spec.name}")
    console.print(
        f"Plan: {len(spec.benchmarks)} benchmark(s) × {len(spec.configs)} config(s)"
        f" × {spec.repeat} trial(s) = [bold]{spec.cell_count}[/] runs"
    )

    done = skipped = 0
    interrupted = False
    total = len(plans)
    try:
        for index, plan in enumerate(plans, start=1):
            label = (
                f"[{index:02d}/{total:02d}] {plan.benchmark_name} / {plan.config_name} /"
                f" trial {plan.trial}"
            )
            if manifest.cell_done(plan.cell_key):
                skipped += 1
                console.print(f"{label}   [dim]already complete — skipping[/]")
                continue
            console.print(f"{label}   running…")
            cell_spec = specs[plan.benchmark_name]
            config = next(c for c in spec.configs if c.name == plan.config_name)
            execution = (
                spec.execution.merged_with(config.execution)
                if (spec.execution or config.execution)
                else None
            )
            try:
                outcome = run_benchmark(
                    cell_spec,
                    adapter=get_adapter(config.agent.type),
                    results_root=results_dir,
                    keep_workspace=keep_workspace,
                    timeout_seconds=timeout_seconds,
                    trial=plan.trial,
                    repository=repositories[plan.benchmark_name],
                    benchmark_dir=manifests[plan.benchmark_name].parent,
                    manifest_path=manifests[plan.benchmark_name],
                    execution=execution,
                    agent_override=config.agent,
                    experiment_id=experiment_id,
                    config_name=plan.config_name,
                )
            except (OSError, RuntimeError) as exc:
                # A broken cell must not abort the remaining matrix.
                status = "setup_failed"
                detail = f"{type(exc).__name__}: {exc}"
                manifest.record({
                    "cell_key": plan.cell_key,
                    "benchmark": plan.benchmark_name,
                    "config": plan.config_name,
                    "trial": plan.trial,
                    "status": status,
                    "run_id": None,
                    "run_dir": None,
                    "error": detail,
                })
                save_manifest(manifest, results_dir / "experiments" / experiment_id)
                console.print(f"{label}   [red]ERROR[/]  {detail}")
                done += 1
                continue
            _index_outcome(results_dir, outcome)
            manifest.record({
                "cell_key": plan.cell_key,
                "benchmark": plan.benchmark_name,
                "config": plan.config_name,
                "trial": plan.trial,
                "status": outcome.result.overall["status"],
                "run_id": outcome.result.run_id,
                "run_dir": str(outcome.run_dir),
            })
            save_manifest(manifest, results_dir / "experiments" / experiment_id)
            console.print(
                f"{label}   {status_markup(outcome.result.overall.get('status'))}"
                f"  ({format_duration(outcome.result.overall.get('duration_seconds'))})"
            )
            done += 1
    except KeyboardInterrupt:
        interrupted = True
        manifest.interrupted = True
        save_manifest(manifest, results_dir / "experiments" / experiment_id)
        console.print(
            f"\n[yellow]Interrupted — {done} new run(s) preserved;"
            f" experiment marked incomplete.[/]"
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
            console.print(
                f"{group.label}: {group.passes}/{group.runs} passed"
                f" ({format_percent(group.pass_rate)}{bounds}),"
                f" median time {format_duration(group.median_duration)},"
                f" median tokens {format_count(group.median_total_tokens)}"
            )

    if interrupted:
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
    table = Table(title=f"Provenance: {run_id} → {outcome.result.run_id}")
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
    fmt: str = typer.Option("csv", "--format", help="csv or json"),
    output: Path | None = typer.Option(None, "--output", help="Write to file instead of stdout."),
    results_dir: str = typer.Option(DEFAULT_RESULTS_DIR, "--results-dir"),
) -> None:
    """Export flattened run metrics as CSV or JSON (never logs/secrets)."""
    from agentbench.export import write_export

    index = _open_index(results_dir)
    rows = index.query(experiment_id=experiment, benchmark=benchmark_name, limit=None)
    if not rows:
        console.print("No matching runs to export.")
        return
    rendered_or_path = write_export(rows, fmt=fmt, output=output)
    if output is None:
        console.print(rendered_or_path)
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
                shutil.rmtree(target)
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
