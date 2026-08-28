"""SQLite migration torture + concurrency stress (missions XXII, XLII).

Historical schema levels (v1/v3/v4) must migrate to v5 idempotently —
including a simulated interrupted migration where columns already exist but
``user_version`` was never bumped. Old rows stay queryable with NULL modern
columns; unknown statuses survive; concurrent readers never see lock errors.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from agentbench.storage import ResultIndex, default_db_path, _SCHEMA


def _raw_db(tmp_path: Path, user_version: int) -> sqlite3.Connection:
    """Create a database at an *older* schema level with one legacy row."""
    path = default_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    if user_version >= 2:
        for stmt in ("ALTER TABLE runs ADD COLUMN execution_backend TEXT",
                     "ALTER TABLE runs ADD COLUMN image_id TEXT",
                     "ALTER TABLE runs ADD COLUMN image_digest TEXT",
                     "ALTER TABLE runs ADD COLUMN experiment_id TEXT",
                     "ALTER TABLE runs ADD COLUMN config_name TEXT",
                     "ALTER TABLE runs ADD COLUMN files_added INTEGER",
                     "ALTER TABLE runs ADD COLUMN files_deleted INTEGER"):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
    if user_version >= 3:
        for stmt in ("ALTER TABLE runs ADD COLUMN failure_stage TEXT",
                     "ALTER TABLE runs ADD COLUMN violation_count INTEGER"):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
    if user_version >= 4:
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN cost_provenance TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "INSERT INTO runs (run_id, schema_version, benchmark, repository,"
        " requested_commit, resolved_commit, config_hash, agent, status,"
        " result_dir, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("legacy-1", 1, "oldbench", "fixture", "a" * 40, "b" * 40,
         "hash1", "claude-code", "mysteriously_unknown_status",
         str(tmp_path), "2025-01-01T00:00:00+00:00"))
    conn.commit()
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    return conn


def test_v1_database_migrates_and_legacy_row_survives(tmp_path):
    _raw_db(tmp_path, user_version=1).close()
    index = ResultIndex(default_db_path(tmp_path))
    row = index.get_run("legacy-1")
    assert row is not None
    assert row["status"] == "mysteriously_unknown_status"   # unknown survives
    assert row["validity"] is None                          # modern col NULL
    # New-style evidence indexes alongside the legacy row.
    payload = _payload("new-1")
    index.index_result(payload, result_dir=tmp_path)
    assert index.get_run("new-1")["validity"] == "valid"


def test_v3_and_v4_databases_migrate(tmp_path):
    for version in (3, 4):
        sub = tmp_path / f"v{version}"
        sub.mkdir()
        _raw_db(sub, user_version=version).close()
        index = ResultIndex(default_db_path(sub))
        assert index.get_run("legacy-1") is not None


def test_migration_is_idempotent_across_reopens(tmp_path):
    _raw_db(tmp_path, user_version=1).close()
    for _ in range(3):
        index = ResultIndex(default_db_path(tmp_path))
        assert index.get_run("legacy-1") is not None
        index.close()


def test_interrupted_migration_recovers(tmp_path):
    """Columns exist but user_version was never bumped (crash mid-migration)."""
    conn = _raw_db(tmp_path, user_version=1)
    for stmt in ("ALTER TABLE runs ADD COLUMN validity TEXT",):
        try:
            conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = 0")   # pretend it crashed
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()
    index = ResultIndex(default_db_path(tmp_path))     # must not raise
    assert index.get_run("legacy-1") is not None


def test_concurrent_readers_during_writes_never_lock(tmp_path):
    index = ResultIndex(default_db_path(tmp_path))
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                ResultIndex(default_db_path(tmp_path)).query(limit=10).extend([])
            except sqlite3.OperationalError as exc:      # pragma: no cover
                errors.append(str(exc))
            time.sleep(0.001)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(200):
        index.index_result(_payload(f"conc-{i}"), result_dir=tmp_path)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors, errors[:3]
    assert len(index.query(experiment_id="E-conc", limit=None)) == 200


def test_thousand_run_indexing_and_query_latency(tmp_path):
    """Mission XLII smoke: 1000 indexed runs stay fast on history queries."""
    import sys

    index = ResultIndex(default_db_path(tmp_path))
    start = time.perf_counter()
    for i in range(1000):
        index.index_result(_payload(f"bulk-{i}"), result_dir=tmp_path)
    insert_s = time.perf_counter() - start

    start = time.perf_counter()
    rows = index.query(experiment_id="E-conc", limit=None)
    assert len(rows) == 1000
    query_s = time.perf_counter() - start

    # Deliberately loose bounds: catches O(N^2)-style regressions only.
    assert insert_s < 60, f"1000 inserts took {insert_s:.1f}s"
    assert query_s < 5, f"full query took {query_s:.1f}s"


def _payload(run_id: str) -> dict:
    return {
        "schema_version": 2, "run_id": run_id, "trial": 1,
        "benchmark": {"name": "alpha", "repository": "fixture",
                      "commit": "a" * 40, "resolved_commit": "b" * 40,
                      "config_hash": "cafe123"},
        "agent": {"type": "command", "exit_code": 0, "timed_out": False},
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "total_tokens": 15, "cost_usd": 0.01},
        "diff": {"files_changed": 1, "insertions": 3, "deletions": 1},
        "overall": {"status": "passed", "failure_reason": None,
                    "started_at": "2026-08-24T00:00:00+00:00",
                    "finished_at": "2026-08-24T00:01:00+00:00",
                    "duration_seconds": 60.0},
        "experiment_id": "E-conc",
        "config_name": "cfg-a",
    }


def test_scan_results_logs_skipped_artifacts(tmp_path, caplog):
    """Rebuild must surface (not silently swallow) unreadable evidence."""
    import json
    import logging

    from agentbench.storage import ResultIndex

    good = tmp_path / "bench" / "run-good"
    good.mkdir(parents=True)
    (good / "result.json").write_text(
        json.dumps({
            "schema_version": 4,
            "run_id": "good",
            "benchmark": {"name": "bench", "repository": "r", "commit": "a" * 40,
                          "resolved_commit": "b" * 40, "config_hash": "h"},
            "overall": {"status": "passed"},
        }),
        encoding="utf-8",
    )
    bad = tmp_path / "bench" / "run-bad"
    bad.mkdir(parents=True)
    (bad / "result.json").write_text("{corrupt", encoding="utf-8")

    index = ResultIndex(tmp_path / ".agentbench" / "agentbench.db")
    with caplog.at_level(logging.WARNING, logger="agentbench.storage"):
        indexed, skipped = index.scan_results(tmp_path)

    assert (indexed, skipped) == (1, 1)
    assert any("run-bad" in r.message for r in caplog.records)
