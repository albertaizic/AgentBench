"""Command line entry point: ``agentbench run <benchmark.yaml>``."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from agentbench.adapters import UnknownAgentError, get_adapter
from agentbench.loader import LoaderError, load_benchmark
from agentbench.models import BenchmarkSpec
from agentbench.results import RunResult
from agentbench.runner import run_benchmark
from agentbench.workspace import WorkspaceError

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Reproducible evaluation framework for coding agents.",
)
console = Console()


@app.callback()
def _root() -> None:
    """AgentBench: reproducible evaluation framework for coding agents."""


def _print_summary(spec: BenchmarkSpec, result: RunResult, run_dir: Path) -> None:
    agent = result.agent
    diff = result.diff

    console.print()
    console.print(f"[bold]Benchmark:[/] {spec.name}")
    console.print(
        f"[bold]Repository:[/] {result.benchmark['repository']}"
        f" @ {str(result.benchmark['resolved_commit'])[:12]}"
    )
    console.print(
        f"[bold]Agent ({agent['type']}):[/] exit {agent['exit_code']}"
        f" in {agent['duration_seconds']}s"
        + (" [yellow](timed out)[/]" if agent["timed_out"] else "")
    )
    console.print(
        f"[bold]Diff:[/] {diff['files_changed']} file(s) changed,"
        f" +{diff['insertions']}/-{diff['deletions']}"
    )

    table = Table(title="Evaluations", show_lines=False, expand=False)
    table.add_column("Evaluation", style="bold")
    table.add_column("Exit")
    table.add_column("Duration")
    table.add_column("Result", justify="right")

    for evaluation in result.evaluations:
        passed: bool = evaluation["passed"]
        table.add_row(
            evaluation["name"],
            str(evaluation["exit_code"]),
            f"{evaluation['duration_seconds']}s",
            "[green]PASS[/]" if passed else "[red]FAIL[/]",
        )
    console.print(table)

    status = result.overall["status"]
    if status == "passed":
        console.print("[bold green]Overall: PASSED[/]")
    else:
        console.print("[bold red]Overall: FAILED[/]")
    console.print(f"Results saved to: [underline]{run_dir}[/]")


@app.command()
def run(
    benchmark: Path = typer.Argument(..., exists=True, readable=True, help="Path to the benchmark YAML file."),
    results_dir: Path | None = typer.Option(None, "--results-dir", help="Directory for run results (default: from the benchmark)."),
    keep_workspace: bool = typer.Option(False, "--keep-workspace", help="Keep the temporary workspace for debugging."),
    timeout_seconds: float | None = typer.Option(None, "--timeout-seconds", min=0.1, help="Override the per-step timeout."),
) -> None:
    """Parse BENCHMARK, run the configured agent once, evaluate, and report."""
    try:
        spec = load_benchmark(benchmark)
    except (LoaderError, ValidationError) as exc:
        console.print(f"[red]Invalid benchmark file ({benchmark}):[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    try:
        adapter = get_adapter(spec.agent.type)
        outcome = run_benchmark(
            spec,
            adapter=adapter,
            results_root=results_dir,
            keep_workspace=keep_workspace,
            timeout_seconds=timeout_seconds,
        )
    except (UnknownAgentError, WorkspaceError, RuntimeError, OSError) as exc:
        # OSError covers unresolvable/non-executable agent binaries — a setup
        # error (exit 2), never a benchmark FAIL (exit 1).
        console.print(f"[red]Run failed:[/]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    _print_summary(spec, outcome.result, outcome.run_dir)
    raise typer.Exit(code=EXIT_PASS if outcome.result.overall["status"] == "passed" else EXIT_FAIL)


if __name__ == "__main__":  # pragma: no cover
    app()
