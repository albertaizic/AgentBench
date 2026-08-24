"""Tests for workspace lifecycle (agentbench.workspace): clone, checkout, cleanup."""

from __future__ import annotations

import errno
import os
import shutil
import stat

import pytest

from conftest import run_git
from agentbench import workspace as workspace_mod
from agentbench.workspace import (
    Workspace,
    WorkspaceError,
    _on_readonly,
    create_workspace,
    remove_tree,
)


class TestCreateWorkspace:
    def test_clones_and_checks_out_exact_commit(self, local_repo):
        repo_path, sha = local_repo

        with create_workspace(str(repo_path), sha) as ws:
            assert isinstance(ws, Workspace)
            assert ws.head_commit == sha
            assert run_git(ws.path, "rev-parse", "HEAD") == sha
            assert (ws.path / "README.md").read_text(encoding="utf-8") == "# demo\n"

    def test_head_is_detached_at_configured_commit(self, local_repo):
        repo_path, sha = local_repo

        with create_workspace(str(repo_path), sha) as ws:
            # Detached HEAD proves the checkout is pinned to the exact
            # commit rather than floating on a branch.
            assert run_git(ws.path, "branch", "--show-current") == ""

    def test_missing_repository_raises_workspace_error(self, tmp_path):
        with pytest.raises(WorkspaceError, match="clone"):
            create_workspace(str(tmp_path / "does-not-exist"), "a" * 40)

    def test_unknown_commit_raises_workspace_error(self, local_repo):
        repo_path, _ = local_repo

        with pytest.raises(WorkspaceError, match="checkout"):
            create_workspace(str(repo_path), "f" * 40)

    def test_custom_parent_directory_is_used(self, local_repo, tmp_path):
        repo_path, sha = local_repo
        parent = tmp_path / "custom-parent"
        parent.mkdir()

        with create_workspace(str(repo_path), sha, parent=parent) as ws:
            assert ws.path.parent == parent


class TestCleanup:
    def test_remove_tree_retries_through_transient_locks(self, tmp_path, monkeypatch):
        # Antivirus/indexer processes routinely hold fresh files for a moment
        # on Windows; deletion must retry instead of leaking the workspace.
        import shutil as _shutil

        target = tmp_path / "ws"
        target.mkdir()
        (target / "f.txt").write_text("x", encoding="utf-8")

        attempts = {"count": 0}
        real_rmtree = _shutil.rmtree

        def flaky_rmtree(path, **kwargs):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise PermissionError(32, "file in use by another process")
            return real_rmtree(path, **kwargs)

        monkeypatch.setattr(_shutil, "rmtree", flaky_rmtree)

        remove_tree(target)

        assert attempts["count"] == 3
        assert not target.exists()

    def test_cleanup_removes_the_workspace(self, local_repo):
        repo_path, sha = local_repo
        ws = create_workspace(str(repo_path), sha)
        path = ws.path

        ws.cleanup()

        assert not path.exists()

    def test_cleanup_handles_readonly_git_files(self, local_repo):
        # Git object files are read-only; a naive rmtree fails on Windows.
        repo_path, sha = local_repo
        ws = create_workspace(str(repo_path), sha)
        # Files only: rglob also yields directories, and on Linux the first
        # match was a fan-out directory (e.g. objects/a7) — chmodding that to
        # S_IREAD strips its traversal bit, silently changing the scenario.
        target = next(
            p for p in (ws.path / ".git" / "objects").rglob("*") if p.is_file()
        )
        os.chmod(target, stat.S_IREAD)

        ws.cleanup()

        assert not ws.path.exists()

    @pytest.mark.skipif(
        os.name == "nt",
        reason="read-only directories only lose their traversal bit on POSIX; "
        "on Windows this would degenerate into a second normal-cleanup test",
    )
    def test_cleanup_removes_non_traversable_directory(self, local_repo):
        # A read-only directory loses its traversal (x) bit on POSIX, which
        # blocks fd-based rmtree from even opening it. AgentBench owns the
        # whole workspace, so cleanup must clear that too.
        repo_path, sha = local_repo
        ws = create_workspace(str(repo_path), sha)
        locked = ws.path / "locked-dir"
        locked.mkdir()
        (locked / "payload.bin").write_bytes(b"x")
        os.chmod(locked, stat.S_IREAD)

        ws.cleanup()

        assert not ws.path.exists()

    @pytest.mark.skipif(os.name == "nt", reason="fd-based rmtree walk is POSIX-only")
    def test_posix_non_traversable_dir_defeats_naive_rmtree(self, local_repo):
        # Proves the CI scenario is real on Linux: plain rmtree cannot delete
        # a workspace containing a non-traversable directory.
        repo_path, sha = local_repo
        ws = create_workspace(str(repo_path), sha)
        locked = ws.path / ".git" / "objects" / "zz"
        locked.mkdir()
        (locked / "object").write_bytes(b"x")
        os.chmod(locked, stat.S_IREAD)

        with pytest.raises(OSError):
            shutil.rmtree(ws.path)

    def test_onexc_handler_redoes_failed_directory_open(self, tmp_path):
        # Exact shape of the Linux CI crash: the fd-based walk reports a
        # directory it cannot open as onexc(os.open, path, err). The old
        # handler called func(path), i.e. os.open(path) without flags ->
        # "TypeError: open() missing required argument 'flags'".
        locked = tmp_path / "a7"
        locked.mkdir()
        (locked / "object").write_bytes(b"x")
        exc = OSError(errno.EACCES, "Permission denied")
        exc.filename = str(locked)

        _on_readonly(os.open, str(locked), exc)

        assert not locked.exists()

    @pytest.mark.skipif(os.name != "nt", reason="Windows readonly-attribute semantics")
    def test_windows_readonly_file_is_removed(self, tmp_path):
        victim = tmp_path / "ro.txt"
        victim.write_text("x", encoding="utf-8")
        os.chmod(victim, stat.S_IREAD)

        remove_tree(tmp_path)

        assert not tmp_path.exists()

    def test_remove_tree_removes_normal_tree(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")

        remove_tree(tmp_path)

        assert not tmp_path.exists()

    def test_unexpected_errors_are_not_swallowed(self, tmp_path, monkeypatch):
        # A non-OSError escaping AgentBench's recovery path must propagate
        # immediately: only OSError earns the retry/backoff treatment. The
        # failure is delivered through shutil's own reporting seam (stubbed,
        # since whether deleting a read-only regular file invokes the handler
        # at all is OS-dependent — verified on linux 3.12: zero calls).
        (tmp_path / "some_file.txt").write_text("x", encoding="utf-8")
        hooks: list = []

        def fake_rmtree(path, **kwargs):
            # Report one deletion failure exactly as both shutil
            # implementations do: hand it to the caller's onexc hook.
            onexc = kwargs["onexc"]
            hooks.append(onexc)
            onexc(os.unlink, str(path / "some_file.txt"),
                  PermissionError(13, "Permission denied"))

        monkeypatch.setattr(workspace_mod.shutil, "rmtree", fake_rmtree)

        recovery_calls: list = []

        def broken_recovery(func, path, exc):
            recovery_calls.append(path)
            raise RuntimeError("boom")

        monkeypatch.setattr(workspace_mod, "_on_readonly", broken_recovery)

        with pytest.raises(RuntimeError, match="boom"):
            remove_tree(tmp_path)

        assert len(hooks) == 1
        assert hooks[0] is broken_recovery
        assert len(recovery_calls) == 1  # failed fast — no swallow, no retry

    def test_handler_surfaces_its_own_recovery_failure(self, tmp_path, monkeypatch):
        # Same guarantee one layer down: if the handler's own repair step
        # (chmod) raises a non-OSError, it must escape, not vanish. Exercised
        # directly so no platform has to cooperate by making deletion fail.
        victim = tmp_path / "target.txt"
        victim.write_text("x", encoding="utf-8")

        def broken_chmod(path, mode):
            raise RuntimeError("boom")

        monkeypatch.setattr(os, "chmod", broken_chmod)

        with pytest.raises(RuntimeError, match="boom"):
            _on_readonly(
                os.unlink, str(victim), OSError(errno.EACCES, "Permission denied")
            )

    def test_transient_oserror_is_retried(self, tmp_path, monkeypatch):
        # Antivirus/indexer locks are why the backoff loop exists: one
        # transient PermissionError, then success.
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        calls: list = []
        real_rmtree = shutil.rmtree

        def flaky(path, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(32, "in use by another process")
            return real_rmtree(path, **kwargs)

        monkeypatch.setattr(workspace_mod.shutil, "rmtree", flaky)

        remove_tree(tmp_path)

        assert not tmp_path.exists()
        assert len(calls) == 2  # exactly one retry

    def test_persistent_oserror_fails_after_backoff(self, tmp_path, monkeypatch):
        # When every attempt fails, the last error surfaces to the caller —
        # the workspace leak is reported, not hidden.
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        calls: list = []

        def always_locked(path, **kwargs):
            calls.append(path)
            raise PermissionError(32, "in use by another process")

        monkeypatch.setattr(workspace_mod.shutil, "rmtree", always_locked)

        with pytest.raises(PermissionError):
            remove_tree(tmp_path)

        assert len(calls) == 4  # three backoff attempts + the final raise-through

    def test_context_manager_cleans_up_after_body_exception(self, local_repo):
        repo_path, sha = local_repo
        ws = create_workspace(str(repo_path), sha)

        with pytest.raises(RuntimeError, match="agent blew up"):
            with ws:
                raise RuntimeError("agent blew up")

        assert not ws.path.exists()

    def test_keep_flag_prevents_cleanup(self, local_repo, tmp_path):
        repo_path, sha = local_repo

        with create_workspace(str(repo_path), sha, keep=True) as ws:
            pass

        # keep=True means the library will never delete it — the caller owns
        # the directory now (this is what --keep-workspace promises).
        assert ws.path.exists()
        remove_tree(ws.path)  # test's own teardown
