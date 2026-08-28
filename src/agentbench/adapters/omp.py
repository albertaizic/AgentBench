"""OMP adapter: headless one-shot runs of the ``omp`` CLI (Oh My Pi).

OMP is a genuine coding-agent harness: it reads repository files, edits them,
and executes shell commands through its own tool loop. Benchmarking it is
agent benchmarking, never chat-completion scoring.

Isolation and reproducibility:

* ``--mode json -p`` streams one JSON object per line to stdout — native
  structured evidence for metrics AND normalized trajectories;
* ``--no-session`` keeps session storage out of the picture entirely (the
  stream itself is the record); nothing lands inside the workspace clone;
* ``--no-extensions --no-skills`` drop user customization sources so every
  benchmark starts from the same state;
* ``--cwd <workspace>`` pins the agent's working directory;
* the prompt travels as the final positional argument (list-form argv, no
  shell).

Auth note: credentials come from the ambient OMP profile (there is no safe
per-run credential injection today); extension/skill isolation covers the
behavioral surface, not the credential store.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from agentbench.adapters.base import AgentAdapter, AgentInvocation, AgentOutput, AgentUsage
from agentbench.models import AgentSpec
from agentbench.process import resolve_executable, run_command

DEFAULT_COMMAND = "omp"


class OmpAdapter(AgentAdapter):
    """Run ``omp --mode json -p`` against an isolated workspace."""

    name = "omp"

    def build_invocation(
        self, *, workspace: Path, prompt: str, agent_spec: AgentSpec
    ) -> AgentInvocation:
        argv = [
            agent_spec.command or DEFAULT_COMMAND,
            "--mode", "json",
            "-p",                      # non-interactive: run task, print, exit
            "--no-session",            # the stdout stream is the record
            "--no-extensions",
            "--no-skills",
            "--cwd", str(workspace),
        ]
        if agent_spec.model:
            argv += ["--model", agent_spec.model]
        if agent_spec.provider:
            argv += ["--provider", agent_spec.provider]
        if agent_spec.reasoning:
            # OMP thinking levels: off/minimal/low/medium/high/xhigh/max/auto.
            argv += ["--thinking", str(agent_spec.reasoning)]
        # Caller-supplied args stay flags; the prompt remains the last element.
        argv += agent_spec.extra_args
        argv.append(prompt)
        return AgentInvocation(argv=argv, input_text=None)

    def capabilities(self) -> set[str]:
        return {
            self.CAP_STRUCTURED_USAGE,
            self.CAP_MODEL_REPORTING,
            self.CAP_COST_REPORTING,
            self.CAP_SESSION_ID,
            self.CAP_NATIVE_JSON_OUTPUT,
        }

    def parse_output(self, stdout: str) -> AgentOutput | None:
        """Read metrics from the last usage-bearing frame of the JSON stream.

        Any structural surprise returns ``None`` so optional metrics stay
        unavailable instead of failing a completed run.
        """
        session_id: str | None = None
        last_usage_frame: dict | None = None
        turns = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "session" and rec.get("id"):
                session_id = str(rec["id"])
            elif kind == "message_end":
                message = rec.get("message") or {}
                if message.get("role") == "assistant":
                    turns += 1
                    usage = message.get("usage")
                    if isinstance(usage, dict) and usage.get("totalTokens"):
                        last_usage_frame = message
        if last_usage_frame is None:
            return None
        usage = last_usage_frame.get("usage") or {}
        cost = usage.get("cost") or {}
        cost_total = cost.get("total") if isinstance(cost, dict) else None
        return AgentOutput(
            usage=AgentUsage(
                input_tokens=_int(usage.get("input")),
                output_tokens=_int(usage.get("output")),
                total_tokens=_int(usage.get("totalTokens")),
                cost_usd=_float(cost_total),
                tool_calls=None,  # OMP does not report a per-run tool count
                num_turns=float(turns) if turns else None,
                session_id=session_id,
                cost_provenance="reported" if _float(cost_total) else None,
            ),
            model=last_usage_frame.get("model") or None,
            is_error=None,  # outcome is decided by evaluations, not the answer
        )

    def cli_version(self) -> str | None:
        return _cached_cli_version(DEFAULT_COMMAND)


def _int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@lru_cache(maxsize=8)
def _cached_cli_version(command: str) -> str | None:
    result = run_command(
        [resolve_executable(command), "--version"],
        cwd=None,
        timeout=30.0,
    )
    first = (result.stdout or "").strip().splitlines()
    return first[0].strip() if first else None
