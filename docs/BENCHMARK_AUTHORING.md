# Benchmark Authoring Guide

The full lifecycle from idea to release-grade. Every step has a
machine-checkable gate except the two marked *(human)*.

## 1. Initialize

```bash
agentbench benchmark init my-task --language python
```

Creates `benchmarks/my-task/` with a manifest skeleton containing every v0.6
metadata field, plus `hidden/`, `reference/`, and a `create_fixture.py`
stub.

## 2. Define the fixture

Implement `create_fixture.py` (copy the pattern from
`benchmarks/csvroll/create_fixture.py`): it must reproduce the broken
fixture repository deterministically — fixed dates, fixed author, seeded
data — and print the resulting head SHA. Paste that SHA into the manifest's
`commit:` field. The `fixture/` directory itself is generated, never
hand-edited.

Aim for 4–15 relevant files across multiple modules when the task is meant
to be long-horizon; single-file tasks are fine for easy bugfix tiers.

## 3–4. Prompt and requirements

Write the prompt as a realistic maintainer request: observable behavior,
no internal implementation steps, no answer leakage. Then list every distinct
observable requirement in `prompt_requirements[]`.

*(human)* Check the prompt cannot be satisfied by special-casing the public
tests' literal inputs.

## 5–7. Scorers and reference

* Public evaluator(s): fast smoke checks in `evaluations`.
* Hidden evaluator(s) under `hidden/`: the real contract, using DIFFERENT
  data than public tests. They run outside the workspace; agents never see
  them.
* Declare `scoring_groups` with weights; mark only what is genuinely
  mandatory as `required`. Optional groups earn partial credit.
* Implement `reference/fix.patch` (except patch-free test-writing tasks).

## 8–10. Baseline + stability gates

```bash
agentbench benchmark validate my-task          # baseline broken / reference passes
agentbench benchmark audit my-task --oracle-runs 5 --nop-runs 5
```

Gate: reference passes everything; untouched baseline fails required
evaluations; repeated runs are stable (5/5 both ways). See
[docs/BENCHMARK_QUALITY.md](BENCHMARK_QUALITY.md).

## 11–12. Leakage and metadata

The audit checks hidden/reference isolation, protected paths, requirement
mapping completeness, provenance fields. Add an expert-time estimate if you
can justify it (`human_time`); leave it null otherwise.

## 13. Real-agent calibration *(human + agent)*

Run one trial each on at least two harnesses:

```bash
agentbench experiment experiments/calibrate-my-task.yaml
```

Interpretable evidence = failures look like genuine agent limitations (see
trajectory: `agentbench trajectory <run-id>`), not task ambiguity or
evaluator overconstraint.

## 14–15. Audit and promotion

```bash
agentbench benchmark audit my-task            # full quality audit
agentbench benchmark audit --all --report     # corpus quality table
```

Promote `quality_status` to `release-grade` only after: oracle/nop stable at
5×, calibration interpretable, alignment reviewed *(human)*. Everything else
stays `provisional` and is excluded from headline studies.
