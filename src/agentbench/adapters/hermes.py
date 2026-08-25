"""Hermes adapter: headless one-shot runs of the ``hermes`` CLI.

Hermes is an OpenRouter-capable coding agent with a real tool loop — it
reads repository files, edits them, and executes commands until done — so
benchmarking it is agent benchmarking, not chat-completion scoring.
Hermes-specific behavior is confined to this module; generic result models
never mention Hermes.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

from agentbench.adapters.base import AgentAdapter, AgentInvocation, AgentOutput, AgentUsage
from agentbench.models import AgentSpec
from agentbench.process import resolve_executable, run_command

DEFAULT_COMMAND = "hermes"


class HermesAdapter(AgentAdapter):
    """Run Hermes in ``-z/--oneshot`` mode with customization sources disabled.

    Runs are isolated and reproducible:

    * ``--safe-mode`` drops user config, AGENTS.md/memory/rules injection,
      plugins, and MCP servers — every benchmark starts from the same state
      (credentials from the hermes environment are still loaded).
    * ``--yolo`` bypasses dangerous-command approval prompts: runs are
      unattended by contract.
    * ``--in <workspace>`` pins the agent's working directory to the isolated
      workspace clone for this cell.
    * The prompt travels as the final ``-z`` argument (list-form argv, no
      shell); hermes does not consume benchmark prompts via stdin.
    * ``--usage-file`` writes a JSON usage report (token counts, estimated
      cost, model) even when the run fails. It points OUTSIDE the workspace:
      capture_diff stages untracked files with ``git add -A``, so evidence
      inside the clone would pollute the agent patch. Parsed after the run,
      then removed best-effort.
    """

    name = "hermes"

    def __init__(self) -> None:
        self._usage_file: Path | None = None

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        self._usage_file = Path(tempfile.gettempdir()) / (
            f"agentbench-hermes-usage-{uuid.uuid4().hex}.json"
        )
        argv = [
            agent_spec.command or DEFAULT_COMMAND,
            "--safe-mode",  # reproducible state: no user config/memory/plugins/MCP
            "--yolo",  # unattended: never block on dangerous-command approvals
            "--in", str(workspace),
            "--usage-file", str(self._usage_file),
        ]
        if agent_spec.model:
            argv += ["-m", agent_spec.model]
        if agent_spec.provider:
            argv += ["--provider", agent_spec.provider]
        if agent_spec.reasoning:
            argv += ["--reasoning", agent_spec.reasoning]
        # Caller-supplied args stay flags; the prompt remains the last element.
        argv += agent_spec.extra_args
        argv += ["-z", prompt]
        # Hermes prefers a piped stdin over its -z argument whenever stdin is
        # not a TTY, so the prompt travels on BOTH channels: whichever the
        # execution context honors, the agent reads the same task text.
        return AgentInvocation(argv=argv, input_text=prompt)

    def capabilities(self) -> set[str]:
        return {
            self.CAP_STRUCTURED_USAGE,
            self.CAP_COST_REPORTING,
            self.CAP_MODEL_REPORTING,
            self.CAP_SESSION_ID,
        }

    def parse_output(self, stdout: str) -> AgentOutput | None:
        """Parse the ``--usage-file`` JSON written beside the run.

        One-shot stdout is the agent's final answer text — not structured —
        so all metrics come from the usage report. Any problem reading or
        interpreting it returns None: missing metrics must never fail a run.
        """
        usage_path = self._usage_file
        if usage_path is None:
            return None
        try:
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        finally:
            try:
                usage_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not isinstance(payload, dict):
            return None

        tokens = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
        input_tokens = _num(tokens, "input_tokens", "prompt_tokens")
        output_tokens = _num(tokens, "output_tokens", "completion_tokens")
        total = _num(tokens, "total_tokens")
        if total is None and input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens

        cost = _num(payload, "cost_usd", "estimated_cost_usd", "total_cost_usd")
        provenance = _cost_provenance(payload)
        # An exact $0 price means the model has no pricing data on the
        # provider side (stealth/unlisted models report 0.0 with status
        # "estimated" or "unknown"), not that inference was free — recording
        # $0 would fabricate a cross-agent cost result. A genuinely billed
        # run is never exactly zero.
        if cost == 0:
            cost = None
            provenance = f"unpriced/{provenance}" if provenance else "unpriced"

        return AgentOutput(
            usage=AgentUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
                cost_usd=cost,
                tool_calls=_num(payload, "tool_calls", "tool_call_count"),
                num_turns=_num(payload, "api_calls", "num_turns", "turns"),
                session_id=_str(payload, "session_id"),
                cost_provenance=provenance,
            ),
            model=_str(payload, "model", "resolved_model", "model_used"),
            is_error=None,
        )

    def cli_version(self) -> str | None:
        return _cached_cli_version(DEFAULT_COMMAND)


@lru_cache(maxsize=8)
def _cached_cli_version(command: str) -> str | None:
    result = run_command(
        [resolve_executable(command), "--version"],
        cwd=Path.cwd(),
        timeout=30.0,
    )
    line = (result.stdout or "").strip().splitlines()
    return line[0].strip() if line else None


def _num(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _cost_provenance(payload: dict) -> str | None:
    """Describe where the cost figure came from (P16: metric provenance)."""
    source = payload.get("cost_source")
    status = payload.get("cost_status")
    parts = [str(p) for p in (source, status) if isinstance(p, str) and p.strip()]
    return "/".join(parts) if parts else None


def _str(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
