"""Cost/token accounting provenance tests (v0.6 hardening, mission XI).

The three adapters have different measurement sources; these tests pin the
exact semantics so no surface can silently treat "unknown" as "$0" or merge
materially different measurements.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentbench.adapters.claude_code import ClaudeCodeAdapter
from agentbench.adapters.hermes import HermesAdapter
from agentbench.adapters.omp import OmpAdapter
from agentbench.models import AgentSpec
from agentbench.storage import _normalized_cost


def test_claude_total_tokens_include_cache_components():
    payload = {
        "is_error": False, "num_turns": 3,
        "usage": {"input_tokens": 100, "output_tokens": 50,
                  "cache_read_input_tokens": 9000,
                  "cache_creation_input_tokens": 250},
        "total_cost_usd": 0.1234,
        "modelUsage": {"claude-sonnet-4-6": {"costUSD": 0.1234}},
    }
    out = ClaudeCodeAdapter().parse_output(json.dumps(payload))
    assert out is not None
    assert out.usage.total_tokens == 100 + 50 + 9000 + 250
    assert out.usage.cost_usd == 0.1234
    assert out.usage.cost_provenance == "reported"


def test_claude_multi_model_session_picks_main_model_deterministically():
    payload = {
        "is_error": False,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "modelUsage": {
            "claude-haiku": {"costUSD": 0.001},
            "claude-sonnet-4-6": {"costUSD": 0.5},   # the workhorse
        },
    }
    out = ClaudeCodeAdapter().parse_output(json.dumps(payload))
    assert out.model == "claude-sonnet-4-6"


def test_hermes_zero_cost_is_unpriced_never_free(tmp_path):
    # Drive parse_output through its real contract: it reads the temp usage
    # file path chosen at build time.
    invocation = HermesAdapter()
    inv = invocation.build_invocation(workspace=tmp_path, prompt="p",
                                      agent_spec=AgentSpec(type="hermes"))
    argv = inv.argv
    path = Path(argv[argv.index("--usage-file") + 1])
    path.write_text(json.dumps({
        "usage": {"input_tokens": 10, "output_tokens": 20,
                  "total_tokens": 30},
        "cost_usd": 0.0,
        "cost_status": "estimated", "cost_source": "openrouter",
    }), encoding="utf-8")
    out = invocation.parse_output("final answer text")
    assert out is not None
    assert out.usage.cost_usd is None          # never a fabricated $0
    assert out.usage.cost_provenance == "unpriced/openrouter/estimated"


def test_hermes_reported_nonzero_cost_keeps_provenance(tmp_path):
    invocation = HermesAdapter()
    inv = invocation.build_invocation(workspace=tmp_path, prompt="p",
                                      agent_spec=AgentSpec(type="hermes"))
    argv = inv.argv
    path = Path(argv[argv.index("--usage-file") + 1])
    path.write_text(json.dumps({
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "cost_usd": 0.05, "cost_status": "reported",
        "cost_source": "openrouter", "model": "stealth/ox-alpha",
    }), encoding="utf-8")
    out = invocation.parse_output("")
    assert out.usage.cost_usd == 0.05
    assert out.usage.cost_provenance == "openrouter/reported"
    assert out.model == "stealth/ox-alpha"


def test_omp_last_usage_frame_wins_and_zero_cost_stays_unpriced_in_index():
    frames = [
        {"type": "session", "id": "sess-1"},
        {"type": "message_end", "message": {
            "role": "assistant", "model": "model-a",
            "usage": {"input": 10, "output": 5, "totalTokens": 15}}},
        {"type": "message_end", "message": {
            "role": "assistant", "model": "model-b",
            "usage": {"input": 20, "output": 10, "totalTokens": 30,
                      "cost": {"total": 0.0}}}},
    ]
    stream = "\n".join(json.dumps(f) for f in frames) + "\n"
    out = OmpAdapter().parse_output(stream)
    assert out.model == "model-b"              # last frame's model
    assert out.usage.total_tokens == 30
    assert out.usage.cost_usd == 0.0           # preserved raw...
    cost, prov = _normalized_cost({"cost_usd": out.usage.cost_usd,
                                   "cost_provenance": out.usage.cost_provenance})
    assert cost is None and prov == "unpriced"  # ...but unpriced downstream


def test_omp_no_usage_frames_returns_none_not_zeroes():
    frames = [{"type": "session", "id": "s"},
              {"type": "message_end", "message": {"role": "assistant"}}]
    stream = "\n".join(json.dumps(f) for f in frames) + "\n"
    assert OmpAdapter().parse_output(stream) is None


def test_storage_normalization_treats_exact_zero_as_missing():
    cost, prov = _normalized_cost({"cost_usd": 0.0, "cost_provenance": "reported"})
    assert cost is None and prov == "unpriced/reported"
    cost, prov = _normalized_cost({"cost_usd": None})
    assert cost is None
