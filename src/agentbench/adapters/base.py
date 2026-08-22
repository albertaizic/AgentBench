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


class AgentAdapter(ABC):
    """Knows how to turn an :class:`AgentSpec` into an executable invocation."""

    name: ClassVar[str]

    @abstractmethod
    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        """Build the invocation for a run inside *workspace*."""
        raise NotImplementedError
