"""Tests for the agent adapter interface and the Claude Code adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbench.adapters import UnknownAgentError, get_adapter
from agentbench.adapters.base import AgentInvocation
from agentbench.adapters.claude_code import ClaudeCodeAdapter
from agentbench.models import AgentSpec


class TestRegistry:
    def test_resolves_claude_code_adapter(self):
        adapter = get_adapter("claude-code")

        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_unknown_agent_type_raises(self):
        with pytest.raises(UnknownAgentError, match="gpt-cli"):
            get_adapter("gpt-cli")


class TestClaudeCodeAdapter:
    def build(self, **agent_overrides) -> AgentInvocation:
        adapter = ClaudeCodeAdapter()
        return adapter.build_invocation(
            workspace=Path("some/workspace"),
            prompt="Fix the failing test.",
            agent_spec=AgentSpec(type="claude-code", **agent_overrides),
        )

    def test_builds_headless_claude_invocation(self):
        invocation = self.build()

        assert invocation.argv[0] == "claude"
        assert "--print" in invocation.argv  # headless print mode
        assert "--dangerously-skip-permissions" in invocation.argv  # unattended edits

    def test_prompt_is_delivered_via_stdin_not_argv(self):
        # Long prompts blow past Windows command-line length limits, and
        # prompt text in argv risks argument injection if it starts with '-'.
        invocation = self.build()

        assert invocation.input_text == "Fix the failing test."
        assert all("Fix the failing test" not in arg for arg in invocation.argv)

    def test_command_override_replaces_binary(self):
        invocation = self.build(command="/opt/wrapper/claude")

        assert invocation.argv[0] == "/opt/wrapper/claude"
        assert "--print" in invocation.argv

    def test_extra_args_are_appended(self):
        invocation = self.build(extra_args=["--model", "claude-sonnet-5"])

        assert invocation.argv[-2:] == ["--model", "claude-sonnet-5"]

    def test_invocation_carries_workspace_independent_command(self):
        # The adapter decides *what* to run; the runner decides *where*.
        # argv must therefore not embed the workspace path.
        invocation = self.build()

        assert all("some/workspace" not in arg for arg in invocation.argv)


class TestClaudeCodeIsolation:
    """Benchmark runs must be reproducible: nothing from the user's own
    tooling (MCP servers, global hooks/skills) may execute inside the
    workspace and pollute the recorded diff."""

    def build(self, **agent_overrides) -> AgentInvocation:
        return ClaudeCodeAdapter().build_invocation(
            workspace=Path("some/workspace"),
            prompt="Fix the failing test.",
            agent_spec=AgentSpec(type="claude-code", **agent_overrides),
        )

    def test_disables_all_inherited_mcp_servers(self):
        invocation = self.build()

        argv = invocation.argv
        strict_at = argv.index("--strict-mcp-config")
        config_at = argv.index("--mcp-config")
        assert strict_at == config_at - 1  # strict guard precedes the config
        assert json.loads(argv[config_at + 1]) == {"mcpServers": {}}

    def test_excludes_user_and_local_setting_sources(self):
        invocation = self.build()

        sources_at = invocation.argv.index("--setting-sources")
        sources = invocation.argv[sources_at + 1].split(",")

        assert "user" not in sources
        assert "local" not in sources
        assert "project" in sources  # repo-local config stays under test

    def test_isolation_flags_come_before_extra_args(self):
        # extra_args are an escape hatch; they must be able to override but
        # never displace the isolation guarantees' presence.
        invocation = self.build(extra_args=["--model", "sonnet"])

        assert invocation.argv.index("--strict-mcp-config") < invocation.argv.index("--model")
