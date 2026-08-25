# AgentBench study — v05-study

- Experiment: `20260825T010230Z-4caf630c`
- Generated: 2026-08-25T09:32:14+00:00 by AgentBench 0.5.0
- Created: 2026-08-25T01:02:30.155912+00:00
- Backend: host · repeats/cell: 3 · runs recorded: 60 of 60 planned cells

## Configurations

| config | identity | config hash |
|---|---|---|
| claude-code | claude-code / sonnet | `400f98ad94a8` |
| hermes-openrouter | hermes / stealth/ox-alpha / provider=openrouter / reasoning=medium | `c8396f624342` |

## Per-config aggregate

| config | runs | passes | pass rate | Wilson 95% | median time | median tokens | median cost | cost evidence | failures |
|---|---|---|---|---|---|---|---|---|---|
| claude-code | 30 | 27 | 90% | [74%–97%] | 32s | 171.4k | $0.1073 | reported, unpriced/reported | agent_failed:3, passed:27 |
| hermes-openrouter | 30 | 28 | 93% | [79%–98%] | 1:41 | 148.7k | — | unpriced/provider_models_api/unknown | evaluation_failed:2, passed:28 |

## Paired outcomes (matched benchmark × trial cells)

- **claude-code vs hermes-openrouter** over 30 matched cells: both pass 26 · A only 1 · B only 2 · both fail 1 · McNemar exact p=1

## Per-benchmark results

| benchmark | claude-code | hermes-openrouter |
|---|---|---|
| jobqueue | 3/3 passed · 26s · 163.3k tok | 3/3 passed · 1:34 · 129k tok |
| ledgerpad | 3/3 passed · 28s · 165.8k tok | 3/3 passed · 1:36 · 145.1k tok |
| iniforge | 3/3 passed · 30s · 237.7k tok | 3/3 passed · 2:15 · 217.8k tok |
| csvroll | 3/3 passed · 34s · 234.1k tok | 3/3 passed · 1:40 · 148.6k tok |
| prefsfile | 3/3 passed · 38s · 167.6k tok | 3/3 passed · 1:41 · 133.8k tok |
| tokenbucket | 3/3 passed · 30s · 165.9k tok | 2/3 passed · 1:38 · 139k tok |
| vercomp | 3/3 passed · 46s · 234.1k tok | 3/3 passed · 2:06 · 170.7k tok |
| leasekit | 0/3 passed † · 8s · 0 tok | 2/3 passed · 1:41 · 155.5k tok |
| statediag | 3/3 passed · 40s · 247k tok | 3/3 passed · 2:15 · 182.5k tok |
| bankday | 3/3 passed · 33s · 171k tok | 3/3 passed · 2:04 · 157.2k tok |

\† At least one cell in this column failed without producing any
tokens — the agent never reached a model (e.g. a provider-side abort
or local CLI crash). Such cells measure infrastructure, not capability.

## Corpus difficulty / saturation

| benchmark | runs | classification | evidence |
|---|---|---|---|
| bankday | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| csvroll | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| iniforge | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| jobqueue | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| leasekit | 6 | **discriminating** (33%) | config pass-rate spread 0%-67% >= 25% across 6 runs |
| ledgerpad | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| prefsfile | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| statediag | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |
| tokenbucket | 6 | **discriminating** (83%) | config pass-rate spread 67%-100% >= 25% across 6 runs |
| vercomp | 6 | **likely_saturated** (100%) | all 2 trusted config(s) pass at >= 90% (6 runs) |

## Known limitations

- Results reflect this corpus and these configurations only; they do not generalize to other tasks or workloads.
- Each cell was run 3 time(s); small samples carry wide uncertainty (Wilson intervals are shown, not hidden).
- Cost/token figures come from each harness's own reporting; where measurement sources differ materially, cross-agent cost comparisons are indicative only.

## Reproduce

```text
# re-run the identical matrix (same benchmarks/configs/commits)
agentbench show <run-id>            # inspect one cell's evidence
agentbench reproduce <run-id>       # preflight + rerun one cell
```
