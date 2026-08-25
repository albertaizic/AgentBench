"""Capture the agent's changes as one Git patch plus basic line statistics.

Staging everything first (``git add -A``) folds brand-new untracked files —
exactly what agents tend to produce — into ``git diff HEAD``, which a plain
``git diff`` would silently miss. Staging alters only the index of our own
throwaway workspace, never file contents.

Conventional tooling artifacts (virtualenvs, caches) never enter the
capture: they are installed into the workspace clone's ``.git/info/exclude``
before staging, so an agent that auto-creates a virtualenv cannot fabricate
diff statistics (a real Hermes run once recorded 488k "insertions" from an
auto-created ``.venv``). Gitignore semantics apply at any depth; already
tracked fixture files are unaffected, and agents can still force-add
deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentbench.process import ProcessResult, resolve_executable, run_command

# Absolute path: see workspace.GIT_EXECUTABLE for the reasoning.
GIT_EXECUTABLE = resolve_executable("git")


# Never treated as agent output wherever they appear in the workspace tree.
TOOLING_DIRS: tuple[str, ...] = (
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "htmlcov", ".eggs", "node_modules",
)
TOOLING_FILES: tuple[str, ...] = (".coverage",)


def _install_tooling_exclusions(workspace: Path) -> None:
    """List TOOLING artifacts in the clone's local exclude file.

    ``.git/info/exclude`` uses gitignore rules, so ``dir/`` matches at any
    depth while touching neither tracked files nor the working tree.
    """
    lines = [
        "# agentbench: conventional tooling artifacts are never agent output",
        *(f"{name}/" for name in TOOLING_DIRS),
        "*.egg-info/",
        *TOOLING_FILES,
        "",
    ]
    info_dir = workspace / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude = info_dir / "exclude"
    existing = ""
    try:
        existing = exclude.read_text(encoding="utf-8")
    except OSError:
        pass
    if "agentbench:" not in existing:
        exclude.write_text(existing + "\n".join(lines), encoding="utf-8")

@dataclass(frozen=True)
class DiffStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class DiffResult:
    patch: str
    stats: DiffStats
    # Repository-relative paths (POSIX separators, as Git reports them).
    changed_paths: tuple[str, ...] = ()
    # Objective change-shape evidence from `git diff --name-status`:
    added_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    renamed_paths: tuple[str, ...] = ()  # "old -> new"
    binary_paths: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return self.stats.files_changed > 0


def _git(workspace: Path, args: list[str]) -> ProcessResult:
    result = run_command([GIT_EXECUTABLE, "--no-pager", *args], cwd=workspace)
    if result.exit_code != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed inside {workspace}:\n{(result.stderr or result.stdout).strip()}"
        )
    return result


def _parse_numstat(output: str) -> tuple[DiffStats, list[str]]:
    files = insertions = deletions = 0
    paths: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[2]
        files += 1
        paths.append(path)
        if added != "-":  # '-' marks binary files: counted, not line-counted
            insertions += int(added)
        if removed != "-":
            deletions += int(removed)
    return DiffStats(files_changed=files, insertions=insertions, deletions=deletions), paths


def capture_diff(workspace: Path, *, base: str | None = None) -> DiffResult:
    """Return the full patch (including untracked files) and its statistics.

    *base* must be the commit that was checked out before the agent ran:
    diffing against ``HEAD`` would silently swallow anything the agent
    commits, leaving a no-change record behind a passing run. Textconv and
    external diff drivers are disabled because the agent is untrusted and
    must not be able to forge patch content via repo-local git config.
    """
    ref = base if base is not None else "HEAD"
    tamper_guards = ["--no-textconv", "--no-ext-diff", "--find-renames"]
    _install_tooling_exclusions(workspace)
    _git(workspace, ["add", "-A"])
    patch = _git(workspace, ["diff", *tamper_guards, ref]).stdout
    numstat = _git(workspace, ["diff", "--numstat", *tamper_guards, ref]).stdout
    name_status = _git(
        workspace, ["diff", "--name-status", *tamper_guards, ref]
    ).stdout
    stats, changed_paths = _parse_numstat(numstat)
    binary_paths = [
        line.split("\t", 2)[2]
        for line in numstat.splitlines()
        if line.startswith("-\t")
    ]
    buckets = _parse_name_status(name_status)
    return DiffResult(
        patch=patch,
        stats=stats,
        changed_paths=tuple(changed_paths),
        added_paths=tuple(buckets["added"]),
        deleted_paths=tuple(buckets["deleted"]),
        renamed_paths=tuple(buckets["renamed"]),
        binary_paths=tuple(binary_paths),
    )


def _parse_name_status(output: str) -> dict[str, list[str]]:
    """Parse `git diff --name-status` into status buckets."""
    buckets: dict[str, list[str]] = {"added": [], "deleted": [], "renamed": [], "changed": []}
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0].upper()
        if status == "A" and len(parts) >= 2:
            buckets["added"].append(parts[1])
        elif status == "D" and len(parts) >= 2:
            buckets["deleted"].append(parts[1])
        elif status in ("R", "C") and len(parts) >= 3:
            buckets["renamed"].append(f"{parts[1]} -> {parts[2]}")
        elif len(parts) >= 2:
            buckets["changed"].append(parts[1])
    return buckets
