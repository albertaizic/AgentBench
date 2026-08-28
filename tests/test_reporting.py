"""Report generation: markdown/HTML rendering and safe public bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbench.experiments import ExperimentManifest
from agentbench.reporting import (
    SecretLeakError,
    build_study,
    export_bundle,
    render_html,
    render_markdown,
    scan_for_secrets,
)


def make_manifest(tmp_path: Path) -> ExperimentManifest:
    return ExperimentManifest.model_validate({
        "experiment_id": "20260824T000000Z-deadbeef",
        "name": "study",
        "created_at": "2026-08-24T00:00:00+00:00",
        "results_dir": str(tmp_path / "results"),
        "planned_cells": 4,
        "repeat": 2,
        "benchmark_identities": {"alpha": "h1", "beta": "h2"},
        "config_identities": {"claude-a": "cfgaaa", "hermes-b": "cfgbbb"},
        "config_definitions": {
            "claude-a": {
                "agent": {"type": "claude-code", "model": "claude-sonnet-4-6"},
                "execution": None,
            },
            "hermes-b": {
                "agent": {
                    "type": "hermes", "model": "openai/gpt-5-mini",
                    "provider": "openrouter", "reasoning": "low",
                },
                "execution": None,
            },
        },
        "resolved_benchmarks": ["alpha", "beta"],
    })


def make_rows() -> list[dict]:
    rows = []
    for bench in ("alpha", "beta"):
        for trial in (1, 2):
            rows.append({
                "run_id": f"r-{bench}-{trial}-a", "benchmark": bench,
                "config_name": "claude-a", "agent": "claude-code",
                "model": "claude-sonnet-4-6", "status": "passed", "trial": trial,
                "duration_seconds": 10.0 + trial, "total_tokens": 900 + trial,
                "cost_usd": 0.01 * trial, "insertions": 4, "deletions": 1,
                "execution_backend": "host",
            })
            rows.append({
                "run_id": f"r-{bench}-{trial}-b", "benchmark": bench,
                "config_name": "hermes-b", "agent": "hermes",
                "model": "openai/gpt-5-mini",
                "status": "passed" if trial == 1 else "failed",
                "trial": trial,
                "duration_seconds": 20.0 + trial, "total_tokens": 700 + trial,
                "cost_usd": 0.002 * trial, "insertions": 9, "deletions": 3,
                "execution_backend": "host",
            })
    return rows


class TestBuildStudy:
    def test_study_fields_from_manifest_and_rows(self, tmp_path):
        study = build_study(make_manifest(tmp_path), make_rows())
        assert study.total_runs == 8
        assert {a.name for a in study.aggregates} == {"claude-a", "hermes-b"}
        assert len(study.paired) == 1
        pair = study.paired[0]
        assert pair["matched"] == 4
        assert pair["both_pass"] == 2      # alpha trial 1; beta trial 1
        assert pair["b_only"] == 0
        assert pair["a_only"] == 2         # trial 2: claude passed, hermes failed

    def test_saturation_attached(self, tmp_path):
        study = build_study(make_manifest(tmp_path), make_rows())
        assert {s.benchmark for s in study.saturation} == {"alpha", "beta"}
        # 4 runs < default min_runs: everything must stay uncalibrated.
        assert all(s.classification == "uncalibrated" for s in study.saturation)


class TestRenderMarkdown:
    def test_contains_identity_statistics_and_limitations(self, tmp_path):
        md = render_markdown(build_study(make_manifest(tmp_path), make_rows()))
        assert "# AgentBench study — study" in md
        assert "claude-sonnet-4-6" in md and "openai/gpt-5-mini" in md
        assert "provider=openrouter" in md and "reasoning=low" in md
        assert "Wilson" in md or "%" in md          # intervals rendered
        assert "McNemar exact p=" in md             # paired statistics present
        assert "both pass 2 · claude-a only 2 · hermes-b only 0" in md
        assert "marginal check: claude-a 4 passes (2+2)" in md
        assert "uncalibrated" in md                 # saturation table
        assert "do not generalize" in md            # limitations stated

    def test_no_best_agent_score_is_ever_emitted(self, tmp_path):
        md = render_markdown(build_study(make_manifest(tmp_path), make_rows()))
        assert "winner" not in md.lower()
        assert "best agent" not in md.lower()
        assert "overall score" not in md.lower()

    def test_agent_failed_zero_token_cells_get_outage_marker(self, tmp_path):
        # An agent_failed cell with no tokens never reached a model; the
        # report must flag it as infrastructure, not hide or explain it away.
        rows = make_rows()
        rows.append(dict(rows[0], run_id="r-crash", benchmark="alpha",
                         trial=3, status="agent_failed", total_tokens=0,
                         duration_seconds=8.0))
        study = build_study(make_manifest(tmp_path), rows)
        md = render_markdown(study)
        assert "†" in md
        assert "never reached a model" in md
        # The healthy benchmark stays unmarked.
        assert "| beta |" in md
        alpha_row = next(line for line in md.splitlines() if line.startswith("| alpha "))
        assert "†" in alpha_row


class TestRenderHtml:
    def test_self_contained_static_document(self, tmp_path):
        page = render_html(build_study(make_manifest(tmp_path), make_rows()))
        assert page.startswith("<!doctype html>")
        assert "http://" not in page and "https://" not in page  # no CDN
        assert "<table>" in page and "<th>" in page
        assert "openai/gpt-5-mini" in page


class TestSecretScanning:
    @pytest.mark.parametrize("text", [
        "key=sk-or-v1-abcdef1234567890abcdef",
        "Authorization: Bearer abcdef0123456789abcdef",
        "OPENROUTER_API_KEY=hunter2hunter2",
        "token: sk-ant-api03-aaaaaaaaaaaaaaaaaaaa",
    ])
    def test_detects_common_credential_shapes(self, text):
        assert scan_for_secrets(text), text

    def test_clean_text_passes(self):
        assert scan_for_secrets("pass rate 50% [30%-70%], config hash cfgaaa123") == []


class TestExportBundle:
    def test_bundle_contents_are_complete_and_secret_free(self, tmp_path):
        manifest = make_manifest(tmp_path)
        study = build_study(manifest, make_rows())
        dest = tmp_path / "bundle"

        written = export_bundle(study, manifest, make_rows(), dest)

        names = {p.name for p in written}
        assert names == {"README.md", "experiment.json", "identities.json",
                         "metrics.csv", "report.md", "report.html", "hashes.json"}
        hashes = json.loads((dest / "hashes.json").read_text(encoding="utf-8"))
        assert set(hashes) == names - {"hashes.json"}
        metrics = (dest / "metrics.csv").read_text(encoding="utf-8")
        assert "result_dir" not in metrics          # local paths never ship
        identities = json.loads((dest / "identities.json").read_text(encoding="utf-8"))
        assert identities["config_definitions"]["hermes-b"]["agent"]["provider"] == "openrouter"
        for path in written:
            assert scan_for_secrets(path.read_text(encoding="utf-8")) == []

    def test_bundle_aborts_when_a_secret_would_ship(self, tmp_path):
        manifest = make_manifest(tmp_path)
        manifest.config_definitions["leaky"] = {
            "agent": {"type": "command", "argv": ["x"],
                      "extra_args": [], "prompt_mode": "stdin",
                      "model": "sk-or-v1-abcdef1234567890abcdef"},
            "execution": None,
        }
        manifest.config_identities["leaky"] = "cfgccc"
        study = build_study(manifest, make_rows())

        with pytest.raises(SecretLeakError, match="OpenRouter"):
            export_bundle(study, manifest, make_rows(), tmp_path / "bundle")


def test_setup_failed_cells_never_enter_graded_denominator(tmp_path):
    """A setup failure with migrated validity=valid must stay out of the
    capability denominator while remaining visible as an attempted cell."""
    rows = make_rows() + [{
        "run_id": "r-alpha-setup", "benchmark": "alpha",
        "config_name": "claude-a", "agent": "claude-code",
        "model": "claude-sonnet-4-6", "status": "setup_failed", "trial": 3,
        "duration_seconds": 2.0, "total_tokens": None, "cost_usd": None,
        "insertions": 0, "deletions": 0, "execution_backend": "docker",
        "validity": "valid",  # what the storage migration records
    }]
    study = build_study(make_manifest(tmp_path), rows)
    agg = next(a for a in study.aggregates if a.name == "claude-a")
    assert agg.runs == 5            # attempted cells stay visible
    assert agg.graded == 4          # denominator excludes the setup failure
    assert agg.passes == 4
    assert agg.pass_rate == 1.0     # ...and the rate is computed over graded only
