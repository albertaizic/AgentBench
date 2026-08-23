"""Tests for experiment planning, manifests, resume semantics, and export."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml as pyyaml
from typer.testing import CliRunner

from agentbench.cli import app
from agentbench.experiments import (
    ExperimentManifest,
    cell_key,
    experiment_id_for,
    new_manifest,
    plan_cells,
)
from agentbench.models import ExperimentSpec
from agentbench.storage import ResultIndex

runner = CliRunner()


@pytest.fixture
def stub_agents(monkeypatch):
    """claude-code → writing stub; command → no-op stub."""
    from agentbench.adapters.base import AgentAdapter, AgentInvocation

    class StubClaude(AgentAdapter):
        name = "claude-code"

        def build_invocation(self, *, workspace, prompt, agent_spec) -> AgentInvocation:
            return AgentInvocation(argv=[sys_executable(), "-c", STUB_WRITE])

    class StubNoop(AgentAdapter):
        name = "command"

        def build_invocation(self, *, workspace, prompt, agent_spec) -> AgentInvocation:
            return AgentInvocation(argv=[sys_executable(), "-c", "print('noop')"])

    real = {
        "claude-code": StubClaude(),
        "command": StubNoop(),
    }
    monkeypatch.setattr(
        "agentbench.cli.get_adapter", lambda t: real[t]
    )
    return real


STUB_WRITE = (
    "import sys, pathlib\n"
    "sys.stdin.read()\n"
    "pathlib.Path('agent_change.txt').write_text('x')\n"
)


def sys_executable() -> str:
    return sys.executable


def write_corpus(tmp_path: Path, monkeypatch) -> dict[str, tuple[Path, str]]:
    """Two tiny corpus benchmarks discoverable by name."""
    from conftest import init_repo

    corpus = tmp_path / "benchmarks"
    corpus.mkdir()
    specs = {}
    for name in ("alpha", "beta"):
        bench_dir = corpus / name
        repo = bench_dir / "fixture"
        sha = init_repo(
            repo,
            files={
                "README.md": f"# {name}\n",
                "check_agent_file.py": (
                    "import pathlib, sys\n"
                    "sys.exit(0 if pathlib.Path('agent_change.txt').exists() else 1)\n"
                ),
            },
        )
        manifest = bench_dir / "benchmark.yaml"
        manifest.write_text(yaml_text(str(repo.as_posix()), sha), encoding="utf-8")
        specs[name] = (manifest, sha)
    # Discovery must find these: run the CLI from tmp_path.
    monkeypatch.chdir(tmp_path)
    return specs


def yaml_text(repository: str, sha: str) -> str:
    return (
        f"name: PLACEHOLDER\n"
        f"repository: {repository}\n"
        f"commit: {sha}\n"
        f"prompt: create agent_change.txt\n"
        f"agent:\n  type: claude-code\n"
        f"evaluations:\n  - name: file-created\n"
        f"    command: '\"{sys.executable}\" check_agent_file.py'\n"
    )


def write_experiment_file(tmp_path: Path, repeat: int = 2) -> Path:
    path = tmp_path / "experiment.yaml"
    path.write_text(
        pyyaml.safe_dump(
            {
                "name": "matrix",
                "benchmarks": ["alpha", "beta"],
                "configs": [
                    {"name": "solver",
                     "agent": {"type": "claude-code"}},
                    {"name": "noop",
                     "agent": {"type": "command",
                               "argv": [sys.executable, "-c", "print('noop')"]}},
                ],
                "repeat": repeat,
                "results_dir": str(tmp_path / "results"),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


class TestExperimentExecution:
    def test_matrix_runs_all_cells_and_persists_manifest(
        self, tmp_path, monkeypatch, stub_agents
    ):
        write_corpus(tmp_path, monkeypatch)
        experiment_file = write_experiment_file(tmp_path, repeat=2)

        result = runner.invoke(app, ["experiment", str(experiment_file)])

        assert result.exit_code == 0, result.output
        assert "8 runs" in result.output  # 2 x 2 x 2
        results_dir = tmp_path / "results"
        manifest_files = list((results_dir / "experiments").glob("*/experiment.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
        assert manifest["planned_cells"] == 8
        assert len(manifest["completed"]) == 8
        assert manifest["interrupted"] is False
        # Every run row is linked to the experiment.
        index = ResultIndex(results_dir / ".agentbench" / "agentbench.db")
        rows = index.query(limit=None)
        assert all(r["experiment_id"] == manifest["experiment_id"] for r in rows)

    def test_solver_config_passes_and_noop_fails(self, tmp_path, monkeypatch, stub_agents):
        write_corpus(tmp_path, monkeypatch)
        experiment_file = write_experiment_file(tmp_path, repeat=1)

        result = runner.invoke(app, ["experiment", str(experiment_file)])

        # Experiment completion is success (exit 0): cell outcomes are DATA.
        # The solver config passes its evaluations; the noop agent fails them.
        assert result.exit_code == 0, result.output
        solver_passes = result.output.count("PASS")
        noop_fails = result.output.count("FAIL")
        assert solver_passes >= 2
        assert noop_fails >= 2

    def test_interrupt_marks_incomplete_and_preserves_runs(
        self, tmp_path, monkeypatch, stub_agents
    ):
        write_corpus(tmp_path, monkeypatch)
        experiment_file = write_experiment_file(tmp_path, repeat=2)
        results_dir = tmp_path / "results"

        import agentbench.cli as cli_module

        real_run = cli_module.run_benchmark
        calls = {"count": 0}

        def interrupting_run(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] > 3:
                raise KeyboardInterrupt()
            return real_run(*args, **kwargs)

        monkeypatch.setattr("agentbench.cli.run_benchmark", interrupting_run)

        result = runner.invoke(app, ["experiment", str(experiment_file)])

        assert result.exit_code == 130
        manifest_files = list((results_dir / "experiments").glob("*/experiment.json"))
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
        assert manifest["interrupted"] is True
        assert 0 < len(manifest["completed"]) < 8
        # Completed runs are on disk and indexed.
        assert len(list((results_dir / ".agentbench").glob("agentbench.db"))) == 1


class TestResume:
    def test_resume_completes_only_missing_cells(self, tmp_path, monkeypatch, stub_agents):
        write_corpus(tmp_path, monkeypatch)
        experiment_file = write_experiment_file(tmp_path, repeat=2)
        results_dir = tmp_path / "results"

        import agentbench.cli as cli_module

        real_run = cli_module.run_benchmark
        calls = {"count": 0}

        def interrupting_run(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] > 5:
                raise KeyboardInterrupt()
            return real_run(*args, **kwargs)

        monkeypatch.setattr("agentbench.cli.run_benchmark", interrupting_run)
        first = runner.invoke(app, ["experiment", str(experiment_file)])
        assert first.exit_code == 130
        completed_first = calls["count"]

        experiment_id = sorted(
            (results_dir / "experiments").iterdir()
        )[0].name

        # Fresh budget for the resumed session: it must be able to finish.
        calls["count"] = 0
        resume = runner.invoke(
            app, ["experiment", str(experiment_file), "--resume", experiment_id]
        )

        assert resume.exit_code == 0, resume.output
        assert "skipping" in resume.output
        manifest = json.loads(
            (results_dir / "experiments" / experiment_id / "experiment.json")
            .read_text(encoding="utf-8")
        )
        assert len(manifest["completed"]) == 8
        assert manifest["interrupted"] is False
        # Resume executed exactly the missing cells (8 - preserved) and
        # skipped the rest.
        assert calls["count"] == 8 - (completed_first - 1)
        assert completed_first < 8

    def test_resume_rejects_changed_config(self, tmp_path, monkeypatch, stub_agents):
        write_corpus(tmp_path, monkeypatch)
        experiment_file = write_experiment_file(tmp_path, repeat=1)
        results_dir = tmp_path / "results"

        runner.invoke(app, ["experiment", str(experiment_file)])
        experiment_id = sorted((results_dir / "experiments").iterdir())[0].name

        # Mutate the experiment: different agent for the same config name.
        mutated = pyyaml.safe_load(experiment_file.read_text(encoding="utf-8"))
        mutated["configs"][0]["agent"]["type"] = "command"
        mutated["configs"][0]["agent"]["argv"] = [sys.executable, "-c", "print('x')"]
        experiment_file.write_text(pyyaml.safe_dump(mutated), encoding="utf-8")

        result = runner.invoke(
            app, ["experiment", str(experiment_file), "--resume", experiment_id]
        )

        assert result.exit_code == 2
        assert "Cannot resume" in result.output


class TestExperimentPlanning:
    def test_plan_covers_full_matrix_with_stable_keys(self, tmp_path):
        spec = ExperimentSpec(
            name="m",
            benchmarks=["a", "b"],
            configs=[
                {"name": "c1", "agent": {"type": "command", "argv": ["x"]}},
            ],
            repeat=2,
        )
        manifests = {"a": tmp_path / "a.yaml", "b": tmp_path / "b.yaml"}
        plans = plan_cells(spec, manifests, {"a": "hash-a", "b": "hash-b"})

        assert len(plans) == 4
        keys = [p.cell_key for p in plans]
        assert len(set(keys)) == 4  # every cell unique
        assert plan_cells(spec, manifests, {"a": "hash-a", "b": "hash-b"})[0].cell_key == keys[0]

    def test_unknown_benchmark_rejected_at_planning(self, tmp_path):
        spec = ExperimentSpec(
            name="m",
            benchmarks=["ghost"],
            configs=[{"name": "c1", "agent": {"type": "command", "argv": ["x"]}}],
        )

        from agentbench.experiments import ExperimentError

        with pytest.raises(ExperimentError, match="ghost"):
            plan_cells(spec, {}, {})

    def test_manifest_roundtrip_and_cell_done(self, tmp_path):
        manifest = new_manifest(
            ExperimentSpec(
                name="m",
                benchmarks=["a"],
                configs=[{"name": "c1", "agent": {"type": "command", "argv": ["x"]}}],
                repeat=1,
            ),
            "20260823T000000Z-deadbe",
            tmp_path,
        )
        manifest.benchmark_identities = {"a": "hash-a"}
        key = cell_key("a", "c1", "hash-a", manifest.config_identities["c1"], 1)
        assert manifest.cell_done(key) is False
        manifest.record({"cell_key": key, "status": "passed"})

        assert manifest.cell_done(key) is True

        from agentbench.experiments import load_manifest, save_manifest

        path = save_manifest(manifest, tmp_path / "exp")
        reloaded = load_manifest(path)
        assert reloaded.cell_done(key) is True


class TestAgentOverride:
    def test_experiment_config_agent_replaces_benchmark_agent(self, tmp_path):
        """Regression: the experiment config's agent must drive the run.

        v0.3-rc bug: the runner built invocations from the benchmark's own
        agent spec, so experiment configs were silently ignored (empty argv).
        """
        from agentbench.adapters import get_adapter
        from agentbench.models import AgentSpec, BenchmarkSpec, Evaluation
        from agentbench.runner import run_benchmark
        from conftest import init_repo

        repo = tmp_path / "repo"
        checker = (
            "import pathlib, sys\n"
            "sys.exit(0 if pathlib.Path('agent_change.txt').exists() else 1)\n"
        )
        sha = init_repo(repo, files={"check_agent_file.py": checker})
        spec = BenchmarkSpec(
            name="override-demo",
            repository=str(repo),
            commit=sha,
            prompt="irrelevant for stub",
            agent=AgentSpec(type="claude-code"),  # benchmark says claude...
            evaluations=[Evaluation(name="file-created",
                                    command=f'"{sys.executable}" check_agent_file.py')],
        )
        override = AgentSpec(
            type="command",
            argv=[sys.executable, "-c",
                  "import pathlib; pathlib.Path('agent_change.txt').write_text('x')"],
        )

        outcome = run_benchmark(
            spec,
            adapter=get_adapter(override.type),
            results_root=tmp_path / "out",
            workspace_parent=tmp_path / "workspaces",
            agent_override=override,
        )

        assert outcome.result.overall["status"] == "passed"
        # Identity must reflect the EFFECTIVE agent, not the benchmark's.
        assert outcome.result.config["agent"] == override.model_dump(mode="json")
