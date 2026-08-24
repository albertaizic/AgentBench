"""Setup-failure persistence and the failure-stage model.

A run whose environment breaks (unclonable source, missing agent binary,
unavailable Docker image) is evidence like any other: it persists
``setup_failed`` with a structured ``failure_stage`` instead of vanishing
into an exit code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentbench.adapters.base import AgentInvocation
from agentbench.models import AgentSpec, BenchmarkSpec, Evaluation
from agentbench.process import ProcessResult
from agentbench.runner import run_benchmark
from agentbench.stages import (
    STAGE_AGENT,
    STAGE_BACKEND_PREPARE,
    STAGE_EVALUATION,
    STAGE_WORKSPACE,
)
from agentbench.storage import ResultIndex, default_db_path
from agentbench.taxonomy import classify_run


@pytest.fixture
def bench_repo(make_git_repo):
    return make_git_repo(
        files={
            "README.md": "# demo\n",
            "check_agent_file.py": (
                "import pathlib, sys\n"
                "sys.exit(0 if pathlib.Path('agent_change.txt').exists() else 1)\n"
            ),
        }
    )


def make_spec(repo_path, sha, **overrides) -> BenchmarkSpec:
    fields = dict(
        name="demo",
        repository=str(repo_path),
        commit=sha,
        prompt="create agent_change.txt",
        agent=AgentSpec(type="command", argv=[sys.executable, "-c", "pass"]),
        evaluations=[Evaluation(name="file-created", command=f'"{sys.executable}" check_agent_file.py')],
    )
    fields.update(overrides)
    return BenchmarkSpec(**fields)


class TestSetupFailurePersistence:
    def test_unclonable_repository_persists_setup_failed_evidence(self, tmp_path):
        spec = make_spec(tmp_path / "no-such-repo", "0" * 40)

        outcome = run_benchmark(spec, results_root=tmp_path / "out")

        overall = outcome.result.overall
        assert overall["status"] == "setup_failed"
        assert overall["failure_stage"] == STAGE_WORKSPACE
        assert "clone" in (overall["failure_reason"] or "").lower()
        # Evidence landed on disk as a first-class result.json.
        payload = json.loads((outcome.run_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["overall"]["status"] == "setup_failed"
        assert payload["overall"]["failure_stage"] == STAGE_WORKSPACE
        assert payload["benchmark"]["name"] == "demo"

    def test_missing_agent_binary_is_backend_prepare_not_crash(self, bench_repo, tmp_path):
        repo_path, sha = bench_repo
        spec = make_spec(
            repo_path,
            sha,
            agent=AgentSpec(type="command", argv=["definitely-not-a-real-binary-xyz"]),
        )

        outcome = run_benchmark(spec, results_root=tmp_path / "out")

        assert outcome.result.overall["status"] == "setup_failed"
        assert outcome.result.overall["failure_stage"] == STAGE_BACKEND_PREPARE

    def test_docker_infra_failure_is_setup_not_agent_failure(self, bench_repo, tmp_path, monkeypatch):
        repo_path, sha = bench_repo

        class FakeDockerBackend:
            name = "docker"

            def run_agent(self, invocation, *, workspace, timeout, env):
                return ProcessResult(
                    exit_code=125,
                    stdout="",
                    stderr="docker: Unable to find image 'missing:v0'.",
                    duration_seconds=0.4,
                )

            def provenance(self):
                return {"backend": "docker", "image_requested": "missing:v0"}

            def cleanup(self):
                self.cleaned = True

        fake = FakeDockerBackend()
        import agentbench.runner as runner_mod

        monkeypatch.setattr(runner_mod, "make_backend", lambda spec, **kw: fake)
        monkeypatch.setattr(runner_mod, "DockerExecutionBackend", FakeDockerBackend)

        spec = make_spec(repo_path, sha)
        outcome = run_benchmark(spec, results_root=tmp_path / "out")

        assert outcome.result.overall["status"] == "setup_failed"
        assert outcome.result.overall["failure_stage"] == STAGE_BACKEND_PREPARE
        assert "Unable to find image" in (outcome.result.overall["failure_reason"] or "")
        # The docker CLI output that explained the failure survives as evidence.
        stderr_log = (outcome.run_dir / "agent.stderr.log").read_text(encoding="utf-8")
        assert "Unable to find image" in stderr_log

    def test_docker_infra_detection_real_predicate(self):
        from agentbench.backends.docker import is_infrastructure_failure

        missing_image = ProcessResult(
            exit_code=125, stdout="", stderr="docker: Unable to find image 'nope:v0'.", duration_seconds=0.5
        )
        real_agent_failure = ProcessResult(exit_code=3, stdout="", stderr="boom", duration_seconds=1.0)
        daemon_down = ProcessResult(exit_code=None, stdout="", stderr="Cannot connect to the Docker daemon", duration_seconds=0.2)

        assert is_infrastructure_failure(missing_image)
        assert is_infrastructure_failure(daemon_down)
        assert not is_infrastructure_failure(real_agent_failure)


class TestStageTimings:
    def test_happy_path_records_all_major_stages(self, bench_repo, tmp_path):
        repo_path, sha = bench_repo
        spec = make_spec(repo_path, sha, agent=AgentSpec(type="command", argv=[sys.executable, "-c", "pass"]))

        outcome = run_benchmark(spec, results_root=tmp_path / "out")

        timings = outcome.result.stage_timings
        assert timings is not None
        for stage in ("workspace", "backend_prepare", "agent", "evaluation", "persistence", "cleanup"):
            assert stage in timings, f"missing {stage} in {timings}"
            assert timings[stage] >= 0

    def test_source_preparation_provenance_recorded(self, bench_repo, tmp_path):
        repo_path, sha = bench_repo
        spec = make_spec(repo_path, sha, agent=AgentSpec(type="command", argv=[sys.executable, "-c", "pass"]))

        outcome = run_benchmark(spec, results_root=tmp_path / "out")

        prep = outcome.result.execution.get("source_preparation")
        assert prep is not None
        assert prep["cache_hit"] is False  # local fixtures never use the remote cache
        assert prep["duration_seconds"] >= 0


class TestClassificationStages:
    def test_failures_carry_their_stage(self):
        assert classify_run(
            agent_timed_out=True, agent_exit_code=None, evaluations_passed=False,
            has_evaluation_results=True, protected_violation=False,
        ).stage == STAGE_AGENT
        assert classify_run(
            agent_timed_out=False, agent_exit_code=0, evaluations_passed=False,
            has_evaluation_results=True, protected_violation=False,
        ).stage == STAGE_EVALUATION
        assert classify_run(
            agent_timed_out=False, agent_exit_code=0, evaluations_passed=True,
            has_evaluation_results=True, protected_violation=False,
        ).stage is None


class TestIndexIntegration:
    def test_setup_failed_runs_are_indexed_with_stage(self, tmp_path):
        spec = make_spec(tmp_path / "nope", "0" * 40)

        outcome = run_benchmark(spec, results_root=tmp_path / "out")
        index = ResultIndex(default_db_path(tmp_path / "out"))
        index.scan_results(tmp_path / "out")
        row = index.get_run(outcome.result.run_id)

        assert row is not None
        assert row["status"] == "setup_failed"
        assert row["failure_stage"] == STAGE_WORKSPACE


class TestErrorSanitization:
    def test_secret_values_are_masked_from_summaries(self, monkeypatch):
        from agentbench.runner import _safe_error_summary

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
        error = RuntimeError("request failed with key sk-super-secret-value at endpoint")

        summary = _safe_error_summary(error)

        assert "sk-super-secret-value" not in summary
        assert "***" in summary

    def test_long_errors_are_truncated(self):
        from agentbench.runner import _safe_error_summary

        summary = _safe_error_summary(RuntimeError("x" * 10_000))

        assert len(summary) <= 420
        assert summary.endswith("…[truncated]")
