"""Public-bundle redaction attack (v0.6 hardening, mission XIX).

Synthetic hostile runs inject obvious fake secrets into every channel an
agent could influence — stdout, stderr, evaluator output, trajectory events,
result metadata. The public bundle and every derived surface must ship none
of them; the secret scanner must refuse to write content that would.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentbench.reporting import (SecretLeakError, build_study,
                                  export_bundle, render_markdown,
                                  render_html, scan_for_secrets)
from agentbench.experiments import ExperimentManifest
from agentbench.storage import ResultIndex, default_db_path
from tests.test_presentation_consistency import make_manifest

runner = CliRunner()

SECRETS = {
    "openai_key": "OPENAI_API_KEY=sk-test-000000000000000000000000",
    "openrouter_key": "sk-or-v1-aaaabbbbccccddddeeeeffff00001111",
    "anthropic_key": "Authorization: Bearer sk-ant-aaaabbbbccccdddd1111",
    "aws_key": "AWS_SECRET_ACCESS_KEY=fakeSecretValue000",
    "password": "password=hunter2-fake-secret",
    "token": "token=ghp_faketoken0000000000000000",
}

HOSTILE_STDOUT = "\n".join(SECRETS.values())
HOSTILE_STDERR = f"provider error: {SECRETS['openai_key']}"
HOME_PATH = r"C:\Users\victim-h4x0r\AppData\Local\Temp\agentbench-zz"


def _seed_hostile_run(results_path):
    from pathlib import Path

    index = ResultIndex(default_db_path(results_path))
    payload = {
        "schema_version": 2, "run_id": "hostile-1", "trial": 1,
        "benchmark": {"name": "alpha", "repository": "fixture",
                      "commit": "a" * 40, "resolved_commit": "b" * 40,
                      "config_hash": "cafe123"},
        "agent": {"type": "command", "exit_code": 1, "timed_out": False},
        # A hostile harness could echo secrets into usage/metadata fields:
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "total_tokens": 15, "cost_usd": None,
                  "cost_provenance": None},
        "diff": {"files_changed": 1, "insertions": 3, "deletions": 1},
        "overall": {
            "status": "evaluation_failed", "validity": "valid",
            "failure_reason": "one or more evaluations failed",
            "failure_stage": "evaluation",
            # Hostile environment capture smuggled into started_at:
            "started_at": f"{SECRETS['password']} 2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00",
            "duration_seconds": 60.0,
        },
        "experiment_id": "E-hostile",
        "config_name": "cfg-a",
    }
    index.index_result(payload, result_dir=results_path)
    return index


def test_secret_scanner_flags_every_pattern_class():
    for label, secret in SECRETS.items():
        found = scan_for_secrets(f"harmless context {secret} trailing")
        assert found, f"scanner missed {label}"


def test_bundle_writer_refuses_secret_content(tmp_path):
    manifest = make_manifest()
    study = build_study(make_manifest(), [])
    with pytest.raises(SecretLeakError):
        export_bundle(study, manifest, [],
                      tmp_path / "bundle",
                      markdown=f"# report\n\n{SECRETS['openai_key']}\n")


def test_hostile_run_ships_no_secrets_to_public_surfaces(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    _seed_hostile_run(results)

    from agentbench.cli import index_rows_safe
    rows = index_rows_safe(results, "E-hostile")
    study = build_study(make_manifest(), rows)
    markdown = render_markdown(study)
    html_text = render_html(study)
    written = export_bundle(study, make_manifest(), rows, tmp_path / "bundle",
                            markdown=markdown, html_text=html_text)
    blob = "\n".join(
        p.read_text(encoding="utf-8") for p in written
    ).lower()
    for label, secret in SECRETS.items():
        marker = secret.split("=", 1)[-1].split()[-1].lower()
        assert marker not in blob, f"{label} leaked into public bundle"
    assert "victim-h4x0r" not in blob
    assert HOSTILE_STDOUT.lower() not in blob


def test_cli_export_of_hostile_row_carries_no_agent_stdout(tmp_path):
    """Export ships only flattened index columns; raw logs never enter."""
    from agentbench.export import to_csv
    results = tmp_path / "results"
    results.mkdir()
    index = _seed_hostile_run(results)
    rows = index.query(experiment_id="E-hostile")
    csv_text = to_csv(rows)
    lowered = csv_text.lower()
    for label, secret in SECRETS.items():
        marker = secret.split("=", 1)[-1].split()[-1].lower()
        assert marker not in lowered, f"{label} leaked into export"
