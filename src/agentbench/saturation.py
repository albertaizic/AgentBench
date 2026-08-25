"""Corpus difficulty and saturation analysis from real run evidence.

Classifies each benchmark mechanically from persisted outcomes — never from
intuition, and never rewriting difficulty metadata from a handful of trials.
The vocabulary is deliberately small:

* ``uncalibrated``      – not enough real-agent evidence yet (or no spread signal)
* ``discriminating``    – configs measurably separate on this benchmark
* ``likely_saturated``  – every measured config passes essentially always
* ``likely_too_hard``   – no measured config ever passes

Classification requires a minimum number of runs per benchmark; below that,
everything stays ``uncalibrated`` regardless of how the few results look.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentbench.aggregate import wilson_interval

#: Minimum real-agent runs per benchmark before any classification is honest.
MIN_RUNS_DEFAULT = 6

#: Config pass-rate gap that counts as genuine discrimination between agents.
DISCRIMINATION_GAP = 0.25

#: Per-config pass rate at/above which a config is considered saturated.
SATURATION_RATE = 0.9

CLASS_UNCALIBRATED = "uncalibrated"
CLASS_DISCRIMINATING = "discriminating"
CLASS_SATURATED = "likely_saturated"
CLASS_TOO_HARD = "likely_too_hard"

_CLASSES = (CLASS_UNCALIBRATED, CLASS_DISCRIMINATING, CLASS_SATURATED, CLASS_TOO_HARD)


@dataclass(frozen=True)
class ConfigSaturation:
    """One agent configuration's record on one benchmark."""

    label: str
    runs: int
    passes: int
    pass_rate: float | None
    interval: tuple[float, float] | None
    median_duration: float | None
    median_tokens: float | None
    median_cost_usd: float | None
    median_diff_lines: int | None


@dataclass(frozen=True)
class BenchmarkSaturation:
    """Evidence-based difficulty verdict for one benchmark."""

    benchmark: str
    total_runs: int
    classification: str
    reason: str
    overall_pass_rate: float | None
    configs: list[ConfigSaturation] = field(default_factory=list)

    @property
    def best_pass_rate(self) -> float | None:
        rates = [c.pass_rate for c in self.configs if c.pass_rate is not None]
        return max(rates) if rates else None

    @property
    def worst_pass_rate(self) -> float | None:
        rates = [c.pass_rate for c in self.configs if c.pass_rate is not None]
        return min(rates) if rates else None


def _passed(row: dict) -> bool:
    return row.get("status") == "passed"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _config_summary(label: str, rows: list[dict]) -> ConfigSaturation:
    runs = len(rows)
    passes = sum(1 for r in rows if _passed(r))
    durations = [r["duration_seconds"] for r in rows if isinstance(r.get("duration_seconds"), (int, float))]
    tokens = [r["total_tokens"] for r in rows if isinstance(r.get("total_tokens"), (int, float))]
    costs = [r["cost_usd"] for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    diffs = [
        (r.get("insertions") or 0) + (r.get("deletions") or 0)
        for r in rows
        if isinstance(r.get("insertions"), (int, float)) or isinstance(r.get("deletions"), (int, float))
    ]
    rate = passes / runs if runs else None
    return ConfigSaturation(
        label=label,
        runs=runs,
        passes=passes,
        pass_rate=rate,
        interval=wilson_interval(passes, runs) if runs else None,
        median_duration=_median(durations),
        median_tokens=_median(tokens),
        median_cost_usd=_median(costs),
        median_diff_lines=int(_median([float(d) for d in diffs])) if diffs else None,
    )


def analyze_benchmark(benchmark: str, rows: list[dict], *, min_runs: int = MIN_RUNS_DEFAULT) -> BenchmarkSaturation:
    total = len(rows)
    by_config: dict[str, list[dict]] = {}
    for row in rows:
        label = str(row.get("config_name") or row.get("agent") or "default")
        by_config.setdefault(label, []).append(row)
    summaries = [_config_summary(label, group) for label, group in sorted(by_config.items())]
    passes = sum(1 for r in rows if _passed(r))
    overall = passes / total if total else None

    def verdict(classification: str, reason: str) -> BenchmarkSaturation:
        return BenchmarkSaturation(
            benchmark=benchmark, total_runs=total,
            classification=classification, reason=reason,
            overall_pass_rate=overall, configs=summaries,
        )

    if total < min_runs:
        return verdict(CLASS_UNCALIBRATED, f"only {total} run(s); classification needs >= {min_runs}")

    # Every config must itself carry enough evidence to be trusted as "always
    # passing" or "never passing"; single-run configs cannot anchor a verdict.
    trusted = [s for s in summaries if s.runs >= max(2, min_runs // 3)]
    if not trusted:
        return verdict(CLASS_UNCALIBRATED, "no configuration has enough paired evidence")

    never = all(s.passes == 0 for s in trusted)
    saturated = [s for s in trusted if s.pass_rate is not None and s.pass_rate >= SATURATION_RATE]
    rates = [s.pass_rate for s in trusted if s.pass_rate is not None]

    if never:
        return verdict(CLASS_TOO_HARD, f"0/{total} passed across {len(trusted)} config(s)")
    if len(trusted) >= 2 and len(saturated) == len(trusted):
        return verdict(
            CLASS_SATURATED,
            f"all {len(trusted)} trusted config(s) pass at >= {SATURATION_RATE:.0%} ({total} runs)",
        )
    if len(rates) >= 2 and max(rates) - min(rates) >= DISCRIMINATION_GAP:
        return verdict(
            CLASS_DISCRIMINATING,
            f"config pass-rate spread {min(rates):.0%}-{max(rates):.0%} "
            f">= {DISCRIMINATION_GAP:.0%} across {total} runs",
        )
    return verdict(
        CLASS_UNCALIBRATED,
        f"{total} runs, pass-rate spread under {DISCRIMINATION_GAP:.0%}; "
        "no saturation/difficulty signal yet",
    )


def analyze(rows: list[dict], *, min_runs: int = MIN_RUNS_DEFAULT) -> list[BenchmarkSaturation]:
    """Classify every benchmark present in *rows* (persisted run records)."""
    by_benchmark: dict[str, list[dict]] = {}
    for row in rows:
        name = str(row.get("benchmark") or "")
        if name:
            by_benchmark.setdefault(name, []).append(row)
    return [
        analyze_benchmark(name, group, min_runs=min_runs)
        for name, group in sorted(by_benchmark.items())
    ]
