"""Generic command adapter: benchmark any non-interactive coding-agent CLI.

The agent command MUST be an argv list (never a shell string). The prompt is
delivered on stdin by default, or substituted into a ``{prompt}`` placeholder
in one argv element when ``prompt_mode="arg"``. ``{python}`` resolves to the
AgentBench interpreter so experiment matrices stay machine-portable.

No usage parsing is attempted: metrics stay null unless a future dedicated
adapter understands a specific tool's output format.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentbench.adapters.base import AgentAdapter, AgentInvocation
from agentbench.models import AgentSpec

PROMPT_PLACEHOLDER = "{prompt}"
PYTHON_PLACEHOLDER = "{python}"


class GenericCommandAdapter(AgentAdapter):
    name = "command"

    def build_invocation(self, *, workspace: Path, prompt: str, agent_spec: AgentSpec) -> AgentInvocation:
        argv = [
            arg.replace(PYTHON_PLACEHOLDER, sys.executable)
            .replace(PROMPT_PLACEHOLDER, prompt)
            for arg in (agent_spec.argv or [])
        ]
        if agent_spec.prompt_mode == "arg":
            return AgentInvocation(argv=argv, input_text=None)
        return AgentInvocation(argv=argv, input_text=prompt)
