"""Read-only local web dashboard over the AgentBench result index.

Standard-library only (``http.server``), server-rendered HTML, bound to
localhost by default. The dashboard reads exclusively through
:mod:`agentbench.storage` plus the ``result.json`` evidence files — it has no
knowledge of any specific agent adapter.

Security posture:

* binds to ``127.0.0.1`` unless told otherwise;
* strictly read-only — no POST/PUT handlers exist at all;
* artifact paths are never built from URL components alone: the run id must
  resolve to a stored run, every path segment is pattern-checked, and the
  resolved file must live inside that run's directory;
* all dynamic content is HTML-escaped.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agentbench.aggregate import (
    aggregate_by_config,
    failure_counts,
    format_count,
    format_duration,
    format_percent,
    pairwise_statistics,
)
from agentbench.discovery import find_manifest
from agentbench.loader import load_benchmark
from agentbench.storage import ResultIndex, default_db_path

RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
ARTIFACT_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_STATUS_COLORS = {
    "passed": "#2e7d32",
    "evaluation_failed": "#c62828",
    "agent_failed": "#c62828",
    "agent_timeout": "#e65100",
    "invalid_result": "#6a1b9a",
    "protected_path_violation": "#c62828",
}

_PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; background: #f5f5f5; color: #1a1a1a; }
header { background: #1a237e; color: #fff; padding: 10px 20px; }
header a { color: #c5cae9; margin-right: 16px; text-decoration: none; font-weight: 600; }
main { padding: 20px; max-width: 1200px; margin: 0 auto; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; background: #fff; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 14px; }
th { background: #e8eaf6; }
.passed { color: #2e7d32; font-weight: 700; }
.failed { color: #c62828; font-weight: 700; }
.violation { color: #c62828; font-weight: 700; }
pre { background: #263238; color: #eceff1; padding: 12px; overflow-x: auto;
      max-height: 480px; overflow-y: auto; font-size: 13px; }
.kv td:first-child { font-weight: 600; width: 220px; }
form label { margin-right: 12px; font-size: 14px; }
h2 { margin-top: 28px; }
"""


def esc(value) -> str:
    return html.escape("—" if value is None else str(value))


def status_badge(status: str | None) -> str:
    if not status:
        return "—"
    css = "passed" if status == "passed" else "failed"
    return f'<span class="{css}">{html.escape(status)}</span>'


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}">no data</td></tr>'
    return f"<table><tr>{head}</tr>{body}</table>"


def key_value_table(pairs: list[tuple[str, object]]) -> str:
    rows = [f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in pairs]
    return f'<table class="kv">{"".join(rows)}</table>'


def bar_chart(values: list[tuple[str, float]], unit: str = "") -> str:
    """Minimal horizontal SVG bar chart."""
    usable = [(label, value) for label, value in values if isinstance(value, (int, float))]
    if not usable:
        return "<p>no numeric data</p>"
    peak = max(value for _, value in usable) or 1
    bars = []
    for i, (label, value) in enumerate(usable[:40]):
        width = max(2, int(value / peak * 520))
        y = i * 24
        bars.append(
            f"<text x='0' y='{y + 14}' font-size='11' fill='#1a1a1a'>{html.escape(label)}</text>"
            f"<rect x='150' y='{y + 4}' width='{width}' height='14' fill='#3949ab'></rect>"
            f"<text x='{160 + width}' y='{y + 14}' font-size='11' fill='#1a1a1a'>{value:g}{unit}</text>"
        )
    height = len(usable[:40]) * 24 + 8
    return f"<svg width='720' height='{height}' role='img'>{''.join(bars)}</svg>"


class DashboardStore:
    """SQLite-backed view store with throttled rescanning of JSON evidence.

    The dashboard serves each request on its own thread, so every operation
    opens a short-lived SQLite connection under a lock instead of sharing one
    connection across threads.
    """

    _SCAN_THROTTLE_SECONDS = 1.0

    def __init__(self, results_root: Path) -> None:
        self.results_root = Path(results_root)
        self._lock = threading.Lock()
        self._last_scan = 0.0

    def _scan(self) -> None:
        try:
            ResultIndex(default_db_path(self.results_root)).scan_results(self.results_root)
            self._last_scan = time.monotonic()
        except Exception:  # noqa: BLE001 - corrupted index must not kill pages
            pass

    def _fresh_rows(self, **filters) -> list[dict]:
        with self._lock:
            return ResultIndex(default_db_path(self.results_root)).query(**filters)

    def _maybe_rescan(self) -> None:
        if time.monotonic() - self._last_scan < self._SCAN_THROTTLE_SECONDS:
            return
        with self._lock:
            if time.monotonic() - self._last_scan >= self._SCAN_THROTTLE_SECONDS:
                self._scan()

    def _rescan_now(self) -> None:
        with self._lock:
            self._scan()

    def query(self, **filters) -> list[dict]:
        self._maybe_rescan()
        rows = self._fresh_rows(**filters)
        if not rows:
            # Nothing yet (or brand-new evidence): rescan once and retry.
            self._rescan_now()
            rows = self._fresh_rows(**filters)
        return rows

    def get_run(self, run_id: str) -> dict | None:
        self._maybe_rescan()
        run = self._fresh_row(run_id)
        if run is None:
            self._rescan_now()
            run = self._fresh_row(run_id)
        return run

    def _fresh_row(self, run_id: str) -> dict | None:
        with self._lock:
            return ResultIndex(default_db_path(self.results_root)).get_run(run_id)


# -- route bodies (pure-ish functions for testability) -----------------------


def render_overview(store: DashboardStore) -> str:
    rows = store.query(limit=None)
    passes = sum(1 for r in rows if r.get("status") == "passed")
    configs = aggregate_by_config(rows)
    config_rows = [
        [
            esc(g.label),
            str(g.runs),
            format_percent(g.pass_rate),
            format_duration(g.median_duration),
            format_count(g.median_total_tokens),
        ]
        for g in configs
    ]
    recent = [
        [
            f"<a href='/runs/{esc(r['run_id'])}'>{esc(r['run_id'])}</a>",
            esc(r.get("benchmark")),
            esc(r.get("agent")),
            status_badge(r.get("status")),
            format_duration(r.get("duration_seconds")),
            esc((r.get("created_at") or "")[:19]),
        ]
        for r in sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)[:10]
    ]
    body = f"""
    <h2>Overview</h2>
    {key_value_table([
        ("Total runs", str(len(rows))),
        ("Overall pass rate", format_percent(passes / len(rows) if rows else None)),
        ("Benchmarks", str(len({r.get('benchmark') for r in rows}))),
        ("Agent/model configurations", str(len(configs))),
    ])}
    <h2>Configurations</h2>
    {table(["CONFIG", "RUNS", "PASS RATE", "MEDIAN TIME", "MEDIAN TOKENS"], config_rows)}
    <h2>Recent runs</h2>
    {table(["RUN ID", "BENCHMARK", "AGENT", "STATUS", "DURATION", "TIME (UTC)"], recent)}
    """
    return _page("AgentBench — Overview", body)


def render_runs(store: DashboardStore, params: dict) -> str:
    filters = {
        key: params[key][0]
        for key in ("benchmark", "agent", "model", "status")
        if params.get(key, [""])[0]
    }
    limit_raw = params.get("limit", ["100"])[0]
    try:
        limit = max(1, min(1000, int(limit_raw)))
    except ValueError:
        limit = 100
    offset_raw = params.get("offset", ["0"])[0]
    try:
        offset = max(0, int(offset_raw))
    except ValueError:
        offset = 0
    # One extra row beyond the page reveals whether a next page exists
    # without a separate COUNT query over the whole index.
    rows = store.query(**filters, limit=limit + 1, offset=offset)
    has_next = len(rows) > limit
    rows = rows[:limit]

    def page_link(new_offset: int, label: str) -> str:
        from urllib.parse import urlencode

        query = urlencode({**filters, "limit": limit, "offset": new_offset})
        return f'<a href="/runs?{query}">{label}</a>'

    pager_parts = []
    if offset > 0:
        pager_parts.append(page_link(max(0, offset - limit), f"← prev {limit}"))
    if has_next:
        pager_parts.append(page_link(offset + limit, f"next {limit} →"))
    pager = (
        f'<p class="pager">{ " · ".join(pager_parts) }'
        f' <span class="dim">(rows {offset + 1}–{offset + len(rows)})</span></p>'
        if pager_parts or rows
        else ""
    )

    table_rows = [
        [
            f"<a href='/runs/{esc(r['run_id'])}'>{esc(r['run_id'])}</a>",
            esc(r.get("benchmark")),
            esc(r.get("agent")),
            esc(r.get("model")),
            status_badge(r.get("status")),
            format_duration(r.get("duration_seconds")),
            esc((r.get("created_at") or "")[:19]),
        ]
        for r in rows
    ]
    filter_form = """
    <form method="get" action="/runs">
      <label>Benchmark <input name="benchmark" size="14"></label>
      <label>Agent <input name="agent" size="10"></label>
      <label>Model <input name="model" size="18"></label>
      <label>Status <input name="status" size="12"></label>
      <label>Per page <input name="limit" size="5"></label>
      <button type="submit">Filter</button>
    </form>
    """
    body = f"<h2>Runs</h2>{filter_form}{pager}" + table(
        ["RUN ID", "BENCHMARK", "AGENT", "MODEL", "STATUS", "DURATION", "TIME (UTC)"],
        table_rows,
    )
    return _page("AgentBench — Runs", body)


def _load_evidence(store: DashboardStore, run_id: str) -> dict | None:
    run = store.get_run(run_id)
    if run is None:
        return None
    result_file = Path(run["result_dir"]) / "result.json"
    try:
        import json

        return json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render_run_detail(store: DashboardStore, run_id: str) -> str | None:
    run = store.get_run(run_id)
    if run is None:
        return None
    payload = _load_evidence(store, run_id) or {}
    benchmark = payload.get("benchmark") or {}
    agent = payload.get("agent") or {}
    diff = payload.get("diff") or {}
    overall = payload.get("overall") or {}
    usage = payload.get("usage")
    environment = payload.get("environment") or {}

    def eval_table(evaluations: list[dict], title: str) -> str:
        rows = [
            [
                esc(e.get("name")),
                status_badge("passed" if e.get("passed") else "evaluation_failed"),
                esc(e.get("exit_code")),
                format_duration(e.get("duration_seconds")),
            ]
            for e in evaluations
        ]
        return f"<h3>{title}</h3>" + table(["EVALUATION", "RESULT", "EXIT", "DURATION"], rows)

    violation_note = ""
    protected = payload.get("protected_paths")
    if protected:
        violations = protected.get("violations") or []
        violation_rows = [[esc(v.get("path")), esc(v.get("pattern"))] for v in violations]
        violation_note = (
            f"<h2 class='violation'>Protected path violations ({len(violations)})</h2>"
            + table(["PATH", "MATCHED PATTERN"], violation_rows)
        )

    artifacts = ["diff.patch", "agent.stdout.log", "agent.stderr.log"]
    for section in ("evaluations", "hidden_evaluations"):
        for e in payload.get(section) or []:
            for key in ("stdout_file", "stderr_file"):
                if e.get(key):
                    artifacts.append(f"evals/{Path(e[key]).name}")
    artifact_links = "<br>".join(
        f"<a href='/artifacts/{esc(run_id)}/{quote_path(name)}'>{esc(name)}</a>" for name in artifacts
    )

    usage_pairs = []
    if usage:
        usage_pairs = [
            ("Input tokens", esc(usage.get("input_tokens"))),
            ("Output tokens", esc(usage.get("output_tokens"))),
            ("Total tokens", esc(usage.get("total_tokens"))),
            ("Cost (USD)", esc(usage.get("cost_usd"))),
            ("Tool calls", esc(usage.get("tool_calls"))),
            ("Turns", esc(usage.get("num_turns"))),
            ("Session", esc(usage.get("session_id"))),
        ]

    execution = payload.get("execution") or {}
    execution_rows: list[tuple[str, str]] = []
    if execution:
        digests = execution.get("image_digests") or []
        execution_rows = [
            ("Backend", esc(execution.get("backend"))),
            ("Network policy", esc(execution.get("network"))),
            ("Docker version", esc(execution.get("docker_version"))),
            ("Image requested", esc(execution.get("image_requested"))),
            ("Image id", esc(execution.get("image_id"))),
            ("Image digest", esc(digests[0] if digests else None)),
            ("Memory limit", esc(execution.get("memory_limit"))),
            ("CPU limit", esc(execution.get("cpus_limit"))),
            ("PIDs limit", esc(execution.get("pids_limit"))),
            ("Forwarded env (names only)", esc(", ".join(execution.get("passed_env_names") or []) or "none")),
            ("Container workspace", esc(execution.get("container_workspace"))),
        ]

    body = f"""
    <h2>Run {esc(run_id)} {status_badge(overall.get('status'))}</h2>
    {key_value_table([
        ("Benchmark", esc(benchmark.get("name"))),
        ("Repository", esc(benchmark.get("repository"))),
        ("Requested commit", esc(benchmark.get("commit"))),
        ("Resolved commit", esc(benchmark.get("resolved_commit"))),
        ("Config hash", esc(benchmark.get("config_hash"))),
        ("Trial", esc(payload.get("trial"))),
        ("Experiment",
         f"<a href='/experiments/{esc(payload.get('experiment_id'))}'>"
         f"{esc(payload.get('experiment_id'))}</a>" if payload.get("experiment_id") else "—"),
        ("Config name", esc(payload.get("config_name"))),
        ("Agent", esc(agent.get("type"))),
        ("Model", esc(agent.get("model"))),
        ("Capabilities", esc(", ".join((agent.get("capabilities") or [])) or "—")),
        ("Status", status_badge(overall.get("status"))),
        ("Failure reason", esc(overall.get("failure_reason"))),
        ("Failure stage", esc(overall.get("failure_stage") or "—")),
        ("Duration", format_duration(overall.get("duration_seconds"))),
    ])}
    <h2>Stage timings</h2>
    {key_value_table(sorted((payload.get('stage_timings') or {}).items()))}
    <h2>Evaluations</h2>
    {eval_table(payload.get('evaluations') or [], 'Public')}
    {eval_table(payload.get('hidden_evaluations') or [], 'Hidden (executed outside the workspace)')}
    {violation_note}
    <h2>Diff statistics</h2>
    {key_value_table([
        ("Files changed", esc(diff.get("files_changed"))),
        ("Insertions", esc(diff.get("insertions"))),
        ("Deletions", esc(diff.get("deletions"))),
        ("Changed paths", esc(", ".join(diff.get("changed_paths") or []))),
    ])}
    <h3>Usage &amp; cost</h3>
    {key_value_table(usage_pairs) if usage_pairs else '<p>usage unavailable for this agent/run</p>'}
    <h3>Execution provenance</h3>
    {key_value_table(execution_rows) if execution_rows else '<p>no execution provenance recorded (v0.1/v0.2 result)</p>'}
    <h3>Environment</h3>
    {key_value_table([(k, esc(v)) for k, v in environment.items()])}
    <h3>Artifacts</h3>
    <p>{artifact_links}</p>
    <p>Result directory: <code>{esc(run.get('result_dir'))}</code></p>
    """
    return _page(f"AgentBench — {run_id}", body)


def render_benchmark(store: DashboardStore, name: str) -> str | None:
    rows = store.query(benchmark=name, limit=None)
    if not rows:
        return None
    configs = aggregate_by_config(rows)
    config_rows = [
        [
            esc(g.label),
            esc(", ".join(sorted({c[:10] for c in g.resolved_commits})) or "—"),
            str(g.runs),
            format_percent(g.pass_rate),
            format_duration(g.median_duration),
            format_count(g.median_lines_changed, " lines"),
            format_count(g.median_total_tokens),
        ]
        for g in configs
    ]
    commits = {r.get("resolved_commit") for r in rows}
    heterogeneity = (
        f"<p class='violation'>Warning: this benchmark was evaluated at "
        f"{len(commits)} different resolved commits — comparison spans "
        f"materially different code states.</p>"
        if len(commits) > 1
        else ""
    )
    trials_with_number = [r for r in rows if r.get("trial") is not None]
    trial_note = (
        f"<p>{len(trials_with_number)} of these runs are numbered repeated trials.</p>"
        if trials_with_number
        else ""
    )
    duration_chart = bar_chart(
        [(str(r["run_id"]), r["duration_seconds"]) for r in rows if r.get("duration_seconds")],
        unit="s",
    )
    body = f"""
    <h2>Benchmark {esc(name)}</h2>
    {heterogeneity}{trial_note}
    <h3>Durations per run</h3>
    {duration_chart}
    <h3>Configuration aggregates</h3>
    {table(["CONFIG", "COMMITS", "RUNS", "PASS RATE", "MEDIAN TIME", "MEDIAN LINES", "MEDIAN TOKENS"], config_rows)}
    <h3>All runs</h3>
    {table(
        ["RUN ID", "AGENT", "MODEL", "STATUS", "TRIAL", "DURATION"],
        [
            [
                f"<a href='/runs/{esc(r['run_id'])}'>{esc(r['run_id'])}</a>",
                esc(r.get("agent")),
                esc(r.get("model")),
                status_badge(r.get("status")),
                esc(r.get("trial")),
                format_duration(r.get("duration_seconds")),
            ]
            for r in rows
        ],
    )}
    """
    return _page(f"AgentBench — {name}", body)


def quote_path(path: str) -> str:
    return "/".join(html.escape(part, quote=True) for part in path.split("/"))


def resolve_artifact(result_dir: Path, segments: list[str]) -> Path | None:
    """Resolve an artifact request to a file inside *result_dir*, or None.

    Defense in depth: segment pattern checks, explicit '.'/'..' rejection,
    and a realpath containment check.
    """
    if not segments or any(not ARTIFACT_SEGMENT.match(segment) for segment in segments):
        return None
    if any(segment in (".", "..") for segment in segments):
        return None
    candidate = (Path(result_dir) / Path(*segments)).resolve()
    base = Path(result_dir).resolve()
    if base != candidate and base not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def render_page(title: str, body: str) -> str:
    return _page(title, body)


# -- experiment / corpus views (v0.3) ----------------------------------------


def _experiment_manifests(results_root: Path) -> dict[str, dict]:
    """Load every persisted experiment manifest keyed by experiment id."""
    manifests: dict[str, dict] = {}
    experiments_dir = Path(results_root) / "experiments"
    for manifest_file in sorted(experiments_dir.glob("*/experiment.json")):
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifests[manifest["experiment_id"]] = manifest
        except (OSError, ValueError, KeyError):
            continue
    return manifests


def render_experiments(store: DashboardStore) -> str:
    rows = store.query(limit=None)
    experiments = _experiment_manifests(store.results_root)

    table_rows = []
    for experiment_id, manifest in experiments.items():
        completed = len(manifest.get("completed") or [])
        planned = int(manifest.get("planned_cells") or 0)
        state = "incomplete" if manifest.get("interrupted") else "complete"
        if completed < planned and not manifest.get("interrupted"):
            state = "partial"
        config_names = ", ".join(sorted((manifest.get("config_identities") or {}).keys()))
        benchmark_names = ", ".join(sorted((manifest.get("benchmark_identities") or {}).keys()))
        table_rows.append([
            f"<a href='/experiments/{esc(experiment_id)}'>{esc(experiment_id)}</a>",
            esc(manifest.get("name")),
            esc(state),
            f"{completed}/{planned}",
            esc(benchmark_names),
            esc(config_names),
            format_percent(completed / planned if planned else None),
        ])
    # Runs with an experiment_id but no manifest still deserve visibility.
    for row in rows:
        exp_id = row.get("experiment_id")
        if exp_id and exp_id not in experiments:
            table_rows.append([
                f"<a href='/experiments/{esc(exp_id)}'>{esc(exp_id)}</a>",
                esc("(manifest missing)"), "unknown", "?", "—", "—", "—",
            ])

    nav_note = "<p><a href='/benchmarks'>Browse the benchmark corpus →</a></p>"
    body = (
        "<h2>Experiments</h2>"
        + nav_note
        + table(["EXPERIMENT", "NAME", "STATE", "CELLS", "BENCHMARKS", "CONFIGS", "PASS RATE"],
                table_rows)
    )
    return _page("AgentBench — Experiments", body)


def render_experiment_detail(store: DashboardStore, experiment_id: str) -> str | None:
    manifests = _experiment_manifests(store.results_root)
    manifest = manifests.get(experiment_id)
    if manifest is None:
        return None

    rows = [r for r in store.query(limit=None) if r.get("experiment_id") == experiment_id]
    configs = list((manifest.get("config_identities") or {}).keys())
    benchmarks = list((manifest.get("benchmark_identities") or {}).keys())

    pass_by_cell: dict[tuple, dict] = {}
    for record in manifest.get("completed") or []:
        cell = (record["benchmark"], record["config"])
        stats = pass_by_cell.setdefault(cell, {"runs": 0, "passes": 0})
        stats["runs"] += 1
        stats["passes"] += 1 if record.get("status") == "passed" else 0

    header_row = "".join(f"<th>{esc(c)}</th>" for c in ["BENCHMARK"] + configs)
    matrix_rows = []
    for benchmark in benchmarks:
        cells = [f"<td>{esc(benchmark)}</td>"]
        for config in configs:
            stats = pass_by_cell.get((benchmark, config))
            cells.append(
                "<td>—</td>" if not stats else
                f"<td>{stats['passes']}/{stats['runs']}</td>"
            )
        matrix_rows.append("<tr>" + "".join(cells) + "</tr>")
    matrix = f"<table><tr>{header_row}</tr>{''.join(matrix_rows)}</table>"

    groups = aggregate_by_config(rows)
    group_rows = []
    for g in groups:
        interval = g.pass_rate_interval
        bounds = (
            f"{interval[0] * 100:.0f}–{interval[1] * 100:.0f}%"
            if interval else "—"
        )
        iqr = g.duration_iqr
        iqr_text = (
            f"{format_duration(iqr[0])}–{format_duration(iqr[1])}"
            if iqr else "—"
        )
        group_rows.append([
            esc(g.label),
            str(g.runs),
            format_percent(g.pass_rate) + f' <span class="dim">[{bounds}]</span>',
            format_duration(g.median_duration)
            + f' <span class="dim">[{iqr_text}]</span>',
            format_count(g.median_total_tokens),
            esc(format_count(g.avg_cost_usd, "$") if g.costs else "—"),
        ])
    taxonomy = failure_counts(rows)

    # Pairwise comparisons between configurations over matched cells only.
    pair_sections = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            stats = pairwise_statistics(
                [r for r in rows if r.get("config_hash") == groups[i].config_hash],
                [r for r in rows if r.get("config_hash") == groups[j].config_hash],
            )
            if not stats:
                continue
            p_value = stats.get("mcnemar_p")
            p_text = f"{p_value:.3f}" if isinstance(p_value, float) else "—"
            rows_pair = [
                ["Matched cells", str(stats["matched"])],
                ["A passed / B failed", str(stats["a_only"])],
                ["B passed / A failed", str(stats["b_only"])],
                ["Both passed", str(stats["both_pass"])],
                ["Both failed", str(stats["both_fail"])],
                ["McNemar exact p", p_text],
                [
                    "Median time among mutual passes",
                    f"A {format_duration(stats['a_median_duration_mutual_pass'])}"
                    f" · B {format_duration(stats['b_median_duration_mutual_pass'])}",
                ],
                [
                    "Median tokens among mutual passes",
                    f"A {format_count(stats['a_median_tokens_mutual_pass'])}"
                    f" · B {format_count(stats['b_median_tokens_mutual_pass'])}",
                ],
                [
                    "Median cost among mutual passes",
                    f"A {esc(format_count(stats['a_median_cost_usd_mutual_pass'], '$') if stats['a_median_cost_usd_mutual_pass'] is not None else '—')}"
                    f" · B {esc(format_count(stats['b_median_cost_usd_mutual_pass'], '$') if stats['b_median_cost_usd_mutual_pass'] is not None else '—')}",
                ],
            ]
            pair_sections.append(
                f"<h4>{esc(groups[i].label)} vs {esc(groups[j].label)}</h4>"
                + key_value_table(rows_pair)
            )
    pairs_html = (
        "".join(pair_sections)
        if pair_sections
        else "<p>Two configs with matched cells are needed for comparison.</p>"
    )

    durations = [
        (str(r["run_id"]), r["duration_seconds"])
        for r in rows if isinstance(r.get("duration_seconds"), (int, float))
    ]

    body = f"""
    <h2>Experiment {esc(manifest.get('name'))} <small>({esc(experiment_id)})</small></h2>
    {key_value_table([
        ("Planned cells", str(manifest.get("planned_cells"))),
        ("Completed", str(len(manifest.get("completed") or []))),
        ("Interrupted", esc(manifest.get("interrupted"))),
        ("Repeat", str(manifest.get("repeat"))),
        ("Resolved benchmarks", esc(", ".join(manifest.get("resolved_benchmarks") or []) or "—")),
    ])}
    <h3>Benchmark × config success matrix</h3>
    <table><tr>{header_row}</tr>{''.join(matrix_rows)}</table>
    <h3>Configuration aggregates</h3>
    {table(['CONFIG', 'RUNS', 'PASS RATE [95% WILSON]', 'MEDIAN TIME [IQR]', 'MEDIAN TOKENS', 'AVG COST'], group_rows)}
    <h3>Pairwise configuration comparison</h3>
    {pairs_html}
    <h3>Failure taxonomy</h3>
    {table(['STATUS', 'COUNT'], [[esc(k), str(v)] for k, v in sorted(taxonomy.items())])}
    <h3>Duration distribution</h3>
    {bar_chart(durations, unit='s')}
    """
    return _page(f"AgentBench — {manifest.get('name')}", body)


def render_benchmarks_page(store: DashboardStore) -> str | None:
    from agentbench.discovery import discover as discover_manifests
    from agentbench.loader import load_benchmark as load_spec

    all_rows = store.query(limit=None)
    by_benchmark: dict[str, list[dict]] = {}
    for row in all_rows:
        by_benchmark.setdefault(str(row.get("benchmark")), []).append(row)

    table_rows = []
    seen = set()
    for manifest_path in discover_manifests():
        try:
            spec = load_spec(manifest_path)
        except Exception:  # noqa: BLE001 - broken manifests listed with a note
            spec = None
        name = manifest_path.parent.name
        seen.add(name)
        runs = by_benchmark.get(name, [])
        passes = sum(1 for r in runs if r.get("status") == "passed")
        category = spec.category if spec else "—"
        language = spec.language if spec else "—"
        difficulty = spec.difficulty if spec else "—"
        hidden = "yes" if (spec and spec.hidden_evaluations) else "no"
        link = f"<a href='/benchmarks/{esc(name)}'>{esc(name)}</a>"
        table_rows.append([
            link, esc(category), esc(language), hidden,
            str(len(runs)), format_percent(passes / len(runs) if runs else None),
            esc(difficulty),
        ])
    for name, runs in by_benchmark.items():
        if name in seen:
            continue
        passes = sum(1 for r in runs if r.get("status") == "passed")
        table_rows.append([
            f"<a href='/benchmarks/{esc(name)}'>{esc(name)}</a>",
            "—", "—", "—", str(len(runs)),
            format_percent(passes / len(runs) if runs else None), "—",
        ])

    body = (
        "<h2>Benchmark corpus</h2>"
        + table(["BENCHMARK", "CATEGORY", "LANGUAGE", "HIDDEN EVALS", "RUNS",
                 "PASS RATE", "DIFFICULTY"], table_rows)
    )
    return _page("AgentBench — Benchmarks", body)


def render_corpus_benchmark_detail(store: DashboardStore, name: str):
    """Enrich the standard benchmark view with manifest metadata."""
    base_page = render_benchmark(store, name)
    if base_page is None:
        return None
    metadata_rows = []
    try:
        manifest_path = find_manifest(name)
        spec = load_benchmark(manifest_path)
        metadata_rows = [
            ("Description", f"<p>{esc(spec.description)}</p>" if spec.description else "—"),
            ("Tags", esc(", ".join(spec.tags) or "—")),
            ("Manifest", esc(manifest_path)),
        ]
    except Exception:  # noqa: BLE001 - corpus metadata is best-effort enrichment
        metadata_rows = [("Description", "—")]
    insertion = "<h3>Metadata</h3>" + key_value_table(metadata_rows)
    return base_page.replace("</main>", insertion + "</main>")


def _page(title: str, body: str) -> str:
    nav = """
    <header>
      <strong style="margin-right:24px">AgentBench</strong>
      <a href="/">Overview</a><a href="/runs">Runs</a><a href="/experiments">Experiments</a><a href="/benchmarks">Benchmarks</a>
    </header>"""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_PAGE_CSS}</style></head>"
        f"<body>{nav}<main>{body}</main></body></html>"
    )


def make_dashboard(results_root: Path, port: int = 8765, host: str = "127.0.0.1"):
    """Build (but do not start) the dashboard HTTP server."""
    store = DashboardStore(results_root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            parsed = urlparse(self.path)
            segments = [s for s in parsed.path.split("/") if s]
            params = parse_qs(parsed.query)
            try:
                status, content_type, body = self._route(segments, params)
            except Exception:  # noqa: BLE001 - one bad page must not kill the server
                import sys
                import traceback

                traceback.print_exc(file=sys.stderr)
                status, content_type = 500, "text/html; charset=utf-8"
                body = "<h2>internal error</h2>"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def _route(self, segments, params):
            if not segments:
                return 200, "text/html; charset=utf-8", render_overview(store)
            if segments[0] == "experiments" and len(segments) == 1:
                return 200, "text/html; charset=utf-8", render_experiments(store)
            if segments[0] == "experiments" and len(segments) == 2:
                page = render_experiment_detail(store, segments[1])
                if page is None:
                    return 404, "text/html; charset=utf-8", _page("Not found", "<h2>Unknown experiment</h2>")
                return 200, "text/html; charset=utf-8", page
            if segments[0] == "benchmarks" and len(segments) == 1:
                page = render_benchmarks_page(store)
                if page is None:
                    return 404, "text/html; charset=utf-8", _page("Not found", "<h2>No benchmarks</h2>")
                return 200, "text/html; charset=utf-8", page
            if segments[0] == "benchmarks" and len(segments) == 2:
                page = render_corpus_benchmark_detail(store, segments[1])
                if page is None:
                    return 404, "text/html; charset=utf-8", _page("Not found", "<h2>Unknown benchmark</h2>")
                return 200, "text/html; charset=utf-8", page
            if segments[0] == "runs" and len(segments) == 1:
                return 200, "text/html; charset=utf-8", render_runs(store, params)
            if segments[0] == "runs" and len(segments) == 2:
                page = render_run_detail(store, segments[1])
                if page is None:
                    return 404, "text/html; charset=utf-8", _page("Not found", "<h2>Unknown run</h2>")
                return 200, "text/html; charset=utf-8", page
            if segments[0] == "benchmarks" and len(segments) == 2:
                page = render_benchmark(store, segments[1])
                if page is None:
                    return 404, "text/html; charset=utf-8", _page("Not found", "<h2>Unknown benchmark</h2>")
                return 200, "text/html; charset=utf-8", page
            if segments[0] == "artifacts" and len(segments) >= 2:
                run_id = segments[1]
                if not RUN_ID_PATTERN.match(run_id):
                    return 404, "text/plain; charset=utf-8", "not found"
                run = store.get_run(run_id)
                if run is None:
                    return 404, "text/plain; charset=utf-8", "not found"
                artifact = resolve_artifact(Path(run["result_dir"]), segments[2:])
                if artifact is None:
                    return 404, "text/plain; charset=utf-8", "not found"
                return 200, "text/plain; charset=utf-8", artifact.read_text(encoding="utf-8", errors="replace")
            return 404, "text/html; charset=utf-8", _page("Not found", "<h2>Not found</h2>")

        def do_POST(self):  # noqa: N802 - dashboard is read-only
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.end_headers()

        def log_message(self, *args):  # silence per-request stderr noise
            return

    return ThreadingHTTPServer((host, port), Handler)
