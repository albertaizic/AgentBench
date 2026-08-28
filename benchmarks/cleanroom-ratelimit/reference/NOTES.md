# cleanroom-ratelimit — maintainer notes (maintainer-only)

## Task shape

Cleanroom implementation task (`source_kind: cleanroom`). The agent receives
only a stub (`ratelimit.py` raising `NotImplementedError`) and an executable
specification, `docs/api.md`, which pins the token-bucket contract precisely:
construction validation, full initial balance, lazy exact fractional refill
with capacity clamping, non-spending failed acquisitions, retry_after math
(0.0 / deficit ÷ rate / inf), injected-clock determinism, and instance
independence.

No solution is exposed to agents: `reference/fix.patch` is a maintainer-only
minimal correct implementation used exclusively so `benchmark validate` can
prove the task is solvable and that public + hidden evaluators agree with the
spec. The manifest's reference_solution pointer is never mounted into agent
workspaces.

## Why it discriminates

* Baseline is broken by construction (`expect_broken_baseline: true`) — every
  public test errors on the stub.
* Hidden black-box tests use different capacities/rates, boundary validation
  (tokens == capacity legal, capacity+ε ValueError), bit-exact no-spend on
  failure, retry-after-then-acquire consistency, long-idle clamping,
  fractional rates/balances, zero-refill semantics, stress independence, an
  independent operation-trace model, and identical traces on wildly offset
  clocks (proves all timing flows through the injected callable — any
  wall-clock leakage diverges).
* Spec-reading precision is graded, not pattern matching: subtle clauses
  (failed acquire must not refill-and-spend, retry_after applies pending
  refill first, balance stays in [0, capacity]) each have dedicated cases.

## Reference implementation summary

Single `_TokenBucket` class with `__slots__`: stores float capacity/rate, the
clock, balance, and last-refill timestamp (observed once at construction).
`_refill()` lazily adds `(now - last) * rate` clamped at capacity;
`try_acquire` validates then spends only on success; `retry_after` returns
0.0 / deficit÷rate / math.inf per spec.
