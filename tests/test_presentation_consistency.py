"""Report ↔ dashboard ↔ export consistency (v0.6 hardening, mission X).

One synthetic experiment containing every interesting outcome shape
(PASS, FAIL, TIMEOUT, SETUP loss, INFRA-INVALID, protected-path violation,
partial-score failure) is rendered through the three machine-readable
surfaces — study report, bundle metrics.csv, CLI export CSV. The tests pin:

* identical status/validity/cost-provenance facts on all surfaces;
* capability denominators count graded cells only everywhere;
* lost cells stay visible; nothing is silently dropped.
"""

from __future__ import annotations

import csv
import io
import json

from agentbench.experiments import ExperimentManifest
from agentbench.reporting import build_study, export_bundle, render_markdown
from agentbench.export import flatten_row


def make_row(bench, cfg, trial, status, validity="valid", **extra):
    row = {
        "run_id": f"{bench}-{cfg}-{trial}",
        "experiment_id": "E", "benchmark": bench, "config_name": cfg,
        "trial": trial, "status": status, "validity": validity,
        "duration_seconds": 12.0, "total_tokens": 900,
        "cost_usd": 0.02, "cost_provenance": "reported",
        "agent": "command", "model": "m", "execution_backend": "host",
        "files_changed": 2, "insertions": 5, "deletions": 1,
    }
    row.update(extra)
    return row


def build_rows() -> list[dict]:
    return [
        # alpha: clean pass / fail / timeout mix for both configs
        make_row("alpha", "cfg-a", 1, "passed"),
        make_row("alpha", "cfg-a", 2, "agent_timeout"),
        make_row("alpha", "cfg-b", 1, "passed"),
        make_row("alpha", "cfg-b", 2, "evaluation_failed"),
        # beta: infra-invalid on A only + a protected-path violation on B
        make_row("beta", "cfg-a", 1, "agent_failed", "infra_invalid",
                 total_tokens=0),
        make_row("beta", "cfg-a", 2, "passed"),
        make_row("beta", "cfg-b", 1, "protected_path_violation"),
        make_row("beta", "cfg-b", 2, "evaluation_failed"),
        # partial-score failure: resolved=False despite high partial credit
        make_row("gamma", "cfg-a", 1, "evaluation_failed",
                 scoring={"resolved": False, "partial_score": 0.9,
                          "scorers": [], "group_fractions": {},
                          "scorer_set_hash": "abc123"}),
        make_row("gamma", "cfg-b", 1, "passed"),
    ]


def make_manifest() -> ExperimentManifest:
    return ExperimentManifest(
        experiment_id="E", name="consistency", created_at="2026-01-01T00:00:00+00:00",
        results_dir="results", planned_cells=10, repeat=2,
        resolved_benchmarks=["alpha", "beta", "gamma"],
        config_definitions={"cfg-a": {"agent": {"type": "command"}},
                            "cfg-b": {"agent": {"type": "command"}}},
        config_identities={"cfg-a": "a" * 12, "cfg-b": "b" * 12},
        completed=[
            {"cell_key": "lost", "benchmark": "gamma", "config": "cfg-b",
             "trial": 2, "status": "setup_failed", "run_id": None},
            *[{"cell_key": f"k{i}", "benchmark": r["benchmark"],
               "config": r["config_name"], "trial": r["trial"],
               "status": r["status"], "run_id": r["run_id"]}
              for i, r in enumerate(build_rows())],
        ],
    )


def _bundle_metrics_csv(tmp_path) -> dict[tuple[str, str, str], dict]:
    study = build_study(make_manifest(), build_rows())
    export_bundle(study, make_manifest(), build_rows(), tmp_path / "bundle")
    text = (tmp_path / "bundle" / "metrics.csv").read_text(encoding="utf-8")
    return {(r["benchmark"], r["config_name"], r["trial"]): r
            for r in csv.DictReader(io.StringIO(text))}


def _export_csv_rows() -> dict[tuple[str, str, str], dict]:
    return {(r["benchmark"], r["config_name"], str(r["trial"])): flatten_row(r)
            for r in build_rows()}


def test_status_and_validity_agree_across_bundle_and_export(tmp_path):
    bundle = _bundle_metrics_csv(tmp_path)
    exported = _export_csv_rows()
    assert set(bundle) == set(exported), "surfaces disagree on which runs exist"
    for key in bundle:
        assert bundle[key]["status"] == exported[key]["status"], key
        assert bundle[key]["validity"] == exported[key]["validity"], key
        assert (bundle[key]["cost_provenance"]
                == exported[key]["cost_provenance"]), key




def test_infra_invalid_cell_never_becomes_a_pass_anywhere():
    study = build_study(make_manifest(), build_rows())
    beta = study.per_benchmark["beta"]["cfg-a"]
    assert beta["runs"] == 2 and beta["graded"] == 1 and beta["passed"] == 1
    pair = next(p for p in study.paired)
    # The infra-invalid beta cell is absent from BOTH sides' matched set.
    assert pair["matched"] <= 8


def test_lost_setup_cell_is_visible_in_report():
    md = render_markdown(build_study(make_manifest(), build_rows()))
    assert "cfg-b / gamma trial 2 — setup_failed" in md


def test_partial_score_failure_is_not_a_pass_in_bundle(tmp_path):
    bundle = _bundle_metrics_csv(tmp_path)
    row = bundle[("gamma", "cfg-a", "1")]
    assert row["status"] == "evaluation_failed"
    # and the raw scoring payload survives in result-level evidence only —
    # never converted into a pass by any derived surface.
    study = build_study(make_manifest(), build_rows())
    ps = study.partial_scores.get("cfg-a")
    assert ps is not None and ps["n"] >= 1


def test_report_headline_uses_graded_denominator_only():
    study = build_study(make_manifest(), build_rows())
    agg = next(a for a in study.aggregates if a.name == "cfg-a")
    assert agg.runs == 5            # attempted
    assert agg.graded == 4          # one infra-invalid excluded
    md = render_markdown(study)
    assert "Attempted 10 runs · validly graded 9 · infra-invalid 1" in md
