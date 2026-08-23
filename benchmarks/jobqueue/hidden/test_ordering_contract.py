"""Hidden checks: the full ordering contract across mixed submissions."""

from __future__ import annotations

from jobqueue.queue import JobQueue


def test_mixed_priorities_and_ties_follow_full_contract():
    q = JobQueue()
    submitted = [
        ("cleanup", 5),
        ("boot", 1),
        ("sync-b", 3),
        ("sync-a", 3),
        ("urgent", 1),
        ("report", 5),
    ]
    for index, (name, priority) in enumerate(submitted):
        q.submit(f"{name}-{index}", priority)

    drained = [job.name for job in q.drain()]

    assert drained == [
        "boot-1",
        "urgent-4",
        "sync-b-2",
        "sync-a-3",
        "cleanup-0",
        "report-5",
    ]


def test_negative_and_large_priorities_sort_numerically():
    q = JobQueue()
    for name, priority in [("big", 100), ("neg", -1), ("zero", 0)]:
        q.submit(name, priority)

    assert [job.name for job in q.drain()] == ["neg", "zero", "big"]
