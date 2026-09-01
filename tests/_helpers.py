"""Shared construction helpers for v0.6 hardening tests.

Lives in ``tests/`` (not under ``src/agentbench/``) because these fixtures
exist solely to build synthetic experiments for cross-surface consistency
tests — they have no production role and must never be importable from an
installed wheel.

Importing convention: ``from _helpers import ...`` works under any pytest
invocation mode because pytest's default ``import-mode=prepend`` always
inserts each test module's parent directory (``tests/``) at ``sys.path[0]``,
regardless of whether pytest was launched as ``pytest`` or ``python -m pytest``.
"""

from __future__ import annotations

from agentbench.experiments import ExperimentManifest


def make_row(bench, cfg, trial, status, validity="valid", **extra):
    row = {
        "run_id": f"{bench}-{cfg}-{trial}",
        "experiment_id": "E", "benchmark": bench, "config_name": cfg,
        "trial": trial, "status": status, "validity": validity,
        "duration_seconds": 12.0, "total_tokens": 900,
        "cost_usd": 0.02, "cost_provenance": "reported",
        "agent": "command", "model": "m", "execution_backend": "host",
        "files_changed": 2, "insertions": 5, "deletions": 1,
    }
    row.update(extra)
    return row


def build_rows() -> list[dict]:
    return [
        # alpha: clean pass / fail / timeout mix for both configs
        make_row("alpha", "cfg-a", 1, "passed"),
        make_row("alpha", "cfg-a", 2, "agent_timeout"),
        make_row("alpha", "cfg-b", 1, "passed"),
        make_row("alpha", "cfg-b", 2, "evaluation_failed"),
        # beta: infra-invalid on A only + a protected-path violation on B
        make_row("beta", "cfg-a", 1, "agent_failed", "infra_invalid",
                 total_tokens=0),
        make_row("beta", "cfg-a", 2, "passed"),
        make_row("beta", "cfg-b", 1, "protected_path_violation"),
        make_row("beta", "cfg-b", 2, "evaluation_failed"),
        # partial-score failure: resolved=False despite high partial credit
        make_row("gamma", "cfg-a", 1, "evaluation_failed",
                 scoring={"resolved": False, "partial_score": 0.9,
                          "scorers": [], "group_fractions": {},
                          "scorer_set_hash": "abc123"}),
        make_row("gamma", "cfg-b", 1, "passed"),
    ]


def make_manifest() -> ExperimentManifest:
    return ExperimentManifest(
        experiment_id="E", name="consistency", created_at="2026-01-01T00:00:00+00:00",
        results_dir="results", planned_cells=10, repeat=2,
        resolved_benchmarks=["alpha", "beta", "gamma"],
        config_definitions={"cfg-a": {"agent": {"type": "command"}},
                            "cfg-b": {"agent": {"type": "command"}}},
        config_identities={"cfg-a": "a" * 12, "cfg-b": "b" * 12},
        completed=[
            {"cell_key": "lost", "benchmark": "gamma", "config": "cfg-b",
             "trial": 2, "status": "setup_failed", "run_id": None},
            *[{"cell_key": f"k{i}", "benchmark": r["benchmark"],
               "config": r["config_name"], "trial": r["trial"],
               "status": r["status"], "run_id": r["run_id"]}
              for i, r in enumerate(build_rows())],
        ],
    )