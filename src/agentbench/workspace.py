"""Temporary workspace lifecycle: clone, checkout an exact commit, guaranteed cleanup.

Cleanup must survive Windows semantics where Git object files are read-only,
so removal retries after clearing the read-only flag.

An optional bare-mirror cache accelerates repeated clones of the same remote.
The cache is an optimization only: every run still gets an independent fresh
clone, the exact commit is always verified after checkout, and any cache
problem falls back to cloning directly from the origin.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from types import TracebackType

from agentbench.process import ProcessResult, resolve_executable, run_command

# Resolved once: an absolute git path cannot be shadowed by an
# agent-planted executable in the workspace, even where the OS would
# otherwise search the cwd first.
GIT_EXECUTABLE = resolve_executable("git")

_CACHE_LOCK_TIMEOUT = 30.0


class WorkspaceError(RuntimeError):
    """Raised when a workspace cannot be cloned or checked out."""


def default_cache_root() -> Path:
    base = os.environ.get("AGENTBENCH_CACHE_DIR")
    if base:
        return Path(base)
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "agentbench" / "git-cache"
    return Path.home() / ".cache" / "agentbench" / "git-cache"


def _cache_lock(mirror_dir: Path):
    """Exclusive lock file around cache mutation; breaks stale locks."""
    import errno

    lock = mirror_dir.with_suffix(".lock")
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.EACCES) or time.monotonic() > deadline:
                if time.monotonic() > deadline and lock.exists():
                    # Stale lock from a killed process: break it.
                    try:
                        lock.unlink()
                    except OSError:
                        pass
                    continue
                raise
        time.sleep(0.1)


def _release_cache_lock(mirror_dir: Path, handle: int) -> None:
    """Close the lock fd AND unlink the file: os.close alone leaves the lock
    path behind, forcing every later acquirer to wait out the full stale-lock
    timeout before breaking it."""
    os.close(handle)
    try:
        mirror_dir.with_suffix(".lock").unlink()
    except OSError:
        pass


def _update_mirror(repository: str, mirror_dir: Path) -> None:
    handle = _cache_lock(mirror_dir)
    try:
        result = run_command([GIT_EXECUTABLE, "remote", "update", "--prune"], cwd=mirror_dir)
        if result.exit_code != 0:
            raise WorkspaceError(f"cache refresh failed:\n{(result.stderr or result.stdout).strip()}")
    finally:
        _release_cache_lock(mirror_dir, handle)


def _mirror_for(repository: str, cache_root: Path) -> Path:
    digest = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:16]
    return cache_root / f"{digest}.git"


def _run_git(args: list[str], *, cwd: Path) -> ProcessResult:
    result = run_command([GIT_EXECUTABLE, "--no-pager", *args], cwd=cwd)
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorkspaceError(f"git {' '.join(args)} failed:\n{detail}")
    return result


def _on_readonly(func, path: str, exc: BaseException) -> None:
    """``shutil.rmtree`` ``onexc`` handler that clears read-only bits and redoes
    the failed step.

    ``func`` is whichever stdlib operation failed, and its signature varies by
    platform and rmtree implementation: the POSIX fd-based walk reports
    failures from ``os.lstat``/``os.open``/``os.scandir``, while the fallback
    walk used on Windows reports ``os.unlink``/``os.rmdir``/``os.scandir``.
    Only some of those take a single argument — ``os.open`` needs flags — so
    each supported case is redone explicitly instead of a blind ``func(path)``
    (Python 3.12 shutil semantics: when the handler returns, rmtree does *not*
    retry; the handler owns redoing the operation).
    """
    st = os.lstat(path)  # lstat, never stat: do not follow symlinks out of the workspace
    if stat.S_ISLNK(st.st_mode):
        raise exc  # symlink handling stays shutil's business; never chmod through a link
    os.chmod(path, stat.S_IRWXU)  # owner rwx: writable file, traversable directory
    if func is os.close:
        return  # draining an fd whose subtree is already gone; nothing to repair
    if func is os.open:
        # POSIX fd-based walk could not open this directory, so none of its
        # frames were pushed and nothing inside has been visited yet. Clear
        # the subtree here; the outer walk finds it gone when removing parents.
        remove_tree(Path(path))
        return
    func(path)  # os.unlink / os.rmdir / os.lstat / os.scandir / ...


def remove_tree(path: Path) -> None:
    """rmtree that survives read-only bits and transient locks.

    Antivirus/indexer processes routinely hold freshly written files for a
    moment on Windows; a single failed attempt would leak the workspace, so
    deletion retries with a short backoff before giving up. Only OSError is
    retried — anything else is a real bug and surfaces immediately.
    """

    for delay in (0.1, 0.3, 0.9):
        try:
            shutil.rmtree(path, onexc=_on_readonly)
            return
        except OSError:
            time.sleep(delay)
    shutil.rmtree(path, onexc=_on_readonly)  # final attempt: raise if still locked


class Workspace:
    """A checked-out working copy that cleans up after itself."""

    def __init__(
        self,
        path: Path,
        head_commit: str,
        *,
        keep: bool = False,
        prep_info: dict | None = None,
    ) -> None:
        self.path = path
        self.head_commit = head_commit
        self._keep = keep
        # Provenance about how this workspace was produced (cache hit/miss,
        # preparation seconds). Recorded with run evidence; never identity.
        self.prep_info = prep_info

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


def _clone_and_checkout(source: str, path: Path, commit: str) -> tuple[bool, str]:
    """Clone from *source* and detach at *commit*. Returns (ok, detail)."""
    clone = run_command([GIT_EXECUTABLE, "clone", "--quiet", source, str(path)], cwd=path.parent)
    if clone.exit_code != 0:
        detail = (clone.stderr or clone.stdout).strip()
        if path.exists():
            _remove_tree_quietly(path)
        return False, f"Failed to clone {source!r}:\n{detail}"

    checkout = run_command(
        [GIT_EXECUTABLE, "checkout", "--quiet", "--detach", commit], cwd=path
    )
    if checkout.exit_code != 0:
        detail = (checkout.stderr or checkout.stdout).strip()
        _remove_tree_quietly(path)
        return False, f"Failed to checkout {commit!r}:\n{detail}"
    return True, ""


def create_workspace(
    repository: str,
    commit: str,
    *,
    parent: Path | None = None,
    keep: bool = False,
    use_cache: bool | None = None,
) -> Workspace:
    """Clone *repository* into a fresh temp dir and check out *commit* exactly.

    ``use_cache`` (default on unless ``AGENTBENCH_NO_CACHE=1``) routes remote
    clones through a bare-mirror cache. The cache is only an accelerator:
    every failure falls back to a direct clone and the resolved commit is
    always verified afterwards.
    """
    root = Path(parent) if parent is not None else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"agentbench-{uuid.uuid4().hex[:12]}"

    if use_cache is None:
        use_cache = os.environ.get("AGENTBENCH_NO_CACHE", "") != "1"
    is_remote = "://" in repository or repository.startswith("git@")

    prep_started = time.monotonic()
    cache_error: str | None = None
    cache_success = False
    if use_cache and is_remote:
        try:
            cache_root = default_cache_root()
            cache_root.mkdir(parents=True, exist_ok=True)
            mirror_dir = _mirror_for(repository, cache_root)
            handle = _cache_lock(mirror_dir)
            try:
                if not mirror_dir.exists():
                    init = run_command(
                        [GIT_EXECUTABLE, "clone", "--quiet", "--bare",
                         repository, str(mirror_dir)],
                        cwd=cache_root,
                    )
                    if init.exit_code != 0:
                        raise WorkspaceError(init.stderr or init.stdout)
                else:
                    update = run_command(
                        [GIT_EXECUTABLE, "remote", "update", "--prune"], cwd=mirror_dir
                    )
                    if update.exit_code != 0:
                        raise WorkspaceError(update.stderr or update.stdout)
            finally:
                _release_cache_lock(mirror_dir, handle)

            # Clone locally from the mirror, then refresh refs from origin so
            # a stale mirror cannot hide newer commits.
            clone = run_command(
                [GIT_EXECUTABLE, "clone", "--quiet", str(mirror_dir), str(path)],
                cwd=cache_root,
            )
            if clone.exit_code == 0:
                fetch = run_command(
                    [GIT_EXECUTABLE, "fetch", "--quiet", repository],
                    cwd=path,
                )
                if fetch.exit_code != 0:
                    raise WorkspaceError(fetch.stderr or fetch.stdout)
                checkout = run_command(
                    [GIT_EXECUTABLE, "checkout", "--quiet", "--detach", commit], cwd=path
                )
                if checkout.exit_code != 0:
                    raise WorkspaceError(
                        f"cache missing {commit!r}: {(checkout.stderr or '').strip()}"
                    )
                cache_success = True
            else:
                _remove_tree_quietly(path)
                raise WorkspaceError(clone.stderr or clone.stdout)
        except (WorkspaceError, OSError):
            # Cache problems never fail a run: wipe and fall back to origin.
            cache_success = False
            if path.exists():
                _remove_tree_quietly(path)

    if not cache_success:
        clone_ok, detail = _clone_and_checkout(repository, path, commit)
        if not clone_ok:
            raise WorkspaceError(detail)

    head = _run_git(["rev-parse", "HEAD"], cwd=path)
    resolved = head.stdout.strip()
    if resolved != _resolve_commit(commit, path):
        raise WorkspaceError(f"checkout produced {resolved}, expected {commit}")
    return Workspace(
        path,
        resolved,
        keep=keep,
        prep_info={
            "cache_hit": cache_success,
            "cache_enabled": bool(use_cache and is_remote),
            "duration_seconds": round(time.monotonic() - prep_started, 3),
        },
    )


def _resolve_commit(abbrev: str, repo: Path) -> str:
    result = run_command([GIT_EXECUTABLE, "--no-pager", "rev-parse", abbrev], cwd=repo)
    return result.stdout.strip()


def cache_hit_for(repository: str, use_cache: bool | None = None) -> bool:
    """Whether a mirror already exists for *repository* (provenance hint)."""
    if use_cache is False:
        return False
    if not ("://" in repository or repository.startswith("git@")):
        return False
    return _mirror_for(repository, default_cache_root()).exists()


def _remove_tree_quietly(path: Path) -> None:
    if path.exists():
        try:
            remove_tree(path)
        except OSError:
            pass  # best effort while reporting the original git failure
