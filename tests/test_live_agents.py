"""Live coding-agent canaries — marked ``live_agent``, never run in CI.

Each test makes a REAL API call through an installed agent CLI and proves the
full adapter path end-to-end: invocation flags, agentic file editing, metric
capture. Run them explicitly before benchmarking:

    pytest -m live_agent tests/test_live_agents.py

Deterministic unit tests for the same adapters live in test_adapters_hermes.py
and friends; nothing here may be required for CI to pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentbench.models import AgentSpec

pytestmark = pytest.mark.live_agent

CANARY_PROMPT = (
    "Create a file named hello.txt in the current directory containing exactly "
    "the word hi (no other text in the file). Then reply with just: DONE"
)


def _run(adapter, workspace: Path, spec: AgentSpec, *, timeout: float = 420.0):
    invocation = adapter.build_invocation(
        workspace=workspace, prompt=CANARY_PROMPT, agent_spec=spec
    )
    # stdin only for adapters that consume it (claude); a forced-empty pipe
    # makes hermes' oneshot read stdin instead of its -z argument.
    kwargs = (
        {"input": invocation.input_text}
        if invocation.input_text is not None
        else {"stdin": subprocess.DEVNULL}
    )
    import time

    last = None
    for attempt in range(2):  # providers throttle bursts; one retry absorbs 429s
        last = subprocess.run(
            invocation.argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **kwargs,
        )
        combined = f"{last.stdout}\n{last.stderr}"
        if "429" not in combined and "rate" not in combined.lower():
            return last
        time.sleep(20)
        # fresh workspace so a partial first attempt cannot confuse the second
        for stale in workspace.rglob("hello.txt"):
            stale.unlink()
    pytest.skip(f"provider rate-limited the canary after a retry: {combined[:300]}")
    return last


def _assert_hello(workspace: Path, stdout: str) -> None:
    hello = workspace / "hello.txt"
    assert hello.exists(), f"agent did not create hello.txt; stdout tail:\n{stdout[-2000:]}"
    assert "hi" in hello.read_text(encoding="utf-8")


class TestHermesLive:
    def test_oneshot_edits_files_and_reports_usage(self, tmp_path):
        pytest.importorskip("shutil")
        import shutil

        if shutil.which("hermes") is None:
            pytest.skip("hermes CLI not installed")

        from agentbench.adapters.hermes import HermesAdapter

        adapter = HermesAdapter()
        run = _run(adapter, tmp_path, AgentSpec(type="hermes"))
        assert run.returncode == 0, f"stderr:\n{run.stderr[-2000:]}"
        _assert_hello(tmp_path, run.stdout)

        output = adapter.parse_output(run.stdout)
        assert output is not None, "usage report missing/unparseable after successful run"
        assert output.model, "usage report must record the model actually used"
        usage = output.usage
        assert any(v is not None for v in (
            usage.total_tokens, usage.input_tokens, usage.output_tokens,
        )), "token counts expected from --usage-file"


class TestClaudeCodeLive:
    def test_print_mode_edits_files_and_reports_usage(self, tmp_path):
        import shutil

        if shutil.which("claude") is None:
            pytest.skip("claude CLI not installed")

        from agentbench.adapters.claude_code import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        run = _run(adapter, tmp_path, AgentSpec(type="claude-code"))
        assert run.returncode == 0, f"stderr:\n{run.stderr[-2000:]}"
        _assert_hello(tmp_path, run.stdout)

        output = adapter.parse_output(run.stdout)
        assert output is not None, "--output-format json envelope not parsed"
        usage = output.usage
        assert usage.session_id, "claude envelope carries a session id"
