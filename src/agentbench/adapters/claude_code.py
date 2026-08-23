"""Claude Code adapter: headless, unattended runs of the ``claude`` CLI.

Claude-specific behavior is confined to this module: the isolation flags and
the parsing of Claude's structured JSON output. Generic result models never
mention Claude.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from agentbench.adapters.base import AgentAdapter, AgentInvocation, AgentOutput, AgentUsage
from agentbench.models import AgentSpec
from agentbench.process import resolve_executable, run_command

DEFAULT_COMMAND = "claude"
# No MCP servers at all: user-scoped servers (code indexers, project tools)
# would otherwise start inside the workspace and write files into the diff.
EMPTY_MCP_CONFIG = '{"mcpServers": {}}'


class ClaudeCodeAdapter(AgentAdapter):
    """Run Claude Code in print mode with permissions pre-approved.

    Runs are isolated from the invoking user's tooling so results are
    reproducible:

    * ``--strict-mcp-config`` + an empty MCP config ignore every inherited
      MCP server (user, project, and local scopes).
    * ``--setting-sources project`` drops user-global and local settings
      (hooks, skills, keybindings) while keeping the benchmark repo's own
      committed config, which is part of the fixture under test.
    * ``--output-format json`` yields one structured result object with real
      usage/cost evidence; it is preserved verbatim as raw output and parsed
      for metrics here.

    Built-in coding tools are unaffected. The prompt is delivered on stdin:
    prompts can be arbitrarily long (Windows command lines are not) and
    cannot be mistaken for argv flags.
    """

    name = "claude-code"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        argv = [
            agent_spec.command or DEFAULT_COMMAND,
            "--print",  # headless: run the task, print the result, exit
            "--dangerously-skip-permissions",  # unattended: no permission prompts
            "--strict-mcp-config",
            "--mcp-config",
            EMPTY_MCP_CONFIG,
            "--setting-sources",
            "project",
            "--output-format",
            "json",
        ]
        if agent_spec.model:
            argv += ["--model", agent_spec.model]
        argv += agent_spec.extra_args
        return AgentInvocation(argv=argv, input_text=prompt)

    def capabilities(self) -> set[str]:
        return {
            self.CAP_STRUCTURED_USAGE,
            self.CAP_MODEL_REPORTING,
            self.CAP_COST_REPORTING,
            self.CAP_SESSION_ID,
            self.CAP_NATIVE_JSON_OUTPUT,
        }

    def parse_output(self, stdout: str) -> AgentOutput | None:
        """Parse the single-result JSON envelope emitted by ``--print --output-format json``.

        Returns None for anything that is not the expected structure — stubs,
        plain text, truncated output — so optional metrics simply stay
        unavailable instead of failing the run.
        """
        try:
            payload = json.loads(stdout.strip())
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or "usage" not in payload:
            return None

        usage_payload = payload.get("usage") or {}
        input_tokens = _int_or_none(usage_payload.get("input_tokens"))
        output_tokens = _int_or_none(usage_payload.get("output_tokens"))
        cache_read = _int_or_none(usage_payload.get("cache_read_input_tokens")) or 0
        cache_creation = _int_or_none(usage_payload.get("cache_creation_input_tokens")) or 0
        total = None
        if input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens + cache_read + cache_creation

        model = None
        model_usage = payload.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            model = ", ".join(sorted(str(key) for key in model_usage))

        return AgentOutput(
            usage=AgentUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
                cost_usd=_float_or_none(payload.get("total_cost_usd")),
                # The CLI does not expose a reliable tool-call count in this
                # envelope; num_turns is session metadata, reported as such.
                tool_calls=None,
                num_turns=_int_or_none(payload.get("num_turns")),
                session_id=_str_or_none(payload.get("session_id")),
            ),
            model=model,
            is_error=bool(payload["is_error"]) if isinstance(payload.get("is_error"), bool) else None,
        )

    def cli_version(self) -> str | None:
        command = DEFAULT_COMMAND
        return _cached_cli_version(command)


@lru_cache(maxsize=8)
def _cached_cli_version(command: str) -> str | None:
    result = run_command(
        [resolve_executable(command), "--version"],
        cwd=Path.cwd(),
        timeout=30.0,
    )
    line = (result.stdout or "").strip().splitlines()
    return line[0].strip() if line else None


def _int_or_none(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float_or_none(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _str_or_none(value) -> str | None:
    return str(value) if isinstance(value, str) else None
