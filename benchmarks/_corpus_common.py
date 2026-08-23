"""Shared helper for deterministic corpus fixture generators.

Every corpus generator builds its fixture repository from embedded file
contents with pinned authorship/dates so the commit sha is identical on any
machine — the same contract as benchmarks/stockflow/create_fixture.py.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

AUTHOR_NAME = "AgentBench Fixture"
AUTHOR_EMAIL = "fixture@agentbench.invalid"
DATE = "2026-02-01T09:00:00+00:00"


def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": DATE,
        "GIT_COMMITTER_DATE": DATE,
    }


def clear_contents(directory: Path) -> None:
    """Empty a directory without deleting it (top dir may be CWD-pinned)."""
    import shutil

    def _on_readonly(func, target, _exc):
        os.chmod(target, 0o200)
        func(target)

    if not directory.exists():
        directory.mkdir(parents=True)
        return
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, onexc=_on_readonly)
        else:
            entry.unlink()


def create_fixture_repo(fixture_dir: Path, files: dict[str, str], message: str) -> str:
    """(Re)create the fixture repo deterministically; return the HEAD sha."""
    import shutil
    import time

    clear_contents(fixture_dir)
    run_git = _make_git_runner(fixture_dir)
    run_git("init", "-q", "-b", "main")
    for relative, content in sorted(files.items()):
        target = fixture_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_git("add", "-A")
    run_git("commit", "-q", "-m", message)
    return run_git("rev-parse", "HEAD")


def _make_git_runner(cwd: Path):
    def run_git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=_git_env(),
            check=True,
        )
        return result.stdout.strip()

    return run_git


def pin_commit(benchmark_yaml: Path, sha: str) -> None:
    """Idempotently set the 'commit:' line to *sha* (initial pin or re-pin)."""
    import re

    text = benchmark_yaml.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^commit: [0-9a-fA-FPENDING]+$", f"commit: {sha}", text)
    if count:
        benchmark_yaml.write_text(updated, encoding="utf-8")
        print(f"pinned {sha} into {benchmark_yaml.name}")
    else:
        print(f"no 'commit:' line found in {benchmark_yaml.name}", file=sys.stderr)


def main_for(fixture_dir: Path, files: dict[str, str], message: str, yaml_path: Path) -> int:
    sha = create_fixture_repo(fixture_dir, files, message)
    print(f"fixture repository created at {fixture_dir}")
    print(f"commit: {sha}")
    pin_commit(yaml_path, sha)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
