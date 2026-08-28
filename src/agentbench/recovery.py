"""First-attempt vs recovery classification from trajectories (v0.6 P19).

Uses only observable test/edit cycles — no manufactured "attempt" boundaries.
A run without a trajectory classifies as unknown; nothing is inferred.
"""

from __future__ import annotations

from typing import Any

SOLVED_FIRST = "solved_without_detected_test_failure"
SOLVED_RECOVERY = "solved_after_test_failure"
SOLVED_MULTI = "solved_after_multiple_failed_iterations"
FAILED_NO_TESTS = "failed_without_testing"
FAILED_AFTER_TESTS = "failed_after_testing"
UNKNOWN = "unknown"


def classify_recovery(events: list[dict[str, Any]], *, passed: bool) -> str:
    tests = [e for e in events if e.get("event_type") == "test_command"]
    failing_tests = [e for e in tests if e.get("success") is False]

    if not tests:
        return SOLVED_FIRST if passed else FAILED_NO_TESTS

    if passed:
        if len(failing_tests) >= 2:
            return SOLVED_MULTI
        if failing_tests:
            return SOLVED_RECOVERY
        return SOLVED_FIRST

    # failed despite running tests: did any edit follow the last failure?
    last_fail_ms = None
    for ev in reversed(tests):
        if ev.get("success") is False:
            last_fail_ms = _rel(ev)
            break
    edits_after = [
        e for e in events
        if e.get("event_type") in ("file_edit", "file_write")
        and last_fail_ms is not None
        and _rel(e) is not None and _rel(e) > last_fail_ms
    ]
    return FAILED_AFTER_TESTS


def recovery_summary(runs: list[dict[str, Any]]) -> dict[str, int]:
    """runs: {passed: bool, trajectory_status, events} dicts (already loaded)."""
    counts: dict[str, int] = {}
    for run in runs:
        category = UNKNOWN
        header = run.get("header") or {}
        status = (header or {}).get("trajectory_status")
        if status == "complete" and run.get("events"):
            category = classify_recovery(run["events"], passed=bool(run.get("passed")))
        counts[category] = counts.get(category, 0) + 1
    return counts


def _rel(ev: dict) -> float | None:
    ms = ev.get("relative_ms")
    return float(ms) if isinstance(ms, (int, float)) else None


__all__ = [
    "classify_recovery", "recovery_summary",
    "SOLVED_FIRST", "SOLVED_RECOVERY", "SOLVED_MULTI",
    "FAILED_NO_TESTS", "FAILED_AFTER_TESTS", "UNKNOWN",
]
