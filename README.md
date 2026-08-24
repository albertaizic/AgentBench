# AgentBench

A reproducible evaluation framework for coding agents. AgentBench clones a
repository at an exact commit into a throwaway workspace, runs a coding agent
on a task, captures every piece of evidence about what the agent did, and
scores the result with evaluations the agent never sees.

It is not an AI chat wrapper: runs are batch, headless, measured, persisted,
and queryable.

**AgentBench provides reproducible experimental *conditions*. It does not
guarantee deterministic LLM *output*.**

## Quick start

```bash
pip install -e .
agentbench doctor                                   # environment checks
agentbench benchmark list                           # discover the corpus
agentbench benchmark list --suite python-core       # filter by suite
agentbench benchmark validate stockflow             # offline solvability check
agentbench benchmark validate --all                 # sweep the whole corpus
agentbench run benchmarks/stockflow/benchmark.yaml
agentbench run benchmarks/stockflow/benchmark.yaml --repeat 5
agentbench run benchmarks/stockflow/benchmark.yaml --baseline reference
agentbench experiment experiments/python-agents.yaml --dry-run
agentbench experiment experiments/python-agents.yaml --jobs 4 --max-runs 20
agentbench history
agentbench show <run-id>
agentbench compare stockflow
agentbench benchmark report                         # evidence-informed corpus view
agentbench reproduce <run-id>
agentbench export --experiment <experiment-id> --format csv
agentbench dashboard          # http://127.0.0.1:8765
```

Requires Python ≥ 3.12 and `git` on PATH. Docker Desktop enables the optional
Docker backend. The Claude Code adapter requires the `claude` CLI (adapter
flags verified against 2.1.239–2.1.241).

## Commands

| Command | Purpose |
| --- | --- |
| `run <benchmark.yaml>` | one trial (`--repeat N` trials, `--baseline reference` for the maintenance check) |
| `experiment <matrix.yaml> [--resume id] [--jobs N] [--max-runs K] [--dry-run]` | benchmark × config × repeat matrix, bounded parallelism, resumable |
| `history [--benchmark --agent --model --status --limit]` | list persisted runs (incl. `setup_failed`) |
| `show <run-id>` | full evidence view incl. failure stage + per-stage timings |
| `compare <benchmark>` | aggregate by configuration; warns on commit drift |
| `reproduce <run-id>` | re-run under recorded conditions; prints provenance diff |
| `export [--experiment/--benchmark/--agent/--status] --format csv\|json` | flattened safe metrics |
| `benchmark list [--suite s]` / `benchmark validate <name>\|--all` | corpus discovery / validation sweep with rollup counts |
| `benchmark report` | pass rates, medians and calibration flags per benchmark |
| `dashboard [--port --host]` | read-only local web UI (paginated, filterable) |
| `doctor` | environment readiness checks |
| `cleanup workspaces\|containers [--apply]` | remove AgentBench-owned stale resources (dry-run default) |

Exit codes: `run` 0 pass · 1 fail · 2 setup error · 130 interrupted;
`experiment` exits 0 when the matrix completes (cell outcomes are data);
query commands use 2 only for AgentBench-level errors.

## Execution backends

The **adapter** describes how to invoke an agent; the **execution backend**
describes where that invocation runs:

```yaml
execution:
  backend: docker        # host (default) or docker
  image: python:3.12-slim
  network: enabled       # or disabled (--network none)
  memory: 2g             # optional limits, recorded in results
  cpus: 1.5
  pids_limit: 256
  pass_env:              # credential allowlist — names only, never values
    - ANTHROPIC_API_KEY
```

Only the workspace is mounted — never AgentBench source, hidden evaluators,
results storage, host home, or the Docker socket. Container env starts empty;
only allowlisted variables are forwarded. Containers carry the
`org.agentbench.run=true` label and are removed after runs. Resolved image ID
and digests are recorded as provenance (mutable tags alone are not evidence).

## Benchmark format

```yaml
name: stockflow                     # directory-safe identity [A-Za-z0-9._-]
repository: fixture                 # git URL or path; relative = beside this file
commit: 020f9f1e2617…               # exact commit checked out (hex)
prompt: |
  Task given verbatim to the agent.
agent:
  type: claude-code                 # claude-code or command
  model: sonnet                     # optional, adapter-honored
  # command: /opt/wrapper/claude    # binary override (claude-code)
  # extra_args: ["--verbose"]
evaluations:                        # public: run in the workspace (same backend as agent)
  - name: public-tests
    command: '"{python}" -m pytest -q'
hidden_evaluations:                 # optional: run OUTSIDE the workspace, host-side
  source: hidden
  evaluations:
    - name: behavioral-checks
      command: '"{python}" -m pytest -q'
protected_paths: [tests/**]         # globs over Git-changed paths
fail_on_protected_path_violation: true
change_policies:                    # optional declarative policy groups
  - patterns: ["pyproject.toml"]
    policy: warn                    # warn | fail | allowed
execution: {backend: host}          # optional execution block
timeout_seconds: 600
results_dir: results

# Optional metadata (never used for scoring):
description: …
category: bugfix
tags: [state, cache]
language: python
difficulty: medium
reference_solution: {patch: reference/fix.patch}   # maintenance-only
expect_broken_baseline: true
```

Placeholders in evaluation commands: `{python}` (backend-appropriate
interpreter), `{workspace}`, `{hidden_dir}`. Unknown fields are rejected.
See `docs/ARCHITECTURE.md` and `docs/THREAT_MODEL.md`.

## Benchmark corpus

`benchmarks/` ships sixteen deterministic Python benchmarks, each with public
tests, hidden behavioral evaluators, protected paths, and — except where
grading is patch-free — a reference solution patch. Every fixture repo is
reproduced, at its exact commit sha, by its generator on any machine:

| Benchmark | Category | Difficulty | Task shape |
| --- | --- | --- | --- |
| stockflow | bugfix | medium | stale cache + duplicate handling + input validation across files |
| ledgerpad | bugfix | easy | missing boundary/currency validation at an entry point |
| configschema | api-change | medium | propagate strict mode through every caller |
| jobqueue | bugfix | easy | numeric priority ordering + stable tie-breaks |
| prefsfile | bugfix | medium | schema versioning + legacy coercion for settings files |
| fuzzysearch | performance | hard | replace quadratic scan under a comparison budget |
| iniforge | config parsing | easy | last-duplicate-wins keys + strict boolean coercion |
| csvroll | serialization | medium | quoted CSV round-trip + header-less v1 migration |
| tokenbucket | state/rate logic | medium | no negative balances, fractional refill credit |
| logroll | resource cleanup | easy | idempotent logger handlers, closed old streams |
| vercomp | compatibility | medium | numeric version parts + pre-release ordering |
| retryloop | error handling | medium | retry only retryable errors; re-raise the last |
| typegate | api-change | medium | strict-mode flag propagated through all consumers |
| bankday | transactional | hard | validate-then-mutate transfers, atomic batches |
| htmlstrip | parser/performance | hard | character-scanning HTML→text mini-parser |
| testforge | test-writing | hard | write a suite that kills five seeded mutants |

Suites are declarative metadata on each manifest (`suites: [...]`) — e.g.
`smoke` (ledgerpad, jobqueue), `bugfix`, `performance`, `python-core` —
usable by `benchmark list --suite` and experiment benchmark selectors.
Difficulty labels are provisional metadata; `benchmark report` adds
evidence-based calibration flags once enough runs accumulate.

Maintain fixtures by editing the generator's embedded contents, re-running
it (deterministic shas), and re-pinning the commit. Reference patches are
regenerated with `python benchmarks/_make_reference.py`.

## Reproducibility model

* fixed repository + pinned commit, detached checkout in a fresh temp
  workspace per trial — no state carries between runs; a bare-mirror cache
  accelerates remote clones (hit/miss + preparation seconds recorded in the
  run's execution provenance, `AGENTBENCH_NO_CACHE=1` disables it, any cache
  fault falls back to a direct clone);
* Claude Code runs headless with user tooling isolated: no inherited MCP
  servers (`--strict-mcp-config` + empty config), no user/local settings
  (`--setting-sources project`), structured JSON output for metrics;
* diff captured against the pre-agent commit (agent commits included,
  textconv/ext-diff forgery disabled);
* evaluations decide PASS/FAIL from exit codes only; hidden evaluators count;
* every run persists `result.json` (schema v4: adds `failure_stage` +
  `stage_timings`), `diff.patch`, and raw logs under
  `results/<benchmark>/<run-id>/`; SQLite at
  `results/.agentbench/agentbench.db` is a derived index (WAL mode for
  parallel writers) — JSON is the source of truth and is rescanned
  automatically.

### Setup failures are evidence too

A run whose environment breaks — unclonable repository, missing commit or
Docker image, missing agent binary — persists a `setup_failed` result with a
structured `failure_stage` (`load`, `source`, `workspace`, `backend_prepare`,
`agent`, `evaluation`, `evidence`, `persistence`, `cleanup`). Statuses stay a
small closed set; stages answer *where* it happened. Docker infrastructure
failures (exit codes 125/126/127, "Unable to find image", daemon unreachable)
are classified at `backend_prepare`, never as `agent_failed`. Only invalid
manifest YAML — where no identity exists — remains unpersisted.

## Hidden evaluations & protected paths

Hidden evaluator sources live beside the benchmark manifest, are never copied
into the workspace, and execute host-side after the agent finishes with the
workspace on `PYTHONPATH`. Their results count toward PASS/FAIL and are
labeled separately everywhere.

Protected paths and change policies match fnmatch globs against Git-changed
paths. Violations are always recorded with the matched pattern; `fail`-grade
hits classify the run as `protected_path_violation`. Guarantees stop there:
hidden tests are hidden from the workspace, not cryptographically secret.

## Metrics

- **Always available**: status, failure reason, duration, agent exit code /
  timeout flag, per-evaluation exit codes / durations / pass-fail, diff stats
  + changed/added/deleted/renamed/binary file lists, requested & resolved
  commits, run id, timestamp, trial number, config hash, environment
  metadata (AgentBench / Python / platform / Git versions).
- **Adapter-dependent** (Claude Code JSON output): input/output/total tokens,
  cost USD, turns, session id, model. Adapters declare these via small
  capability sets stored with each result.
- **Unavailable stays null** — tool-call counts are currently null even for
  Claude; nothing is estimated. Raw agent output is preserved regardless.

## Experiments, statistics, provenance

Experiments persist a manifest (planned/completed/interrupted cells, identity
hashes, the resolved benchmark list). Benchmarks come as explicit names or a
metadata selector (`suite:` / `tags:` / `category:`); the selector resolves
once at creation and the resolved list is stored, so later corpus changes
never silently alter an existing experiment. `--dry-run` prints benchmarks,
configs, resource limits, total cells and maximum parallelism without touching
the filesystem. `--jobs N` (default 1) runs up to N cells concurrently — each
cell keeps an independent workspace/container/process state; Ctrl+C stops
scheduling new cells while in-flight ones finish so their evidence stays
complete; `--max-runs K` is a hard stop that remains resumable. Docker
configs without memory/cpus/pids limits are clamped to 4-way parallelism with
an explicit notice.

Resume skips completed cells and rejects changed benchmark or config
identities. Comparison aggregates group strictly by config hash and warn when
a benchmark was evaluated at multiple resolved commits. Reports include N,
pass rate with Wilson 95% intervals, median/IQR durations and tokens, median
cost, failure taxonomy counts, median changed lines and protected-violation
rates. Pairwise config comparisons use matched cells only (both-pass /
A-only / B-only / both-fail) with a dependency-free exact McNemar test;
ranking offers sortable dimensions plus a Pareto frontier (quality vs cost,
quality vs speed) instead of a fabricated composite score. Sample sizes are
always visible; nothing claims "better" on thin data.

`agentbench reproduce <run-id>` reconstructs conditions from stored evidence,
refuses to silently mix changed configurations, and prints which provenance
fields match (identity, resolved commit, backend, image digest).

## Baselines

`agentbench run <benchmark.yaml> --baseline reference` applies the benchmark's
declared reference patch in a throwaway workspace and evaluates it exactly
like an agent run. It proves the task is solvable and the evaluators agree.
These runs persist with agent type `reference-baseline` and are never
presented as AI coding agents anywhere. The patch itself lives beside the
manifest, is used only by this command and `benchmark validate`, and is never
visible to any agent workspace.

## Dashboard

Read-only localhost UI: overview totals, experiments list + matrix detail
(success grid, taxonomy, duration charts), corpus page with per-benchmark
pass rates, run details including full execution provenance, and artifact
viewing with strict traversal protection. Server-rendered stdlib HTML — no
frontend framework.

## Current limitations

- **No true sandbox**: host-backend agents run with your process permissions;
  the default `inherit` environment policy passes your full environment
  through (needed for ambient authentication). An opt-in
  `execution.env_policy: restricted` starts children from a minimal OS base +
  the `pass_env` allowlist with a disposable HOME, but this is policy, not a
  security boundary — see `docs/THREAT_MODEL.md`. Docker improves containment
  (workspace-only mount, empty env, optional network-off) but is also not a
  perfect sandbox.
- Stochastic model behavior: single runs are anecdotes; use `--repeat`,
  `experiment`, and Wilson intervals.
- Hidden tests are hidden from the workspace, not encrypted; protected-path
  detection records evidence, it does not prevent edits.
- Docker backend requires a reachable daemon. Claude-in-Docker works via a
  locally-built image with credentials forwarded only through `pass_env`;
  building that image is documented but out of scope of the package.
- Only Claude Code is a real validated adapter; `command` covers generic CLIs
  with null usage metrics.
- Parallel experiments stop scheduling on Ctrl+C but in-flight cells run to
  their own timeouts; killing AgentBench outright can leak workspaces or
  containers until `agentbench cleanup` reclaims them by ownership label.
