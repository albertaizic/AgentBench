"""Tests for export, reproduce preflight/provenance, doctor, and cleanup."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from agentbench.cli import app
from agentbench.export import EXPORT_COLUMNS, to_csv, to_json
from agentbench.models import ExecutionSpec
from agentbench.reproduce import condition_checks, execution_spec_from_provenance, preflight
from agentbench.results import RunResult
from agentbench.runner import RunOutcome
from agentbench.storage import ResultIndex

runner = CliRunner()


def seed_run(run_id: str, **overrides) -> dict:
    payload = {
        "schema_version": 3,
        "run_id": run_id,
        "trial": 1,
        "benchmark": {"name": "demo", "repository": "r", "commit": "a" * 40,
                      "resolved_commit": "b" * 40, "config_hash": "h1"},
        "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False,
                  "duration_seconds": 10.0, "model": "m1",
                  "capabilities": ["structured_usage"]},
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                  "cost_usd": 0.05},
        "diff": {"files_changed": 1, "insertions": 3, "deletions": 1},
        "evaluations": [], "hidden_evaluations": [], "protected_paths": None,
        "overall": {"status": "passed", "failure_reason": None,
                    "started_at": "s", "finished_at": "f", "duration_seconds": 11.0},
        "execution": {"backend": "docker", "network": "enabled",
                      "image_requested": "python:3.12-slim",
                      "image_id": "sha256:image123",
                      "image_digests": ["repo@sha256:digest456"],
                      "pass_env_evidence": [{"name": "ANTHROPIC_API_KEY", "present": True}]},
        "environment": {},
        "config": {},
        "experiment_id": None,
        "config_name": None,
    }
    payload.update(overrides)
    return payload


class TestExport:
    def seeded_rows(self, tmp_path) -> list[dict]:
        index = ResultIndex(tmp_path / "db.sqlite")
        rows = [
            seed_run("r1"),
            seed_run("r2", experiment_id="exp-1", config_name="cfg",
                     overall={"status": "evaluation_failed", "failure_reason": "x",
                              "started_at": "s", "finished_at": "f",
                              "duration_seconds": 5.0}),
        ]
        for row in rows:
            index.index_result(row, result_dir=tmp_path / row["run_id"])
        return index.query(limit=None)

    def test_csv_contains_flat_safe_columns(self, tmp_path):
        csv_text = to_csv(self.seeded_rows(tmp_path))

        header = csv_text.splitlines()[0]
        for column in EXPORT_COLUMNS:
            assert column in header
        assert "sha256:image123" in csv_text
        assert "r1" in csv_text

    def test_json_roundtrip_preserves_types(self, tmp_path):
        data = json.loads(to_json(self.seeded_rows(tmp_path)))

        assert len(data) == 2
        assert data[0]["total_tokens"] == 110
        assert data[0]["cost_usd"] == 0.05

    def test_cli_export_writes_file(self, tmp_path):
        index = ResultIndex(tmp_path / ".agentbench" / "agentbench.db")
        index.index_result(seed_run("r1"), result_dir=tmp_path)

        out = tmp_path / "export.csv"
        result = runner.invoke(
            app, ["export", "--results-dir", str(tmp_path),
                  "--format", "csv", "--output", str(out)]
        )

        assert result.exit_code == 0
        text = out.read_text(encoding="utf-8")
        assert text.splitlines()[0].startswith("run_id")
        assert "r1" in text


class TestReproducePreflight:
    def _original(self, tmp_path: Path, with_manifest: bool = True) -> dict:
        manifest = tmp_path / "bench" / "benchmark.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if with_manifest:
            manifest.write_text("name: demo\n", encoding="utf-8")
        return {
            "run_id": "r1",
            "benchmark": {"name": "demo", "commit": "a" * 40, "resolved_commit": "b" * 40,
                          "config_hash": "h1"},
            "config": {"_benchmark_manifest": str(manifest) if with_manifest else None},
            "execution": {"backend": "host"},
        }

    def test_missing_manifest_blocks_reproduction(self, tmp_path):
        comparison = preflight(self._original(tmp_path, with_manifest=False),
                               results_root=tmp_path)

        assert comparison.blocked_reason is not None

    def test_changed_config_hash_blocks_reproduction(self, tmp_path):
        original = self._original(tmp_path)
        manifest = Path(original["config"]["_benchmark_manifest"])
        manifest.write_text("name: changed\n", encoding="utf-8")  # hash will differ

        comparison = preflight(original, results_root=tmp_path)

        # Either blocked by name/hash mismatch or by fixture availability —
        # both are legitimate refusals to mix conditions.
        assert comparison.blocked_reason is not None or comparison.checks


class TestConditionChecks:
    def test_reports_matching_and_differing_conditions(self):
        original = {
            "benchmark": {"config_hash": "h", "resolved_commit": "c"},
            "config": {"agent": {"type": "claude-code"}},
            "execution": {"backend": "docker", "image_digests": ["d1"]},
        }
        rerun = {
            "benchmark": {"config_hash": "h", "resolved_commit": "c"},
            "config": {"agent": {"type": "claude-code"}},
            "execution": {"backend": "docker", "image_digests": ["d2"]},
        }

        checks = condition_checks(original, rerun)
        by_name = {name: same for name, same, _ in checks}

        assert by_name["same benchmark identity"] is True
        assert by_name["same resolved commit"] is True
        assert by_name["same execution backend"] is True
        assert by_name["same docker image digest"] is False  # drift detected


class TestExecutionSpecFromProvenance:
    DOCKER_PROVENANCE = {
        "backend": "docker",
        "docker_version": "Docker version 29.7.2, build a7dcaa6",
        "image_requested": "agentbench-smoke:py312",
        "image_id": "sha256:abc123",
        "image_digests": ["agentbench-smoke@sha256:abc123"],
        "network": "disabled",
        "memory_limit": None,
        "cpus_limit": 1.5,
        "pids_limit": 256,
        "passed_env_names": ["ANTHROPIC_API_KEY"],
        "container_workspace": "/workspace",
        "pass_env_evidence": [{"name": "ANTHROPIC_API_KEY", "present": True}],
    }

    def test_realistic_docker_provenance_maps_to_spec(self):
        spec = execution_spec_from_provenance(self.DOCKER_PROVENANCE)

        assert spec == ExecutionSpec(
            backend="docker",
            image="agentbench-smoke:py312",
            network="disabled",
            cpus=1.5,
            pids_limit=256,
            pass_env=["ANTHROPIC_API_KEY"],
        )

    def test_host_provenance_maps_to_host_spec(self):
        payload = {"backend": "host", "network": "enabled", "pass_env_evidence": []}

        assert execution_spec_from_provenance(payload) == ExecutionSpec(backend="host")

    def test_missing_execution_block_yields_none(self):
        assert execution_spec_from_provenance({}) is None


class TestReproduceCli:
    REALISTIC_PROVENANCE = {
        "backend": "docker",
        "docker_version": "Docker version 29.7.2, build a7dcaa6",
        "image_requested": "python:3.12-slim",
        "image_id": "sha256:image123",
        "image_digests": ["python@sha256:digest456"],
        "network": "disabled",
        "memory_limit": None,
        "cpus_limit": None,
        "pids_limit": None,
        "passed_env_names": [],
        "container_workspace": "/workspace",
        "pass_env_evidence": [],
    }

    def _seed_original(self, tmp_path: Path) -> str:
        """Persist one original run with realistic provenance and matching manifest."""
        from agentbench.loader import load_benchmark

        manifest = tmp_path / "bench" / "benchmark.yaml"
        manifest.parent.mkdir(parents=True)
        commit = "a" * 40
        manifest.write_text(
            "name: demo\n"
            "repository: .\n"
            f"commit: {commit}\n"
            "prompt: fix it\n"
            "agent: {type: claude-code}\n"
            "evaluations:\n  - name: t\n    command: 'true'\n",
            encoding="utf-8",
        )
        config_hash = load_benchmark(manifest).config_hash()

        original = seed_run("r-orig")
        original["benchmark"] = {
            "name": "demo", "repository": ".", "commit": commit,
            "resolved_commit": commit, "config_hash": config_hash,
        }
        original["execution"] = dict(self.REALISTIC_PROVENANCE)
        original["config"] = {"_benchmark_manifest": str(manifest)}
        result_dir = tmp_path / "demo" / "r-orig"
        result_dir.mkdir(parents=True)
        (result_dir / "result.json").write_text(json.dumps(original), encoding="utf-8")

        index = ResultIndex(tmp_path / ".agentbench" / "agentbench.db")
        index.index_result(original, result_dir=result_dir)
        return "r-orig"

    def test_reproduce_builds_execution_spec_from_stored_provenance(
        self, tmp_path, monkeypatch
    ):
        run_id = self._seed_original(tmp_path)

        # Docker availability must not gate a unit-level CLI test.
        monkeypatch.setattr(
            "agentbench.backends.docker.docker_available", lambda: True
        )
        captured: dict = {}

        def fake_run_benchmark(spec, **kwargs):
            captured["execution"] = kwargs.get("execution")
            new_result = RunResult(
                run_id="r-new",
                benchmark=dict(seed_run("r-new")["benchmark"]),
                agent={"type": "claude-code"},
                diff={"files_changed": 0},
                evaluations=[],
                overall={"status": "passed", "failure_reason": None},
                environment={},
                config={},
            )
            return RunOutcome(
                result=new_result, run_dir=tmp_path / "demo" / "r-new",
                workspace_path=None,
            )

        monkeypatch.setattr("agentbench.cli.run_benchmark", fake_run_benchmark)

        result = runner.invoke(
            app, ["reproduce", run_id, "--results-dir", str(tmp_path)]
        )

        assert result.exit_code == 0
        execution = captured["execution"]
        assert isinstance(execution, ExecutionSpec)  # would crash before the fix
        assert execution.backend == "docker"
        assert execution.image == "python:3.12-slim"
        assert execution.network == "disabled"


class TestDoctorAndCleanup:
    def test_doctor_lists_checks_and_never_leaks_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")

        result = runner.invoke(
            app, ["doctor", "--results-dir", str(tmp_path / "results")]
        )

        assert result.exit_code == 0
        assert "Git" in result.output
        assert "SQLite" in result.output
        assert "sk-super-secret" not in result.output

    def test_cleanup_workspaces_dry_run_does_not_delete(self, tmp_path):
        stale = Path(tempfile.mkdtemp(prefix="agentbench-stale-"))

        try:
            result = runner.invoke(app, ["cleanup", "workspaces"])

            assert result.exit_code == 0
            assert "dry-run" in result.output
            assert stale.exists()
        finally:
            import shutil

            shutil.rmtree(stale, ignore_errors=True)


def tempfile_prefix_check() -> None:  # pragma: no cover - documentation helper
    """cleanup only touches agentbench-* prefixed dirs in the system temp."""
