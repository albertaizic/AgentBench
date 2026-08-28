"""Scorer abstraction, partial credit, and offline rescoring (v0.6 P8-P10).

Binary resolution stays the primary backward-compatible metric: a run is
``resolved`` (legacy ``passed``) only when every *required* scorer exits 0
and all hidden evaluators pass. Partial credit is additive evidence on top:

    partial_score = Σ_g weight_g × fraction_g / Σ_g weight_g

where ``fraction_g`` is the mean normalized score of the scorers mapped to
group *g*. A broken solution can never pass through partial credit: groups
marked required gate the binary result exactly as before.

Deterministic only. There is deliberately no LLM-judge path here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SCORE_MARKER = "agentbench-score:"
_MARKER_RE = re.compile(re.escape(SCORE_MARKER) + r"\s*(-?[0-9]*\.?[0-9]+)")


@dataclass(frozen=True)
class ScorerSpecView:
    """Adapter view so legacy Evaluations and new Scorers share one path."""

    id: str
    command: str
    score_type: str = "binary"
    groups: tuple[str, ...] = ("default",)
    required: bool = True
    max_count: int | None = None


@dataclass
class ScorerResult:
    id: str
    groups: list[str]
    score_type: str
    passed: bool
    exit_code: int | None
    score: float | None = None
    raw_count: float | None = None
    weight: float = 1.0
    required: bool = True
    max_count: int | None = None
    duration_seconds: float | None = None


@dataclass
class ScoringSummary:
    resolved: bool
    partial_score: float | None
    scorers: list[ScorerResult] = field(default_factory=list)
    group_fractions: dict[str, float] = field(default_factory=dict)
    scorer_set_hash: str | None = None
    # Declared groups with no executed scorers; excluded from the partial
    # score instead of silently counting as 0.0.
    uncovered_groups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "partial_score": self.partial_score,
            "group_fractions": {k: round(v, 4) for k, v in self.group_fractions.items()},
            "scorers": [
                {
                    "id": s.id, "groups": s.groups, "score_type": s.score_type,
                    "score": s.score, "raw_count": s.raw_count, "passed": s.passed,
                    "exit_code": s.exit_code, "weight": s.weight,
                    "required": s.required, "duration_seconds": s.duration_seconds,
                }
                for s in self.scorers
            ],
            "scorer_set_hash": self.scorer_set_hash,
            "uncovered_groups": list(self.uncovered_groups),
        }


def parse_embedded_score(stdout: str | None, score_type: str) -> float | None:
    """Extract the final ``agentbench-score:`` marker from evaluator output."""
    if not stdout or score_type == "binary":
        return None
    best: float | None = None
    for line in stdout.splitlines():
        m = _MARKER_RE.search(line)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            best = max(0.0, min(1.0, value))
    return best


def normalize_score(result: ScorerResult) -> float:
    if result.score_type == "binary":
        return 1.0 if result.passed else 0.0
    if result.score_type == "count" and result.max_count:
        # Raw counts normalize against the declared maximum; the clamped
        # marker score must never win here (a count of 3 is not "1.0").
        raw = result.raw_count if result.raw_count is not None else (
            1.0 if result.passed else 0.0
        )
        return max(0.0, min(1.0, raw / result.max_count))
    if result.score is not None:
        return result.score
    # A non-binary scorer without parseable output contributes its pass/fail.
    return 1.0 if result.passed else 0.0


def parse_raw_marker(stdout: str | None) -> float | None:
    """Unclamped last-marker value; used by ``count`` scorers."""
    if not stdout:
        return None
    best: float | None = None
    for line in stdout.splitlines():
        m = _MARKER_RE.search(line)
        if m:
            try:
                best = float(m.group(1))
            except ValueError:
                continue
    return best


def _group_field(declared_groups: dict | None, group: str, field_name: str,
                 default):
    """Read weight/required from ScoringGroup models or plain dicts."""
    entry = (declared_groups or {}).get(group)
    if entry is None:
        return default
    if hasattr(entry, field_name):        # pydantic ScoringGroup model
        return getattr(entry, field_name)
    if isinstance(entry, dict):
        return entry.get(field_name, default)
    return default


def compute_scoring(
    specs: list[ScorerSpecView],
    outcomes: list[Any],
    *,
    declared_groups: dict[str, dict] | None = None,
) -> ScoringSummary:
    """Mechanically derive resolved + partial from executed scorer outcomes."""
    by_name: dict[str, Any] = {}
    for out in outcomes:
        by_name.setdefault(getattr(out, "name", None), out)

    results: list[ScorerResult] = []
    for sp in specs:
        out = by_name.get(sp.id)
        passed = bool(out.passed) if out is not None else False
        raw_stdout = getattr(out, "stdout", None)
        score_value = parse_embedded_score(raw_stdout, sp.score_type)
        if sp.score_type == "count":
            # Counts are real quantities (e.g. "3 of 6 mutants killed") and
            # must not be clamped into [0,1] by the marker parser.
            raw_count = parse_raw_marker(raw_stdout)
        else:
            raw_count = score_value
        weight = 1.0
        if declared_groups:
            weights = [_group_field(declared_groups, g, "weight", 1.0)
                       for g in sp.groups if declared_groups and declared_groups.get(g)]
            weight = min(weights) if weights else 1.0
        required = any(
            _group_field(declared_groups, g, "required", False) for g in sp.groups
        ) or sp.required
        results.append(ScorerResult(
            id=sp.id,
            groups=list(sp.groups),
            score_type=sp.score_type,
            passed=passed,
            exit_code=getattr(out, "exit_code", None),
            score=score_value,
            raw_count=raw_count,
            weight=float(weight),
            required=bool(required),
            max_count=sp.max_count,
            duration_seconds=getattr(out, "duration_seconds", None),
        ))

    # Declared order first, then any observed-but-undeclared group (legacy
    # evaluations always execute in "default").
    group_names: list[str] = []
    if declared_groups:
        group_names = list(declared_groups)
    for r in results:
        for g in r.groups:
            if g not in group_names:
                group_names.append(g)

    fractions: dict[str, float] = {}
    covered: dict[str, float] = {}
    for g in group_names:
        members = [r for r in results if g in r.groups]
        if members:
            fractions[g] = sum(normalize_score(r) for r in members) / len(members)
            w = float(_group_field(declared_groups, g, "weight", 1.0))
            if w > 0:
                covered[g] = w
    # Partial credit averages ONLY groups that actually executed scorers.
    # Declared-but-uncovered groups (e.g. legacy manifests that declare
    # weighted groups without v0.6 scorers) previously contributed 0.0 and
    # dragged fully-resolved runs to partial=0.0 — a fabricated statistic.
    # Renormalizing over covered weights keeps the score honest; the gap
    # stays visible in ``uncovered_groups``.
    total_weight = sum(covered.values())
    weighted = sum(w * fractions.get(g, 0.0) for g, w in covered.items())
    partial = round(weighted / total_weight, 4) if total_weight > 0 else None
    uncovered_groups = [g for g in (declared_groups or {})
                        if g not in covered]

    # A scoring definition that executed nothing must never vacuously
    # "resolve" a run; malformed manifests are caught at validation time,
    # but this stays safe if callers bypass them.
    resolved = bool(results) and all(r.passed for r in results if r.required)

    canon = json.dumps(
        [{"id": s.id, "command": s.command, "type": s.score_type,
          "groups": list(s.groups), "required": s.required} for s in sorted(
              specs, key=lambda x: x.id)],
        sort_keys=True,
    )
    return ScoringSummary(
        resolved=resolved,
        partial_score=partial,
        scorers=results,
        group_fractions=fractions,
        scorer_set_hash=hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12],
        uncovered_groups=uncovered_groups,
    )
