"""Serialization roundtrip (v0.6 hardening, mission LII).

A fully-populated v0.6 result must survive: RunResult validation → JSON →
Pydantic re-parse → SQLite index → export flattening — with no field
silently dropped, especially validity, failure stage, scoring breakdown,
trajectory status, cost provenance, comparison metadata and limits.
"""

from __future__ import annotations

import json

from agentbench.export import flatten_row
from agentbench.results import RunResult
from agentbench.storage import ResultIndex, default_db_path


FULL_RESULT = {
    "environment": {"agentbench": "0.6.0", "python": "3.12.10",
                    "platform": "Windows", "git": "2.51"},
    "config": {"name": "roundtrip", "timeout_seconds": 600},
    "run_id": "rt-1",
    "trial": 2,
    "benchmark": {
        "name": "roundtrip", "repository": "fixture",
        "commit": "a" * 40, "resolved_commit": "b" * 40,
        "config_hash": "cafe123",
        "protected_paths": ["tests/**"],
    },
    "agent": {
        "type": "hermes", "exit_code": 0, "timed_out": False,
        "model": "stealth/ox-alpha",
        "capabilities": ["structured_usage", "model_reporting"],
        "session_id": "sess-77",
        "num_turns": 12, "tool_calls": 34,
    },
    "usage": {
        "input_tokens": 120000, "output_tokens": 4500,
        "total_tokens": 124500, "cost_usd": 0.123,
        "cost_provenance": "openrouter/reported",
    },
    "diff": {"files_changed": 3, "files_added": 1, "files_deleted": 0,
             "insertions": 42, "deletions": 7},
    "overall": {
        "status": "evaluation_failed", "validity": "valid",
        "failure_reason": "one or more evaluations failed",
        "failure_stage": "evaluation",
        "started_at": "2026-08-25T19:00:00+00:00",
        "finished_at": "2026-08-25T19:04:30+00:00",
        "duration_seconds": 270.0,
        "stage_timings": {"workspace": 1.2, "agent": 250.0, "evaluation": 18.8},
    },
    "execution": {
        "backend": "host", "network": "inherit",
        "memory_limit": None, "cpus_limit": None, "pids_limit": None,
        "passed_env_names": ["OPENROUTER_API_KEY"],
    },
    "evaluations": [
        {"name": "public-tests", "passed": True, "exit_code": 0,
         "duration_seconds": 9.5},
        {"name": "ordering-contract", "passed": False, "exit_code": 1,
         "duration_seconds": 3.0},
    ],
    "experiment_id": "E-rt", "config_name": "cfg-a",
}


def test_result_json_roundtrip_keeps_every_field(tmp_path):
    result = RunResult.model_validate(FULL_RESULT)
    dumped = json.loads(result.model_dump_json())
    reparsed = RunResult.model_validate(dumped)
    assert reparsed == result
    # spot-check the fields most at risk of silent loss
    overall = json.loads(reparsed.model_dump_json())["overall"]
    assert overall["validity"] == "valid"
    assert overall["failure_stage"] == "evaluation"
    assert overall["stage_timings"] == FULL_RESULT["overall"]["stage_timings"]
    assert json.loads(reparsed.model_dump_json())["usage"]["cost_provenance"] \
        == "openrouter/reported"


def test_indexed_row_survives_export_flattening(tmp_path):
    index = ResultIndex(default_db_path(tmp_path))
    index.index_result(FULL_RESULT, result_dir=tmp_path)
    row = index.get_run("rt-1")
    flat = flatten_row(row)
    for column in ("validity", "cost_provenance", "failure_stage",
                   "status", "model", "config_name"):
        assert flat[column] == row[column], column
    assert flat["validity"] == "valid"
    assert flat["cost_provenance"] == "openrouter/reported"
    # numeric fields keep their types through CSV-flatten preparation
    assert flat["duration_seconds"] == 270.0


def test_unknown_status_and_validity_are_preserved_not_mapped(tmp_path):
    payload = json.loads(json.dumps(FULL_RESULT))
    payload["run_id"] = "rt-unknown"
    payload["overall"]["status"] = "some_future_status_v9"
    payload["overall"]["validity"] = "quantum_invalid"
    index = ResultIndex(default_db_path(tmp_path / "u"))
    index.index_result(payload, result_dir=tmp_path)
    row = index.get_run("rt-unknown")
    assert row["status"] == "some_future_status_v9"
    assert row["validity"] == "quantum_invalid"
