"""Tests for workspace lifecycle (agentbench.workspace): clone, checkout, cleanup."""

from __future__ import annotations

import os
import stat

import pytest

from conftest import run_git
from agentbench.workspace import Workspace, WorkspaceError, create_workspace, remove_tree


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
        target = next((ws.path / ".git" / "objects").rglob("*"))
        os.chmod(target, stat.S_IREAD)

        ws.cleanup()

        assert not ws.path.exists()

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
