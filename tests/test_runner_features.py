"""Runner tests for hidden evaluations, protected paths, usage, and identity."""

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
    "sys.stdin.read()\n"
    "pathlib.Path('agent_change.txt').write_text('x')\n"
    "print('agent done')\n"
)

STUB_IDLE = "import sys\nprint('did nothing')\n"


class StubWriteAgent(AgentAdapter):
    name = "stub-write"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
        return AgentInvocation(argv=[sys.executable, "-c", STUB_WRITES_FILE], input_text=prompt)


class StubIdleAgent(AgentAdapter):
    name = "stub-idle"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
        return AgentInvocation(argv=[sys.executable, "-c", STUB_IDLE])


@pytest.fixture
def bench_repo(make_git_repo):
    """Repo with an agent-target checker, mirroring other runner tests."""
    return make_git_repo(
        files={"README.md": "# demo\n", "src/app.py": "print('hello')\n"}
    )


class TestHiddenEvaluations:
    def bench_spec_with_hidden(self, bench_repo, tmp_path: Path) -> tuple[BenchmarkSpec, Path]:
        repo_path, sha = bench_repo
        benchmark_dir = tmp_path / "bench"
        (benchmark_dir / "hidden").mkdir(parents=True)
        (benchmark_dir / "hidden" / "test_agent_file.py").write_text(
            textwrap.dedent(
                """
                import os
                from pathlib import Path

                workspace = Path(os.environ["PYTHONPATH"].split(os.pathsep)[0])


                def test_agent_created_its_file():
                    assert (workspace / "agent_change.txt").exists()
                """
            ),
            encoding="utf-8",
        )
        spec = BenchmarkSpec(
            name="demo",
            repository=str(repo_path),
            commit=sha,
            prompt="create agent_change.txt",
            agent=AgentSpec(type="claude-code"),
            evaluations=[Evaluation(name="public-check", command=f'"{sys.executable}" -c "pass"')],
            hidden_evaluations=HiddenEvaluationSpec(
                source="hidden",
                evaluations=[Evaluation(name="behavioral", command='"{python}" -m pytest -q')],
            ),
        )
        return spec, benchmark_dir

    def test_hidden_evaluations_run_outside_the_workspace(self, bench_repo, tmp_path):
        spec, benchmark_dir = self.bench_spec_with_hidden(bench_repo, tmp_path)

        outcome = run_benchmark(
            spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
            benchmark_dir=benchmark_dir,
        )

        assert outcome.result.overall["status"] == "passed"
        assert len(outcome.result.hidden_evaluations) == 1
        assert outcome.result.hidden_evaluations[0]["passed"] is True
        # The hidden evaluator's own files must never land in the run diff.
        assert all(not p.startswith("hidden") for p in outcome.result.diff["changed_paths"])

    def test_failing_hidden_evaluation_fails_the_run(self, bench_repo, tmp_path):
        spec, benchmark_dir = self.bench_spec_with_hidden(bench_repo, tmp_path)
        (benchmark_dir / "hidden" / "test_agent_file.py").write_text(
            "def test_never_true():\n    assert False\n", encoding="utf-8"
        )

        outcome = run_benchmark(
            spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
            benchmark_dir=benchmark_dir,
        )

        # Public evals passed; the hidden one decides the outcome.
        assert outcome.result.evaluations[0]["passed"] is True
        assert outcome.result.hidden_evaluations[0]["passed"] is False
        assert outcome.result.overall["status"] == "evaluation_failed"

    def test_missing_hidden_source_is_invalid_result_not_crash(self, bench_repo, tmp_path):
        repo_path, sha = bench_repo
        spec = BenchmarkSpec(
            name="demo",
            repository=str(repo_path),
            commit=sha,
            prompt="x",
            agent=AgentSpec(type="claude-code"),
            evaluations=[Evaluation(name="public", command=f'"{sys.executable}" -c "pass"')],
            hidden_evaluations=HiddenEvaluationSpec(
                source="missing-dir",
                evaluations=[Evaluation(name="behavioral", command='"{python}" -m pytest -q')],
            ),
        )

        outcome = run_benchmark(
            spec,
            adapter=StubWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
            benchmark_dir=tmp_path / "bench",
        )

        assert outcome.result.overall["status"] == "invalid_result"


class TestProtectedPaths:
    class ProtectedWriteAgent(AgentAdapter):
        name = "stub-protected"

        def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
            script = (
                "import pathlib\n"
                "p = pathlib.Path('tests'); p.mkdir(exist_ok=True)\n"
                "(p / 'test_cheat.py').write_text('def test_ok(): pass\\n')\n"
            )
            return AgentInvocation(argv=[sys.executable, "-c", script])

    @staticmethod
    def make_protected_repo(make_git_repo) -> tuple[Path, str]:
        return make_git_repo(
            files={"README.md": "# demo\n", "tests/test_original.py": "def test_t(): pass\n"}
        )

    @staticmethod
    def protected_spec(repo_path: Path, sha: str, *, fail_on_violation: bool) -> BenchmarkSpec:
        return BenchmarkSpec(
            name="demo",
            repository=str(repo_path),
            commit=sha,
            prompt="modify a protected test file",
            agent=AgentSpec(type="claude-code"),
            evaluations=[Evaluation(name="always-pass", command=f'"{sys.executable}" -c "pass"')],
            protected_paths=["tests/**"],
            fail_on_protected_path_violation=fail_on_violation,
        )

    def test_violation_recorded_without_failing_by_default(self, make_git_repo, tmp_path):
        repo_path, sha = self.make_protected_repo(make_git_repo)

        outcome = run_benchmark(
            self.protected_spec(repo_path, sha, fail_on_violation=False),
            adapter=self.ProtectedWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
        )

        protected = outcome.result.protected_paths
        assert protected["violations"][0]["path"] == "tests/test_cheat.py"
        assert protected["fail_on_violation"] is False
        # Evidence kept; the run itself is still judged by evaluations alone.
        assert outcome.result.overall["status"] == "passed"

    def test_fail_flag_classifies_as_protected_violation(self, make_git_repo, tmp_path):
        repo_path, sha = self.make_protected_repo(make_git_repo)

        outcome = run_benchmark(
            self.protected_spec(repo_path, sha, fail_on_violation=True),
            adapter=self.ProtectedWriteAgent(),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
        )

        assert outcome.result.overall["status"] == "protected_path_violation"
        # Evaluations still ran and their evidence is preserved.
        assert outcome.result.evaluations[0]["passed"] is True


class TestUsageAndIdentity:
    class MetricsAdapter(AgentAdapter):
        name = "stub-metrics"

        def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
            return AgentInvocation(argv=[sys.executable, "-c", "print('ok')"], input_text=prompt)

        def parse_output(self, stdout: str) -> AgentOutput:
            return AgentOutput(
                usage=AgentUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    cost_usd=0.01,
                    num_turns=2,
                    session_id="sess-1",
                ),
                model="model-x",
            )

        def cli_version(self) -> str:
            return "stub-cli 1.2.3"

    @pytest.fixture
    def simple_spec(self, make_git_repo) -> BenchmarkSpec:
        repo_path, sha = make_git_repo(files={"README.md": "# x\n"})
        return BenchmarkSpec(
            name="demo",
            repository=str(repo_path),
            commit=sha,
            prompt="do nothing",
            agent=AgentSpec(type="claude-code"),
            evaluations=[Evaluation(name="pass", command=f'"{sys.executable}" -c "pass"')],
        )

    def run_simple(self, simple_spec, tmp_path: Path, adapter: AgentAdapter, **kwargs):
        return run_benchmark(
            simple_spec,
            adapter=adapter,
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
            **kwargs,
        )

    def test_adapter_metrics_flow_into_result(self, simple_spec, tmp_path):
        outcome = self.run_simple(simple_spec, tmp_path, self.MetricsAdapter())

        assert outcome.result.agent["model"] == "model-x"
        assert outcome.result.usage["total_tokens"] == 15
        assert outcome.result.usage["cost_usd"] == 0.01
        assert outcome.result.usage["session_id"] == "sess-1"

    def test_stub_output_leaves_usage_null(self, simple_spec, tmp_path):
        outcome = self.run_simple(simple_spec, tmp_path, StubIdleAgent())

        assert outcome.result.usage is None
        assert outcome.result.agent["model"] is None

    def test_run_id_matches_directory_name(self, simple_spec, tmp_path):
        outcome = self.run_simple(simple_spec, tmp_path, StubIdleAgent())

        assert outcome.run_dir.name == outcome.result.run_id

    def test_trial_number_recorded(self, simple_spec, tmp_path):
        outcome = self.run_simple(simple_spec, tmp_path, StubIdleAgent(), trial=7)

        assert outcome.result.trial == 7

    def test_environment_metadata_captured(self, simple_spec, tmp_path):
        outcome = self.run_simple(simple_spec, tmp_path, self.MetricsAdapter())

        environment = outcome.result.environment
        assert environment["agent_cli_version"] == "stub-cli 1.2.3"
        assert environment["agentbench_version"]
        assert environment["python_version"]
        assert environment["platform"]
        assert "git version" in (environment["git_version"] or "")

    def test_config_snapshot_persisted(self, simple_spec, tmp_path):
        outcome = self.run_simple(simple_spec, tmp_path, StubIdleAgent())

        payload = json.loads((outcome.run_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["config"]["name"] == "demo"
        assert payload["benchmark"]["config_hash"] == simple_spec.config_hash()
