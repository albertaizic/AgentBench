# AgentBench study — v06-system-comparison

- Experiment: `20260825T203921Z-05a0f1e4`
- Generated: 2026-08-26T02:52:02+00:00 by AgentBench 0.6.0
- Created: 2026-08-25T20:39:21.004211+00:00
- Backend: host · repeats/cell: 3 · runs recorded: 36 of 36 planned cells

## Configurations

| config | identity | config hash |
|---|---|---|
| claude-code-sonnet | claude-code / sonnet | `400f98ad94a8` |
| omp-ox-alpha | omp / stealth/ox-alpha / provider=openrouter / reasoning=medium | `a63fcd2ae8a5` |

- Attempted 36 runs · validly graded 36 · infra-invalid 0
- **Cell without recorded run**: omp-ox-alpha / cacheflow trial 1 — setup_failed (`PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: '<local-path>'`)
- **Comparison mode**: `system-comparison` — complete coding systems differ (harness AND model)

## Per-config aggregate

| config | runs | passes | pass rate | Wilson 95% | median time | median tokens | median cost | cost evidence | failures |
|---|---|---|---|---|---|---|---|---|---|
| claude-code-sonnet | 18 | 15 | 83% | [61%–94%] | 49s | 299.7k | $0.1579 | reported | evaluation_failed:3, passed:15 |
| omp-ox-alpha | 18 | 17 | 94% | [74%–99%] | 1:26 | 24k | — | unpriced | evaluation_failed:1, passed:17 |

## Paired outcomes (matched benchmark × trial cells)

- **claude-code-sonnet vs omp-ox-alpha** over 18 matched cells: both pass 15 · claude-code-sonnet only 0 · omp-ox-alpha only 2 · both fail 1
  → marginal check: claude-code-sonnet 15 passes (15+0) · omp-ox-alpha 17 passes (15+2) · McNemar exact p=0.5

## Per-benchmark results

| benchmark | claude-code-sonnet | omp-ox-alpha |
|---|---|---|
| tokenbucket | 3/3 passed · 29s · 166.2k tok | 3/3 passed · 1:08 · 21.5k tok |
| leasekit | 0/3 passed · 1:16 · 332k tok | 2/3 passed · 1:47 · 22.4k tok |
| statelock | 3/3 passed · 1:43 · 335.9k tok | 3/3 passed · 2:21 · 26.2k tok |
| pluginreg | 3/3 passed · 46s · 315.1k tok | 3/3 passed · 1:08 · 23.1k tok |
| cacheflow | 3/3 passed · 37s · 178.9k tok | 3/3 passed · 1:32 · 24.4k tok |
| apimigrate | 3/3 passed · 53s · 383.3k tok | 3/3 passed · 1:32 · 25.3k tok |

## Corpus difficulty / saturation

| benchmark | runs | classification | evidence |
|---|---|---|---|
| apimigrate | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| cacheflow | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| leasekit | 6 | **discriminating** (33%) | >= 25% across 6 graded runs |
| pluginreg | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| statelock | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| tokenbucket | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |

## Reliability (observed repeated trials)

| config | tasks | runs | passes | pass@1 [Wilson 95%] | any-in-k | all-k | mean p* |
|---|---|---|---|---|---|---|---|
| claude-code-sonnet | 6 | 18 | 15 | 83% [61%/94%] | 83% (k=3) | 83% | 0.800 |
| omp-ox-alpha | 6 | 18 | 17 | 94% [74%/99%] | 100% (k=3) | 83% | 0.900 |

\* mean p is a Beta(1,1) smoothed posterior mean — descriptive, not a
classic pass@k estimator: these are observed repeated trials.

## Known limitations

- Results reflect this corpus and these configurations only; they do not generalize to other tasks or workloads.
- Each cell was run 3 time(s); small samples carry wide uncertainty (Wilson intervals are shown, not hidden).
- Cost/token figures come from each harness's own reporting; where measurement sources differ materially, cross-agent cost comparisons are indicative only.
- Comparison mode: system-comparison.

## Reproduce

```text
# re-run the identical matrix (same benchmarks/configs/commits)
agentbench show <run-id>            # inspect one cell's evidence
agentbench reproduce <run-id>       # preflight + rerun one cell
```
