"""Tests for the SQLite result index (agentbench.storage)."""

from __future__ import annotations

import json
import sqlite3
import sys

from agentbench.storage import ResultIndex, default_db_path


def make_payload(run_id: str = "20260822T100000Z-aaa111", **overrides) -> dict:
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "trial": None,
        "benchmark": {
            "name": "demo",
            "repository": "https://example.com/repo.git",
            "commit": "a" * 40,
            "resolved_commit": "b" * 40,
            "config_hash": "hash123",
        },
        "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False, "model": "m1"},
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": 0.5},
        "diff": {"files_changed": 2, "insertions": 5, "deletions": 1},
        "overall": {
            "status": "passed",
            "failure_reason": None,
            "started_at": "2026-08-22T10:00:00+00:00",
            "finished_at": "2026-08-22T10:01:00+00:00",
            "duration_seconds": 60.0,
        },
    }
    payload.update(overrides)
    return payload


class TestIndexing:
    def test_indexed_run_is_queryable(self, tmp_path):
        index = ResultIndex(tmp_path / "db.sqlite")
        index.index_result(make_payload(), result_dir=tmp_path)

        rows = index.query()

        assert len(rows) == 1
        assert rows[0]["run_id"] == "20260822T100000Z-aaa111"
        assert rows[0]["status"] == "passed"
        assert rows[0]["total_tokens"] == 110
        assert rows[0]["cost_usd"] == 0.5

    def test_reindexing_same_run_does_not_duplicate(self, tmp_path):
        index = ResultIndex(tmp_path / "db.sqlite")

        index.index_result(make_payload(), result_dir=tmp_path)
        index.index_result(make_payload(), result_dir=tmp_path)

        assert len(index.query(limit=None)) == 1

    def test_null_metrics_stay_null_for_stub_runs(self, tmp_path):
        index = ResultIndex(tmp_path / "db.sqlite")
        payload = make_payload(usage=None)

        index.index_result(payload, result_dir=tmp_path)

        row = index.query()[0]
        assert row["input_tokens"] is None
        assert row["cost_usd"] is None

    def test_cost_provenance_round_trips(self, tmp_path):
        index = ResultIndex(tmp_path / "db.sqlite")
        payload = make_payload(usage={"cost_usd": 0.25, "cost_provenance": "reported"})
        index.index_result(payload, result_dir=tmp_path)
        row = index.query(limit=None)[0]
        assert row["cost_provenance"] == "reported"

    def test_v3_database_migrates_and_stays_queryable(self, tmp_path):
        # A DB written before the cost-provenance column must keep working:
        # opening it applies additive migrations and indexing still succeeds.
        db = tmp_path / "db.sqlite"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, schema_version INTEGER,"
            " benchmark TEXT, repository TEXT, requested_commit TEXT,"
            " resolved_commit TEXT, config_hash TEXT, agent TEXT, model TEXT,"
            " status TEXT, failure_reason TEXT, trial INTEGER, started_at TEXT,"
            " duration_seconds REAL, agent_exit_code INTEGER,"
            " agent_timed_out INTEGER NOT NULL DEFAULT 0, files_changed INTEGER,"
            " insertions INTEGER, deletions INTEGER, input_tokens INTEGER,"
            " output_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,"
            " result_dir TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        index = ResultIndex(db)  # triggers migration to current version
        payload = make_payload(usage={"cost_usd": 0.25, "cost_provenance": "reported"})
        index.index_result(payload, result_dir=tmp_path)
        rows = index.query(limit=None)
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == 0.25

    def test_exact_zero_cost_is_normalized_to_unpriced(self, tmp_path):
        # Providers report $0.00 for unpriced models; the derived index must
        # not serve a fabricated free-inference figure to reports. (The
        # primary result.json evidence is never rewritten.)
        index = ResultIndex(tmp_path / "db.sqlite")
        payload = make_payload(usage={"cost_usd": 0.0,
                                      "cost_provenance": "provider_models_api/unknown"})
        index.index_result(payload, result_dir=tmp_path)
        row = index.query(limit=None)[0]
        assert row["cost_usd"] is None
        assert row["cost_provenance"] == "unpriced/provider_models_api/unknown"


class TestQueries:
    seeded = [
        ("r1", "bench-a", "claude-code", "m1", "passed"),
        ("r2", "bench-a", "claude-code", "m1", "evaluation_failed"),
        ("r3", "bench-a", "claude-code", "m2", "passed"),
        ("r4", "bench-b", "stub-agent", None, "agent_timeout"),
    ]

    def seed(self, tmp_path) -> ResultIndex:
        index = ResultIndex(tmp_path / "db.sqlite")
        for run_id, benchmark, agent, model, status in self.seeded:
            payload = make_payload(
                run_id=run_id,
                benchmark={**make_payload()["benchmark"], "name": benchmark},
                agent={**make_payload()["agent"], "type": agent, "model": model},
                overall={
                    **make_payload()["overall"],
                    "status": status,
                    "finished_at": f"2026-08-22T10:0{len(run_id)}:00+00:00",
                },
            )
            index.index_result(payload, result_dir=tmp_path / run_id)
        return index

    def test_filter_by_benchmark(self, tmp_path):
        rows = self.seed(tmp_path).query(benchmark="bench-b")

        assert [r["run_id"] for r in rows] == ["r4"]

    def test_filter_by_model(self, tmp_path):
        rows = self.seed(tmp_path).query(model="m2")

        assert [r["run_id"] for r in rows] == ["r3"]

    def test_filter_by_status_and_limit(self, tmp_path):
        index = self.seed(tmp_path)

        passed = index.query(status="passed")
        limited = index.query(limit=2)

        assert {r["run_id"] for r in passed} == {"r1", "r3"}
        assert len(limited) == 2

    def test_benchmarks_listing(self, tmp_path):
        benchmarks = self.seed(tmp_path).benchmarks()

        assert benchmarks == ["bench-a", "bench-b"]


class TestScanAndCompatibility:
    def write_result_file(self, results_root, benchmark: str, run_id: str, payload: dict):
        run_dir = results_root / benchmark / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        return run_dir

    def test_scan_picks_up_result_files(self, tmp_path):
        results_root = tmp_path / "results"
        self.write_result_file(results_root, "demo", "20260822T100000Z-aaa111", make_payload())

        index = ResultIndex(tmp_path / "db.sqlite")
        indexed, skipped = index.scan_results(results_root)

        assert (indexed, skipped) == (1, 0)
        assert index.get_run("20260822T100000Z-aaa111") is not None

    def test_scan_skips_malformed_files_without_harm(self, tmp_path):
        results_root = tmp_path / "results"
        good = self.write_result_file(results_root, "demo", "20260822T100000Z-aaa111", make_payload())
        bad_dir = results_root / "broken" / "20260822T100001Z-bbb222"
        bad_dir.mkdir(parents=True)
        (bad_dir / "result.json").write_text("{not json", encoding="utf-8")

        index = ResultIndex(tmp_path / "db.sqlite")
        indexed, skipped = index.scan_results(results_root)

        assert (indexed, skipped) == (1, 1)
        assert json.loads((good / "result.json").read_text(encoding="utf-8"))["run_id"]

    def test_scan_handles_v01_results_with_missing_fields(self, tmp_path):
        # A v0.1-era result has no run_id/usage/hidden sections; the indexer
        # must not crash and must still surface what it can.
        legacy = {
            "schema_version": 1,
            "run_id": "20260101T000000Z-old001",
            "benchmark": {"name": "legacy", "commit": "c" * 40, "resolved_commit": "d" * 40,
                          "repository": "https://example.com/r.git", "config_hash": ""},
            "agent": {"type": "claude-code"},
            "diff": {},
            "overall": {"status": "passed"},
        }
        self.write_result_file(tmp_path / "results", "legacy", "20260101T000000Z-old001", legacy)

        index = ResultIndex(tmp_path / "db.sqlite")
        indexed, skipped = index.scan_results(tmp_path / "results")

        assert indexed == 1
        row = index.get_run("20260101T000000Z-old001")
        assert row["model"] is None
        assert row["status"] == "passed"

    def test_corrupted_database_raises_but_evidence_remains(self, tmp_path):
        db_path = default_db_path(tmp_path)
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"this is not a sqlite database" * 100)
        evidence = tmp_path / "results" / "demo" / "r1" / "result.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps(make_payload()), encoding="utf-8")

        try:
            ResultIndex(db_path)
            raised = False
        except sqlite3.DatabaseError:
            raised = True

        assert raised
        assert json.loads(evidence.read_text(encoding="utf-8"))["run_id"] == "20260822T100000Z-aaa111"
