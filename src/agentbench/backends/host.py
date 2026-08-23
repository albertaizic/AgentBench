"""Host execution backend: plain local subprocesses (the v0.1/v0.2 behavior)."""

from __future__ import annotations

from pathlib import Path

from agentbench.adapters.base import AgentInvocation
from agentbench.backends.base import ExecutionBackend
from agentbench.models import ExecutionSpec
from agentbench.process import ProcessResult, run_command, run_shell_command


class HostExecutionBackend(ExecutionBackend):
    name = "host"

    def run_agent(
        self,
        invocation: AgentInvocation,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        return run_command(
            invocation.argv,
            cwd=workspace,
            timeout=timeout,
            input_text=invocation.input_text,
        )

    def run_public_evaluation(
        self,
        command: str,
        *,
        workspace: Path,
        timeout: float,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        return run_shell_command(command, cwd=workspace, timeout=timeout, env=env)

    def provenance(self) -> dict:
        return {"backend": "host", "network": self.config.network}
