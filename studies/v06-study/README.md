# AgentBench v0.6 study of record

**Model-controlled comparison**: one harness (Hermes 0.20.1), two models,
eight benchmarks spanning discriminating core tasks and the new long-horizon
tier. 8 × 2 × 3 = **48 completed runs**, host backend, `--jobs 2`.

- Experiment: `20260825T191521Z-4727103d` · mode `model-controlled`
- Secondary system-comparison subset: `20260825T203921Z-05a0f1e4` —
  Claude Code (Sonnet) vs OMP (ox-alpha), 6 × 2 × 3 = 36 runs
  ([system-comparison-report.md](system-comparison-report.md))
- Calibration gate for new tasks: `20260825T210516Z-a2d5dc7c` — hermes/ox-alpha
  passed **11/11** single-trial runs across every new v0.6 benchmark.

## Configurations

| config | identity |
|---|---|
| hermes-ox-alpha | Hermes CLI 0.20.1 / stealth/ox-alpha / openrouter / reasoning medium |
| hermes-gpt-oss-20b | Hermes CLI 0.20.1 / openai/gpt-oss-20b / openrouter / reasoning medium |

## Headline result (48/48 validly graded, 0 infra-invalid)

| model | passed | rate [Wilson 95%] | any-in-3 | all-3 | mean tokens/run |
|---|---|---|---|---|---|
| stealth/ox-alpha | 20/24 | 83% [64–93%] | see report.md | | ~205k |
| openai/gpt-oss-20b | 13/24 | 54% [35–72%] | see report.md | | ~370k |

Paired over 24 matched cells and full per-benchmark breakdowns, reliability
rows, partial scores and trajectory-derived behavior are in
[report.md](report.md) (machine-generated; verify with
`agentbench study verify studies/v06-study --results-dir results` after a
local rescan).

Key mechanical findings:

* The models separate most clearly on the **long-horizon tier**: gpt-oss-20b
  went 0/3 on statelock (vs ox-alpha 3/3) and 2/3 on txnrollback, while
  ox-alpha held 3/3 across the long-horizon set.
* Two protected-path violations (agents editing `tests/**`): tokenbucket by
  ox-alpha and cacheflow by gpt-oss-20b. Flagged by the taxonomy and the
  integrity scanner, counted as failures — never as passes — in every
  denominator, and visible in the report's failure rows.
* leasekit remains hard-for-Sonnet evidence: Claude Code scored 0/3 there in
  the same-day system subset (third consecutive experiment), while both
  Hermes models and OMP solve it regularly.

## Contents & integrity

Machine-generated bundle: `report.md`, `report.html`,
`system-comparison-report.{md,html}`, `experiment.json`, `identities.json`,
`metrics.csv`, `hashes.json` (SHA-256 of every shipped file). Secret-scanned;
no raw trajectories, prompts-with-secrets, credentials or local paths.

## Limitations

Single harness for the headline comparison; two models only; three trials
per cell (wide intervals); long-horizon tasks are provisional (first
calibration only); human-time estimates absent so no time-horizon fit is
reported. Results describe this corpus only.
