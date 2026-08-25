"""Hermes adapter: invocation shape, isolation, usage parsing, registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbench import workspace as workspace_mod  # noqa: F401  (import sanity)
from agentbench.adapters import get_adapter
from agentbench.adapters.hermes import HermesAdapter
from agentbench.models import AgentSpec


def spec(**over) -> AgentSpec:
    payload = {"type": "hermes"}
    payload.update(over)
    return AgentSpec.model_validate(payload)


class TestInvocation:
    def test_minimal_invocation_is_isolated_and_oneshot(self, tmp_path):
        adapter = HermesAdapter()
        inv = adapter.build_invocation(
            workspace=tmp_path, prompt="fix the bug", agent_spec=spec(),
        )
        argv = inv.argv
        assert argv[0] == "hermes"
        # Reproducible isolation: none of the user's config, memory, skills,
        # plugins, or MCP servers may leak into a benchmark run.
        assert "--safe-mode" in argv
        # Unattended: dangerous-command approvals must never block a run.
        assert "--yolo" in argv
        assert argv[argv.index("--in") + 1] == str(tmp_path)
        # One-shot prompt mode; prompt is the final argv element.
        assert "-z" in argv
        assert argv[-1] == "fix the bug"
        # Usage evidence lands OUTSIDE the workspace: capture_diff stages
        # untracked files, so anything inside would pollute the agent patch.
        usage_arg = Path(argv[argv.index("--usage-file") + 1])
        assert tmp_path not in usage_arg.parents
        # Prompt travels on BOTH channels (argv -z + piped stdin): hermes
        # prefers a non-TTY stdin over its argument, so both must agree.
        assert inv.input_text == "fix the bug"

    def test_model_provider_reasoning_reach_argv(self, tmp_path):
        s = spec(model="openai/gpt-5-mini", provider="openrouter", reasoning="low")
        argv = HermesAdapter().build_invocation(
            workspace=tmp_path, prompt="p", agent_spec=s,
        ).argv
        assert argv[argv.index("-m") + 1] == "openai/gpt-5-mini"
        assert argv[argv.index("--provider") + 1] == "openrouter"
        assert argv[argv.index("--reasoning") + 1] == "low"

    def test_command_override_and_extra_args(self, tmp_path):
        s = spec(command="hermes-dev", extra_args=["--accept-hooks"])
        argv = HermesAdapter().build_invocation(
            workspace=tmp_path, prompt="p", agent_spec=s,
        ).argv
        assert argv[0] == "hermes-dev"
        assert "--accept-hooks" in argv
        # Prompt stays the last element so injected args remain flags.
        assert argv[-2:] == ["-z", "p"]


class TestRegistration:
    def test_get_adapter_resolves_hermes(self):
        assert isinstance(get_adapter("hermes"), HermesAdapter)


class TestCapabilitiesAndVersion:
    def test_capabilities_declared(self):
        caps = HermesAdapter().capabilities()
        from agentbench.adapters.base import AgentAdapter

        assert AgentAdapter.CAP_STRUCTURED_USAGE in caps
        assert AgentAdapter.CAP_COST_REPORTING in caps
        assert AgentAdapter.CAP_MODEL_REPORTING in caps
        assert AgentAdapter.CAP_SESSION_ID in caps  # usage report carries session ids

    def test_cli_version_discovery(self, monkeypatch):
        from agentbench.process import ProcessResult

        def fake_run(*argv, **kwargs):
            return ProcessResult(
                exit_code=0,
                stdout="Hermes Agent v9.9.9 (2026.1.1)\nmore\n",
                stderr="",
                duration_seconds=0.0,
            )

        from agentbench.adapters import hermes as hermes_mod

        hermes_mod._cached_cli_version.cache_clear()
        monkeypatch.setattr(hermes_mod, "run_command", fake_run)
        assert hermes_mod.HermesAdapter().cli_version() == "Hermes Agent v9.9.9 (2026.1.1)"
        hermes_mod._cached_cli_version.cache_clear()


class TestUsageParsing:
    SAMPLE_USAGE = {
        "api_calls": 6,
        "cache_read_tokens": 1200,
        "cache_write_tokens": 300,
        "completed": True,
        "cost_source": "estimated",
        "cost_status": "ok",
        "estimated_cost_usd": 0.042,
        "failed": False,
        "input_tokens": 810,
        "model": "stealth/ox-alpha",
        "output_tokens": 240,
        "provider": "openrouter",
        "reasoning_tokens": 90,
        "session_id": "sess-123",
        "total_tokens": 1140,
    }

    def _adapter_with_usage(self, tmp_path: Path, payload) -> HermesAdapter:
        adapter = HermesAdapter()
        adapter.build_invocation(workspace=tmp_path, prompt="p", agent_spec=spec())
        if payload is not None:
            adapter._usage_file.write_text(json.dumps(payload), encoding="utf-8")
        return adapter

    def test_real_schema_maps_all_metrics(self, tmp_path):
        adapter = self._adapter_with_usage(tmp_path, self.SAMPLE_USAGE)
        output = adapter.parse_output("final answer text")
        assert output is not None
        assert output.model == "stealth/ox-alpha"
        u = output.usage
        assert u.input_tokens == 810
        assert u.output_tokens == 240
        assert u.total_tokens == 1140
        assert u.cost_usd == 0.042
        assert u.num_turns == 6          # api_calls
        assert u.session_id == "sess-123"

    def test_zero_estimated_cost_means_unpriced_not_free(self, tmp_path):
        # OpenRouter stealth models have no pricing data: the usage report
        # carries estimated_cost_usd 0.0 + cost_status "estimated". That must
        # persist as unknown cost, never as a real $0 result.
        payload = dict(self.SAMPLE_USAGE)
        payload["cost_status"] = "estimated"
        payload["estimated_cost_usd"] = 0.0
        adapter = self._adapter_with_usage(tmp_path, payload)
        output = adapter.parse_output("reply")
        assert output is not None
        assert output.usage.cost_usd is None
        assert output.usage.cost_provenance == "unpriced/estimated/estimated"

    def test_zero_unknown_status_cost_also_means_unpriced(self, tmp_path):
        # Study runs observed cost_status "unknown" (not just "estimated")
        # with estimated_cost_usd 0.0; any exact-zero price is missing data.
        payload = dict(self.SAMPLE_USAGE)
        payload["cost_status"] = "unknown"
        payload["estimated_cost_usd"] = 0.0
        adapter = self._adapter_with_usage(tmp_path, payload)
        output = adapter.parse_output("reply")
        assert output is not None
        assert output.usage.cost_usd is None
        assert output.usage.cost_provenance == "unpriced/estimated/unknown"

    def test_missing_usage_file_yields_none(self, tmp_path):
        adapter = HermesAdapter()
        adapter.build_invocation(workspace=tmp_path, prompt="p", agent_spec=spec())
        adapter._usage_file.unlink(missing_ok=True)
        assert adapter.parse_output("plain text reply") is None

    def test_corrupt_usage_file_yields_none(self, tmp_path):
        adapter = HermesAdapter()
        adapter.build_invocation(workspace=tmp_path, prompt="p", agent_spec=spec())
        adapter._usage_file.write_text("not json{", encoding="utf-8")
        assert adapter.parse_output("reply") is None

    def test_never_started_yields_none(self):
        # parse_output without build_invocation must not explode.
        assert HermesAdapter().parse_output("reply") is None


class TestModelIdentity:
    def test_distinct_models_produce_distinct_config_identities(self):
        from agentbench.models import ConfigSpec, ExecutionSpec

        def make(model):
            return ConfigSpec(
                name="cfg",
                agent=AgentSpec.model_validate({"type": "hermes", "model": model}),
            )

        assert make("openai/gpt-5-mini").config_hash != make(
            "anthropic/claude-sonnet-4.6"
        ).config_hash

    def test_distinct_reasoning_produces_distinct_config_identity(self):
        from agentbench.models import ConfigSpec

        def make(reasoning):
            return ConfigSpec(
                name="cfg",
                agent=AgentSpec.model_validate(
                    {"type": "hermes", "reasoning": reasoning}
                ),
            )

        assert make("low").config_hash != make("high").config_hash
