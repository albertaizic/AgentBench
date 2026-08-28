"""Agent adapter registry: the only agent-specific seam in the runner."""

from __future__ import annotations

from agentbench.adapters.base import AgentAdapter, AgentInvocation
from agentbench.adapters.claude_code import ClaudeCodeAdapter
from agentbench.adapters.command import GenericCommandAdapter
from agentbench.adapters.hermes import HermesAdapter
from agentbench.adapters.omp import OmpAdapter


class UnknownAgentError(KeyError):
    """Raised when a benchmark requests an adapter type that does not exist."""


_REGISTRY: dict[str, type[AgentAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    GenericCommandAdapter.name: GenericCommandAdapter,
    HermesAdapter.name: HermesAdapter,
    OmpAdapter.name: OmpAdapter,
}


def get_adapter(type_name: str) -> AgentAdapter:
    """Resolve an adapter by its ``agent.type`` string."""
    try:
        adapter_cls = _REGISTRY[type_name]
    except KeyError:
        supported = ", ".join(sorted(_REGISTRY))
        raise UnknownAgentError(f"Unknown agent type {type_name!r} (supported: {supported})") from None
    return adapter_cls()


__all__ = [
    "AgentAdapter",
    "AgentInvocation",
    "ClaudeCodeAdapter",
    "GenericCommandAdapter",
    "HermesAdapter",
    "OmpAdapter",
    "UnknownAgentError",
    "get_adapter",
]
