"""Tests for the dashboard: rendering, escaping, artifact security, routes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentbench.dashboard import (
    DashboardStore,
    make_dashboard,
    render_benchmark,
    render_overview,
    render_run_detail,
    render_runs,
    resolve_artifact,
)
from agentbench.storage import ResultIndex, default_db_path


RUN_ID = "20260822T100000Z-aaa111"


@pytest.fixture
def seeded_store(tmp_path) -> DashboardStore:
    results_root = tmp_path / "results"
    run_dir = results_root / "demo" / RUN_ID
    (run_dir / "evals").mkdir(parents=True)
    payload = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "trial": 1,
        "benchmark": {"name": "demo", "repository": "https://x/r.git",
                      "commit": "a" * 40, "resolved_commit": "b" * 40, "config_hash": "h1"},
        "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False,
                  "duration_seconds": 12.0, "model": "sonnet"},
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                  "cost_usd": 0.01, "tool_calls": None, "num_turns": 2, "session_id": "s1"},
        "diff": {"files_changed": 1, "insertions": 1, "deletions": 0,
                 "patch_file": "diff.patch", "changed_paths": ["canary.txt"]},
        "evaluations": [{"name": "check", "command": "x", "exit_code": 0, "passed": True,
                         "timed_out": False, "duration_seconds": 1.0,
                         "stdout_file": "evals/000-check.stdout.log",
                         "stderr_file": "evals/000-check.stderr.log"}],
        "hidden_evaluations": [],
        "protected_paths": None,
        "overall": {"status": "passed", "failure_reason": None, "started_at": "s",
                    "finished_at": "f", "duration_seconds": 13.0},
        "environment": {"agentbench_version": "0.2.0", "python_version": "3.12.10",
                        "platform": "Windows", "git_version": "git version 2.51",
                        "agent_cli_version": "2.1.239"},
        "config": {"name": "demo"},
        "workspace_kept": False,
        "workspace_path": None,
    }
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "diff.patch").write_text("diff --git a/canary.txt b/canary.txt\n+alive\n", encoding="utf-8")
    (run_dir / "evals" / "000-check.stdout.log").write_text("eval output\n", encoding="utf-8")

    index = ResultIndex(default_db_path(results_root))
    index.index_result(payload, result_dir=run_dir)
    return DashboardStore(results_root)


class TestRendering:
    def test_overview_shows_totals_and_configs(self, seeded_store):
        page = render_overview(seeded_store)

        assert "Total runs" in page
        assert "100%" in page
        assert "claude-code/sonnet" in page
        assert RUN_ID in page

    def test_runs_table_renders(self, seeded_store):
        page = render_runs(seeded_store, {})

        assert RUN_ID in page
        assert "passed" in page

    def test_run_detail_shows_evidence_sections(self, seeded_store):
        page = render_run_detail(seeded_store, RUN_ID)

        assert "Requested commit" in page
        assert "canary.txt" in page
        assert "Hidden" in page  # section exists even when empty
        assert "agent.stdout.log" in page
        assert "git version 2.51" in page

    def test_run_detail_unknown_run_returns_none(self, seeded_store):
        assert render_run_detail(seeded_store, "20260822T000000Z-zzzzzz") is None

    def test_benchmark_page_shows_aggregates_and_chart(self, seeded_store):
        page = render_benchmark(seeded_store, "demo")

        assert page is not None
        assert "PASS RATE" in page
        assert "<svg" in page  # duration chart

    def test_benchmark_page_unknown_returns_none(self, seeded_store):
        assert render_benchmark(seeded_store, "ghost") is None


class TestEscaping:
    def test_agent_controlled_content_is_escaped(self, tmp_path, seeded_store):
        # Inject through BOTH the DB index and the on-disk evidence: agent-
        # supplied values containing HTML must never reach the page raw.
        # (The benchmark name stays Windows-safe: < > are invalid in dir names;
        # the realistic injection surface is values like model/failure_reason.)
        results_root = seeded_store.results_root
        run_id = "20260822T100001Z-bbb222"
        run_dir = results_root / "evilbench" / run_id
        run_dir.mkdir(parents=True)
        payload = {
            "schema_version": 2,
            "run_id": run_id,
            "trial": None,
            "benchmark": {"name": "evilbench", "repository": "r", "commit": "a" * 40,
                          "resolved_commit": "b" * 40, "config_hash": "h2"},
            "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False,
                      "duration_seconds": 1.0, "model": "<script>alert(1)</script>"},
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
                      "cost_usd": 0.0, "tool_calls": None, "num_turns": 1,
                      "session_id": "<b>sess</b>"},
            "diff": {"files_changed": 0, "insertions": 0, "deletions": 0,
                     "changed_paths": []},
            "evaluations": [], "hidden_evaluations": [],
            "overall": {"status": "evaluation_failed",
                        "failure_reason": "<img src=x onerror=alert(2)>",
                        "started_at": "s", "finished_at": "f", "duration_seconds": 1.0},
            "environment": {},
            "config": {},
        }
        (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        index = ResultIndex(default_db_path(results_root))
        index.index_result(payload, result_dir=run_dir)
        seeded_store._last_scan = 0.0  # force rescan of the new evidence

        for page in (render_overview(seeded_store), render_runs(seeded_store, {}),
                     render_run_detail(seeded_store, run_id),
                     render_benchmark(seeded_store, "evilbench")):
            assert "<script>alert(1)</script>" not in page
            assert "<img src=x" not in page
            assert "<b>sess</b>" not in page
        assert "&lt;script&gt;" in render_run_detail(seeded_store, run_id)


class TestArtifactSecurity:
    def test_valid_artifact_resolves(self, seeded_store):
        run = seeded_store.get_run(RUN_ID)
        artifact = resolve_artifact(Path(run["result_dir"]), ["diff.patch"])

        assert artifact is not None
        assert artifact.read_text(encoding="utf-8").startswith("diff --git")

    def test_nested_eval_log_resolves(self, seeded_store):
        run = seeded_store.get_run(RUN_ID)

        artifact = resolve_artifact(Path(run["result_dir"]), ["evals", "000-check.stdout.log"])

        assert artifact is not None

    @pytest.mark.parametrize("segments", [
        [".."],
        ["..", "other", "secret.txt"],
        ["evals", "..", "..", "outside.txt"],
        ["evals/../../escape.txt"],
        ["", "x"],
        ["nonexistent.txt"],
    ])
    def test_traversal_attempts_rejected(self, seeded_store, segments):
        run = seeded_store.get_run(RUN_ID)

        assert resolve_artifact(Path(run["result_dir"]), segments) is None

    def test_unknown_run_id_pattern_rejected_by_route(self, seeded_store):
        # The route layer rejects ids that do not match the run-id shape
        # before any filesystem access happens.
        from agentbench.dashboard import RUN_ID_PATTERN

        assert not RUN_ID_PATTERN.match("../evil")
        assert not RUN_ID_PATTERN.match("with space")
        assert RUN_ID_PATTERN.match(RUN_ID)


class TestExperimentAndCorpusViews:
    @pytest.fixture
    def store_with_experiment(self, tmp_path, seeded_store):
        """Seed a second run linked to an experiment + its manifest."""
        results_root = seeded_store.results_root
        experiment_id = "20260823T000000Z-exp001"
        payload = {
            "schema_version": 3,
            "run_id": "20260823T100000Z-ccc333",
            "trial": 2,
            "benchmark": {"name": "demo", "repository": "r", "commit": "a" * 40,
                          "resolved_commit": "b" * 40, "config_hash": "h1"},
            "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False,
                      "duration_seconds": 20.0, "model": None},
            "usage": None,
            "diff": {"files_changed": 0, "insertions": 0, "deletions": 0,
                     "changed_paths": []},
            "evaluations": [], "hidden_evaluations": [], "protected_paths": None,
            "overall": {"status": "agent_timeout",
                        "failure_reason": "agent process exceeded the timeout",
                        "started_at": "s", "finished_at": "f", "duration_seconds": 20.0},
            "execution": {"backend": "docker", "network": "enabled",
                          "image_requested": "python:3.12-slim",
                          "image_id": "sha256:xyz", "image_digests": ["d@sha256:1"],
                          "memory_limit": "2g"},
            "environment": {},
            "config": {},
            "experiment_id": experiment_id,
            "config_name": "cfg-a",
        }
        run_dir = results_root / "demo" / "20260823T100000Z-ccc333"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        index = ResultIndex(default_db_path(results_root))
        index.index_result(payload, result_dir=run_dir)

        experiments_dir = results_root / "experiments" / experiment_id
        experiments_dir.mkdir(parents=True)
        (experiments_dir / "experiment.json").write_text(
            json.dumps({
                "schema_version": 1,
                "experiment_id": experiment_id,
                "name": "matrix-demo",
                "created_at": "2026-08-23T00:00:00+00:00",
                "results_dir": str(results_root),
                "planned_cells": 4,
                "repeat": 2,
                "benchmark_identities": {"demo": "h1"},
                "config_identities": {"cfg-a": "ch1"},
                "completed": [
                    {"cell_key": "k1", "benchmark": "demo", "config": "cfg-a",
                     "trial": 1, "status": "passed", "run_id": RUN_ID},
                    {"cell_key": "k2", "benchmark": "demo", "config": "cfg-a",
                     "trial": 2, "status": "agent_timeout", "run_id":
                     "20260823T100000Z-ccc333"},
                ],
                "interrupted": False,
            }),
            encoding="utf-8",
        )
        return seeded_store

    def test_experiments_page_lists_manifest(self, store_with_experiment):
        from agentbench.dashboard import render_experiments

        page = render_experiments(store_with_experiment)

        assert "matrix-demo" in page
        assert "2/4" in page
        assert "cfg-a" in page

    def test_experiment_detail_shows_matrix_and_taxonomy(self, store_with_experiment):
        from agentbench.dashboard import render_experiment_detail

        page = render_experiment_detail(store_with_experiment, "20260823T000000Z-exp001")

        assert "success matrix" in page
        assert "1/2" in page  # demo x cfg-a cell
        assert "agent_timeout" in page  # failure taxonomy counts
        assert "<svg" in page  # duration chart

    def test_experiment_detail_unknown_returns_none(self, store_with_experiment):
        from agentbench.dashboard import render_experiment_detail

        assert render_experiment_detail(store_with_experiment, "nope") is None

    def test_run_detail_shows_execution_provenance_and_experiment_link(
        self, store_with_experiment
    ):
        from agentbench.dashboard import render_run_detail

        page = render_run_detail(store_with_experiment, "20260823T100000Z-ccc333")

        assert "Execution provenance" in page
        assert "docker" in page
        assert "sha256:xyz" in page
        assert "2g" in page
        assert "/experiments/" in page  # experiment link


class TestHttpServer:
    def test_routes_served_over_real_socket(self, seeded_store):
        server = make_dashboard(seeded_store.results_root, port=0, host="127.0.0.1")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            overview = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10)
            assert overview.status == 200
            assert "AgentBench" in overview.read().decode("utf-8")

            runs_page = urllib.request.urlopen(f"http://127.0.0.1:{port}/runs", timeout=10)
            assert RUN_ID in runs_page.read().decode("utf-8")

            experiments = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/experiments", timeout=10
            )
            assert experiments.status == 200

            benchmarks_page = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/benchmarks", timeout=10
            )
            assert benchmarks_page.status == 200

            detail = urllib.request.urlopen(f"http://127.0.0.1:{port}/runs/{RUN_ID}", timeout=10)
            assert "canary.txt" in detail.read().decode("utf-8")

            artifact = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/artifacts/{RUN_ID}/evals/000-check.stdout.log", timeout=10
            )
            assert "eval output" in artifact.read().decode("utf-8")

            traversal_attempt_status = None
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/artifacts/{RUN_ID}/..%2f..%2fresult.json", timeout=10
                )
            except urllib.error.HTTPError as exc:
                traversal_attempt_status = exc.code
            # %2f decodes to '/' inside the segment list; the guard must
            # reject it (404) rather than serve files outside the run directory.
            assert traversal_attempt_status == 404
        finally:
            server.shutdown()
            server.server_close()
