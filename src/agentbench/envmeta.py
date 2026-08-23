"""Reproducibility metadata about the environment a run executed in.

Only facts useful for interpreting results are captured. Secrets, environment
variable dumps, and home-directory paths are never collected; anything that
cannot be determined reliably is ``None``.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from agentbench import __version__


@lru_cache(maxsize=1)
def git_version() -> str | None:
    """The git version string, resolved once per process."""
    from agentbench.process import run_command

    git = _git_executable()
    if git is None:
        return None
    result = run_command([git, "--version"], cwd=Path.cwd())
    line = (result.stdout or "").strip()
    return line or None


def _git_executable() -> str | None:
    import shutil

    return shutil.which("git")


def capture_environment(*, agent_cli_version: str | None) -> dict:
    return {
        "agentbench_version": __version__,
        "python_version": sys.version.split(" ", 1)[0],
        "platform": platform.platform(),
        "git_version": git_version(),
        "agent_cli_version": agent_cli_version,
    }
