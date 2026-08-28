# goal-dedupe — defect notes (maintainer-only)

## Mechanism

`ingest/pipeline.py::Pipeline.submit` constructs a **fresh `Deduper` per
submission batch**. Within one batch, repeats of a record id are correctly
short-circuited (the in-batch `Deduper` sees them), so small tests look fine.
But the dedupe state dies with the batch:

* A record id submitted again in a *later* batch is re-executed from scratch.
* A sibling `Pipeline` sharing the same `RecordStore` also re-executes ids the
  first pipeline already handled.

Under load (many overlapping batches over a hot working set) each popular id
executes once per containing batch instead of exactly once ever. The
`RecordStore` audit trail (`note_execution`) makes this observable:
`duplicate_executions()` counts executions minus one per distinct id and sits
at `> 0` on the broken baseline.

Output values stay CORRECT because `process_record` is deterministic — that is
what makes the defect insidious: only redundant work and audit noise betray it.

## Required outcome (goal-oriented prompt)

The prompt states only the goal: eliminate redundant processing while
preserving public behavior and ordering guarantees. No function names, no file
hints beyond package layout (`ingest.pipeline`, `ingest.dedupe`,
`ingest.store`). The agent must locate the state-lifetime bug itself.

## Reference fix (reference/fix.patch)

Hoist `Deduper` to `Pipeline.__init__` (pipeline-lifetime state) **and**
consult `RecordStore.has()` before executing, so ids are executed exactly once
across batches, sibling pipelines sharing a store, and any interleaving.
Public API surface (`ingest`, `ingest.pipeline`, `ingest.dedupe`,
`ingest.store` exports) is unchanged.

## Discrimination

* Baseline fails 3/7 public tests (cross-batch duplicate, load loop,
  shared-store) — `expect_broken_baseline: true`.
* Hidden evaluator uses disjoint data (seeded RNG load simulation with 30-id
  universe / 25 batches), an import-surface freeze, save-event first-seen
  ordering checks, late-arriving unique ids (guards against over-caching),
  same-id/conflicting-payload identity semantics, and direct use of the
  `Deduper`/`RecordStore` APIs. Special-casing the public inputs cannot pass.
