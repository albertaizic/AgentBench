"""Adapter interface: the runner never depends on agent-specific logic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agentbench.models import AgentSpec


@dataclass(frozen=True)
class AgentInvocation:
    """A complete description of how to run the agent inside a workspace.

    ``argv`` is workspace-independent (the runner supplies ``cwd``); bulky or
    injection-prone content such as the prompt travels via ``input_text``.
    """

    argv: list[str]
    input_text: str | None = None


# Generic, agent-independent usage metrics. Every field is optional: an
# adapter reports only what its CLI actually exposes, and missing values stay
# None rather than being estimated.
@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    # Where the cost figure came from (e.g. "reported", "estimated/ok") so
    # cross-harness cost comparisons can be judged fairly. Null = unknown.
    cost_provenance: str | None = None
    tool_calls: int | None = None
    num_turns: int | None = None
    session_id: str | None = None


class AgentOutput:
    """Parsed view of an agent's raw output, plus generic usage metrics."""

    def __init__(self, usage: AgentUsage | None = None, model: str | None = None,
                 is_error: bool | None = None) -> None:
        self.usage = usage
        self.model = model
        self.is_error = is_error


class AgentAdapter(ABC):
    """Knows how to turn an :class:`AgentSpec` into an executable invocation."""

    name: ClassVar[str]

    # Capability names an adapter may report. Metrics outside these stay null.
    CAP_STRUCTURED_USAGE = "structured_usage"
    CAP_MODEL_REPORTING = "model_reporting"
    CAP_COST_REPORTING = "cost_reporting"
    CAP_SESSION_ID = "session_id"
    CAP_TOOL_CALL_COUNT = "tool_call_count"
    CAP_NATIVE_JSON_OUTPUT = "native_json_output"
    # P39: an adapter declares these ONLY if the harness can interrupt
    # itself mid-run when a budget is exceeded. Requested budgets are always
    # recorded; enforcement is never assumed.
    CAP_TOKEN_BUDGET_ENFORCEMENT = "token_budget_enforcement"
    CAP_COST_BUDGET_ENFORCEMENT = "cost_budget_enforcement"

    @abstractmethod
    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        """Build the invocation for a run inside *workspace*."""
        raise NotImplementedError

    def capabilities(self) -> set[str]:
        """What this adapter can reliably report. Small set, checked by name."""
        return set()

    def parse_output(self, stdout: str) -> AgentOutput | None:
        """Interpret raw agent stdout; return None when nothing is structured.

        Parsing problems must never turn a successful coding run into a
        failure — callers treat None as "metrics unavailable".
        """
        return None

    def cli_version(self) -> str | None:
        """Version string of the underlying agent CLI, when discoverable."""
        return None
