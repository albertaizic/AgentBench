"""Environment checks for setup troubleshooting. Never prints secret values."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    state: str  # ok / warn / fail
    detail: str


def _first_line(argv: list[str]) -> str | None:
    try:
        result = subprocess_run(argv)
    except (OSError, RuntimeError):
        return None
    if result.exit_code != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else None


def subprocess_run(argv: list[str]):
    from agentbench.process import run_command

    return run_command(argv, cwd=Path.cwd(), timeout=30.0)


def run_checks(results_dir: Path) -> list[Check]:
    from agentbench import __version__
    from agentbench.adapters import get_adapter, UnknownAgentError
    from agentbench.backends.docker import docker_available, docker_version
    from agentbench.envmeta import git_version

    checks: list[Check] = []
    checks.append(Check("AgentBench version", OK, __version__))
    checks.append(
        Check("Python", OK, sys.version.split(" ", 1)[0])
    )

    git_v = git_version()
    checks.append(
        Check("Git", OK if git_v else FAIL, git_v or "git not found on PATH")
    )

    if shutil.which("docker"):
        version = docker_version()
        if docker_available():
            checks.append(Check("Docker", OK, version or "daemon reachable"))
            checks.append(
                Check("Docker daemon", OK, "reachable")
            )
        else:
            checks.append(Check("Docker", WARN, "CLI present but daemon unreachable"))
            checks.append(Check("Docker daemon", WARN, "unreachable — docker backend unavailable"))
    else:
        checks.append(Check("Docker", WARN, "not installed (host backend still works)"))
        checks.append(Check("Docker daemon", WARN, "n/a"))

    claude_path = shutil.which("claude")
    if claude_path:
        version = _first_line(["claude", "--version"])
        checks.append(Check("Claude Code", OK, version or "installed"))
    else:
        checks.append(Check("Claude Code", WARN, "claude CLI not found on PATH"))

    hermes_path = shutil.which("hermes")
    if hermes_path:
        version = _first_line(["hermes", "--version"])
        checks.append(
            Check("Hermes agent", OK, version or "installed (OpenRouter-backed coding agent)")
        )
    else:
        checks.append(
            Check("Hermes agent", WARN, "hermes CLI not found on PATH (host backend only)")
        )

    omp_path = shutil.which("omp")
    if omp_path:
        version = _first_line(["omp", "--version"])
        checks.append(
            Check("OMP agent", OK, version or "installed (JSON-streaming coding agent)")
        )
    else:
        checks.append(
            Check("OMP agent", WARN, "omp CLI not found on PATH (host backend only)")
        )

    for adapter_type in ("claude-code", "command", "hermes", "omp"):
        try:
            get_adapter(adapter_type)
            checks.append(Check(f"Adapter '{adapter_type}'", OK, "available"))
        except UnknownAgentError as exc:  # pragma: no cover - registry is static
            checks.append(Check(f"Adapter '{adapter_type}'", FAIL, str(exc)))

    results_dir.mkdir(parents=True, exist_ok=True)
    probe = results_dir / ".doctor-write-probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        checks.append(Check("Results path writable", OK, str(results_dir)))
    except OSError as exc:
        checks.append(Check("Results path writable", FAIL, str(exc)))
    finally:
        if probe.exists():
            probe.unlink()

    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE t (x)")
        connection.close()
        checks.append(Check("SQLite", OK, "in-memory database usable"))
    except sqlite3.Error as exc:  # pragma: no cover - stdlib sqlite always works
        checks.append(Check("SQLite", FAIL, str(exc)))

    cache_env = os.environ.get("AGENTBENCH_CACHE_DIR")
    checks.append(
        Check("Git source cache", OK, cache_env or "default location (set AGENTBENCH_CACHE_DIR to override)")
    )
    return checks


def worst_state(checks: list[Check]) -> str:
    if any(c.state == FAIL for c in checks):
        return FAIL
    if any(c.state == WARN for c in checks):
        return WARN
    return OK
