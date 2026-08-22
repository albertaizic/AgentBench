"""Capture the agent's changes as one Git patch plus basic line statistics.

Staging everything first (``git add -A``) folds brand-new untracked files —
exactly what agents tend to produce — into ``git diff HEAD``, which a plain
``git diff`` would silently miss. Staging alters only the index of our own
throwaway workspace, never file contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentbench.process import ProcessResult, resolve_executable, run_command

# Absolute path: see workspace.GIT_EXECUTABLE for the reasoning.
GIT_EXECUTABLE = resolve_executable("git")


@dataclass(frozen=True)
class DiffStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class DiffResult:
    patch: str
    stats: DiffStats

    @property
    def has_changes(self) -> bool:
        return self.stats.files_changed > 0


def _git(workspace: Path, args: list[str]) -> ProcessResult:
    result = run_command(["git", "--no-pager", *args], cwd=workspace)
    if result.exit_code != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed inside {workspace}:\n{(result.stderr or result.stdout).strip()}"
        )
    return result


def _parse_numstat(output: str) -> DiffStats:
    files = insertions = deletions = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        added, removed = parts[0], parts[1]
        files += 1
        if added != "-":  # '-' marks binary files: counted, not line-counted
            insertions += int(added)
        if removed != "-":
            deletions += int(removed)
    return DiffStats(files_changed=files, insertions=insertions, deletions=deletions)


def capture_diff(workspace: Path, *, base: str | None = None) -> DiffResult:
    """Return the full patch (including untracked files) and its statistics.

    *base* must be the commit that was checked out before the agent ran:
    diffing against ``HEAD`` would silently swallow anything the agent
    commits, leaving a no-change record behind a passing run. Textconv and
    external diff drivers are disabled because the agent is untrusted and
    must not be able to forge patch content via repo-local git config.
    """
    ref = base if base is not None else "HEAD"
    tamper_guards = ["--no-textconv", "--no-ext-diff"]
    _git(workspace, ["add", "-A", "--"])
    patch = _git(workspace, ["diff", *tamper_guards, ref]).stdout
    numstat = _git(workspace, ["diff", "--numstat", *tamper_guards, ref]).stdout
    return DiffResult(patch=patch, stats=_parse_numstat(numstat))
