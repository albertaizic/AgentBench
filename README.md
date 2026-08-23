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
agentbench benchmark validate stockflow             # offline solvability check
agentbench run benchmarks/stockflow/benchmark.yaml
agentbench run benchmarks/stockflow/benchmark.yaml --repeat 5
agentbench experiment experiments/python-agents.yaml
agentbench history
agentbench show <run-id>
agentbench compare stockflow
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
| `run <benchmark.yaml>` | one trial (`--repeat N` for independent trials + aggregate) |
| `experiment <matrix.yaml> [--resume id]` | benchmark × config × repeat matrix with manifest + resume |
| `history [--benchmark --agent --model --status --limit]` | list persisted runs |
| `show <run-id>` | full evidence view for one run |
| `compare <benchmark>` | aggregate by configuration; warns on commit drift |
| `reproduce <run-id>` | re-run under recorded conditions; prints provenance diff |
| `export [--experiment/--benchmark] --format csv\|json` | flattened safe metrics |
| `benchmark list` / `benchmark validate <name>` | corpus discovery / offline solvability validation |
| `dashboard [--port --host]` | read-only local web UI |
| `doctor` | environment readiness checks |
| `cleanup workspaces\|containers [--apply]` | remove AgentBench-owned stale resources |

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

`benchmarks/` ships six deterministic Python benchmarks, each with public
tests, hidden behavioral evaluators, protected paths, a reference solution
patch (maintenance-only), and a generator that reproduces the fixture repo —
and its exact commit sha — on any machine:

| Benchmark | Category | Task shape |
| --- | --- | --- |
| stockflow | bugfix (medium) | stale cache + duplicate handling + input validation across files |
| ledgerpad | bugfix (easy) | missing boundary/currency validation at an entry point |
| configschema | api-change (medium) | propagate strict mode through every caller |
| jobqueue | bugfix (easy) | numeric priority ordering + stable tie-breaks |
| prefsfile | bugfix (medium) | schema versioning + legacy coercion for settings files |
| fuzzysearch | performance (hard) | replace quadratic scan under a deterministic comparison budget |

Maintain fixtures by editing the generator's embedded contents, re-running
it (deterministic shas), and re-pinning the commit. Reference patches are
regenerated with `python benchmarks/_make_reference.py`.

## Reproducibility model

* fixed repository + pinned commit, detached checkout in a fresh temp
  workspace per trial — no state carries between runs;
* Claude Code runs headless with user tooling isolated: no inherited MCP
  servers (`--strict-mcp-config` + empty config), no user/local settings
  (`--setting-sources project`), structured JSON output for metrics;
* diff captured against the pre-agent commit (agent commits included,
  textconv/ext-diff forgery disabled);
* evaluations decide PASS/FAIL from exit codes only; hidden evaluators count;
* every run persists `result.json` (schema v3), `diff.patch`, and raw logs
  under `results/<benchmark>/<run-id>/`; SQLite at
  `results/.agentbench/agentbench.db` is a derived index — JSON is the
  source of truth and is rescanned automatically.

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
hashes, run ids). Resume skips completed cells and rejects changed benchmark
or config identities. Comparison aggregates group strictly by config hash and
warn when a benchmark was evaluated at multiple resolved commits. Pass rates
carry Wilson 95% intervals; durations report medians with p25/p75. Pairwise
config comparisons count matched-cell outcomes without overclaiming.

`agentbench reproduce <run-id>` reconstructs conditions from stored evidence,
refuses to silently mix changed configurations, and prints which provenance
fields match (identity, resolved commit, backend, image digest).

## Dashboard

Read-only localhost UI: overview totals, experiments list + matrix detail
(success grid, taxonomy, duration charts), corpus page with per-benchmark
pass rates, run details including full execution provenance, and artifact
viewing with strict traversal protection. Server-rendered stdlib HTML — no
frontend framework.

## Current limitations

- **No true sandbox yet**: host-backend agents run with your process
  permissions and network access; credentials necessarily come from your
  environment. Docker improves containment but is not a perfect sandbox —
  see `docs/THREAT_MODEL.md`.
- Stochastic model behavior: single runs are anecdotes; use `--repeat`,
  `experiment`, and Wilson intervals.
- Hidden tests are hidden from the workspace, not encrypted; protected-path
  detection records evidence, it does not prevent edits.
- Docker backend requires a reachable daemon; live Docker+Claude
  authentication inside containers has not been validated on all platforms.
- Only Claude Code is a real validated adapter; `command` covers generic
  CLIs with null usage metrics.
- Setup failures are not persisted as runs; v0.1/v0.2 results are indexed
  best-effort with nulls for fields that did not exist then.
