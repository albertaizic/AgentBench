# AgentBench study — v05-study

- Experiment: `20260824T213115Z-78d3985e`
- Generated: 2026-08-26T02:52:04+00:00 by AgentBench 0.6.0
- Created: 2026-08-24T21:31:15.832885+00:00
- Backend: host · repeats/cell: 3 · runs recorded: 60 of 60 planned cells

## Configurations

| config | identity | config hash |
|---|---|---|
| claude-code | claude-code / sonnet | `400f98ad94a8` |
| hermes-openrouter | hermes / stealth/ox-alpha / provider=openrouter / reasoning=medium | `c8396f624342` |

- Attempted 60 runs · validly graded 60 · infra-invalid 0
- **Comparison mode**: `system-comparison` — complete coding systems differ (harness AND model)

## Per-config aggregate

| config | runs | passes | pass rate | Wilson 95% | median time | median tokens | median cost | cost evidence | failures |
|---|---|---|---|---|---|---|---|---|---|
| claude-code | 30 | 27 | 90% | [74%–97%] | 1:50 | 245.9k | $0.3980 | reported | evaluation_failed:3, passed:27 |
| hermes-openrouter | 30 | 7 | 23% | [12%–41%] | 43s | 182.4k | — | unpriced | evaluation_failed:23, passed:7 |

## Paired outcomes (matched benchmark × trial cells)

- **claude-code vs hermes-openrouter** over 30 matched cells: both pass 6 · claude-code only 21 · hermes-openrouter only 1 · both fail 2
  → marginal check: claude-code 27 passes (6+21) · hermes-openrouter 7 passes (6+1) · McNemar exact p=1.1e-05

## Per-benchmark results

| benchmark | claude-code | hermes-openrouter |
|---|---|---|
| jobqueue | 3/3 passed · 1:18 · 242.9k tok | 0/3 passed · 43s · — tok |
| ledgerpad | 3/3 passed · 1:53 · 242.3k tok | 0/3 passed · 40s · — tok |
| iniforge | 3/3 passed · 53s · 181.6k tok | 0/3 passed · 40s · — tok |
| csvroll | 3/3 passed · 2:36 · 254k tok | 0/3 passed · 41s · — tok |
| prefsfile | 3/3 passed · 2:29 · 250.9k tok | 0/3 passed · 44s · — tok |
| tokenbucket | 3/3 passed · 1:33 · 215.4k tok | 0/3 passed · 40s · — tok |
| vercomp | 3/3 passed · 3:03 · 264.1k tok | 0/3 passed · 39s · — tok |
| leasekit | 0/3 passed · 4:33 · 393.6k tok | 1/3 passed · 2:13 · 193.8k tok |
| statediag | 3/3 passed · 1:45 · 285k tok | 3/3 passed · 3:03 · 222.3k tok |
| bankday | 3/3 passed · 1:46 · 218k tok | 3/3 passed · 2:27 · 175.1k tok |

## Corpus difficulty / saturation

| benchmark | runs | classification | evidence |
|---|---|---|---|
| bankday | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| csvroll | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| iniforge | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| jobqueue | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| leasekit | 6 | **discriminating** (17%) | >= 25% across 6 graded runs |
| ledgerpad | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| prefsfile | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| statediag | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| tokenbucket | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| vercomp | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |

## Reliability (observed repeated trials)

| config | tasks | runs | passes | pass@1 [Wilson 95%] | any-in-k | all-k | mean p* |
|---|---|---|---|---|---|---|---|
| claude-code | 10 | 30 | 27 | 90% [74%/97%] | 90% (k=3) | 90% | 0.875 |
| hermes-openrouter | 10 | 30 | 7 | 23% [12%/41%] | 30% (k=3) | 20% | 0.250 |

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
