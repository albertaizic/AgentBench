"""End-to-end runner tests using a stub agent — never a real Claude session."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from agentbench.adapters.base import AgentAdapter, AgentInvocation, AgentOutput, AgentUsage
from agentbench.models import AgentSpec, BenchmarkSpec, Evaluation, HiddenEvaluationSpec
from agentbench.runner import run_benchmark

STUB_WRITES_FILE = (
    "import sys, pathlib\n"
    "prompt = sys.stdin.read()\n"
    "pathlib.Path('agent_change.txt').write_text('touched via: ' + prompt)\n"
    "print('agent done')\n"
)

STUB_DOES_NOTHING = "import sys\nprint('did nothing')\nsys.exit(0)\n"

STUB_FAILS = "import sys\nprint('agent exploded')\nsys.exit(3)\n"


class StubWriteAgent(AgentAdapter):
    name = "stub-write"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        return AgentInvocation(argv=[sys.executable, "-c", STUB_WRITES_FILE], input_text=prompt)


class StubIdleAgent(AgentAdapter):
    name = "stub-idle"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        return AgentInvocation(argv=[sys.executable, "-c", STUB_DOES_NOTHING])


class StubBrokenAgent(AgentAdapter):
    name = "stub-broken"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        raise RuntimeError("adapter could not build command")


@pytest.fixture
def bench_repo(make_git_repo):
    """Repo whose checker script passes only if the agent created its file."""
    return make_git_repo(
        files={
            "README.md": "# demo\n",
            "src/app.py": "print('hello')\n",
            "check_agent_file.py": (
                "import pathlib, sys\n"
                "sys.exit(0 if pathlib.Path('agent_change.txt').exists() else 1)\n"
            ),
        }
    )


@pytest.fixture
def bench_spec(bench_repo) -> BenchmarkSpec:
    repo_path, sha = bench_repo
    return BenchmarkSpec(
        name="demo",
        repository=str(repo_path),
        commit=sha,
        prompt="create agent_change.txt",
        agent=AgentSpec(type="claude-code"),
        evaluations=[Evaluation(name="file-created", command=f'"{sys.executable}" check_agent_file.py')],
    )


class StubSleepingAgent(AgentAdapter):
    name = "stub-sleeping"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        return AgentInvocation(argv=[sys.executable, "-c", "import time; time.sleep(60)"])


class TestRunBenchmark:
    def test_full_pipeline_passes_and_records_provenance(self, bench_spec, tmp_path):
        results_root = tmp_path / "out"

        outcome = run_benchmark(
            bench_spec,
            adapter=StubWriteAgent(),
            results_root=results_root,
            workspace_parent=tmp_path / "workspaces",
        )

        assert outcome.result.overall["status"] == "passed"
        assert (outcome.run_dir / "result.json").exists()
        # The agent really ran inside the checked-out workspace...
        assert outcome.result.agent["exit_code"] == 0
        assert outcome.result.agent["duration_seconds"] > 0
        # ...its untracked file shows up in the diff...
        assert outcome.result.diff["files_changed"] == 1
        # ...the evaluated commit is the configured one...
        assert outcome.result.benchmark["resolved_commit"] == bench_spec.commit
        # ...and the agent's captured output actually reached the sidecars.
        agent_stdout = (outcome.run_dir / "agent.stdout.log").read_text(encoding="utf-8")
        assert "agent done" in agent_stdout
        first_eval_log = outcome.run_dir / "evals" / "000-file-created.stdout.log"
        assert first_eval_log.exists()

    def test_workspace_is_cleaned_up_after_the_run(self, bench_spec, tmp_path):
        workspace_parent = tmp_path / "workspaces"

        outcome = run_benchmark(
            bench_spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=workspace_parent,
        )

        assert outcome.workspace_path is None
        assert list(workspace_parent.iterdir()) == []

    def test_failing_evaluation_marks_run_failed(self, bench_spec, tmp_path):
        # Agent writes the file; evaluation demands it be absent.
        bench_spec.evaluations = [
            Evaluation(name="impossible", command=f'"{sys.executable}" -c "raise SystemExit(1)"')
        ]

        outcome = run_benchmark(
            bench_spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
        )

        assert outcome.result.overall["status"] == "evaluation_failed"
        assert outcome.result.evaluations[0]["passed"] is False

    def test_agent_failure_is_recorded_but_evaluations_still_decide(self, bench_spec, tmp_path):
        # Agent exits 3 without writing anything; eval fails because the file
        # is missing. PASS/FAIL comes from evaluation exit codes only.
        class StubFailingAgent(AgentAdapter):
            name = "stub-failing"

            def build_invocation(self, *, workspace, prompt, agent_spec):
                return AgentInvocation(argv=[sys.executable, "-c", STUB_FAILS])

        outcome = run_benchmark(
            bench_spec,
            adapter=StubFailingAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
        )

        assert outcome.result.agent["exit_code"] == 3
        assert outcome.result.overall["status"] == "agent_failed"

    def test_idle_agent_leaves_empty_diff(self, bench_spec, tmp_path):
        outcome = run_benchmark(
            bench_spec,
            adapter=StubIdleAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
        )

        assert outcome.result.diff["files_changed"] == 0
        assert outcome.result.overall["status"] == "evaluation_failed"  # checker file missing

    def test_adapter_error_propagates_but_workspace_still_cleaned(self, bench_spec, tmp_path):
        workspace_parent = tmp_path / "workspaces"

        with pytest.raises(RuntimeError, match="adapter could not build command"):
            run_benchmark(
                bench_spec,
                adapter=StubBrokenAgent(),
                results_root=tmp_path / "out",
                workspace_parent=workspace_parent,
            )

        assert list(workspace_parent.iterdir()) == []

    def test_keep_workspace_retains_directory_for_debugging(self, bench_spec, tmp_path):
        workspace_parent = tmp_path / "workspaces"

        outcome = run_benchmark(
            bench_spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=workspace_parent,
            keep_workspace=True,
        )

        assert outcome.workspace_path is not None
        assert outcome.workspace_path.exists()
        assert (outcome.workspace_path / "agent_change.txt").exists()

    def test_adapter_resolved_from_spec_when_not_injected(self, bench_spec, tmp_path, monkeypatch):
        monkeypatch.setattr("agentbench.runner.get_adapter", lambda _type: StubWriteAgent())

        outcome = run_benchmark(
            bench_spec,
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
        )

        assert outcome.result.overall["status"] == "passed"

    def test_timeout_override_bounds_the_agent_and_run_still_completes(self, bench_spec, tmp_path):
        # Bounded runtime must hold end to end: agent killed, failure recorded,
        # results persisted, and the workspace's locks released for cleanup.
        workspace_parent = tmp_path / "workspaces"

        outcome = run_benchmark(
            bench_spec,
            adapter=StubSleepingAgent(),
            results_root=tmp_path / "out",
            workspace_parent=workspace_parent,
            timeout_seconds=2.0,
        )

        assert outcome.result.agent["timed_out"] is True
        assert outcome.result.overall["status"] == "agent_timeout"
        assert (outcome.run_dir / "result.json").exists()
        assert list(workspace_parent.iterdir()) == []
