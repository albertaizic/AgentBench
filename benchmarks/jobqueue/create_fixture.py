"""Deterministic generator for the jobqueue fixture (ordering bugfix task)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "jobqueue"\nversion = "0.1.0"\n',
    # BUG: sort key ignores submission sequence, so equal-priority jobs come
    # out in reverse insertion order; and priority 10 sorts before 9
    # lexicographically because priorities are compared as strings somewhere.
    "jobqueue/queue.py": (
        '"""A small deterministic priority job queue."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from dataclasses import dataclass, field\n\n\n'
        '@dataclass\nclass Job:\n'
        '    name: str\n'
        '    priority: int          # lower value runs first\n'
        '    seq: int               # insertion order tie-breaker\n\n\n'
        'class JobQueue:\n'
        '    def __init__(self) -> None:\n'
        '        self._jobs: list[Job] = []\n'
        '        self._next_seq = 0\n\n'
        '    def submit(self, name: str, priority: int) -> Job:\n'
        '        job = Job(name=name, priority=priority, seq=self._next_seq)\n'
        '        self._next_seq += 1\n'
        '        self._jobs.append(job)\n'
        '        return job\n\n'
        '    def drain(self) -> list[Job]:\n'
        '        ordered = sorted(self._jobs, key=lambda j: str(j.priority))\n'
        '        self._jobs.clear()\n'
        '        return ordered\n'
    ),
    "tests/test_queue.py": (
        '"""Public tests for the job queue ordering contract."""\n\n'
        'from jobqueue.queue import JobQueue\n\n\n'
        'def test_priority_order():\n'
        '    q = JobQueue()\n'
        '    q.submit("low", 5)\n'
        '    q.submit("high", 1)\n'
        '    assert [j.name for j in q.drain()] == ["high", "low"]\n\n'
        'def test_equal_priority_keeps_submission_order():\n'
        '    q = JobQueue()\n'
        '    for index in range(5):\n'
        '        q.submit(f"job{index}", 3)\n'
        '    assert [j.name for j in q.drain()] == [f"job{i}" for i in range(5)]\n\n'
        'def test_numeric_not_lexicographic_priorities():\n'
        '    q = JobQueue()\n'
        '    q.submit("nine", 9)\n'
        '    q.submit("ten", 10)\n'
        '    q.submit("two", 2)\n'
        '    assert [j.name for j in q.drain()] == ["two", "nine", "ten"]\n\n'
        'def test_queue_is_emptied_by_drain():\n'
        '    q = JobQueue()\n'
        '    q.submit("only", 1)\n'
        '    q.drain()\n'
        '    assert q.drain() == []\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "jobqueue: deterministic priority queue", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
