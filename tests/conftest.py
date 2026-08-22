"""Shared test helpers: real git repositories built from local paths.

All fixtures are offline — no network access is ever required.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

# Git refuses to commit without an identity; pin one for fixture repos only.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "agentbench-tests",
    "GIT_AUTHOR_EMAIL": "agentbench@example.com",
    "GIT_COMMITTER_NAME": "agentbench-tests",
    "GIT_COMMITTER_EMAIL": "agentbench@example.com",
}


def run_git(cwd: Path, *args: str) -> str:
    """Run a git command in *cwd* and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.strip()


def init_repo(path: Path, files: dict[str, str], message: str = "initial commit") -> str:
    """Create a real git repository at *path*, commit *files*, return the HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-b", "main")
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-m", message)
    return run_git(path, "rev-parse", "HEAD")


@pytest.fixture
def make_git_repo(tmp_path: Path) -> Callable[..., tuple[Path, str]]:
    """Factory: make_git_repo(files={...}) -> (repo_path, head_sha)."""

    def _make(name: str = "origin-repo", **kwargs) -> tuple[Path, str]:
        repo_dir = tmp_path / name
        sha = init_repo(repo_dir, **kwargs)
        return repo_dir, sha

    return _make


@pytest.fixture
def local_repo(make_git_repo) -> tuple[Path, str]:
    """A ready-made single-commit repository with a couple of files."""
    return make_git_repo(
        files={
            "README.md": "# demo\n",
            "src/app.py": "print('hello')\n",
        }
    )
