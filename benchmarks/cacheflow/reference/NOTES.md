# cacheflow — defect notes

## Symptom

Owner-grouped and status-grouped dashboards serve outdated membership after
any repository mutation. Single-record reads stay fresh, so the bug looks
like "views disagree with the record page" and only a full cache flush
repairs it.

## Mechanism

`RecordService` caches three entry families in the `NamespaceCache`, each
addressed by namespace + composite key:

- `("record_detail", record_id)` — single-record lookups
- `("owner_records", owner)` — derived view over `repository.by_owner`
- `("status_records", status)` — derived view over `repository.by_status`

Every mutation path (`create_record`, `update_record`, `delete_record`)
calls a helper that invalidates **only** `("record_detail", record_id)`.
The two list-view families are never dropped, so once materialized they
keep returning pre-mutation snapshots. A transfer of ownership makes the
*old* owner's cached list stale *and* the *new* owner's list stale; a
status change does the same to both status buckets; deleting or creating
over an already-materialized view leaves ghost/missing rows.

The cache itself is fine — by design it offers only exact-key
`invalidate()`, no wildcard flush — so the fix must live in the service
layer: compute every composite key affected by a mutation (old and new
owner, old and new status, plus the created/deleted id's buckets) and drop
each one explicitly.

## Fix shape

A single `_invalidate_derived(before, after=None)` helper called from all
three mutation paths: always drops `("record_detail", id)`, plus
`("owner_records", o)` / `("status_records", s)` for the union of before/
after values. Reads are unchanged; caching behavior for untouched views is
preserved.

## Why it discriminates

Public tests cover one owner transfer, one status flip, a delete and a
create-over-empty-view with a small fixed dataset. The hidden evaluator
runs different data through multi-hop transfer chains (a→b→c with views
warmed between hops), delete-then-recreate of the same id, noop updates,
and a scripted 7-operation sequence checked against repository truth after
every step. Fixes that special-case the public fixtures (e.g. invalidate
only on status change, or nuke the whole cache on any mutation) fail:
the former fails the scripted-sequence assertions, the latter fails the
"untouched views remain cached" requirement.
