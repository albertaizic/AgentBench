"""Reliability metrics for repeated agent trials (v0.6 P18) and the
logistic time-horizon fit (P17).

Deliberately NOT the Codex-style combinatorial pass@k estimator: that
estimator exists for sampling k draws without repetition data. AgentBench
cells ARE the repetitions, so everything here is directly observed and
labeled as such:

* ``pass@1``     pooled observed pass rate with Wilson interval;
* ``any_in_k``   share of tasks solved at least once within k trials;
* ``all_k``      share of tasks solved in EVERY trial (stability);
* ``mean_p``     Beta(1,1) posterior mean success probability with a
                 bootstrap interval.

Every estimate carries N; callers must display it. The horizon fit is
descriptive ONLY and explicitly not comparable to METR's published horizons
(different tasks, harnesses, estimated-not-measured human times).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence

from agentbench.aggregate import wilson_interval


@dataclass(frozen=True)
class ReliabilityReport:
    n_runs: int
    n_tasks: int
    passes: int
    pass_at_1: float
    wilson: tuple[float, float]
    any_in_k: float | None
    all_k: float | None
    k: int | None
    mean_p: float | None
    bootstrap_ci: tuple[float, float] | None
    n_tasks_with_k: int | None = None

    def to_dict(self) -> dict:
        d = {
            "n_runs": self.n_runs,
            "n_tasks": self.n_tasks,
            "passes": self.passes,
            "pass_at_1": round(self.pass_at_1, 4),
            "wilson_95": [round(self.wilson[0], 4), round(self.wilson[1], 4)],
        }
        if self.k is not None:
            d.update({
                "k": self.k,
                # Denominator transparency: any_in_k/all_k cover only tasks
                # with the full k observed trials (lost cells are excluded,
                # never silently folded into a smaller k).
                "n_tasks_with_k": self.n_tasks_with_k,
                "any_in_k": round(self.any_in_k, 4),
                "all_k": round(self.all_k, 4),
            })
        if self.mean_p is not None:
            d["mean_posterior_p"] = round(self.mean_p, 4)
        if self.bootstrap_ci is not None:
            d["bootstrap_95"] = [round(self.bootstrap_ci[0], 4),
                                 round(self.bootstrap_ci[1], 4)]
        return d


def reliability_from_cells(
    cells: Sequence[Sequence[bool]],
    *,
    k: int = 3,
    bootstrap_iterations: int = 2000,
    seed: int = 12345,
) -> ReliabilityReport:
    """Cells are per-task lists of trial outcomes (True = passed)."""
    flat = [bool(t) for cell in cells for t in cell]
    n = len(flat)
    passes = sum(flat)
    if n:
        low, high = wilson_interval(passes, n)
    else:
        low, high = 0.0, 0.0
    tasks_with_k = [list(cell)[:k] for cell in cells if len(cell) >= k]

    any_in_k = all_k = None
    if tasks_with_k:
        any_hits = sum(1 for c in tasks_with_k if any(c))
        all_hits = sum(1 for c in tasks_with_k if all(c))
        any_in_k = any_hits / len(tasks_with_k)
        all_k = all_hits / len(tasks_with_k)

    mean_p = ci = None
    if n:
        mean_p = (passes + 1) / (n + 2)  # Beta(1,1) posterior mean
        rng = random.Random(seed)
        values = [float(v) for v in flat]
        stats = []
        for _ in range(bootstrap_iterations):
            sample = [rng.choice(values) for _ in values]
            stats.append(sum(sample) / len(sample))
        stats.sort()
        lo = stats[max(0, int(0.025 * len(stats)) - 1)]
        hi = stats[min(len(stats) - 1, int(0.975 * len(stats)))]
        ci = (lo, hi)

    return ReliabilityReport(
        n_runs=n,
        n_tasks=len([c for c in cells if c]),
        passes=passes,
        pass_at_1=(passes / n) if n else 0.0,
        wilson=(low, high),
        any_in_k=any_in_k,
        all_k=all_k,
        k=k if tasks_with_k else None,
        mean_p=mean_p,
        bootstrap_ci=ci,
        n_tasks_with_k=len(tasks_with_k) if tasks_with_k else None,
    )


# -- time horizon (P17) ---------------------------------------------------------


def _sigma(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)




def fit_logistic_horizon(
    points: Sequence[tuple[float, int, int]],
    *,
    iterations: int = 100,
) -> dict:
    """Fit P(success)=sigmoid(a+b*log2(minutes)); returns H50/H80."""
    xs = [math.log2(max(m, 0.5)) for m, _, _ in points]
    ks = [p for _, p, _ in points]
    ns = [t for _, _, t in points]
    # Ridge damping + step clamping: rounded trial counts make perfectly
    # separable data common, which sends unregularized MLE to infinity.
    lam = 1e-3
    a, b = 0.0, 0.5
    for _ in range(iterations):
        grad_a = grad_b = 0.0
        h11 = h12 = h22 = 0.0
        for x, k_i, n_i in zip(xs, ks, ns):
            eta = a + b * x
            p_hat = _sigma(eta)
            w = max(n_i * p_hat * (1 - p_hat), 1e-9)
            resid = k_i - n_i * p_hat
            grad_a += resid
            grad_b += resid * x
            h11 += w
            h12 += w * x
            h22 += w * x * x
        grad_a += lam * a
        grad_b += lam * b
        h11 += lam
        h22 += lam
        det = h11 * h22 - h12 * h12
        if abs(det) < 1e-12:
            break
        delta_a = (h22 * grad_a - h12 * grad_b) / det
        delta_b = (h11 * grad_b - h12 * grad_a) / det
        # Trust region: never take a step that more than doubles |b|.
        max_step = max(2.0, 2.0 * abs(b))
        scale = max(abs(delta_a), abs(delta_b))
        if scale > max_step:
            delta_a *= max_step / scale
            delta_b *= max_step / scale
        a += delta_a
        b += delta_b
        if abs(delta_a) < 1e-8 and abs(delta_b) < 1e-8:
            break
    if b == 0:
        return {"ok": False, "reason": "degenerate slope"}
    target = math.log(0.8 / 0.2)
    return {
        "ok": True,
        "intercept": round(a, 4),
        "slope": round(b, 4),
        "h50_minutes": round(2 ** (-a / b), 1),
        "h80_minutes": round(2 ** ((target - a) / b), 1),
        "n_tasks": len(points),
        "n_runs": sum(ns),
    }


def horizon_with_bootstrap(
    points: Sequence[tuple[float, int, int]],
    *,
    iterations: int = 500,
    seed: int = 777,
    min_tasks: int = 8,
) -> dict:
    usable = [(m, p, t) for m, p, t in points if t > 0]
    n_tasks = len(usable)
    if n_tasks < min_tasks:
        return {"ok": False,
                "reason": f"insufficient data: {n_tasks} tasks with human-time "
                          f"metadata (need >= {min_tasks})",
                "n_tasks": n_tasks}
    spread = [(p / t) for _, p, t in usable]
    if max(spread) - min(spread) < 1e-6:
        return {"ok": False,
                "reason": "all tasks have identical success rates; no horizon signal",
                "n_tasks": n_tasks}

    base = fit_logistic_horizon(usable)
    if not base.get("ok"):
        return base
    rng = random.Random(seed)
    h50s: list[float] = []
    h80s: list[float] = []
    for _ in range(iterations):
        sample = [usable[rng.randrange(n_tasks)] for _ in range(n_tasks)]
        fit = fit_logistic_horizon(sample)
        if fit.get("ok"):
            h50s.append(float(fit["h50_minutes"]))
            h80s.append(float(fit["h80_minutes"]))
    out = dict(base)
    if len(h50s) >= 50:
        h50s.sort()
        h80s.sort()
        cut = max(1, len(h50s) // 20)
        out["h50_ci_minutes"] = [h50s[cut], h50s[-cut]]
        out["h80_ci_minutes"] = [h80s[cut], h80s[-cut]]
    out["contributing_tasks"] = [
        {"minutes": m, "passes": p, "trials": t} for m, p, t in usable
    ]
    return out


__all__ = [
    "ReliabilityReport", "reliability_from_cells",
    "fit_logistic_horizon", "horizon_with_bootstrap",
]
