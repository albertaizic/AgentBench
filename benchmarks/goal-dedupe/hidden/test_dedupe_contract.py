"""Hidden contract: exactly-once execution under load, behavior preserved,
public import surface intact.

Data here deliberately differs from the public suite.
"""
from __future__ import annotations

import random

import ingest
import ingest.dedupe
import ingest.pipeline
import ingest.store
from ingest import Deduper, Pipeline, Record, RecordStore, process_record

# --- import surface: no public API may disappear --------------------------
_EXPECTED_SURFACE = {
    "ingest": ["Deduper", "Pipeline", "Record", "RecordStore", "process_record"],
    "ingest.pipeline": ["Pipeline", "Record", "process_record"],
    "ingest.dedupe": ["Deduper"],
    "ingest.store": ["RecordStore"],
}


def test_public_import_surface_intact():
    for module_name, names in _EXPECTED_SURFACE.items():
        module = {
            "ingest": ingest,
            "ingest.pipeline": ingest.pipeline,
            "ingest.dedupe": ingest.dedupe,
            "ingest.store": ingest.store,
        }[module_name]
        for name in names:
            assert hasattr(module, name), f"{module_name}.{name} disappeared"
            assert callable(getattr(module, name)), f"{module_name}.{name} not callable"


# --- behavior model --------------------------------------------------------
def _model_output(record: Record) -> str:
    return process_record(record)


def test_mixed_batches_outputs_correct_and_ordered():
    store = RecordStore()
    pipe = Pipeline(store)
    batches = [
        [Record("w3", "scan", "ledger"), Record("w1", "mail", "notice")],
        [Record("w1", "mail", "notice"), Record("w2", "fax", "memo"), Record("w3", "scan", "ledger")],
        [Record("w2", "fax", "memo")],
    ]
    expected_stream = []
    for batch in batches:
        out = pipe.submit(batch)
        assert len(out) == len(batch)
        for record, value in zip(batch, out):
            assert value == _model_output(record)
            expected_stream.append((record.record_id, value))
    assert [(r, v) for r, v in expected_stream][:2] == [
        ("w3", "scan:LEDGER"),
        ("w1", "mail:NOTICE"),
    ]


def test_duplicate_executions_reach_zero_under_simulated_load():
    store = RecordStore()
    pipe = Pipeline(store)
    rng = random.Random(20260825)
    universe = [f"rec-{i:03d}" for i in range(30)]
    kinds = ["scan", "mail", "fax"]
    for _ in range(25):
        batch = [Record(rng.choice(universe), rng.choice(kinds), f"payload-{rng.randrange(1000)}")
                 for _ in range(12)]
        pipe.submit(batch)
    # Every distinct id executed exactly once ever: zero redundant executions.
    assert store.duplicate_executions() == 0


def test_save_events_match_unique_ids_in_first_seen_order():
    store = RecordStore()
    pipe = Pipeline(store)
    seen_order: list[str] = []
    for record_id in ["z9", "z4", "z9", "z1", "z4", "z9"]:
        pipe.submit([Record(record_id, "job", record_id)])
        if record_id not in seen_order:
            seen_order.append(record_id)
    saves = [rid for kind, rid in store.events() if kind == "save"]
    assert saves == seen_order
    assert store.duplicate_executions() == 0


def test_late_arriving_unique_records_still_execute_exactly_once():
    store = RecordStore()
    pipe = Pipeline(store)
    for round_index in range(6):
        record_id = f"late-{round_index}"
        out = pipe.submit([
            Record(record_id, "etl", f"chunk{round_index}"),
            Record("anchor", "etl", "core"),
        ])
        assert out[0] == _model_output(out_record(record_id, round_index))
    assert store.execution_count("anchor") == 1
    for round_index in range(6):
        assert store.execution_count(f"late-{round_index}") == 1
    assert store.duplicate_executions() == 0


def out_record(record_id: str, round_index: int) -> Record:
    return Record(record_id, "etl", f"chunk{round_index}")


def test_same_id_conflicting_payload_keeps_first_result_single_execution():
    store = RecordStore()
    pipe = Pipeline(store)
    first_out = pipe.submit([Record("conflict", "web", "original")])
    second_out = pipe.submit([Record("conflict", "web", "MUTATED")])
    assert first_out == ["web:ORIGINAL"]
    assert second_out == ["web:ORIGINAL"]  # stored result wins; id is identity
    assert store.execution_count("conflict") == 1
    assert store.duplicate_executions() == 0


def test_sibling_pipelines_sharing_store_never_double_execute():
    store = RecordStore()
    alpha = Pipeline(store)
    beta = Pipeline(store)
    gamma = Pipeline(store)
    ids = ["shared-a", "shared-b", "shared-c"]
    for pipe in (alpha, beta, gamma, alpha, gamma):
        pipe.submit([Record(rid, "task", rid.upper()) for rid in ids])
    assert store.duplicate_executions() == 0
    for rid in ids:
        assert store.execution_count(rid) == 1


def test_deduper_and_store_apis_usable_directly():
    deduper = Deduper()
    store = RecordStore()
    assert not deduper.already_processed("q7")
    deduper.mark_processed("q7")
    assert deduper.already_processed("q7")
    assert not store.has("q7")
    store.save("q7", "job:Q7")
    assert store.get("q7") == "job:Q7"
    assert store.execution_count("q7") == 0  # save alone is not an execution
