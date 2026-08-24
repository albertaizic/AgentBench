"""Parallel experiment execution through the CLI: --jobs, --max-runs, resume.

Uses stubbed command agents so no model, Docker daemon, or network is needed;
each cell still runs a real subprocess against a real cloned workspace.
"""

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


@pytest.fixture
def bench_repo(make_git_repo):
    return make_git_repo(
        name="par-repo",
        files={"README.md": "# demo\n", "check_agent_file.py": CHECKER},
    )


def write_experiment_yaml(tmp_path: Path, repo: Path, sha: str, repeat: int = 2) -> Path:
    path = tmp_path / "matrix.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            name: parmatrix
            benchmarks:
              - parbench
            configs:
              - name: stub-agent
                agent:
                  type: command
                  argv: ["{{python}}", "-c", "import sys,pathlib;sys.stdin.read();pathlib.Path('agent_change.txt').write_text('x')"]
            repeat: {repeat}
            results_dir: results
            """
        ),
        encoding="utf-8",
    )
    return path


def write_benchmark_manifest(tmp_path: Path, repo: Path, sha: str) -> Path:
    bench_dir = tmp_path / "benchmarks" / "parbench"
    bench_dir.mkdir(parents=True)
    manifest = bench_dir / "benchmark.yaml"
    manifest.write_text(
        textwrap.dedent(
            f"""\
            name: parbench
            repository: {repo.as_posix()}
            commit: {sha}
            prompt: create agent_change.txt
            agent:
              type: command
              argv: ["{{python}}", "-c", "pass"]
            evaluations:
              - name: file-created
                command: '"{sys.executable}" check_agent_file.py'
            """
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def parallel_env(tmp_path, bench_repo, monkeypatch):
    repo, sha = bench_repo
    manifest = write_benchmark_manifest(tmp_path, repo, sha)
    monkeypatch.setattr("agentbench.cli.find_manifest", lambda name, extra_root=None: manifest)
    # Run from tmp so results land under tmp/results.
    monkeypatch.chdir(tmp_path)
    return tmp_path, write_experiment_yaml(tmp_path, repo, sha)


class TestParallelExperiment:
    def test_jobs_two_completes_whole_matrix(self, parallel_env):
        tmp_path, yaml_path = parallel_env

        result = runner.invoke(
            app,
            ["experiment", str(yaml_path), "--jobs", "2"],
        )

        assert result.exit_code == 0, result.output
        manifest_dir = next((tmp_path / "results" / "experiments").iterdir())
        record = json.loads((manifest_dir / "experiment.json").read_text(encoding="utf-8"))
        assert len(record["completed"]) == 2  # 1 benchmark × 1 config × repeat 2
        statuses = {cell["status"] for cell in record["completed"]}
        assert statuses == {"passed"}
        # Every cell has its own persisted evidence.
        run_dirs = [Path(cell["run_dir"]) for cell in record["completed"]]
        assert all((d / "result.json").exists() for d in run_dirs)
        assert len({d.name for d in run_dirs}) == len(run_dirs)  # unique run dirs

    def test_jobs_above_cell_count_is_fine(self, parallel_env):
        tmp_path, yaml_path = parallel_env

        result = runner.invoke(
            app,
            ["experiment", str(yaml_path), "--jobs", "16"],
        )

        assert result.exit_code == 0, result.output
        experiments_root = tmp_path / "results" / "experiments"
        record = json.loads(
            (next(experiments_root.iterdir()) / "experiment.json").read_text(encoding="utf-8")
        )
        assert len(record["completed"]) == 2

    def test_max_runs_stops_early_and_resume_completes_without_duplicates(self, parallel_env):
        tmp_path, yaml_path = parallel_env

        first = runner.invoke(
            app,
            ["experiment", str(yaml_path), "--jobs", "1", "--max-runs", "1"],
        )
        assert first.exit_code == 0, first.output
        experiments_root = tmp_path / "results" / "experiments"
        experiment_id = next(experiments_root.iterdir()).name
        partial = json.loads(
            (experiments_root / experiment_id / "experiment.json").read_text(encoding="utf-8")
        )
        assert len(partial["completed"]) == 1

        second = runner.invoke(
            app,
            ["experiment", str(yaml_path), "--resume", experiment_id],
        )

        assert second.exit_code == 0, second.output
        final = json.loads(
            (experiments_root / experiment_id / "experiment.json").read_text(encoding="utf-8")
        )
        assert len(final["completed"]) == 2
        keys = [cell["cell_key"] for cell in final["completed"]]
        assert len(keys) == len(set(keys)), "resume must not duplicate cells"

    def test_one_broken_benchmark_does_not_block_others(self, tmp_path, bench_repo, monkeypatch):
        repo, sha = bench_repo
        good_manifest = write_benchmark_manifest(tmp_path, repo, sha)
        broken_dir = tmp_path / "benchmarks" / "brokenbench"
        broken_dir.mkdir(parents=True)
        (broken_dir / "benchmark.yaml").write_text(
            textwrap.dedent(
                f"""\
                name: brokenbench
                repository: {tmp_path / 'does-not-exist'}
                commit: {'a' * 40}
                prompt: unreachable
                agent:
                  type: command
                  argv: ["{{python}}", "-c", "pass"]
                evaluations:
                  - name: never
                    command: '"{sys.executable}" true'
                """
            ),
            encoding="utf-8",
        )

        def fake_find(name, extra_root=None):
            if name == "parbench":
                return good_manifest
            return broken_dir / "benchmark.yaml"

        monkeypatch.setattr("agentbench.cli.find_manifest", fake_find)
        monkeypatch.chdir(tmp_path)
        yaml_path = tmp_path / "mix.yaml"
        yaml_path.write_text(
            textwrap.dedent(
                """\
                name: mixmatrix
                benchmarks:
                  - parbench
                  - brokenbench
                configs:
                  - name: stub-agent
                    agent:
                      type: command
                      argv: ["{python}", "-c", "import sys,pathlib;sys.stdin.read();pathlib.Path('agent_change.txt').write_text('x')"]
                repeat: 1
                results_dir: results
                """
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            ["experiment", str(yaml_path), "--jobs", "2"],
        )

        assert result.exit_code == 0, result.output
        experiments_root = tmp_path / "results" / "experiments"
        record = json.loads(
            (next(experiments_root.iterdir()) / "experiment.json").read_text(encoding="utf-8")
        )
        by_benchmark = {cell["benchmark"]: cell["status"] for cell in record["completed"]}
        assert by_benchmark["parbench"] == "passed"
        assert by_benchmark["brokenbench"] == "setup_failed"
        # The setup failure produced real evidence with a stage, not just a note.
        failed_run_dir = next(
            Path(cell["run_dir"])
            for cell in record["completed"]
            if cell["benchmark"] == "brokenbench"
        )
        payload = json.loads((failed_run_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["overall"]["failure_stage"] == "workspace"


class TestMaxRunsHardCap:
    """Regression: --max-runs is a hard cap enforced at submission time.

    Before the fix, ``experiment --jobs 2 --max-runs 2`` on four cells executed
    three: the budget was only consulted after completions, so a replacement
    cell was submitted before the cap ever fired. These tests count real agent
    process launches (the stub appends to a log), not manifest bookkeeping.
    """

    def make_env(self, tmp_path, bench_repo, monkeypatch, *, agent_creates_file: bool):
        repo, sha = bench_repo
        manifest = write_benchmark_manifest(tmp_path, repo, sha)
        monkeypatch.setattr("agentbench.cli.find_manifest", lambda name, extra_root=None: manifest)
        monkeypatch.chdir(tmp_path)

        starts_dir = tmp_path / "starts"
        starts_dir.mkdir()
        tail = (
            "sys.stdin.read()\npathlib.Path('agent_change.txt').write_text('x')\n"
            if agent_creates_file
            else "sys.stdin.read()\n"  # finishes cleanly but fails evaluation
        )
        agent_script = tmp_path / "counting_agent.py"
        agent_script.write_text(
            "import pathlib, sys, uuid\n"
            f"starts_dir = pathlib.Path({starts_dir.as_posix()!r})\n"
            "(starts_dir / f'{uuid.uuid4().hex}.start').write_text("
            "'start', encoding='utf-8')\n"
            + tail,
            encoding="utf-8",
        )
        yaml_path = tmp_path / "capped.yaml"
        yaml_path.write_text(
            textwrap.dedent(
                f"""\
                name: cappedmatrix
                benchmarks:
                  - parbench
                configs:
                  - name: stub-agent
                    agent:
                      type: command
                      argv: ["{{python}}", "{agent_script.as_posix()}"]
                repeat: 4
                results_dir: results
                """
            ),
            encoding="utf-8",
        )
        return tmp_path, yaml_path, starts_dir

    @pytest.fixture
    def four_cell_env(self, tmp_path, bench_repo, monkeypatch):
        return self.make_env(tmp_path, bench_repo, monkeypatch, agent_creates_file=True)

    @staticmethod
    def starts(starts_dir: Path) -> int:
        if not starts_dir.exists():
            return 0
        return sum(
            1
            for marker in starts_dir.glob("*.start")
            if marker.is_file()
        )

    @staticmethod
    def load_manifest(tmp_path: Path, experiment_id: str | None = None) -> dict:
        root = tmp_path / "results" / "experiments"
        dir_ = root / experiment_id if experiment_id else next(root.iterdir())
        return json.loads((dir_ / "experiment.json").read_text(encoding="utf-8"))

    def test_jobs2_maxruns2_executes_exactly_two_of_four(self, four_cell_env):
        tmp_path, yaml_path, starts_dir = four_cell_env

        result = runner.invoke(
            app, ["experiment", str(yaml_path), "--jobs", "2", "--max-runs", "2"],
        )

        assert result.exit_code == 0, result.output
        assert self.starts(starts_dir) == 2  # real process launches, not bookkeeping
        assert len(list((tmp_path / "results").glob("**/result.json"))) == 2
        assert "Stopped after 2 executed run(s)" in result.output
        assert len(self.load_manifest(tmp_path)["completed"]) == 2

    def test_jobs4_maxruns1_starts_exactly_one(self, four_cell_env):
        tmp_path, yaml_path, starts_dir = four_cell_env

        result = runner.invoke(
            app, ["experiment", str(yaml_path), "--jobs", "4", "--max-runs", "1"],
        )

        assert result.exit_code == 0, result.output
        assert self.starts(starts_dir) == 1
        assert "Stopped after 1 executed run(s)" in result.output
        assert len(self.load_manifest(tmp_path)["completed"]) == 1

    def test_capped_runs_then_resume_chain_finishes_without_duplicates(self, four_cell_env):
        tmp_path, yaml_path, starts_dir = four_cell_env

        first = runner.invoke(
            app, ["experiment", str(yaml_path), "--jobs", "2", "--max-runs", "2"],
        )
        assert first.exit_code == 0, first.output
        experiment_id = next((tmp_path / "results" / "experiments").iterdir()).name
        assert self.starts(starts_dir) == 2

        # Resume with a cap: completed cells are skipped, exactly ONE new cell.
        second = runner.invoke(
            app,
            ["experiment", str(yaml_path), "--resume", experiment_id,
             "--jobs", "2", "--max-runs", "1"],
        )
        assert second.exit_code == 0, second.output
        assert self.starts(starts_dir) == 3
        assert second.output.count("already complete") == 2
        assert "Stopped after 1 executed run(s)" in second.output

        # Final resume without the cap: only the last missing cell runs.
        third = runner.invoke(
            app, ["experiment", str(yaml_path), "--resume", experiment_id, "--jobs", "2"],
        )
        assert third.exit_code == 0, third.output
        assert self.starts(starts_dir) == 4
        assert "Stopped after" not in third.output

        record = self.load_manifest(tmp_path, experiment_id)
        assert len(record["completed"]) == 4
        keys = [cell["cell_key"] for cell in record["completed"]]
        assert len(keys) == len(set(keys)), "resume must not duplicate cells"

    def test_failing_cells_count_against_the_cap(self, tmp_path, bench_repo, monkeypatch):
        tmp_path, yaml_path, starts_dir = self.make_env(
            tmp_path, bench_repo, monkeypatch, agent_creates_file=False,
        )

        result = runner.invoke(
            app, ["experiment", str(yaml_path), "--jobs", "2", "--max-runs", "1"],
        )

        assert result.exit_code == 0, result.output
        assert self.starts(starts_dir) == 1  # the failure was a real execution
        assert "Stopped after 1 executed run(s)" in result.output
        cells = self.load_manifest(tmp_path)["completed"]
        assert len(cells) == 1
        assert cells[0]["status"] != "passed"
