# ts-asyncbug — defect & task notes (maintainer-facing)

## Defect mechanism

`src/queue.js` dispatches each job as
`Promise.resolve().then(handler).then(record)` and never awaits or tracks the
resulting promise chain:

1. `flush()` resolves before any outcome is recorded, so callers reading
   `stats`/`results` right after `await flush()` see completed=failed=0.
2. `close()` clears `pending` without waiting for dispatched work; jobs
   flushed immediately before close lose their bookkeeping entirely.
3. `src/scheduler.js` compounds both: its retry branch reads the stale
   `queue.stats.failed` (always 0 at decision time), so failures are never
   retried, and the final report is built from the same stale counters.

Because handlers are deterministic (sync or resolved values), the drop is
reproducible purely through microtask ordering — no timers or real I/O.

## Reference fix

- `flush()` collects one tracked settle-promise per dispatched job and awaits
  `Promise.all` of them; unknown kinds still fail synchronously.
- `close()` marks the queue closed, drains any remaining pending jobs via a
  final flush, and therefore cannot strand accepted work.
- `runPipeline` chains job ids across retries (a retried spec keeps its
  logical id), reads outcomes only after flushes resolve, retries failed jobs
  up to `maxAttempts`, and builds the report from per-job final outcomes so
  `succeeded + failed.length === total` always holds.

## Why it discriminates

- Any fix that merely adds `await handler(...)` inside the loop passes flush
  accounting but still fails close-drain semantics if pending work is
  discarded rather than drained.
- A scheduler "fix" that reports counts from a fresh sweep but never re-enques
  fails the retry tests (hidden flaky-handler case succeeds only on attempt 2).
- Solutions that make close() silently swallow post-close enqueues (no dropped
  counter) fail the explicit-drop assertions.
- Public data differs from hidden data; hidden also uses a 30-job batch and
  retry-budget bounds, defeating input special-casing.

Language note: TypeScript-suite task implemented as zero-dependency Node ESM
`.js` with JSDoc annotations; runner is node:test-based (`node run_tests.mjs`).
