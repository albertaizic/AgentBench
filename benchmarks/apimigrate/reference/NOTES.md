# apimigrate — defect notes

## Symptom

The SDK is stuck mid-migration. `fetch_user_profile()` was superseded by
the `fetch_user()` / `fetch_preferences()` pair, but:

1. every internal caller (`caller_a/b/c`) and the external-facing
   `AccountFacade` still funnel through the deprecated method, so any run
   with `DeprecationWarning` escalated to errors explodes;
2. the shim warns on **every** call, spamming logs and tripping
   `-W error::Deprecation` after the first external call.

## Mechanism

Three independent gaps, all in the fixture as shipped:

- `client.ProfileClient.fetch_user_profile` warns unconditionally; there is
  no per-call-site dedupe at all (the constructor's bookkeeping slot is a
  stub). The required contract is "at most once per calling module per
  client instance": record caller modules via
  `sys._getframe(1).f_globals.get("__name__")` in an instance-level set and
  warn only on first sight — with `stacklevel=2` so the warning points at
  the caller.
- The three internal callers decompose the combined payload they only
  partially use; each should call `fetch_user()` plus `fetch_preferences()`
  for exactly the fields it needs.
- `AccountFacade.account_overview` delegates to the shim instead of
  composing the split API, and the facade exposes no passthroughs.

## Fix shape

Instance-level `_deprecation_seen: set[str]` in the client; guard the warn.
Migrate each caller to the two fine-grained calls (outputs unchanged).
Facade gains `fetch_user` / `fetch_preferences` passthroughs and builds
`account_overview` from them. Shim keeps returning
`{**user, "preferences": preferences}`.

## Why it discriminates

Public tests pin the contract on users u-101..u-103. The hidden evaluator
uses different records (u-204..u-206) and additionally checks: warnings
across repeated shim calls land exactly once *and* are `DeprecationWarning`
instances, a fresh instance warns again (pins the per-instance semantics an
agent might otherwise implement globally), facade passthroughs return exact
backend dicts, shim payload equals manual composition, and unknown-user
KeyError behavior is preserved through the new methods.
