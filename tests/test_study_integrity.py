"""Study-bundle tamper detection (v0.6 hardening, mission XX).

Exercises ``agentbench study verify`` semantics against a real generated
bundle: content edits, file deletions, malformed hash manifests, and extra
unmanifested files. This is tamper DETECTION, not cryptographic auth.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from agentbench.cli import EXIT_FAIL, app
from agentbench.experiments import ExperimentManifest
from agentbench.reporting import build_study, export_bundle
from tests.test_presentation_consistency import (build_rows, make_manifest)

runner = CliRunner()

def _seed_index(results: Path) -> None:
    """Persist every synthetic row through the real indexing path."""
    from agentbench.storage import ResultIndex, default_db_path

    index = ResultIndex(default_db_path(results))
    for r in build_rows():
        payload = {
            "schema_version": 2,
            "run_id": r["run_id"],
            "trial": r["trial"],
            "benchmark": {"name": r["benchmark"], "repository": "fixture",
                          "commit": "a" * 40, "resolved_commit": "b" * 40,
                          "config_hash": "cfg" + r["config_name"]},
            "agent": {"type": "command", "exit_code": 0, "timed_out": False},
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "total_tokens": 150, "cost_usd": 0.02,
                      "cost_provenance": "reported"},
            "diff": {"files_changed": 2, "insertions": 5, "deletions": 1},
            "overall": {"status": r["status"], "validity": r.get("validity"),
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "finished_at": "2026-01-01T00:00:12+00:00",
                        "duration_seconds": 12.0},
            "experiment_id": "E",
            "config_name": r["config_name"],
        }
        index.index_result(payload, result_dir=results)


def make_bundle(tmp_path: Path) -> tuple[Path, Path]:
    from agentbench.cli import index_rows_safe

    results = tmp_path / "results"
    results.mkdir()
    _seed_index(results)
    rows = index_rows_safe(results, "E")
    study = build_study(make_manifest(), rows)
    manifest = make_manifest()
    dest = tmp_path / "study"
    export_bundle(study, manifest, rows, dest)
    return dest, results


def _rehash(study_dir: Path) -> None:
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(study_dir.iterdir())
              if p.is_file() and p.name != "hashes.json"}
    (study_dir / "hashes.json").write_text(
        json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8")


def test_untampered_bundle_verifies(tmp_path):
    study_dir, results = make_bundle(tmp_path)
    result = runner.invoke(app, ["study", "verify", str(study_dir),
                                 "--results-dir", str(results)])
    assert result.exit_code == 0, result.output
    assert "matches recomputation" in result.output


def test_report_edit_is_detected(tmp_path):
    study_dir, results = make_bundle(tmp_path)
    report = study_dir / "report.md"
    report.write_text(report.read_text(encoding="utf-8").replace("50%", "99%"),
                      encoding="utf-8")
    result = runner.invoke(app, ["study", "verify", str(study_dir),
                                 "--results-dir", str(results)])
    assert result.exit_code == EXIT_FAIL
    assert "differs from recomputation" in result.output


def test_deleted_shipped_file_is_detected(tmp_path):
    study_dir, results = make_bundle(tmp_path)
    (study_dir / "metrics.csv").unlink()
    result = runner.invoke(app, ["study", "verify", str(study_dir),
                                 "--results-dir", str(results)])
    assert result.exit_code == EXIT_FAIL
    assert "metrics.csv missing" in result.output


def test_malformed_hashes_json_is_clean_error_not_crash(tmp_path):
    study_dir, results = make_bundle(tmp_path)
    (study_dir / "hashes.json").write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["study", "verify", str(study_dir),
                                 "--results-dir", str(results)])
    assert result.exit_code == EXIT_FAIL
    assert "Malformed hashes.json" in result.output
    assert "Traceback" not in result.output


def test_extra_unmanifested_file_is_allowed_but_flagged(tmp_path):
    study_dir, results = make_bundle(tmp_path)
    (study_dir / "local-note.md").write_text("added later", encoding="utf-8")
    result = runner.invoke(app, ["study", "verify", str(study_dir),
                                 "--results-dir", str(results)])
    assert result.exit_code == 0
    assert "Unmanifested files" in result.output
    assert "local-note.md" in result.output


def test_rehashed_bundle_still_matches_evidence(tmp_path):
    # A legitimately regenerated bundle (hashes rebuilt over new content)
    # verifies as long as the content itself recomputes from evidence.
    study_dir, results = make_bundle(tmp_path)
    _rehash(study_dir)
    result = runner.invoke(app, ["study", "verify", str(study_dir),
                                 "--results-dir", str(results)])
    assert result.exit_code == 0, result.output
