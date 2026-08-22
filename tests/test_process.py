"""Tests for subprocess execution (agentbench.process).

The agent is an untrusted external process: we must capture everything it
emits, enforce timeouts, and not leave orphaned descendants behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentbench.process import run_command, run_shell_command


def pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe used only by tests."""
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


SPAWNING_CHILD = (
    "import subprocess, sys, time\n"
    "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(gc.pid, flush=True)\n"
    "time.sleep(30)\n"
)


class TestRunCommand:
    def test_captures_exit_code_stdout_and_stderr(self, tmp_path: Path):
        result = run_command(
            [sys.executable, "-c", "print('to stdout'); raise SystemExit(3)"],
            cwd=tmp_path,
        )

        assert result.exit_code == 3
        assert "to stdout" in result.stdout
        assert result.timed_out is False
        assert result.duration_seconds > 0

    def test_stderr_is_preserved(self, tmp_path: Path):
        result = run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom')"],
            cwd=tmp_path,
        )

        assert result.exit_code == 0
        assert "boom" in result.stderr

    def test_stdin_input_is_delivered_to_process(self, tmp_path: Path):
        result = run_command(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper(), end='')"],
            cwd=tmp_path,
            input_text="ping",
        )

        assert result.stdout == "PING"

    def test_success_exit_code_zero(self, tmp_path: Path):
        result = run_command([sys.executable, "-c", "pass"], cwd=tmp_path)

        assert result.exit_code == 0


class TestTimeouts:
    def test_timeout_marks_timed_out_and_returns_promptly(self, tmp_path: Path):
        start = time.monotonic()

        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=2.0,
        )

        elapsed = time.monotonic() - start
        assert result.timed_out is True
        assert elapsed < 15
        assert result.exit_code is not None  # process was killed, not left hanging

    def test_timeout_kills_entire_process_tree(self, tmp_path: Path):
        # The direct child spawns its own child; killing only the direct
        # child would leave the grandchild holding locks on workspace files.
        result = run_command(
            [sys.executable, "-c", SPAWNING_CHILD],
            cwd=tmp_path,
            timeout=2.0,
        )

        assert result.timed_out is True
        grandchild_pid = int(result.stdout.split()[0])
        # Poll instead of probing once: reaping (and PID teardown on Windows)
        # is asynchronous, so a single-shot check races the OS.
        deadline = time.monotonic() + 15.0
        while pid_alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not pid_alive(grandchild_pid)

    @pytest.mark.skipif(os.name == "nt", reason="process-group escape via setsid is POSIX-only")
    def test_returns_within_grace_when_escaped_descendant_holds_pipe(self, tmp_path: Path):
        # A grandchild that starts its own session survives killpg and keeps
        # the inherited stdout pipe open; the post-kill output reap must be
        # bounded or the whole run hangs forever.
        escapee = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "start_new_session=True)\n"
            "time.sleep(30)\n"
        )

        start = time.monotonic()
        result = run_command([sys.executable, "-c", escapee], cwd=tmp_path, timeout=1.0)

        elapsed = time.monotonic() - start
        assert result.timed_out is True
        assert elapsed < 20


class TestChildEnvironment:
    def test_windows_children_do_not_resolve_bare_names_via_cwd(self, tmp_path: Path):
        # An untrusted agent can plant executables in the workspace; Windows'
        # CreateProcess searches the cwd before PATH unless this env opt-out
        # is set, which would let a planted 'git.exe' hijack harness calls.
        probe = "import os; print(os.environ.get('NoDefaultCurrentDirectoryInExePath', '<unset>'))"

        result = run_command([sys.executable, "-c", probe], cwd=tmp_path)

        expected = "1" if os.name == "nt" else "<unset>"
        assert result.stdout.strip() == expected


class TestRunShellCommand:
    def test_runs_command_through_the_platform_shell(self, tmp_path: Path):
        result = run_shell_command(f'"{sys.executable}" -c "print(41+1)"', cwd=tmp_path)

        assert result.exit_code == 0
        assert result.stdout.strip() == "42"

    def test_failing_command_preserves_output(self, tmp_path: Path):
        result = run_shell_command(
            f'"{sys.executable}" -c "import sys; print(\'bad\'); sys.exit(1)"',
            cwd=tmp_path,
        )

        assert result.exit_code == 1
        assert "bad" in result.stdout

    def test_shell_timeout_marks_timed_out(self, tmp_path: Path):
        result = run_shell_command(
            f'"{sys.executable}" -c "import time; time.sleep(30)"',
            cwd=tmp_path,
            timeout=2.0,
        )

        assert result.timed_out is True
