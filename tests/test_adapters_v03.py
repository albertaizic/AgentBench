"""Tests for the generic command adapter, capabilities, and experiment stats."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentbench.adapters import GenericCommandAdapter, get_adapter
from agentbench.adapters.base import AgentInvocation
from agentbench.aggregate import (
    failure_counts,
    pairwise_compare,
    quantile,
    wilson_interval,
)
from agentbench.models import AgentSpec, ExperimentSpec
from agentbench.process import run_command


class TestGenericCommandAdapter:
    def test_registry_resolves_command_adapter(self):
        from agentbench.adapters.claude_code import ClaudeCodeAdapter

        assert isinstance(get_adapter("command"), GenericCommandAdapter)
        assert isinstance(get_adapter("claude-code"), ClaudeCodeAdapter)

    def test_prompt_delivered_via_stdin_by_default(self):
        spec = AgentSpec(type="command", argv=["my-agent", "--flag"])

        invocation = GenericCommandAdapter().build_invocation(
            workspace=Path("."), prompt="the task", agent_spec=spec
        )

        assert invocation.argv == ["my-agent", "--flag"]  # prompt NOT in argv
        assert invocation.input_text == "the task"

    def test_prompt_placeholder_mode(self):
        spec = AgentSpec(
            type="command",
            argv=["my-agent", "--task", "{prompt}", "--quiet"],
            prompt_mode="arg",
        )

        invocation = GenericCommandAdapter().build_invocation(
            workspace=Path("."), prompt="do the thing", agent_spec=spec
        )

        assert invocation.argv == ["my-agent", "--task", "do the thing", "--quiet"]
        assert invocation.input_text is None

    def test_argv_is_never_a_shell_string(self):
        spec = AgentSpec(type="command", argv=["single"])
        invocation = GenericCommandAdapter().build_invocation(
            workspace=Path("."), prompt="p", agent_spec=spec
        )
        assert isinstance(invocation.argv, list)

    def test_arg_mode_requires_placeholder(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentSpec(type="command", argv=["my-agent"], prompt_mode="arg")

    def test_end_to_end_stdin_roundtrip(self, tmp_path):
        spec = AgentSpec(type="command", argv=[sys.executable, "-c",
                                              "import sys; print(sys.stdin.read())"])
        invocation = GenericCommandAdapter().build_invocation(
            workspace=tmp_path, prompt="hello adapter", agent_spec=spec
        )

        result = run_command(invocation.argv, cwd=tmp_path,
                             input_text=invocation.input_text)

        assert result.stdout.strip() == "hello adapter"


class TestCapabilities:
    def test_claude_reports_rich_capabilities(self):
        caps = get_adapter("claude-code").capabilities()

        assert "structured_usage" in caps
        assert "cost_reporting" in caps
        assert "native_json_output" in caps

    def test_generic_adapter_reports_none(self):
        assert get_adapter("command").capabilities() == set()


class TestClaudeModelSelection:
    def test_model_flag_inserted_before_extra_args(self):
        adapter = get_adapter("claude-code")
        invocation = adapter.build_invocation(
            workspace=Path("."),
            prompt="p",
            agent_spec=AgentSpec(type="claude-code", model="sonnet",
                                 extra_args=["--verbose"]),
        )

        model_at = invocation.argv.index("--model")
        assert invocation.argv[model_at + 1] == "sonnet"
        assert invocation.argv.index("--verbose") > model_at


class TestExperimentSpec:
    def test_cell_count_matrix(self):
        spec = ExperimentSpec(
            name="matrix",
            benchmarks=["a", "b", "c"],
            configs=[
                {"name": "one", "agent": {"type": "command", "argv": ["x"]}},
                {"name": "two", "agent": {"type": "command", "argv": ["y"]}},
            ],
            repeat=5,
        )

        assert spec.cell_count == 30  # 3 benchmarks x 2 configs x 5 trials

    @pytest.mark.parametrize("overrides", [
        {"configs": [{"name": "dup", "agent": {"type": "command", "argv": ["x"]}},
                     {"name": "dup", "agent": {"type": "command", "argv": ["y"]}}]},
        {"benchmarks": ["a", "a"]},
        {"repeat": 0},
        {"repeat": 101},
    ])
    def test_invalid_matrices_rejected(self, overrides):
        from pydantic import ValidationError

        base = {
            "name": "m",
            "benchmarks": ["a"],
            "configs": [{"name": "c1", "agent": {"type": "command", "argv": ["x"]}}],
            "repeat": 1,
        }
        base.update(overrides)

        with pytest.raises(ValidationError):
            ExperimentSpec.model_validate(base)


class TestStatistics:
    def test_wilson_known_values(self):
        # Wilson at 95% for 8/10 ≈ [0.490, 0.943]; verify against the
        # closed-form computation rather than a library.
        low, high = wilson_interval(8, 10)

        assert low < 8 / 10 < high
        assert 0.45 < low < 0.55
        assert 0.90 < high < 0.99

    def test_wilson_edge_cases(self):
        assert wilson_interval(0, 10)[1] < 0.35
        assert wilson_interval(10, 10)[0] > 0.7
        assert wilson_interval(0, 0) is None

    def test_quantile_linear_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]

        assert quantile(values, 0.25) == 1.75
        assert quantile(values, 0.5) == 2.5
        assert quantile([], 0.5) is None

    def test_failure_counts(self):
        counts = failure_counts([
            {"status": "passed"},
            {"status": "passed"},
            {"status": "agent_timeout"},
        ])

        assert counts == {"passed": 2, "agent_timeout": 1}

    def test_pairwise_matching_ignores_unmatched_cells(self):
        a = [
            {"benchmark": "b1", "trial": 1, "status": "passed"},
            {"benchmark": "b1", "trial": 2, "status": "evaluation_failed"},
            {"benchmark": "b1", "trial": 3, "status": "passed"},  # unmatched
        ]
        b = [
            {"benchmark": "b1", "trial": 1, "status": "passed"},
            {"benchmark": "b1", "trial": 2, "status": "passed"},
        ]

        counts = pairwise_compare(a, b)

        # Trial 1: both pass. Trial 2: a failed while b passed → b_only.
        # (pairwise_compare also returns a/b_passes_matched diagnostics.)
        assert {k: counts[k] for k in ("both_pass", "a_only", "b_only",
                                       "both_fail", "matched")} == {
            "both_pass": 1, "a_only": 0, "b_only": 1,
            "both_fail": 0, "matched": 2}

    def test_pairwise_no_overlap_returns_none(self):
        a = [{"benchmark": "b1", "trial": 1, "status": "passed"}]
        b = [{"benchmark": "b2", "trial": 1, "status": "passed"}]

        assert pairwise_compare(a, b) is None
