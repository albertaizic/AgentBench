"""Claude Code adapter: headless, unattended runs of the ``claude`` CLI."""

from __future__ import annotations

from pathlib import Path

from agentbench.adapters.base import AgentAdapter, AgentInvocation
from agentbench.models import AgentSpec

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
            *agent_spec.extra_args,
        ]
        return AgentInvocation(argv=argv, input_text=prompt)
