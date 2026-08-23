"""Execution backends: where the agent and public evaluations actually run.

Separation of concerns:

* an **adapter** describes how to invoke a coding agent (argv, prompt
  delivery, output parsing);
* an **execution backend** describes where that invocation executes (host
  subprocess or Docker container) and owns everything environment-specific:
  placeholder values, credential forwarding, resource limits, provenance.

The runner depends only on this interface. Workspace management, Git diff
capture, hidden evaluations, and result persistence always stay host-side —
hidden evaluator source is never mounted into any container.
"""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from agentbench.adapters.base import AgentInvocation
from agentbench.models import ExecutionSpec
from agentbench.process import ProcessResult

CONTAINER_WORKSPACE = "/workspace"


def credential_env(allowlist: list[str]) -> tuple[dict[str, str], list[dict]]:
    """Resolve allowlisted variable names against the host environment.

    Returns ``(env, evidence)``: *env* holds actual values for forwarding;
    *evidence* records only presence, never values.
    """
    env = {}
    evidence = []
    for name in allowlist:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
        evidence.append({"name": name, "present": value is not None})
    return env, evidence


class ExecutionBackend(ABC):
    """Executes agent invocations and public evaluations for one run."""

    name: ClassVar[str]

    def __init__(self, config: ExecutionSpec) -> None:
        self.config = config

    @abstractmethod
    def run_agent(
        self,
        invocation: AgentInvocation,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str],
    ) -> ProcessResult:
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def run_public_evaluation(
        self,
        command: str,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str],
    ) -> ProcessResult:
        raise NotImplementedError  # pragma: no cover

    def placeholders(self, *, workspace: Path, hidden_dir: Path | None = None) -> dict[str, str]:
        """Substitution values for evaluation commands under this backend."""
        return {
            "python": sys.executable,
            "workspace": str(workspace),
            "hidden_dir": str(hidden_dir) if hidden_dir else "",
        }

    @abstractmethod
    def provenance(self) -> dict:
        """Evidence about where/how this run executed."""
        raise NotImplementedError  # pragma: no cover

    def cleanup(self) -> None:
        """Release any resources owned by this backend instance."""
