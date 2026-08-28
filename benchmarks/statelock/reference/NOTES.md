# statelock — defect notes

## Defect mechanism

Two interacting holes let a lock reach `committed` with its side effects
never executed:

1. **Unlocked retry fast-path** (`StateMachine.retry`,
   statelock/machine.py). Instead of resuming the interrupted pipeline, the
   retry sets `self._state` straight to `ACTION_TARGET[action]`. The
   intermediate state's required effect (`allocate_stock` for a retried
   reserve, `finalize` for a retried commit) never reaches the journal and
   no guard ever runs.
2. **Label-only guard** (`TransitionGuard.check`, statelock/guards.py).
   The guard receives the current surface state string and compares it to
   `ENTRY_STATES`; it never inspects the effect journal, so it cannot tell
   a legitimate `reserving → reserved → committing → committed` walk from
   one whose evidence was skipped.

Net effect: interrupt + retry yields `reserved` with zero allocation
evidence, and a subsequent commit sails through the vacuous guard.

## Reference fix

- `retry()` replays completed outcomes as before, but for genuinely
  interrupted requests routes through `_drive()` (the single guarded
  transition pipeline), which now accepts either the action's start state
  or its intermediate state. `EffectJournal.run`'s per-request idempotency
  key makes reruns of already-executed effects no-ops, so resuming is safe
  and exactly-once.
- `_drive()` consults `TransitionGuard.check(target_state, journal)` BEFORE
  flipping into the target state; the fixed `check` verifies every entry of
  `REQUIREMENTS[target_state]` exists in the journal (`committed` requires
  journaled `allocate_stock`) and raises `GuardViolation` listing the
  missing evidence. Refusals happen before any state change or effect run.
- Idempotency contract unchanged: `process`/`retry` on a request id with a
  recorded outcome returns it untouched.

## Why it discriminates

- Baseline fails public tests: interrupted retries skip `allocate_stock`,
  and evidence-free machines commit successfully.
- Fixes that only delete the fast-path but leave the label-only guard fail
  hidden checks where a snapshot-restored machine commits without
  evidence; fixes that only harden the guard fail hidden interrupted-commit
  cases (missing `finalize`). Both holes must close.
- Hidden sequences use different ids/orderings (interrupted commits,
  multi-interrupt recovery chains, cross-machine isolation), so literal
  special-casing of public inputs cannot pass.
