"""Tests for run result serialization (agentbench.results)."""

from __future__ import annotations

import json
import re

from agentbench.results import RunArtifacts, RunResult, write_run


def make_result(**overrides) -> tuple[RunResult, RunArtifacts]:
    result = RunResult(
        schema_version=2,
        run_id="20260822T100000Z-abc123",
        benchmark={
            "name": "demo",
            "repository": "https://example.com/repo.git",
            "commit": "a" * 40,
            "resolved_commit": "b" * 40,
            "config_hash": "cf" * 6,
        },
        agent={"type": "claude-code", "exit_code": 0, "timed_out": False, "duration_seconds": 12.5, "model": None},
        usage=None,
        diff={
            "files_changed": 2,
            "insertions": 10,
            "deletions": 3,
            "patch_file": "diff.patch",
            "changed_paths": ["x.txt", "y.txt"],
        },
        evaluations=[
            {
                "name": "smoke",
                "command": "python -c pass",
                "exit_code": 0,
                "passed": True,
                "timed_out": False,
                "duration_seconds": 1.5,
                "stdout_file": "evals/000-smoke.stdout.log",
                "stderr_file": "evals/000-smoke.stderr.log",
            }
        ],
        hidden_evaluations=[],
        protected_paths=None,
        overall={
            "status": "passed",
            "failure_reason": None,
            "started_at": "2026-08-22T10:00:00+00:00",
            "finished_at": "2026-08-22T10:00:14+00:00",
            "duration_seconds": 14.0,
        },
        environment={"python_version": "3.12.10", "platform": "Windows", "agentbench_version": "0.2.0"},
        config={"name": "demo"},
        **overrides,
    )
    artifacts = RunArtifacts(
        agent_stdout="agent said hi\n",
        agent_stderr="agent warned\n",
        patch="diff --git a/x b/x\n",
        eval_outputs={"000-smoke": ("eval ok\n", "")},
    )
    return result, artifacts


class TestWriteRun:
    def test_writes_structured_result_json(self, tmp_path):
        result, artifacts = make_result()

        run_dir = write_run(result, artifacts, results_root=tmp_path)

        payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["schema_version"] == 2
        assert payload["run_id"] == result.run_id
        assert payload["benchmark"]["name"] == "demo"
        assert payload["overall"]["status"] == "passed"
        assert payload["evaluations"][0]["name"] == "smoke"
        assert payload["config"] == {"name": "demo"}

    def test_run_dir_name_can_be_pinned_to_the_run_id(self, tmp_path):
        result, artifacts = make_result()

        run_dir = write_run(result, artifacts, results_root=tmp_path, run_dir_name=result.run_id)

        assert run_dir.name == result.run_id

    def test_raw_outputs_live_in_sidecar_files_not_json(self, tmp_path):
        result, artifacts = make_result()

        run_dir = write_run(result, artifacts, results_root=tmp_path)

        payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert "agent said hi" not in json.dumps(payload)
        assert (run_dir / "agent.stdout.log").read_text(encoding="utf-8") == "agent said hi\n"
        assert (run_dir / "agent.stderr.log").read_text(encoding="utf-8") == "agent warned\n"
        assert (run_dir / "diff.patch").read_text(encoding="utf-8").startswith("diff --git")
        assert (run_dir / "evals" / "000-smoke.stdout.log").read_text(encoding="utf-8") == "eval ok\n"

    def test_run_dir_namespaced_by_benchmark_and_timestamp(self, tmp_path):
        result, artifacts = make_result()

        run_dir = write_run(result, artifacts, results_root=tmp_path)

        assert run_dir.parent.name == "demo"
        # <UTC timestamp>-<suffix> keeps runs sortable and collision-free.
        assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", run_dir.name)

    def test_consecutive_runs_get_distinct_directories(self, tmp_path):
        result, artifacts = make_result()

        first = write_run(result, artifacts, results_root=tmp_path)
        second = write_run(result, artifacts, results_root=tmp_path)

        assert first != second
