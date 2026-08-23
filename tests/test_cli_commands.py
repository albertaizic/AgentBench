"""CLI tests for repeat mode, history, show, compare, and index errors."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentbench.cli import app

runner = CliRunner()

CHECKER = "import pathlib, sys\nsys.exit(0 if pathlib.Path('agent_change.txt').exists() else 1)\n"


def write_benchmark_yaml(tmp_path: Path, repo: Path, sha: str) -> Path:
    path = tmp_path / "benchmark.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            name: demo
            repository: {repo.as_posix()}
            commit: {sha}
            prompt: create agent_change.txt
            agent:
              type: claude-code
            evaluations:
              - name: file-created
                command: '"{sys.executable}" check_agent_file.py'
            """
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def bench_repo(make_git_repo):
    return make_git_repo(
        name="bench-repo",
        files={"README.md": "# demo\n", "check_agent_file.py": CHECKER},
    )


def stub_writes_file(monkeypatch) -> None:
    from agentbench.adapters.base import AgentAdapter, AgentInvocation

    class StubWriteAgent(AgentAdapter):
        name = "stub-write"

        def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
            script = (
                "import sys, pathlib\n"
                "sys.stdin.read()\n"
                "pathlib.Path('agent_change.txt').write_text('x')\n"
                "print('agent done')\n"
            )
            return AgentInvocation(argv=[sys.executable, "-c", script])

    monkeypatch.setattr("agentbench.cli.get_adapter", lambda _type: StubWriteAgent())


class TestRepeatCommand:
    def test_repeat_runs_three_independent_trials(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)

        result = runner.invoke(
            app, ["run", str(yaml_path), "--repeat", "3", "--results-dir", str(results_dir)]
        )

        assert result.exit_code == 0, result.output
        assert "Trial 3/3" in result.output
        run_dirs = sorted((results_dir / "demo").iterdir())
        assert len(run_dirs) == 3  # three distinct persisted runs
        trials = {
            json.loads((d / "result.json").read_text(encoding="utf-8"))["trial"]
            for d in run_dirs
        }
        assert trials == {1, 2, 3}

    def test_repeat_exit_one_when_any_trial_fails(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"

        from agentbench.adapters.base import AgentAdapter, AgentInvocation

        class StubIdleAgent(AgentAdapter):
            name = "stub-idle"

            def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
                return AgentInvocation(argv=[sys.executable, "-c", "print('nothing')"])

        monkeypatch.setattr("agentbench.cli.get_adapter", lambda _type: StubIdleAgent())

        result = runner.invoke(
            app, ["run", str(yaml_path), "--repeat", "2", "--results-dir", str(results_dir)]
        )

        assert result.exit_code == 1, result.output
        assert "FAIL" in result.output

    def test_interrupt_stops_trials_and_preserves_results(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)

        real_run = __import__("agentbench.runner", fromlist=["run_benchmark"]).run_benchmark
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise KeyboardInterrupt()
            return real_run(*args, **kwargs)

        monkeypatch.setattr("agentbench.cli.run_benchmark", flaky)

        result = runner.invoke(
            app, ["run", str(yaml_path), "--repeat", "5", "--results-dir", str(results_dir)]
        )

        assert result.exit_code == 130
        assert "preserved" in result.output
        # Trial 1 completed and was persisted before the interrupt.
        assert calls["count"] == 2
        run_dirs = list((results_dir / "demo").iterdir())
        assert len(run_dirs) == 1


class TestHistoryCommand:
    def test_lists_runs_newest_first_with_filters(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)

        runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(results_dir)])

        listing = runner.invoke(
            app, ["history", "--results-dir", str(results_dir), "--benchmark", "demo"]
        )

        assert listing.exit_code == 0
        assert "RUN ID" in listing.output
        assert "demo" in listing.output
        assert "PASS" in listing.output

    def test_status_filter_returns_only_matching_rows(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)
        runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(results_dir)])

        misses = runner.invoke(
            app,
            ["history", "--results-dir", str(results_dir), "--status", "agent_timeout"],
        )

        assert misses.exit_code == 0
        assert "0 run(s)" in misses.output

    def test_corrupted_index_exits_two_and_mentions_evidence(self, tmp_path):
        db_dir = tmp_path / "out" / ".agentbench"
        db_dir.mkdir(parents=True)
        (db_dir / "agentbench.db").write_bytes(b"garbage" * 100)

        result = runner.invoke(app, ["history", "--results-dir", str(tmp_path / "out")])

        assert result.exit_code == 2
        assert "evidence" in result.output


class TestShowCommand:
    def test_shows_evidence_for_known_run(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)
        runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(results_dir)])

        run_id = sorted((results_dir / "demo").iterdir())[0].name
        shown = runner.invoke(app, ["show", run_id, "--results-dir", str(results_dir)])

        assert shown.exit_code == 0, shown.output
        assert "demo" in shown.output
        assert "file-created" in shown.output
        assert "agent.stdout.log" in shown.output

    def test_unknown_run_id_exits_two(self, tmp_path):
        results_dir = tmp_path / "out"
        (results_dir / ".agentbench").mkdir(parents=True)

        result = runner.invoke(app, ["show", "20260822T000000Z-nosuch", "--results-dir", str(results_dir)])

        assert result.exit_code == 2


class TestCompareCommand:
    def test_aggregates_runs_of_a_benchmark(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)

        for _ in range(2):
            runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(results_dir)])

        comparison = runner.invoke(
            app, ["compare", "demo", "--results-dir", str(results_dir)],
            env={"COLUMNS": "300"},
        )

        assert comparison.exit_code == 0
        assert "claude-code" in comparison.output
        assert "100%" in comparison.output
        assert "2" in comparison.output

    def test_warns_on_mixed_commits(self, tmp_path):
        from agentbench.storage import ResultIndex

        results_dir = tmp_path / "out"
        payload = {
            "schema_version": 2,
            "run_id": "20260822T100000Z-aaa111",
            "trial": None,
            "benchmark": {"name": "demo", "repository": "r", "commit": "a" * 40,
                          "resolved_commit": "commit-one", "config_hash": "h1"},
            "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False, "model": None},
            "usage": None,
            "diff": {"files_changed": 1, "insertions": 1, "deletions": 0},
            "evaluations": [], "hidden_evaluations": [],
            "overall": {"status": "passed", "failure_reason": None,
                        "started_at": "t1", "finished_at": "t2", "duration_seconds": 5.0},
        }
        index = ResultIndex(results_dir / ".agentbench" / "agentbench.db")
        index.index_result(payload, result_dir=tmp_path)
        payload["run_id"] = "20260822T100001Z-bbb222"
        payload["benchmark"] = {**payload["benchmark"], "resolved_commit": "commit-two",
                                "config_hash": "h2"}
        index.index_result(payload, result_dir=tmp_path)

        comparison = runner.invoke(app, ["compare", "demo", "--results-dir", str(results_dir)])

        assert comparison.exit_code == 0
        assert "different resolved commits" in comparison.output

    def test_no_runs_prints_message_and_exits_zero(self, tmp_path):
        (tmp_path / "out").mkdir()

        result = runner.invoke(app, ["compare", "ghost", "--results-dir", str(tmp_path / "out")])

        assert result.exit_code == 0
        assert "No runs" in result.output
