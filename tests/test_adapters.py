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
        assert "--output-format" in invocation.argv  # structured result for metrics
        assert invocation.argv[invocation.argv.index("--output-format") + 1] == "json"

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


class TestClaudeOutputParsing:
    """Parsing of the real ``--output-format json`` envelope (v2.1.239 shape)."""

    ENVELOPE = json.dumps(
        {
            "is_error": False,
            "duration_ms": 77712,
            "num_turns": 3,
            "session_id": "071f1e80-1134-4097-9956-852c53f83b92",
            "total_cost_usd": 0.149622,
            "usage": {
                "input_tokens": 28330,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 64000,
                "output_tokens": 38,
            },
            "modelUsage": {
                "claude-sonnet-5": {"inputTokens": 29098, "costUSD": 0.149622},
            },
            "result": "did the work",
            "subtype": "success",
        }
    )

    def parse(self, stdout: str):
        return ClaudeCodeAdapter().parse_output(stdout)

    def test_extracts_real_usage_metrics(self):
        parsed = self.parse(self.ENVELOPE)

        assert parsed.usage.input_tokens == 28330
        assert parsed.usage.output_tokens == 38
        # Total counts cache traffic too — it is real token consumption.
        assert parsed.usage.total_tokens == 28330 + 38 + 64000
        assert parsed.usage.cost_usd == 0.149622
        assert parsed.usage.num_turns == 3
        assert parsed.usage.session_id == "071f1e80-1134-4097-9956-852c53f83b92"

    def test_model_from_model_usage_block(self):
        assert self.parse(self.ENVELOPE).model == "claude-sonnet-5"

    def test_stub_or_plain_text_output_yields_none(self):
        assert self.parse("agent done\n") is None
        assert self.parse("") is None
        assert self.parse('{"unrelated": true}') is None

    def test_truncated_json_yields_none_not_error(self):
        assert self.parse('{"usage": {"input_tokens"') is None

    def test_missing_optional_fields_stay_none(self):
        minimal = json.dumps({"usage": {}})
        usage = self.parse(minimal).usage

        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.total_tokens is None
        assert usage.cost_usd is None
