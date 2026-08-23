# AgentBench Architecture

AgentBench provides reproducible experimental **conditions** for evaluating
coding agents. It does not — and cannot — guarantee deterministic **LLM
output**. Everything in the design serves that distinction: pin the inputs,
isolate the execution environment, capture exhaustive evidence, and let
statistics speak.

## Component map

```text
┌───────────────────────────────────────────────────────────────┐
│ CLI (Typer)                                                   │
│  run · experiment · history · show · compare · reproduce      │
│  export · benchmark list/validate · dashboard · doctor        │
└──────────────┬────────────────────────────────────────────────┘
               │
┌──────────────▼──────────────┐   ┌───────────────────────────┐
│ Experiment planner          │   │ Benchmark discovery       │
│ cells = bench × config × n  │──▶│ manifests by name         │
│ manifest · resume · ids     │   └───────────────────────────┘
└──────────────┬──────────────┘
               ▼
┌──────────────────────────────┐
│ Runner (one benchmark run)   │
│ 1 clone+checkout workspace   │
│ 2 agent invocation           │────▶ AgentAdapter (how) ──┐
│ 3 optional usage parsing     │                          │
│ 4 diff vs pinned base sha    │────▶ ExecutionBackend( where )
│ 5 change-policy evaluation   │         host │ docker    │
│ 6 public evaluations         │              ▼           │
│ 7 hidden evaluations (host)  │      docker run --rm …   │
│ 8 classify outcome           │                          │
│ 9 persist evidence           │◀─────────────────────────┘
└──────┬───────────────────────┘
       ▼
┌──────────────────────────────┐     ┌──────────────────────────┐
│ results/<bench>/<run-id>/    │ ──▶ │ SQLite index (derived)   │
│   result.json (source truth) │     │ storage.py — only SQL    │
│   diff.patch, *.log sidecars │     └────────┬─────────────────┘
└──────────────────────────────┘              ▼
                        ┌──────────────────────────────────┐
                        │ aggregate.py · export.py         │
                        │ compare · experiments · dashboard│
                        └──────────────────────────────────┘
```

## Boundaries that matter

| Concern | Owner | Notes |
| --- | --- | --- |
| Benchmark schema & identity | `models.py` | config hash excludes `results_dir` and `execution` |
| YAML loading | `loader.py` | strict; relative repository paths resolve against the manifest |
| Agent "how" | `adapters/*` | argv construction, prompt delivery, output parsing, capabilities |
| Execution "where" | `backends/*` | placeholders, credential forwarding, limits, provenance |
| Workspace lifecycle | `workspace.py` | fresh clone per run; exact-commit verification; optional mirror cache |
| Process mechanics | `process.py` | capture, timeout, tree-kill, env hardening |
| Diff evidence | `diffs.py` | pinned base, tamper guards, numstat + name-status |
| Change policies | `protected.py` | warn/fail/allowed globs over POSIX paths |
| Evaluation | `evaluation.py` | exit codes decide; hidden evals always host-side |
| Classification | `taxonomy.py` | small fixed status set |
| Evidence | `results.py` | JSON source of truth + sidecar logs |
| Query layer | `storage.py` | only module with SQL; derived index |
| Aggregation | `aggregate.py` | medians, Wilson intervals, pairwise counts |
| Views | `dashboard.py`, `cli.py` | read-only over the index |

## Data flow: single host run

1. CLI loads the benchmark manifest, resolves relative repository paths,
   resolves the adapter, and computes nothing mutable.
2. `create_workspace` clones (optionally through the bare-mirror cache) into
   a fresh temp dir and checks out the pinned commit detached; the resolved
   sha is verified.
3. The backend runs the adapter invocation inside the workspace. On the host
   backend this is a plain subprocess; on Docker it is a labeled `--rm`
   container mounting only the workspace.
4. `capture_diff` stages everything (`git add -A`) and diffs against the
   pre-agent commit sha with textconv/ext-diff disabled.
5. Public evaluations run through the backend (same environment as the
   agent); hidden evaluations run on the host from their own directory with
   the workspace on `PYTHONPATH`.
6. `classify_run` maps evidence to exactly one taxonomy status.
7. `write_run` persists `result.json` plus sidecars *inside* the workspace
   context manager, so cleanup can never destroy evidence; the CLI then
   indexes it best-effort into SQLite.

## Data flow: Docker run

Same as above except steps 2–5: the agent executes in the container (only
the workspace mounted, empty env + allowlisted variables, explicit network
policy, recorded image id/digest); public evaluations execute in the same
image via stdin-fed `sh -s`; diff capture, hidden evaluations, classification
and persistence remain host-side. The container is `--rm`; resources carry
the `org.agentbench.run=true` label for cleanup.

## Data flow: multi-run experiment

1. `experiments.py` loads the matrix spec, discovers benchmark manifests,
   and plans one cell per benchmark × config × trial with content-derived
   cell keys.
2. A new manifest records identities (benchmark/config hashes) at planning
   time and is rewritten atomically after every cell.
3. Each cell is an ordinary single run tagged with the experiment id;
   resume skips completed cell keys and rejects changed identities.
4. Interrupts finalize the manifest as incomplete while preserving all
   completed run evidence.

## Performance notes

- The Git mirror cache removes repeated network clones; correctness never
  depends on it (fallback to direct clone, commit verified post-checkout).
- Dashboard rescans of the results tree are throttled (1 s) and idempotent.
- SQLite writes are short transactions keyed by run id — re-indexing is safe.
