"""Static benchmark-study reporting from persisted experiment evidence.

Everything here is mechanical: numbers come from the results database and the
experiment manifest; conclusions are limited to what those numbers say. There
is no "best agent" score — different tasks measure different things, and
combining them into one weighted number would be fabrication.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agentbench import __version__
from agentbench.aggregate import (
    format_count,
    format_duration,
    format_percent,
    pairwise_statistics,
)
from agentbench.experiments import ExperimentManifest
from agentbench.saturation import BenchmarkSaturation, analyze

# -- secret scanning ----------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "Anthropic key"),
    (re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{8,}"), "OpenRouter key"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "generic sk- secret"),
    (re.compile(r"(?i)(api[_-]?key|auth[_-]?token|password)\s*[=:]\s*\S+"), "credential assignment"),
    (re.compile(r"(?i)aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*\S+"), "AWS secret key"),
    (re.compile(r"(?i)\b(client[_-]?secret|access[_-]?token)\s*[=:]\s*\S+"), "credential assignment"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "bearer token"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub token"),
]


def scan_for_secrets(text: str) -> list[str]:
    """Return descriptions of anything that looks like a credential in *text*."""
    found: list[str] = []
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


class SecretLeakError(ValueError):
    """Raised when generated bundle content would expose a credential."""


# Local filesystem paths must never reach a public artifact: bundles ship to
# other machines and the repo is public. Primary evidence under results/ keeps
# full detail; this scrubbing applies only to derived public surfaces.
_LOCAL_PATH_RE = [
    re.compile(r"[A-Za-z]:[/\\][^\s`\"')\]]+"),          # Windows drive paths
    re.compile(r"(?<![\w])/+(?:Users|home|tmp|var|private)/[^\s`\"')\]]+"),
]


def redact_local_paths(text: str) -> str:
    """Replace absolute local paths with ``<local-path>`` markers."""
    scrubbed = text
    for pattern in _LOCAL_PATH_RE:
        scrubbed = pattern.sub("<local-path>", scrubbed)
    return scrubbed


# -- study model ---------------------------------------------------------------


def _median_of(values: list):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _iqr_of(values: list):
    if len(values) < 2:
        return None
    lo = values[max(0, int(round(0.25 * (len(values) - 1))))]
    hi = values[min(len(values) - 1, int(round(0.75 * (len(values) - 1))))]
    return (lo, hi)


@dataclass(frozen=True)
class ConfigStudyAggregate:
    """Per-config statistics over one whole experiment (grouped by name)."""

    name: str
    agent: str | None
    model: str | None
    runs: int
    passes: int
    pass_rate: float | None
    interval: tuple[float, float] | None
    median_duration: float | None
    duration_iqr: tuple[float, float] | None
    median_tokens: float | None
    token_iqr: tuple[float, float] | None
    median_cost_usd: float | None
    failures: dict[str, int]
    # Distinct cost-provenance strings behind median_cost_usd (P16): lets a
    # reader see when two configs' costs come from incompatible sources.
    cost_provenances: tuple[str, ...] = ()
    # Capability denominators use *graded* (validity == "valid") cells only;
    # infra-invalid runs stay visible in ``failures``/validity counts but never
    # inflate a pass-rate denominator.
    graded: int = 0


@dataclass(frozen=True)
class Study:
    experiment_id: str
    name: str
    created_at: str
    generated_at: str
    agentbench_version: str
    repeat: int
    planned_cells: int
    interrupted: bool
    resolved_benchmarks: list[str]
    config_definitions: dict[str, dict]
    config_identities: dict[str, str]
    execution_backend: str | None
    comparison_mode: str = "system-comparison"
    comparison_warnings: list[str] = field(default_factory=list)
    validity_counts: dict[str, int] = field(default_factory=dict)
    reliability: dict[str, dict] = field(default_factory=dict)
    partial_scores: dict[str, dict] = field(default_factory=dict)
    behavior: dict[str, dict] = field(default_factory=dict)
    total_runs: int = 0
    unrun_cells: list[dict] = field(default_factory=list)
    aggregates: list[ConfigStudyAggregate] = field(default_factory=list)
    paired: list[dict] = field(default_factory=list)
    per_benchmark: dict[str, dict[str, dict]] = field(default_factory=dict)
    saturation: list[BenchmarkSaturation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def _config_label(definition: dict | None, fallback_rows: list[dict]) -> str:
    """Human-readable identity for one configuration, from persisted evidence."""
    parts: list[str] = []
    if definition:
        agent = definition.get("agent") or {}
        parts.append(str(agent.get("type") or "?"))
        model = agent.get("model")
        if model:
            parts.append(str(model))
        if agent.get("provider"):
            parts.append(f"provider={agent['provider']}")
        if agent.get("reasoning"):
            parts.append(f"reasoning={agent['reasoning']}")
    else:  # older manifests: fall back to what the DB recorded
        agent_types = sorted({str(r.get("agent")) for r in fallback_rows})
        models = sorted({str(m) for m in (r.get("model") for r in fallback_rows) if m})
        parts.extend(agent_types)
        parts.extend(models)
    return " / ".join(parts) or "unknown"


def build_study(manifest: ExperimentManifest, rows: list[dict]) -> Study:
    configs = manifest.config_definitions or {}

    def config_names() -> list[str]:
        names = set(configs) | {str(r.get("config_name")) for r in rows if r.get("config_name")}
        return sorted(names)

    non_graded_statuses = {"setup_failed", "invalid_result"}

    def _graded(rows_subset: list[dict]) -> list[dict]:
        """Capability evidence: validity == "valid" cells only (P41).

        Historical rows without a validity field grade as valid. Cells whose
        OUTCOME already excludes capability measurement — setup failures and
        invalid results — never enter the denominator, even when a migrated
        index normalized their missing validity to ``valid``.
        """
        return [r for r in rows_subset
                if r.get("status") not in non_graded_statuses
                and (r.get("validity") in (None, "")
                     or r.get("validity") == "valid")]

    names = config_names()
    aggregates: list[ConfigStudyAggregate] = []
    for name in names:
        subset = [r for r in rows if r.get("config_name") == name]
        if not subset:
            continue
        definition = configs.get(name) or {}
        agent_info = definition.get("agent") or {}
        graded = _graded(subset)
        passes = sum(1 for r in graded if r.get("status") == "passed")
        durations = [float(r["duration_seconds"]) for r in graded
                     if isinstance(r.get("duration_seconds"), (int, float))]
        tokens = [int(r["total_tokens"]) for r in graded
                  if isinstance(r.get("total_tokens"), (int, float))]
        costs = [float(r["cost_usd"]) for r in graded
                 if isinstance(r.get("cost_usd"), (int, float))]
        provenances = tuple(sorted({
            str(r["cost_provenance"]) for r in graded
            if r.get("cost_provenance")
        }))
        failures: dict[str, int] = {}
        for r in subset:
            status = str(r.get("status") or "unknown")
            failures[status] = failures.get(status, 0) + 1
        rate = passes / len(graded) if graded else None
        from agentbench.aggregate import wilson_interval

        aggregates.append(ConfigStudyAggregate(
            name=name,
            agent=agent_info.get("type"),
            model=agent_info.get("model"),
            runs=len(subset), passes=passes,
            graded=len(graded),
            pass_rate=rate,
            interval=wilson_interval(passes, len(graded)),
            median_duration=_median_of(durations), duration_iqr=_iqr_of(sorted(durations)),
            median_tokens=_median_of(tokens), token_iqr=_iqr_of(sorted(tokens)),
            median_cost_usd=_median_of(costs), failures=failures,
            cost_provenances=provenances,
        ))

    per_benchmark: dict[str, dict[str, dict]] = {}
    for bench in manifest.resolved_benchmarks or []:
        per_benchmark[bench] = {}
    for bench in per_benchmark:
        for name in names:
            subset = [r for r in rows if r.get("benchmark") == bench and r.get("config_name") == name]
            if not subset:
                continue
            graded = _graded(subset)
            durations = sorted(float(r["duration_seconds"]) for r in graded
                               if isinstance(r.get("duration_seconds"), (int, float)))
            tokens = sorted(int(r["total_tokens"]) for r in graded
                            if isinstance(r.get("total_tokens"), (int, float)))
            costs = sorted(float(r["cost_usd"]) for r in graded
                           if isinstance(r.get("cost_usd"), (int, float)))
            per_benchmark[bench][name] = {
                "runs": len(subset),
                "graded": len(graded),
                "passed": sum(1 for r in graded if r.get("status") == "passed"),
                "ungraded_runs": len(subset) - len(graded),
                "duration_median": _median_of(durations),
                "duration_iqr": _iqr_of(durations),
                "tokens_median": _median_of(tokens),
                "tokens_iqr": _iqr_of(tokens),
                "cost_median": _median_of(costs),
                # Mechanical outage flag: an agent_failed cell that produced
                # no tokens never reached a model, so its failure measures
                # infrastructure, not capability. Shown with a marker.
                "suspect_abort": any(
                    r.get("status") == "agent_failed" and not r.get("total_tokens")
                    for r in subset
                ),
            }

    paired: list[dict] = []
    # Pair ordering follows the experiment's DECLARED config order (YAML
    # sequence, persisted via config_definitions) — never lexical display
    # names, which can silently flip A/B when names sort differently.
    declared_order = list(manifest.config_definitions)
    ordered = [n for n in declared_order if n in {str(r.get("config_name")) for r in rows}]
    ordered += sorted(
        {str(r.get("config_name")) for r in rows} - set(declared_order)
    )
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            rows_a = _graded([r for r in rows if r.get("config_name") == ordered[i]])
            rows_b = _graded([r for r in rows if r.get("config_name") == ordered[j]])
            stats = pairwise_statistics(rows_a, rows_b)
            if stats is not None:
                stats["a"] = ordered[i]
                stats["b"] = ordered[j]
                paired.append(stats)

    limitations = [
        "Results reflect this corpus and these configurations only; they do "
        "not generalize to other tasks or workloads.",
        f"Each cell was run {manifest.repeat} time(s); small samples carry wide "
        "uncertainty (Wilson intervals are shown, not hidden).",
        "Cost/token figures come from each harness's own reporting; where "
        "measurement sources differ materially, cross-agent cost comparisons "
        "are indicative only.",
    ]
    if manifest.interrupted:
        limitations.append(
            "The experiment manifest is marked incomplete: some planned cells "
            "never ran, so totals below are smaller than the plan."
        )

    # -- v0.6 derived evidence -------------------------------------------
    validity_counts: dict[str, int] = {}
    for r in rows:
        v = str(r.get("validity") or "valid")
        validity_counts[v] = validity_counts.get(v, 0) + 1

    reliability: dict[str, dict] = {}
    partial_scores: dict[str, dict] = {}
    for name in names:
        subset = [r for r in rows if r.get("config_name") == name]
        if not subset:
            continue
        from agentbench.reliability import reliability_from_cells

        cells_by_bench: dict[str, list[bool]] = {}
        for r in _graded(subset):
            bench = str(r.get("benchmark"))
            passed = r.get("status") == "passed"
            cells_by_bench.setdefault(bench, []).append(passed)
            sc = r.get("scoring") or {}
            if isinstance(sc, dict) and sc.get("partial_score") is not None:
                entry = partial_scores.setdefault(
                    name, {"values": [], "resolved": 0, "runs": 0})
                entry["values"].append(float(sc["partial_score"]))
                entry["resolved"] += 1 if sc.get("resolved") else 0
                entry["runs"] += 1
        rel = reliability_from_cells(list(cells_by_bench.values()), k=manifest.repeat)
        reliability[name] = rel.to_dict()

    for name, entry in partial_scores.items():
        values = sorted(entry.pop("values"))
        n = len(values)
        entry["n"] = n
        entry["mean_partial_score"] = round(sum(values) / n, 4) if n else None
        if n >= 2:
            lo_i, hi_i = int(0.25 * (n - 1)), int(0.75 * (n - 1))
            entry["iqr_partial_score"] = [round(values[lo_i], 4), round(values[hi_i], 4)]
        entry["resolved_rate"] = round(entry.pop("resolved") / n, 4) if n else None

    # Cells the manifest recorded as attempted but that never produced a run
    # (e.g. workspace setup failed before persistence). They must stay visible:
    # a silent drop would turn "16/17 with one lost cell" into a clean 16/17.
    unrun_cells = [
        e for e in (manifest.completed or [])
        if not e.get("run_id")
    ]

    return Study(
        experiment_id=manifest.experiment_id,
        name=manifest.name,
        created_at=manifest.created_at,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        agentbench_version=__version__,
        repeat=manifest.repeat,
        planned_cells=manifest.planned_cells,
        interrupted=manifest.interrupted,
        resolved_benchmarks=list(manifest.resolved_benchmarks),
        config_definitions=configs,
        config_identities=dict(manifest.config_identities),
        execution_backend=manifest.execution_backend,
        comparison_mode=manifest.comparison_mode,
        comparison_warnings=list(manifest.comparison_warnings),
        validity_counts=validity_counts,
        reliability=reliability,
        partial_scores=partial_scores,
        total_runs=len(rows),
        aggregates=aggregates,
        unrun_cells=[
            {"benchmark": e.get("benchmark"), "config": e.get("config"),
             "trial": e.get("trial"), "status": e.get("status"),
             "error": redact_local_paths(str(e.get("error") or ""))}
            for e in unrun_cells
        ],
        paired=paired,
        per_benchmark=per_benchmark,
        saturation=analyze(rows),
        limitations=limitations + [
            f"Comparison mode: {manifest.comparison_mode}."
            + (" Warnings: " + "; ".join(manifest.comparison_warnings)
               if manifest.comparison_warnings else ""),
        ],
    )


# -- markdown ------------------------------------------------------------------


def _fmt_pair(bounds: tuple[float, float] | None) -> str:
    return f"[{bounds[0]*100:.0f}%–{bounds[1]*100:.0f}%]" if bounds else ""


def render_markdown(study: Study) -> str:
    out = io.StringIO()
    w = out.write
    w(f"# AgentBench study — {study.name}\n\n")
    w(f"- Experiment: `{study.experiment_id}`\n")
    w(f"- Generated: {study.generated_at} by AgentBench {study.agentbench_version}\n")
    w(f"- Created: {study.created_at}\n")
    w(f"- Backend: {study.execution_backend or 'host'} · repeats/cell: {study.repeat}"
      f" · runs recorded: {study.total_runs} of {study.planned_cells} planned cells\n")
    if study.interrupted:
        w("- **Incomplete experiment**: some planned cells never ran.\n")
    w("\n## Configurations\n\n")
    w("| config | identity | config hash |\n|---|---|---|\n")
    labels: dict[str, str] = {}
    for name in sorted(study.config_definitions):
        definition = study.config_definitions[name]
        label = _config_label(definition, [])
        labels[name] = label
        w(f"| {name} | {label} | `{study.config_identities.get(name, '')[:12]}` |\n")
    for missing in set(study.config_identities) - set(study.config_definitions):
        labels[missing] = missing
        w(f"| {missing} | (legacy manifest: see DB) | `{study.config_identities[missing][:12]}` |\n")
    w("\n")
    if study.validity_counts:
        attempted = sum(study.validity_counts.values())
        valid = study.validity_counts.get("valid", 0)
        infra = study.validity_counts.get("infra_invalid", 0)
        w("- Attempted %d runs · validly graded %d · infra-invalid %d\n"
          % (attempted, valid, infra))
    for cell in study.unrun_cells:
        err = cell.get("error") or ""
        w("- **Cell without recorded run**: %s / %s trial %s — %s%s\n"
          % (cell.get("config"), cell.get("benchmark"), cell.get("trial"),
             cell.get("status") or "no_run",
             f" (`{err}`)" if err else ""))
    mode_note = {
        "system-comparison": "complete coding systems differ (harness AND model)",
        "model-controlled": "one harness held constant; only model settings vary",
        "scaffold-controlled": ("scaffolds compared with author-declared equivalent "
                                "model configuration (not machine-verifiable)"),
    }.get(study.comparison_mode, study.comparison_mode)
    w("- **Comparison mode**: `%s` — %s\n" % (study.comparison_mode, mode_note))
    for warning in study.comparison_warnings:
        w("- ⚠ %s\n" % warning)

    w("\n## Per-config aggregate\n\n")
    w("| config | runs | passes | pass rate | Wilson 95% | median time | median tokens"
      " | median cost | cost evidence | failures |\n")
    w("|---|---|---|---|---|---|---|---|---|---|\n")
    for agg in study.aggregates:
        breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(agg.failures.items())) or "—"
        cost = f"${agg.median_cost_usd:.4f}" if agg.median_cost_usd is not None else "—"
        evidence = ", ".join(agg.cost_provenances) if agg.cost_provenances else (
            "reported" if agg.median_cost_usd is not None else "—"
        )
        rate = format_percent(agg.pass_rate)
        denom = (f" ({agg.passes}/{agg.graded} graded)"
                 if agg.graded and agg.graded != agg.runs else "")
        w(
            f"| {agg.name} | {agg.runs} | {agg.passes} | {rate}{denom} "
            f"| {_fmt_pair(agg.interval)} | {format_duration(agg.median_duration)} "
            f"| {format_count(agg.median_tokens)} | {cost} | {evidence} | {breakdown} |\n"
        )

    w("\n## Paired outcomes (matched benchmark × trial cells)\n\n")
    if not study.paired:
        w("_No two configurations share matched cells in this experiment._\n")
    for pair in study.paired:
        a, b = pair["a"], pair["b"]
        w(
            f"- **{a} vs {b}** over {pair['matched']} matched cells:"
            f" both pass {pair['both_pass']}"
            f" · {a} only {pair['a_only']}"
            f" · {b} only {pair['b_only']}"
            f" · both fail {pair['both_fail']}"
        )
        # Self-verifying marginals: these MUST equal each side's pass count
        # over the same matched cells (hard invariant from pairwise_compare).
        w(
            f"\n  → marginal check: {a} {pair['a_passes_matched']} passes"
            f" ({pair['both_pass']}+{pair['a_only']}) · "
            f"{b} {pair['b_passes_matched']} passes"
            f" ({pair['both_pass']}+{pair['b_only']})"
        )
        p = pair.get("mcnemar_p")
        if p is not None:
            w(f" · McNemar exact p={p:.3g}")
        else:
            w(" · no discordant pairs (p undefined)")
        w("\n")

    w("\n## Per-benchmark results\n\n")
    column_names = [a.name for a in study.aggregates]
    header = "| benchmark | " + " | ".join(column_names) + " |\n"
    w(header)
    w("|---" * (len(column_names) + 1) + "|\n")
    suspect_any = False
    for bench in study.resolved_benchmarks:
        cells = []
        for name in column_names:
            cell = study.per_benchmark.get(bench, {}).get(name)
            if cell:
                marker = " †" if cell.get("suspect_abort") else ""
                suspect_any = suspect_any or bool(cell.get("suspect_abort"))
                ungraded = cell.get("ungraded_runs") or 0
                cells.append(
                    f"{cell['passed']}/{cell['graded']} passed{marker}"
                    + (f" (+{ungraded} ungraded)" if ungraded else "")
                    + f" · {format_duration(cell['duration_median'])}"
                    + f" · {format_count(cell['tokens_median'])} tok"
                )
            else:
                cells.append("—")
        w(f"| {bench} | " + " | ".join(cells) + " |\n")
    if suspect_any:
        w("\n\\† At least one cell in this column failed without producing any\n")
        w("tokens — the agent never reached a model (e.g. a provider-side abort\n")
        w("or local CLI crash). Such cells measure infrastructure, not capability.\n")

    w("\n## Corpus difficulty / saturation\n\n")
    w("| benchmark | runs | classification | evidence |\n|---|---|---|---|\n")
    for sat in study.saturation:
        rate = format_percent(sat.overall_pass_rate)
        w(f"| {sat.benchmark} | {sat.total_runs} | **{sat.classification}** ({rate}) | {sat.reason} |\n")

    if study.reliability:
        w("\n## Reliability (observed repeated trials)\n\n")
        w("| config | tasks | runs | passes | pass@1 [Wilson 95%] | any-in-k | all-k | mean p* |\n")
        w("|---|---|---|---|---|---|---|---|\n")
        for name, rel in sorted(study.reliability.items()):
            k = rel.get("k")
            k_label = f"k={k}" if k else "—"
            ntk = rel.get("n_tasks_with_k")
            if ntk is not None and rel.get("n_tasks") and ntk < rel["n_tasks"]:
                k_label += f", {ntk}/{rel['n_tasks']} tasks"
            any_v = f"{rel['any_in_k']*100:.0f}% ({k_label})" if rel.get("any_in_k") is not None else "—"
            all_v = f"{rel['all_k']*100:.0f}%" if rel.get("all_k") is not None else "—"
            mean_p = f"{rel['mean_posterior_p']:.3f}" if rel.get("mean_posterior_p") is not None else "—"
            wil = "/".join(f"{v*100:.0f}%" for v in rel.get("wilson_95", (0, 1)))
            w(f"| {name} | {rel['n_tasks']} | {rel['n_runs']} | {rel['passes']} "
              f"| {rel['pass_at_1']*100:.0f}% [{wil}] | {any_v} | {all_v} | {mean_p} |\n")
        w("\n\\* mean p is a Beta(1,1) smoothed posterior mean — descriptive, not a\n")
        w("classic pass@k estimator: these are observed repeated trials.\n")

    if study.partial_scores:
        w("\n## Partial scores (non-required groups earn credit, never pass)\n\n")
        w("| config | n | mean partial | IQR | resolved rate |\n|---|---|---|---|---|\n")
        for name, ps in sorted(study.partial_scores.items()):
            iqr = (
                "–".join(f"{v:.2f}" for v in ps["iqr_partial_score"])
                if ps.get("iqr_partial_score") else "—"
            )
            w(f"| {name} | {ps['n']} | {ps['mean_partial_score']:.3f} | {iqr} "
              f"| {ps['resolved_rate']*100:.0f}% |\n")

    w("\n## Known limitations\n\n")
    for line in study.limitations:
        w(f"- {line}\n")

    w("\n## Reproduce\n\n")
    w("```text\n")
    w(f"# re-run the identical matrix (same benchmarks/configs/commits)\n")
    w(f"agentbench show <run-id>            # inspect one cell's evidence\n")
    w(f"agentbench reproduce <run-id>       # preflight + rerun one cell\n")
    w("```\n")
    return out.getvalue()


# -- html ----------------------------------------------------------------------

_HTML_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font:15px/1.55 system-ui,sans-serif;margin:2rem auto;max-width:60rem;
padding:0 1rem;color:#1a1a2e;background:#fafafa}}
h1,h2{{line-height:1.2}} table{{border-collapse:collapse;margin:.8rem 0;width:100%}}
/* Wide tables (long model names) scroll on narrow screens instead of
   breaking layout; color is never the only status signal (text labels). */
.tablewrap{{overflow-x:auto}}
th,td{{border:1px solid #ccc;padding:.35rem .5rem;text-align:left;vertical-align:top}}
th{{background:#eee}} code{{background:#f0f0f0;padding:.05rem .25rem}}
.tag{{font-weight:600}} .limit{{color:#666}}
@media print{{
  body{{margin:0;max-width:none;font-size:11pt;background:#fff}}
  .tablewrap{{overflow-x:visible}}
  a{{color:inherit;text-decoration:none}}
}}
</style></head><body>{body}<footer class="limit">Generated by AgentBench
{version} on {generated}. Mechanical report: no editorial scoring.</footer>
</body></html>"""


def _md_table_to_html(md: str) -> str:
    """Render the markdown tables/lists this generator emits into simple HTML."""
    lines = md.splitlines()
    out: list[str] = []
    in_table = False
    open_list = False

    def close_list():
        nonlocal open_list
        if open_list:
            out.append("</ul>")
            open_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            close_list()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                continue
            if not in_table:
                out.append('<div class="tablewrap"><table>')
                in_table = True
                header_row = True
            role = "th" if header_row else "td"
            header_row = False
            out.append("<tr>" + "".join(
                f"<{role}>{html.escape(c.replace('**', ''))}</{role}>"
                for c in cells
            ) + "</tr>")
            continue
        if in_table:
            out.append("</table></div>")
            in_table = False
        if stripped.startswith("# "):
            close_list()
            out.append(f"<h1>{html.escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            close_list()
            out.append(f"<h2>{html.escape(stripped[3:])}</h2>")
        elif stripped.startswith("- "):
            if not open_list:
                out.append("<ul>")
                open_list = True
            content = html.escape(stripped[2:])
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(r"`(.+?)`", r"<code>\1</code>", content)
            out.append(f"<li>{content}</li>")
        elif stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 2:
            close_list()
            out.append(f"<p><em>{html.escape(stripped.strip('_'))}</em></p>")
        elif stripped.startswith("```"):
            close_list()
        elif stripped:
            close_list()
            content = html.escape(stripped)
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            out.append(f"<p>{content}</p>")
    if in_table:
        out.append("</table></div>")
    close_list()
    return "\n".join(out)


def render_html(study: Study) -> str:
    markdown = render_markdown(study)
    title = f"AgentBench study — {study.name}"
    body = _md_table_to_html(markdown)
    return _HTML_SHELL.format(
        title=html.escape(title), body=body,
        version=html.escape(study.agentbench_version),
        generated=html.escape(study.generated_at),
    )


# -- public bundle ---------------------------------------------------------------

_BUNDLE_EXCLUDED_ROW_KEYS = {"result_dir"}  # local filesystem paths never leave


def export_bundle(study: Study, manifest: ExperimentManifest, rows: list[dict],
                  dest: Path, *, markdown: str | None = None,
                  html_text: str | None = None) -> list[Path]:
    """Write the safe public bundle; returns files written.

    Only non-secret, path-free evidence ships: manifest identities, flattened
    metrics, reports. Raw agent logs, environment captures, prompts, and local
    paths are excluded by construction, and every written file passes the
    secret scanner before it lands.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    def write(name: str, text: str) -> None:
        leaks = scan_for_secrets(text)
        if leaks:
            raise SecretLeakError(f"{name}: possible credential ({', '.join(leaks)})")
        target = dest / name
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)

    # Manifests may carry cell error strings containing local filesystem
    # paths (setup failures); scrub them before public shipment. The raw
    # manifest under results/experiments/ keeps full detail.
    shipped_manifest = manifest.model_dump(mode="json")
    for entry in shipped_manifest.get("completed", []):
        if isinstance(entry, dict) and entry.get("error"):
            entry["error"] = redact_local_paths(str(entry["error"]))
    write("experiment.json", json.dumps(shipped_manifest, indent=2))

    write("README.md",
          f"# AgentBench study bundle — {study.name}\n\n"
          f"Experiment `{study.experiment_id}`, AgentBench {study.agentbench_version},"
          f" generated {study.generated_at}.\n\nSee report.md for the full study.\n")
    definitions = {
        "benchmark_identities": study.resolved_benchmarks,
        "config_identities": study.config_identities,
        "config_definitions": study.config_definitions,
        "execution_backend": study.execution_backend,
    }
    write("identities.json", json.dumps(definitions, indent=2, sort_keys=True))

    buf = io.StringIO()
    fieldnames = [
        "run_id", "experiment_id", "benchmark", "config_name", "agent", "model",
        "status", "trial", "duration_seconds", "total_tokens", "cost_usd",
        "cost_provenance", "validity",
        "files_changed", "insertions", "deletions", "execution_backend",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        clean = {k: row.get(k) for k in fieldnames}
        writer.writerow(clean)
    write("metrics.csv", buf.getvalue())

    write("report.md", markdown if markdown is not None else render_markdown(study))
    write("report.html", html_text if html_text is not None else render_html(study))

    # P38: integrity hashes over every shipped file (mutation detection,
    # not authentication). Written last so it covers the whole bundle.
    import hashlib as _hashlib

    hashes = {
        Path(f).name: _hashlib.sha256(Path(f).read_bytes()).hexdigest()
        for f in written
    }
    write("hashes.json", json.dumps(hashes, indent=2, sort_keys=True))
    return written
