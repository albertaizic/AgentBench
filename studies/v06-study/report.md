# AgentBench study — v06-model-controlled

- Experiment: `20260825T191521Z-4727103d`
- Generated: 2026-08-26T02:51:59+00:00 by AgentBench 0.6.0
- Created: 2026-08-25T19:15:21.110253+00:00
- Backend: host · repeats/cell: 3 · runs recorded: 48 of 48 planned cells

## Configurations

| config | identity | config hash |
|---|---|---|
| hermes-gpt-oss-20b | hermes / openai/gpt-oss-20b / provider=openrouter / reasoning=medium | `1177c6595f0a` |
| hermes-ox-alpha | hermes / stealth/ox-alpha / provider=openrouter / reasoning=medium | `c8396f624342` |

- Attempted 48 runs · validly graded 48 · infra-invalid 0
- **Comparison mode**: `model-controlled` — one harness held constant; only model settings vary

## Per-config aggregate

| config | runs | passes | pass rate | Wilson 95% | median time | median tokens | median cost | cost evidence | failures |
|---|---|---|---|---|---|---|---|---|---|
| hermes-gpt-oss-20b | 24 | 13 | 54% | [35%–72%] | 3:08 | 362.1k | $0.0117 | provider_models_api/estimated | evaluation_failed:10, passed:13, protected_path_violation:1 |
| hermes-ox-alpha | 24 | 20 | 83% | [64%–93%] | 2:20 | 219.5k | — | unpriced/provider_models_api/unknown | agent_timeout:1, evaluation_failed:2, passed:20, protected_path_violation:1 |

## Paired outcomes (matched benchmark × trial cells)

- **hermes-ox-alpha vs hermes-gpt-oss-20b** over 24 matched cells: both pass 10 · hermes-ox-alpha only 10 · hermes-gpt-oss-20b only 3 · both fail 1
  → marginal check: hermes-ox-alpha 20 passes (10+10) · hermes-gpt-oss-20b 13 passes (10+3) · McNemar exact p=0.0923

## Per-benchmark results

| benchmark | hermes-gpt-oss-20b | hermes-ox-alpha |
|---|---|---|
| tokenbucket | 3/3 passed · 2:00 · 329.7k tok | 2/3 passed · 2:04 · 219.5k tok |
| leasekit | 2/3 passed · 3:12 · 372.7k tok | 0/3 passed · 3:53 · 297.7k tok |
| vercomp | 1/3 passed · 3:04 · 368.8k tok | 3/3 passed · 1:34 · 130.2k tok |
| statelock | 0/3 passed · 2:51 · 378.9k tok | 3/3 passed · 3:16 · 274.9k tok |
| pluginreg | 3/3 passed · 1:31 · 168.8k tok | 3/3 passed · 2:22 · 251.9k tok |
| cacheflow | 2/3 passed · 3:44 · 790.6k tok | 3/3 passed · 2:55 · 219.7k tok |
| txnrollback | 2/3 passed · 4:25 · 268.5k tok | 3/3 passed · 1:51 · 173.9k tok |
| apimigrate | 0/3 passed · 3:36 · 428.2k tok | 3/3 passed · 2:14 · 180.6k tok |

## Corpus difficulty / saturation

| benchmark | runs | classification | evidence |
|---|---|---|---|
| apimigrate | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| cacheflow | 6 | **discriminating** (83%) | >= 25% across 6 graded runs |
| leasekit | 6 | **discriminating** (33%) | >= 25% across 6 graded runs |
| pluginreg | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 graded runs) |
| statelock | 6 | **discriminating** (50%) | >= 25% across 6 graded runs |
| tokenbucket | 6 | **discriminating** (83%) | >= 25% across 6 graded runs |
| txnrollback | 6 | **discriminating** (83%) | >= 25% across 6 graded runs |
| vercomp | 6 | **discriminating** (67%) | >= 25% across 6 graded runs |

## Reliability (observed repeated trials)

| config | tasks | runs | passes | pass@1 [Wilson 95%] | any-in-k | all-k | mean p* |
|---|---|---|---|---|---|---|---|
| hermes-gpt-oss-20b | 8 | 24 | 13 | 54% [35%/72%] | 75% (k=3) | 25% | 0.538 |
| hermes-ox-alpha | 8 | 24 | 20 | 83% [64%/93%] | 88% (k=3) | 75% | 0.808 |

\* mean p is a Beta(1,1) smoothed posterior mean — descriptive, not a
classic pass@k estimator: these are observed repeated trials.

## Known limitations

- Results reflect this corpus and these configurations only; they do not generalize to other tasks or workloads.
- Each cell was run 3 time(s); small samples carry wide uncertainty (Wilson intervals are shown, not hidden).
- Cost/token figures come from each harness's own reporting; where measurement sources differ materially, cross-agent cost comparisons are indicative only.
- Comparison mode: model-controlled.

## Reproduce

```text
# re-run the identical matrix (same benchmarks/configs/commits)
agentbench show <run-id>            # inspect one cell's evidence
agentbench reproduce <run-id>       # preflight + rerun one cell
```
