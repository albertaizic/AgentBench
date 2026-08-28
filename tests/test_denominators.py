"""Denominator adversarial tests (v0.6 release hardening, mission IV).

Synthetic experiment fixtures prove that infra-invalid / ungraded cells never
silently inflate capability denominators on any derived surface:

* per-config pass rates and Wilson intervals (build_study),
* paired McNemar comparisons (pairwise_statistics via build_study/dashboard),
* reliability any-in-k/all-k,
* saturation classification.

Every fixture is deterministic and offline.
"""

from __future__ import annotations

from agentbench.experiments import ExperimentManifest
from agentbench.reporting import build_study, render_markdown
from agentbench.saturation import analyze_benchmark
from agentbench.reliability import reliability_from_cells


def make_row(benchmark: str, config: str, trial: int, status: str,
             validity: str = "valid", **extra) -> dict:
    row = {
        "run_id": f"r-{benchmark}-{config}-{trial}",
        "benchmark": benchmark,
        "config_name": config,
        "trial": trial,
        "status": status,
        "validity": validity,
        "duration_seconds": 10.0,
        "total_tokens": 1000,
        "cost_usd": 0.01,
        "cost_provenance": "reported",
        "agent": "command",
    }
    row.update(extra)
    return row


def make_manifest(**overrides) -> ExperimentManifest:
    fields = dict(
        experiment_id="20260101T000000Z-test",
        name="denominator-test",
        created_at="2026-01-01T00:00:00+00:00",
        results_dir="results",
        planned_cells=8,
        repeat=2,
        resolved_benchmarks=["alpha"],
        config_definitions={
            "cfg-a": {"agent": {"type": "command", "model": "m-a"}},
            "cfg-b": {"agent": {"type": "command", "model": "m-b"}},
        },
        config_identities={"cfg-a": "a" * 12, "cfg-b": "b" * 12},
    )
    fields.update(overrides)
    return ExperimentManifest(**fields)


# -- case 3: 3 pass / 2 fail / 5 infra-invalid --------------------------------

def test_infra_invalid_never_inflates_pass_denominator():
    rows = [make_row("alpha", "cfg-a", i + 1, "passed") for i in range(3)]
    rows += [make_row("alpha", "cfg-a", i + 4, "evaluation_failed") for i in range(2)]
    rows += [
        make_row("alpha", "cfg-a", i + 6, "agent_failed", "infra_invalid")
        for i in range(5)
    ]
    study = build_study(make_manifest(planned_cells=10), rows)
    agg = study.aggregates[0]
    assert agg.runs == 10            # attempted stays visible
    assert agg.graded == 5           # capability denominator
    assert agg.passes == 3
    assert agg.pass_rate == 3 / 5    # NOT 3/10
    lo, hi = agg.interval
    assert 0 < lo < 3 / 5 < hi < 1


def test_report_shows_graded_denominator_and_validity_counts():
    rows = [make_row("alpha", "cfg-a", 1, "passed"),
            make_row("alpha", "cfg-a", 2, "agent_failed", "infra_invalid")]
    study = build_study(
        make_manifest(planned_cells=2, repeat=1,
                      config_definitions={"cfg-a": {"agent": {"type": "command"}}},
                      config_identities={"cfg-a": "a" * 12}),
        rows)
    md = render_markdown(study)
    assert "Attempted 2 runs · validly graded 1 · infra-invalid 1" in md
    assert "(1/1 graded)" in md


# -- case 6: paired cells where one side is infra-invalid ---------------------

def test_paired_comparison_excludes_infra_invalid_side():
    # cfg-a passes cell; cfg-b's run for the same cell was infra-invalid.
    # The pair must be dropped entirely — never counted as a discordant win.
    rows = [
        make_row("alpha", "cfg-a", 1, "passed"),
        make_row("alpha", "cfg-b", 1, "agent_failed", "infra_invalid"),
        make_row("alpha", "cfg-a", 2, "evaluation_failed"),
        make_row("alpha", "cfg-b", 2, "evaluation_failed"),
    ]
    study = build_study(make_manifest(), rows)
    assert len(study.paired) == 1
    pair = study.paired[0]
    assert pair["matched"] == 1          # only the valid/valid cell pairs
    assert pair["both_fail"] == 1
    assert pair["both_pass"] == 0
    assert pair["a_only"] == 0 and pair["b_only"] == 0
    assert pair["mcnemar_p"] is None     # no discordant evidence


# -- unrun cells (setup failure before persistence) ---------------------------

def test_setup_failed_cell_stays_visible_in_report():
    manifest = make_manifest(planned_cells=3, completed=[
        {"cell_key": "k1", "benchmark": "alpha", "config": "cfg-a",
         "trial": 1, "status": "passed", "run_id": "r-1"},
        {"cell_key": "k2", "benchmark": "alpha", "config": "cfg-a",
         "trial": 2, "status": "setup_failed", "run_id": None,
         "error": "PermissionError: temp dir busy"},
    ])
    rows = [make_row("alpha", "cfg-a", 1, "passed")]
    study = build_study(manifest, rows)
    assert len(study.unrun_cells) == 1
    md = render_markdown(study)
    assert "**Cell without recorded run**: cfg-a / alpha trial 2 — setup_failed" in md
    assert "temp dir busy" in md


# -- reliability denominator transparency --------------------------------------

def test_reliability_discloses_partial_task_coverage():
    rel = reliability_from_cells([[True, True, True], [True, True]], k=3).to_dict()
    assert rel["n_tasks_with_k"] == 1
    assert rel["n_tasks"] == 2
    md_k = f"k={rel['k']}, {rel['n_tasks_with_k']}/{rel['n_tasks']} tasks"
    assert md_k == "k=3, 1/2 tasks"


def test_reliability_full_coverage_has_no_partial_marker():
    rel = reliability_from_cells([[False, False], [False, False]], k=2).to_dict()
    assert rel["n_tasks_with_k"] == 2


# -- saturation -----------------------------------------------------------------

def _srow(config: str, status: str, validity: str = "valid") -> dict:
    return make_row("alpha", config, 1, status, validity)


def test_saturation_single_config_cannot_classify_saturated():
    rows = [_srow("only", "passed") for _ in range(6)]
    verdict = analyze_benchmark("alpha", rows)
    assert verdict.classification == "uncalibrated"


def test_saturation_ignores_infra_invalid_but_discloses_them():
    rows = ([_srow("A", "passed") for _ in range(3)]
            + [_srow("B", "passed") for _ in range(3)])
    rows.append(_srow("B", "agent_failed", "infra_invalid"))
    verdict = analyze_benchmark("alpha", rows)
    assert verdict.classification == "likely_saturated"
    assert "1 ungraded run(s) excluded" in verdict.reason


def test_saturation_infra_heavy_is_not_too_hard():
    rows = ([_srow("A", "passed"), _srow("A", "passed")]
            + [_srow("B", "agent_failed", "infra_invalid") for _ in range(4)])
    verdict = analyze_benchmark("alpha", rows)
    assert verdict.classification != "likely_too_hard"


# -- local path redaction on public surfaces -----------------------------------

def test_unrun_cell_error_is_path_redacted():
    manifest = make_manifest(planned_cells=1, completed=[
        {"cell_key": "k1", "benchmark": "alpha", "config": "cfg-a",
         "trial": 1, "status": "setup_failed", "run_id": None,
         "error": "PermissionError: cannot access "
                  "'C:\\Users\\someone\\AppData\\Local\\Temp\\agentbench-x'"},
    ])
    rows: list[dict] = []
    md = render_markdown(build_study(manifest, rows))
    assert "<local-path>" in md
    assert "Users" not in md and "AppData" not in md


def test_export_bundle_redacts_manifest_error_paths(tmp_path):
    from agentbench.reporting import export_bundle

    manifest = make_manifest(planned_cells=1, completed=[
        {"cell_key": "k1", "benchmark": "alpha", "config": "cfg-a",
         "trial": 1, "status": "setup_failed", "run_id": None,
         "error": "OSError: /home/agent/work/agentbench-tmp locked"},
    ])
    study = build_study(manifest, [])
    written = export_bundle(study, manifest, [], tmp_path / "bundle")
    shipped = (tmp_path / "bundle" / "experiment.json").read_text(encoding="utf-8")
    assert "/home/agent" not in shipped
    assert "<local-path>" in shipped
    assert any(w.name == "hashes.json" for w in written)
