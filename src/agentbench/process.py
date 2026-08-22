"""Subprocess execution with full output capture, timeouts, and tree-safe termination.

The coding agent is an untrusted external process: everything it emits is
captured, its runtime is bounded, and on timeout the *entire* descendant
tree is killed — a surviving grandchild holding locks would break workspace
cleanup, especially on Windows.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# How long to wait for output after killing a timed-out process. A descendant
# that escaped its process group can hold the output pipe open indefinitely;
# without this bound it would hang the whole run.
_REAP_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


def resolve_executable(name: str) -> str:
    """Pin *name* to an absolute path so children cannot shadow it via cwd."""
    return shutil.which(name) or name


def _child_env() -> dict[str, str] | None:
    if os.name != "nt":
        return None
    env = dict(os.environ)
    # Windows' CreateProcess searches the current directory before PATH for
    # bare executable names; an untrusted agent could plant e.g. git.exe in
    # the workspace and hijack every harness git call. This opt-out removes
    # cwd from that search.
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def _as_text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _kill_tree(process: subprocess.Popen) -> None:
    """Terminate *process* together with all of its descendants."""
    if os.name == "nt":
        # taskkill /T walks the child tree; Popen.kill() would not.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _execute(
    args: list[str] | str,
    *,
    shell: bool,
    cwd: Path,
    timeout: float | None,
    input_text: str | None,
) -> ProcessResult:
    popen_kwargs: dict = {
        "cwd": cwd,
        # PIPE even without input: unattended agents must never inherit our
        # terminal stdin, and communicate() requires it to deliver input.
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",  # agent output may contain arbitrary bytes
        "shell": shell,
        "env": _child_env(),
    }
    if os.name != "nt":
        # Own process group so the whole tree can be signalled at once.
        popen_kwargs["start_new_session"] = True

    started = time.monotonic()
    process = subprocess.Popen(args, **popen_kwargs)

    timed_out = False
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired as reap:
            # An escaped descendant still holds the pipe open: take what we
            # have instead of hanging the run forever.
            stdout, stderr = _as_text(reap.stdout), _as_text(reap.stderr)
            process.kill()
            try:
                process.communicate(timeout=_REAP_GRACE_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass

    return ProcessResult(
        exit_code=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
    )


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float | None = None,
    input_text: str | None = None,
) -> ProcessResult:
    """Run *argv* directly (no shell), capturing everything."""
    return _execute(argv, shell=False, cwd=cwd, timeout=timeout, input_text=input_text)


def run_shell_command(
    command: str,
    *,
    cwd: Path,
    timeout: float | None = None,
) -> ProcessResult:
    """Run *command* through the platform shell (used for evaluation commands)."""
    return _execute(command, shell=True, cwd=cwd, timeout=timeout, input_text=None)
