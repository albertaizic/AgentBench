"""Protected-path detection: evidence when the agent touches off-limits files.

Git always reports repository paths with forward slashes, so matching is done
against POSIX-style names regardless of the host OS. Patterns are plain
fnmatch globs (``*`` already crosses ``/`` in :mod:`fnmatch`, so
``tests/**`` matches ``tests/a/b.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase


@dataclass(frozen=True)
class Violation:
    path: str
    pattern: str
    policy: str  # "warn" | "fail" | "allowed"


def find_violations(changed_paths: list[str], patterns: list[str]) -> list[Violation]:
    """v0.2-compatible helper: every match is treated as a ``fail``-grade hit."""
    return find_policy_violations(
        changed_paths, [([(pattern)], "fail") for pattern in patterns]
    )


def find_policy_violations(
    changed_paths: list[str],
    policies: list[tuple[list[str], str]],
) -> list[Violation]:
    """Return every (path, pattern, policy) triple where a change hits a policy.

    *policies* is a list of ``(patterns, policy)`` pairs. Each matching pair
    is reported once so evidence shows exactly why a path was flagged;
    ``allowed`` matches are recorded as evidence but never escalate.
    """
    violations: list[Violation] = []
    for path in changed_paths:
        normalized = path.replace("\\", "/")
        for patterns, policy in policies:
            for pattern in patterns:
                if fnmatchcase(normalized, pattern):
                    violations.append(Violation(path=normalized, pattern=pattern, policy=policy))
    return violations
