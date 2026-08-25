"""CLI tests for `report` and `saturation` commands against a seeded index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from agentbench.cli import app
from agentbench.experiments import ExperimentManifest, save_manifest
from agentbench.storage import ResultIndex, default_db_path

runner = CliRunner()


def make_manifest(results_dir: Path) -> ExperimentManifest:
    manifest = ExperimentManifest.model_validate({
        "experiment_id": "20260824T000000Z-study01",
        "name": "clireport",
        "created_at": "2026-08-24T00:00:00+00:00",
        "results_dir": str(results_dir),
        "planned_cells": 4,
        "repeat": 2,
        "benchmark_identities": {"alpha": "h1"},
        "config_identities": {"cfg-a": "cafe123", "cfg-b": "beef456"},
        "config_definitions": {
            "cfg-a": {"agent": {"type": "claude-code", "model": "sonnet"}, "execution": None},
            "cfg-b": {"agent": {"type": "hermes", "provider": "openrouter",
                                 "reasoning": "low"}, "execution": None},
        },
        "resolved_benchmarks": ["alpha"],
    })
    return manifest


def seed(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    manifest = make_manifest(results)
    exp_dir = results / "experiments" / manifest.experiment_id
    exp_dir.mkdir(parents=True)
    save_manifest(manifest, exp_dir)

    payload = {
        "schema_version": 2, "run_id": "r1", "trial": 1,
        "benchmark": {"name": "alpha", "repository": "fixture",
                      "commit": "a" * 40, "resolved_commit": "b" * 40,
                      "config_hash": "cafe123"},
        "agent": {"type": "claude-code", "exit_code": 0, "timed_out": False,
                  "model": "sonnet"},
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                  "cost_usd": 0.02},
        "diff": {"files_changed": 1, "insertions": 3, "deletions": 1},
        "overall": {"status": "passed", "failure_reason": None,
                    "started_at": "2026-08-24T00:00:00+00:00",
                    "finished_at": "2026-08-24T00:01:00+00:00",
                    "duration_seconds": 60.0},
        "experiment_id": manifest.experiment_id,
        "config_name": "cfg-a",
    }
    index = ResultIndex(default_db_path(results))
    index.index_result(payload, result_dir=results)

    payload_b = dict(payload)
    payload_b["run_id"] = "r2"
    payload_b["config_name"] = "cfg-b"
    payload_b["agent"] = {"type": "hermes", "exit_code": 1, "timed_out": False,
                          "model": "openai/gpt-5-mini"}
    payload_b["overall"] = dict(payload["overall"], status="failed")
    index.index_result(payload_b, result_dir=results)
    return results


class TestReportCommand:
    def test_generates_markdown_html_and_bundle(self, tmp_path):
        results = seed(tmp_path)

        result = runner.invoke(
            app, ["report", "20260824T000000Z-study01",
                  "--results-dir", str(results),
                  "--bundle", str(tmp_path / "bundle")],
        )

        assert result.exit_code == 0, result.output
        report_md = results / "reports" / "20260824T000000Z-study01" / "report.md"
        report_html = results / "reports" / "20260824T000000Z-study01" / "report.html"
        assert report_md.exists() and report_html.exists()
        text = report_md.read_text(encoding="utf-8")
        assert "openrouter" in text          # config identity rendered
        assert "McNemar" in text             # paired stats present
        bundle_names = {p.name for p in (tmp_path / "bundle").iterdir()}
        assert {"report.md", "metrics.csv", "identities.json"} <= bundle_names

    def test_unknown_experiment_fails_cleanly(self, tmp_path):
        result = runner.invoke(
            app, ["report", "no-such-id", "--results-dir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "Unknown experiment" in result.output


class TestSaturationCommand:
    def test_json_output_classifies(self, tmp_path):
        results = seed(tmp_path)

        result = runner.invoke(
            app, ["saturation", "--results-dir", str(results), "--json",
                  "--min-runs", "2"],
        )

        assert result.exit_code == 0, result.output
        # rich console may wrap long lines; parse the JSON payload portion
        start = result.output.index("[")
        end = result.output.rindex("]") + 1
        data = json.loads(result.output[start:end])
        assert data[0]["benchmark"] == "alpha"
        assert data[0]["total_runs"] == 2
        assert data[0]["classification"] == "uncalibrated"  # single-config rows
