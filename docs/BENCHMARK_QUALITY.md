# Benchmark Quality

This document defines how AgentBench decides whether a benchmark may be
trusted for cross-agent comparison, and how that trust is earned and kept.

## Outcome vs validity

Every run carries two orthogonal fields:

* **outcome** (`overall.status`): did the task pass? `passed`,
  `evaluation_failed`, `agent_failed`, `agent_timeout`, ...
* **validity** (`overall.validity`): may this evidence be graded as a
  capability measurement? `valid`, `infra_invalid`, `integrity_warning`,
  `invalid`.

A provider outage produces e.g. `outcome=failed / validity=infra_invalid`.
Such cells stay visible in every listing but are excluded from capability
denominators. Detection is deterministic: provider-error markers in captured
output combined with zero model tokens (the agent never reached a model).

## Oracle vs nop

* **oracle**: apply the reference solution to a fresh fixture; every public
  and hidden evaluator must pass. Proves solvability.
* **nop**: leave the fixture untouched; required evaluators must fail.
  Proves the evaluators actually measure something.

Both are repeated (`benchmark audit NAME --oracle-runs 5 --nop-runs 5`).
Release-grade requires 5/5 oracle passes and 5/5 nop failures unless the
manifest documents a deterministic reason otherwise. Flaky baselines or
oracles are the single most common evaluation defect in published coding
benchmarks; repetition is how AgentBench catches them.

## Partial scoring

Benchmarks may declare `scoring_groups` with weights and a `required` flag,
plus named `scorers`. Non-binary scorers print a final line
`agentbench-score: <0..1>`.

* `partial_score` = Σ weight_g × fraction_g / Σ weights — descriptive only.
* Binary resolution still requires EVERY required scorer to exit 0.
* A broken solution can therefore score 0.9 partial and still FAIL.

Legacy benchmarks without scoring groups keep pure binary semantics: one
implicit `default` group, all evaluators required.

## Requirements mapping

`prompt_requirements[]` lists what the prompt asks for;
`requirement_mappings[]` ties each requirement to the groups/scorers that
measure it. The audit flags unscored requirements and unmapped groups.
This makes prompt/test alignment *reviewable*, not provable — human review
is still required before release-grade promotion.

## Contamination metadata

`source_kind` (authored / synthetic / historical-public / fresh-public /
cleanroom), `task_created_at`, `task_public_since`, `solution_public_since`
and `contamination_risk` record provenance. Dates can flag *potential
exposure*; they never prove contamination. Authored AgentBench tasks carry
their first-publication date in `task_public_since` once released.

## Canary strings

New tasks may embed a `canary.string` in the prompt or reference material
(never in evaluator expectations — the audit fails such placements). If the
task text later surfaces in model training data, completions reproducing the
canary reveal ingestion.

## Human-time metadata

Optional `human_time`: `{expert_time_estimate_minutes, estimate_method,
estimate_samples?, notes}` with methods `author_estimate`,
`independent_expert_estimate`, `measured_human(s)`. Estimates feed the
descriptive time-horizon analysis; they are clearly distinguished from
measured timings everywhere estimates are used.

## Trajectory privacy

`trajectory.jsonl` normalizes externally observable activity only: tool
calls, commands, file operations, usage. Model reasoning/thinking content is
dropped at parse time by every adapter extractor. Raw harness logs remain
local evidence; they are never shipped in study bundles.

## Quality statuses

| status | meaning |
|---|---|
| `unreviewed` | default; no audit run yet |
| `provisional` | audited, no hard failures; real-agent calibration pending |
| `release-grade` | audit green incl. 5/5 oracle + 5/5 nop stability |
| `needs-review` | metadata failures or instability detected |
| `invalid` | correctness-critical failure (unsolvable, leaking, flaky) |

Only release-grade and provisional tasks enter headline studies; anything
else is listed separately with its audit findings.

## Promotion gate: provisional → release-grade (v0.6 policy)

A task is promoted to `release-grade` when ALL of the following hold:

1. manifest valid and corpus-discoverable;
2. oracle 5/5 and nop-fail 5/5 across repeated fresh workspaces;
3. deterministic fixture regeneration (`create_fixture.py` reproduces the
   pinned commit);
4. requirements mapping complete — every declared prompt requirement maps to
   a scoring group/evaluator;
5. hidden/reference isolation reviewed (no reference or evaluator path
   reachable from the agent workspace);
6. protected-path review done;
7. no unresolved audit FAIL, and no blocking WARN.

**Blocking warnings**: `reference_isolation`, `oracle_stability`,
`nop_stability`, `requirement_mapping`.
**Non-blocking warnings**: `human_time_metadata` (estimates are optional),
`partial_score_support` (binary-only grading is a capability note, not a
defect).

Promotion is never granted to improve headline counts; the audit evidence
(JSONL) is committed alongside release notes.
