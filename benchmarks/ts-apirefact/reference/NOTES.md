# ts-apirefact — defect & task notes (maintainer-facing)

## Situation

`src/client.js` is a monolithic `ApiClient`: every operation inlines URL
construction and `{ data: ... }` envelope unwrapping. The public surface is
eight flat methods, so the class grows linearly with the API and callers
cannot be reasoned about per resource.

## Target contract (what the reference patch delivers)

- `src/facade.js` exports `createClient({ transport })` returning
  `{ users, projects }`; each resource (`src/resources/*.js`) exposes the
  same operations over the exact historical wire paths:
  - users: `GET/POST /v2/users`, `GET/DELETE /v2/users/:id`
  - projects: `GET/POST /v2/projects`, `GET /v2/projects/:id`,
    `POST /v2/projects/:id/close`
- `src/response.js` centralizes envelope unwrapping (same "malformed
  response envelope" error behavior).
- `src/client.js` becomes a deprecated shim: flat methods delegate to the
  resources and emit `process.emitWarning(..., { code: "ACME-DEP-API" })`
  at most once per instance per method name.
- `tools/dump_users.js` and `tools/audit_projects.js` migrate to the
  resources; their output strings stay byte-identical.

## Why it discriminates

- Naive solutions that delete or rename flat methods fail compatibility
  (hidden parity test compares flat vs namespaced results).
- Solutions that warn on every call fail the once-per-instance-per-method
  check; solutions that never wire the warning through Node's warning
  channel fail both public and hidden warning capture.
- Solutions that change wire paths while keeping shapes fail the request-log
  assertions (public uses one dataset, hidden a different one).
- Callers left un-migrated trigger deprecation warnings during tool runs,
  which the hidden evaluator detects via the captured warning channel.

## Regeneration

- Fixture: `.venv/Scripts/python.exe benchmarks/ts-apirefact/create_fixture.py`
  (deterministic commit; pins itself into benchmark.yaml).
- Reference patch: `.venv/Scripts/python.exe benchmarks/ts-apirefact/make_reference.py`

Language note: declared as TypeScript-suite task, but sources are plain
Node ESM `.js` with JSDoc type annotations to keep the fixture zero-
dependency (no tsc/vitest toolchain); the runner is node:test-based.
