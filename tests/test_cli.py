"""CLI tests: full `agentbench run` flow with a stubbed agent, no Claude session."""

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
        files={
            "README.md": "# demo\n",
            "check_agent_file.py": CHECKER,
        },
    )


def stub_writes_file(monkeypatch) -> None:
    """Install a stub agent adapter in place of Claude Code."""
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


class TestRunCommand:
    def test_successful_run_prints_summary_and_exits_zero(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"
        stub_writes_file(monkeypatch)

        result = runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(results_dir)])

        assert result.exit_code == 0, result.output
        assert "PASS" in result.output
        assert "file-created" in result.output
        run_dirs = list((results_dir / "demo").iterdir())
        assert len(run_dirs) == 1
        payload = json.loads((run_dirs[0] / "result.json").read_text(encoding="utf-8"))
        assert payload["overall"]["status"] == "passed"

    def test_failed_evaluation_exits_one(self, bench_repo, tmp_path, monkeypatch):
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)
        results_dir = tmp_path / "out"

        # No stub installed: the real registry would fail on claude-code, so
        # install an agent that runs but never creates the file.
        from agentbench.adapters.base import AgentAdapter, AgentInvocation

        class StubIdleAgent(AgentAdapter):
            name = "stub-idle"

            def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
                return AgentInvocation(argv=[sys.executable, "-c", "print('did nothing')"])

        monkeypatch.setattr("agentbench.cli.get_adapter", lambda _type: StubIdleAgent())

        result = runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(results_dir)])

        assert result.exit_code == 1, result.output
        assert "FAIL" in result.output
        assert "file-created" in result.output

    def test_missing_benchmark_file_exits_two(self, tmp_path):
        result = runner.invoke(app, ["run", str(tmp_path / "nope.yaml")])

        assert result.exit_code == 2
        assert "nope.yaml" in result.output

    def test_missing_agent_binary_exits_two_not_one(self, bench_repo, tmp_path, monkeypatch):
        # An unresolvable agent binary is a setup error (exit 2), never a
        # benchmark FAIL (exit 1) — CI keys on these codes.
        from agentbench.adapters.base import AgentAdapter, AgentInvocation

        class StubMissingBinary(AgentAdapter):
            name = "stub-missing-binary"

            def build_invocation(self, *, workspace: Path, prompt: str, agent_spec) -> AgentInvocation:
                return AgentInvocation(argv=["definitely-not-a-real-binary-xyz"])

        monkeypatch.setattr("agentbench.cli.get_adapter", lambda _type: StubMissingBinary())
        repo, sha = bench_repo
        yaml_path = write_benchmark_yaml(tmp_path, repo, sha)

        result = runner.invoke(app, ["run", str(yaml_path), "--results-dir", str(tmp_path / "out")])

        assert result.exit_code == 2, result.output
        assert "Run failed" in result.output

    def test_invalid_benchmark_content_exits_two(self, tmp_path):
        path = tmp_path / "benchmark.yaml"
        path.write_text("name: only-a-name\n", encoding="utf-8")

        result = runner.invoke(app, ["run", str(path)])

        assert result.exit_code == 2
