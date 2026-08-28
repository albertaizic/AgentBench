# txnrollback — defect notes

## Defect mechanism

`TransactionBatch.commit()` (txnrollback/transactions.py) drives its staged
operations straight into `Ledger.apply()` one at a time. Validation lives
inside the ledger's per-operation checks, so validation of operation N+1…
N only happens AFTER operations 1…N-1 have already mutated balances, written
`AuditLog.record_post` rows, and enqueued outbox notifications. When a later
operation raises `LedgerError`, the except-path reverses the applied ledger
entries but:

1. never writes compensating (`record_reversal`) audit entries, so the trail
   still claims the aborted operations posted;
2. never cancels the already-enqueued notifications, so customers are told
   a failed batch succeeded;
3. never re-checks `self.committed`, so committing the same batch twice
   duplicates every side effect.

## Reference fix

Two-phase commit in `commit()`:

- `_validate_all()` projects running balances across the whole op list
  (deposits earlier in the batch fund later withdrawals) and raises
  `BatchError` before ANY mutation, audit write, or enqueue.
- Only after full validation does the apply loop run; each step writes the
  post record and queues the notification in order.
- A defensive except path around apply writes a compensating reversal
  record via `AuditLog.record_reversal` so an abort is always explained in
  the trail.
- `commit()` rejects a second invocation (`committed` flag) up front.

## Why it discriminates

- Baseline fails public tests: aborted batches keep posted audit rows,
  deliverable notifications, and double-commit duplication.
- A fix that merely special-cases the public tests' literal amounts/accounts
  fails hidden cases: unknown-counterparty mid-batch aborts, poisoned-state
  follow-on batches, intra-batch funding chains (requires simulating running
  balances rather than checking static ones), and self-transfers.
- Ledger/audit/notifier modules are correct by construction; the entire fix
  belongs in the orchestrator, so solutions must understand three interacting
  modules rather than patching one function locally.
