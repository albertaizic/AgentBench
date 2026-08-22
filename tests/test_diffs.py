"""Tests for diff capture and statistics (agentbench.diffs)."""

from __future__ import annotations

from conftest import run_git
from agentbench.diffs import DiffStats, capture_diff


def change_files(repo_path) -> None:
    """One modification, one addition, one deletion."""
    (repo_path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    added = repo_path / "docs" / "new.md"
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("brand new\nfile\n", encoding="utf-8")
    (repo_path / "README.md").unlink()


class TestCaptureDiff:
    def test_clean_tree_yields_empty_diff(self, local_repo):
        repo_path, _ = local_repo

        result = capture_diff(repo_path)

        assert result.patch == ""
        assert result.has_changes is False
        assert result.stats == DiffStats(files_changed=0, insertions=0, deletions=0)

    def test_modified_added_and_deleted_files_are_captured(self, local_repo):
        repo_path, _ = local_repo
        change_files(repo_path)

        result = capture_diff(repo_path)

        assert result.has_changes is True
        assert "changed" in result.patch  # modified content present
        assert "brand new" in result.patch  # added file content present
        assert "README.md" in result.patch  # deleted file named in patch
        assert result.stats.files_changed == 3
        assert result.stats.insertions == 3  # 1 replacement + 2 new lines
        assert result.stats.deletions == 2  # '# demo' + old print line

    def test_untracked_files_are_included(self, local_repo):
        # Agents routinely create brand-new files; a plain `git diff`
        # would silently miss them.
        repo_path, _ = local_repo
        (repo_path / "untracked.py").write_text("x = 1\n", encoding="utf-8")

        result = capture_diff(repo_path)

        assert result.has_changes is True
        assert "untracked.py" in result.patch
        assert "x = 1" in result.patch
        assert result.stats.files_changed == 1
        assert result.stats.insertions == 1

    def test_binary_file_counted_but_not_line_counted(self, local_repo):
        repo_path, _ = local_repo
        (repo_path / "logo.png").write_bytes(b"\x00\x01\x02\x03")

        result = capture_diff(repo_path)

        assert result.stats.files_changed == 1
        assert result.stats.insertions == 0
        assert result.stats.deletions == 0

    def test_staged_changes_are_included(self, local_repo):
        # The agent may stage its own work; staged-only changes must not be lost.
        repo_path, _ = local_repo
        (repo_path / "staged.txt").write_text("staged work\n", encoding="utf-8")
        run_git(repo_path, "add", "staged.txt")

        result = capture_diff(repo_path)

        assert result.has_changes is True
        assert "staged work" in result.patch

    def test_agent_commits_are_captured_against_pinned_base(self, local_repo):
        # HEAD moves if the agent commits its own work; diffing against the
        # mutable ref would silently report an empty patch for a full pass.
        repo_path, base_sha = local_repo
        (repo_path / "committed.py").write_text("x = 1\n", encoding="utf-8")
        run_git(repo_path, "add", "-A")
        run_git(repo_path, "commit", "-m", "agent commits its work")

        result = capture_diff(repo_path, base=base_sha)

        assert result.has_changes is True
        assert "committed.py" in result.patch
        assert result.stats.insertions == 1
