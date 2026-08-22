"""Temporary workspace lifecycle: clone, checkout an exact commit, guaranteed cleanup.

Cleanup must survive Windows semantics where Git object files are read-only,
so removal retries after clearing the read-only flag.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from types import TracebackType

from agentbench.process import ProcessResult, resolve_executable, run_command

# Resolved once: an absolute git path cannot be shadowed by an
# agent-planted executable in the workspace, even where the OS would
# otherwise search the cwd first.
GIT_EXECUTABLE = resolve_executable("git")


class WorkspaceError(RuntimeError):
    """Raised when a workspace cannot be cloned or checked out."""


def _run_git(args: list[str], *, cwd: Path) -> ProcessResult:
    result = run_command([GIT_EXECUTABLE, "--no-pager", *args], cwd=cwd)
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorkspaceError(f"git {' '.join(args)} failed:\n{detail}")
    return result


def remove_tree(path: Path) -> None:
    """rmtree that survives read-only bits and transient locks.

    Antivirus/indexer processes routinely hold freshly written files for a
    moment on Windows; a single failed attempt would leak the workspace, so
    deletion retries with a short backoff before giving up.
    """

    def _on_readonly(func, target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    for delay in (0.1, 0.3, 0.9):
        try:
            shutil.rmtree(path, onexc=_on_readonly)
            return
        except OSError:
            time.sleep(delay)
    shutil.rmtree(path, onexc=_on_readonly)  # final attempt: raise if still locked


class Workspace:
    """A checked-out working copy that cleans up after itself."""

    def __init__(self, path: Path, head_commit: str, *, keep: bool = False) -> None:
        self.path = path
        self.head_commit = head_commit
        self._keep = keep

    def cleanup(self) -> None:
        """Remove the workspace unless ``keep=True`` was requested."""
        if self._keep or not self.path.exists():
            return
        remove_tree(self.path)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()


def create_workspace(
    repository: str,
    commit: str,
    *,
    parent: Path | None = None,
    keep: bool = False,
) -> Workspace:
    """Clone *repository* into a fresh temp dir and check out *commit* exactly."""
    root = Path(parent) if parent is not None else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"agentbench-{uuid.uuid4().hex[:12]}"

    clone = run_command([GIT_EXECUTABLE, "clone", "--quiet", repository, str(path)], cwd=root)
    if clone.exit_code != 0:
        _remove_tree_quietly(path)
        raise WorkspaceError(
            f"Failed to clone {repository!r}:\n{(clone.stderr or clone.stdout).strip()}"
        )

    checkout = run_command([GIT_EXECUTABLE, "checkout", "--quiet", "--detach", commit], cwd=path)
    if checkout.exit_code != 0:
        _remove_tree_quietly(path)
        raise WorkspaceError(
            f"Failed to checkout {commit!r} in cloned workspace:\n"
            f"{(checkout.stderr or checkout.stdout).strip()}"
        )

    head = _run_git(["rev-parse", "HEAD"], cwd=path)
    return Workspace(path, head.stdout.strip(), keep=keep)


def _remove_tree_quietly(path: Path) -> None:
    if path.exists():
        try:
            remove_tree(path)
        except OSError:
            pass  # best effort while reporting the original git failure
