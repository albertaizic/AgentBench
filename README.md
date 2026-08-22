# AgentBench
A reproducible evaluation framework for coding agents.

## Status: minimal vertical slice

One benchmark → one agent run → one verdict, end to end:

1. Parse and strictly validate a benchmark YAML file.
2. Clone the configured repository into a temporary workspace.
3. Check out the exact configured commit (detached HEAD).
4. Run a coding agent (currently Claude Code) behind a small adapter interface.
5. Capture stdout/stderr, exit code, and duration of the agent process.
6. Capture the full Git diff (including untracked files) with line statistics.
7. Run each evaluation command; its exit code decides PASS/FAIL.
8. Write `result.json` plus sidecar logs under `results/` and print a summary.

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Windows
# .venv/bin/pip install -e ".[dev]"        # POSIX
```

Requires Python ≥ 3.12 and a working `git` on PATH.

## Quickstart

```bash
agentbench run examples/benchmark.example.yaml
agentbench run my-benchmark.yaml --results-dir out --timeout-seconds 300 --keep-workspace
```

Exit codes: `0` all evaluations passed · `1` at least one failed · `2` setup error
(bad YAML, clone failure, unknown agent type).

## Benchmark format

See [`examples/benchmark.example.yaml`](examples/benchmark.example.yaml). Required:
`name`, `repository`, `commit`, `prompt`, `agent.type`, and at least one
`evaluations` entry (`name` + shell `command`). Unknown fields are rejected.

## Results layout

```
results/<benchmark-name>/<UTC timestamp>-<id>/
├── result.json          # structured summary (statuses, durations, diff stats)
├── diff.patch           # complete patch of the agent's changes
├── agent.stdout.log     # raw agent streams, preserved for debugging
├── agent.stderr.log
└── evals/<name>.{stdout,stderr}.log
```

## Architecture

| Module | Responsibility |
| --- | --- |
| `models.py` | Pydantic schemas; strict validation, no silent typos |
| `loader.py` | YAML file → validated `BenchmarkSpec` |
| `workspace.py` | Clone + exact checkout + reliable cleanup (read-only-safe on Windows) |
| `adapters/` | `AgentAdapter` interface; `claude-code` implementation; registry |
| `process.py` | Subprocess runs with output capture, timeouts, tree-safe kills |
| `diffs.py` | Patch capture incl. untracked files + numstat statistics |
| `evaluation.py` | Evaluation commands; PASS/FAIL from exit codes only |
| `results.py` | JSON serialization + sidecar artifacts |
| `runner.py` | Orchestration of the pipeline above |
| `cli.py` | Typer CLI + Rich summary |

The runner depends only on the `AgentAdapter` interface — supporting another
agent means adding one class and registering it.

## Limitations

- No sandboxing: the agent runs as an ordinary local subprocess with your
  environment (API keys included) and your filesystem permissions. The trust
  boundary is the benchmark author: whoever writes the YAML already controls
  arbitrary shell commands via `evaluations`.
- The captured patch is hardened against tampering (`--no-textconv`,
  `--no-ext-diff`, cwd removed from child executable search on Windows), but
  an unsandboxed agent can still edit its own workspace in arbitrary ways —
  treat results as evidence, not as proof.
- Changes inside gitignored paths are excluded from the diff by Git's own
  semantics; the "full" patch covers tracked + untracked, non-ignored files.
- Single trial per run; no repeated-trial aggregation or cost tracking.
- Only Claude Code is implemented; the interface is proven but the live
  binary path should be exercised before relying on it.
- On timeout the direct child tree is killed (`taskkill /T` / `killpg`).
  POSIX descendants that escape their process group survive; the post-kill
  output reap is time-bounded so such an escapee cannot hang the run, but
  it may keep running until its own exit.
