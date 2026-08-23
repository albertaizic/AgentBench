"""Aggregation math for comparison views.

Pure functions over plain row dicts (as returned by
:mod:`agentbench.storage`); no SQL and no agent-specific knowledge.
Statistics are stdlib-only: Wilson score intervals use the closed-form
normal approximation (z = 1.96) with explicit small-sample labeling.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

WILSON_Z_95 = 1.959963984540054


def wilson_interval(
    successes: int, total: int, z: float = WILSON_Z_95
) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion.

    Returns None when total == 0. Well-behaved for small samples (unlike the
    naive normal approximation): intervals stay inside [0, 1].
    """
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - spread), min(1.0, center + spread)


def quantile(values: list[float], q: float) -> float | None:
    """Linear-interpolated quantile on unsorted input; None when empty."""
    usable = sorted(v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    if not usable:
        return None
    if len(usable) == 1:
        return float(usable[0])
    position = q * (len(usable) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(usable[lower])
    fraction = position - lower
    return float(usable[lower] * (1 - fraction) + usable[upper] * fraction)


def failure_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def pairwise_compare(
    rows_a: list[dict], rows_b: list[dict]
) -> dict[str, int] | None:
    """Paired outcome counts between two configs over identical cells.

    Rows are matched on ``(benchmark, trial)``; unmatched rows are ignored so
    incomplete experiments never fabricate comparisons.
    """
    def key(row: dict):
        return (row.get("benchmark"), row.get("trial"))

    map_a = {key(r): r for r in rows_a}
    map_b = {key(r): r for r in rows_b}
    shared = set(map_a) & set(map_b)
    if not shared:
        return None
    counts = {"both_pass": 0, "a_only": 0, "b_only": 0, "both_fail": 0}
    for cell in shared:
        a_passed = map_a[cell].get("status") == "passed"
        b_passed = map_b[cell].get("status") == "passed"
        if a_passed and b_passed:
            counts["both_pass"] += 1
        elif a_passed:
            counts["a_only"] += 1
        elif b_passed:
            counts["b_only"] += 1
        else:
            counts["both_fail"] += 1
    counts["matched"] = len(shared)
    return counts


@dataclass
class ConfigAggregate:
    """Mutable accumulator: group statistics are filled in row by row."""

    config_hash: str
    label: str  # "Agent/Model" display label; model may be unknown
    runs: int = 0
    passes: int = 0
    resolved_commits: set = field(default_factory=set)
    durations: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    lines_changed: list = field(default_factory=list)
    total_tokens: list = field(default_factory=list)
    costs: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    backends: set = field(default_factory=set)
    image_ids: set = field(default_factory=set)

    @property
    def pass_rate(self) -> float | None:
        return self.passes / self.runs if self.runs else None

    @property
    def pass_rate_interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.passes, self.runs)

    @property
    def median_duration(self) -> float | None:
        return statistics.median(self.durations) if self.durations else None

    @property
    def p25_duration(self) -> float | None:
        return quantile(self.durations, 0.25)

    @property
    def p75_duration(self) -> float | None:
        return quantile(self.durations, 0.75)

    @property
    def median_files_changed(self) -> float | None:
        return statistics.median(self.files_changed) if self.files_changed else None

    @property
    def median_lines_changed(self) -> float | None:
        return statistics.median(self.lines_changed) if self.lines_changed else None

    @property
    def median_total_tokens(self) -> float | None:
        return statistics.median(self.total_tokens) if self.total_tokens else None

    @property
    def avg_cost_usd(self) -> float | None:
        return statistics.fmean(self.costs) if self.costs else None

    @property
    def failure_breakdown(self) -> dict[str, int]:
        return failure_counts([{"status": s} for s in self.statuses])


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def aggregate_by_config(rows: list[dict]) -> list[ConfigAggregate]:
    """Group run rows by benchmark configuration identity.

    Grouping key is ``config_hash`` — runs from materially different
    configurations (e.g. different commits) are never merged; callers can
    surface the heterogeneity via ``resolved_commits``.
    """
    groups: dict[str, ConfigAggregate] = {}
    for row in rows:
        config_hash = str(row.get("config_hash") or "unknown")
        group = groups.get(config_hash)
        if group is None:
            model = row.get("model")
            label = str(row.get("agent") or "?") + ("/" + str(model) if model else "")
            group = groups[config_hash] = ConfigAggregate(
                config_hash=config_hash, label=label
            )
        passed = row.get("status") == "passed"
        group.runs += 1
        group.passes += 1 if passed else 0
        if row.get("status"):
            group.statuses.append(str(row["status"]))
        if row.get("execution_backend"):
            group.backends.add(str(row["execution_backend"]))
        if row.get("image_id"):
            group.image_ids.add(str(row["image_id"]))
        if row.get("resolved_commit"):
            group.resolved_commits.add(str(row["resolved_commit"]))
        if _is_number(row.get("duration_seconds")):
            group.durations.append(float(row["duration_seconds"]))
        if _is_number(row.get("files_changed")):
            group.files_changed.append(int(row["files_changed"]))
        ins, dels = row.get("insertions"), row.get("deletions")
        if _is_number(ins) and _is_number(dels):
            group.lines_changed.append(int(ins) + int(dels))
        if _is_number(row.get("total_tokens")):
            group.total_tokens.append(int(row["total_tokens"]))
        if _is_number(row.get("cost_usd")):
            group.costs.append(float(row["cost_usd"]))
    return sorted(groups.values(), key=lambda g: (-g.runs, g.label))


def format_duration(seconds: float | None) -> str:
    """Human duration like ``38.2s`` or ``4:12``."""
    if seconds is None:
        return "—"
    seconds = round(float(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{rem:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{rem:02d}"


def format_count(value: float | int | None, suffix: str = "") -> str:
    """Compact human count like ``142k`` or ``1.2m``."""
    if value is None:
        return "—"
    value = float(value)
    for threshold, unit in ((1_000_000, "m"), (1_000, "k")):
        if value >= threshold:
            formatted = f"{value / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{formatted}{unit}{suffix}"
    rounded = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{rounded}{suffix}"


def format_percent(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f}%"
