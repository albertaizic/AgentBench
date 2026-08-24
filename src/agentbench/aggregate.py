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


def mcnemar_exact_p(b_only: int, a_only: int) -> float | None:
    """Two-sided exact McNemar p-value from discordant pair counts.

    ``a_only`` = pairs where A passed and B failed; ``b_only`` = the reverse.
    Under the null hypothesis each discordant pair favors either side with
    probability ½, so the tail probability is a two-sided binomial test.
    Returns None when there are no discordant pairs (no evidence either way).
    """
    discordant = b_only + a_only
    if discordant == 0:
        return None
    tail = sum(math.comb(discordant, i) for i in range(0, min(b_only, a_only) + 1))
    return min(1.0, 2.0 * tail / (2.0 ** discordant))


def pareto_frontier(
    candidates: list[dict],
    *,
    label_key: str = "label",
) -> list[str]:
    """Labels on the quality/cost/speed Pareto frontier.

    Each candidate dict needs numeric ``pass_rate``, ``median_duration``, and
    ``avg_cost_usd`` (missing values rank as worst). A candidate is on the
    frontier when no other candidate is at least as good on every dimension
    and strictly better on one. Deliberately NOT a combined score: trade-offs
    stay visible.
    """

    def point(c: dict) -> tuple[float, float, float]:
        rate = c.get("pass_rate")
        duration = c.get("median_duration")
        cost = c.get("avg_cost_usd")
        return (
            float(rate) if _num(rate) else -1.0,          # higher better
            float(duration) if _num(duration) else math.inf,   # lower better
            float(cost) if _num(cost) else math.inf,      # lower better
        )

    points = {c[label_key]: point(c) for c in candidates if c.get(label_key)}
    frontier: list[str] = []
    for label, (rate, duration, cost) in points.items():
        dominated = False
        for other_label, (o_rate, o_duration, o_cost) in points.items():
            if other_label == label:
                continue
            if (
                o_rate >= rate and o_duration <= duration and o_cost <= cost
                and (o_rate > rate or o_duration < duration or o_cost < cost)
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(label)
    return sorted(frontier)


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


def pairwise_statistics(rows_a: list[dict], rows_b: list[dict]) -> dict | None:
    """Paired comparison between two configs over identical cells.

    Extends :func:`pairwise_compare` with an exact McNemar p-value over the
    discordant pairs and duration/token/cost summaries computed ONLY among
    mutual passes — a failed run's missing metrics must never masquerade as a
    fast/cheap one. Unmatched cells are excluded, never compared as pairs.
    """
    def key(row: dict):
        return (row.get("benchmark"), row.get("trial"))

    map_a = {key(r): r for r in rows_a}
    map_b = {key(r): r for r in rows_b}
    shared = sorted(set(map_a) & set(map_b))
    if not shared:
        return None

    result = pairwise_compare(rows_a, rows_b)
    assert result is not None  # shared is non-empty
    result["mcnemar_p"] = mcnemar_exact_p(result["b_only"], result["a_only"])

    for prefix, mapping in (("a", map_a), ("b", map_b)):
        durations, tokens, costs = [], [], []
        for cell in shared:
            if map_a[cell].get("status") == "passed" and map_b[cell].get("status") == "passed":
                row = mapping[cell]
                if _is_number(row.get("duration_seconds")):
                    durations.append(float(row["duration_seconds"]))
                if _is_number(row.get("total_tokens")):
                    tokens.append(int(row["total_tokens"]))
                if _is_number(row.get("cost_usd")):
                    costs.append(float(row["cost_usd"]))
        result[f"{prefix}_median_duration_mutual_pass"] = (
            statistics.median(durations) if durations else None
        )
        result[f"{prefix}_median_tokens_mutual_pass"] = (
            int(statistics.median(tokens)) if tokens else None
        )
        result[f"{prefix}_median_cost_usd_mutual_pass"] = (
            float(statistics.median(costs)) if costs else None
        )
    return result


def _num(value) -> bool:
    return _is_number(value)


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
    violation_counts: list = field(default_factory=list)  # protected-path hits per run

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
    def duration_iqr(self) -> tuple[float, float] | None:
        p25, p75 = self.p25_duration, self.p75_duration
        return (p25, p75) if p25 is not None and p75 is not None else None

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
    def token_iqr(self) -> tuple[float, float] | None:
        p25, p75 = quantile(self.total_tokens, 0.25), quantile(self.total_tokens, 0.75)
        return (p25, p75) if p25 is not None and p75 is not None else None

    @property
    def avg_cost_usd(self) -> float | None:
        return statistics.fmean(self.costs) if self.costs else None

    @property
    def median_cost_usd(self) -> float | None:
        return statistics.median(self.costs) if self.costs else None

    @property
    def protected_violation_rate(self) -> float | None:
        if not self.violation_counts:
            return None
        flagged = sum(1 for n in self.violation_counts if n > 0)
        return flagged / len(self.violation_counts)

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
        if _is_number(row.get("violation_count")):
            group.violation_counts.append(int(row["violation_count"]))
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
