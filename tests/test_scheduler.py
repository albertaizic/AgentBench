"""Bounded-parallelism scheduler: concurrency cap, isolation, cancellation."""

from __future__ import annotations

import threading
import time

import pytest

from agentbench.scheduler import CANCELLED, MAX_JOBS, Scheduler


class TestSchedulerContract:
    def test_never_exceeds_the_job_limit(self):
        active = {"n": 0, "max": 0}
        lock = threading.Lock()

        def worker(item):
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.02)
            with lock:
                active["n"] -= 1
            return item * 10

        seen: list = []
        Scheduler(3).run(list(range(12)), worker, lambda item, fut: seen.append(fut.result()))

        assert active["max"] <= 3
        assert sorted(seen) == [x * 10 for x in range(12)]

    def test_worker_exception_is_delivered_not_raised(self):
        def worker(item):
            if item == 1:
                raise RuntimeError("cell exploded")
            return f"ok-{item}"

        outcomes: dict = {}
        Scheduler(2).run(
            [0, 1, 2],
            worker,
            lambda item, fut: outcomes.__setitem__(item, fut),
        )

        assert outcomes[0].result() == "ok-0"
        assert outcomes[2].result() == "ok-2"
        with pytest.raises(RuntimeError, match="cell exploded"):
            outcomes[1].result()

    def test_stop_cancels_queued_but_lets_inflight_finish(self):
        started: list[int] = []
        release = threading.Event()

        def worker(item):
            started.append(item)
            if item == 0:
                release.wait(timeout=5)
            return f"done-{item}"

        completed: dict = {}
        scheduler = Scheduler(1)

        def should_stop() -> bool:
            # Fires after the first item was submitted and reported running.
            return bool(started)

        import threading as th

        def unblock():
            time.sleep(0.05)
            release.set()

        th.Thread(target=unblock, daemon=True).start()
        interrupted = scheduler.run(
            [0, 1, 2],
            worker,
            lambda item, fut: completed.__setitem__(item, fut),
            should_stop=should_stop,
        )

        assert interrupted is True
        assert completed[0].result() == "done-0"  # in-flight finished cleanly
        assert completed.get(1) is None or completed[1].result() is CANCELLED
        assert completed.get(2) is None or completed[2].result() is CANCELLED

    def test_on_completion_runs_on_caller_thread(self):
        main_thread = threading.current_thread().ident
        observed: list = []
        Scheduler(4).run(
            [1, 2],
            lambda item: item,
            lambda item, fut: observed.append(threading.current_thread().ident is main_thread),
        )

        assert observed == [True, True]

    def test_invalid_jobs_rejected(self):
        with pytest.raises(ValueError):
            Scheduler(0)
        with pytest.raises(ValueError):
            Scheduler(MAX_JOBS + 1)

    def test_empty_item_list_completes_immediately(self):
        interrupted = Scheduler(2).run([], lambda item: item, lambda item, fut: None)
        assert interrupted is False


class TestStartBudget:
    """max_starts caps submissions, not completions (the --max-runs contract):
    no more than N cells may ever start, whatever --jobs or completion timing
    would otherwise allow. Assertions count actual worker invocations."""

    def _run(self, jobs, items, worker, max_starts):
        lock = threading.Lock()
        started: list[int] = []

        def tracked(item):
            with lock:
                started.append(item)
            return worker(item)

        outcomes: dict = {}
        scheduler = Scheduler(jobs)
        stopped = scheduler.run(
            items, tracked,
            lambda item, fut: outcomes.__setitem__(item, fut),
            max_starts=max_starts,
        )
        return scheduler, sorted(started), outcomes, stopped

    def test_jobs1_cap1_starts_exactly_one_cell(self):
        scheduler, started, outcomes, stopped = self._run(
            1, [0, 1, 2], lambda i: f"ok-{i}", max_starts=1,
        )

        assert started == [0]
        assert stopped is True
        assert scheduler.budget_exhausted is True
        assert outcomes[0].result() == "ok-0"
        assert outcomes.get(1) is None or outcomes[1].result() is CANCELLED
        assert outcomes.get(2) is None or outcomes[2].result() is CANCELLED

    def test_jobs2_cap1_never_pulls_a_second_cell(self):
        _, started, _, stopped = self._run(
            2, [0, 1, 2], lambda i: f"ok-{i}", max_starts=1,
        )

        assert started == [0]
        assert stopped is True

    def test_jobs2_cap2_over_five_items_starts_exactly_two(self):
        _, started, _, stopped = self._run(
            2, [0, 1, 2, 3, 4], lambda i: f"ok-{i}", max_starts=2,
        )

        assert started == [0, 1]
        assert stopped is True

    def test_jobs4_cap1_single_start_despite_idle_workers(self):
        _, started, _, _ = self._run(4, [0, 1, 2, 3, 4, 5], lambda i: i, max_starts=1)

        assert len(started) == 1

    def test_jobs4_cap3_starts_exactly_three(self):
        _, started, _, _ = self._run(4, list(range(8)), lambda i: i, max_starts=3)

        assert len(started) == 3

    def test_cap_above_item_count_is_not_a_stop(self):
        scheduler, started, outcomes, stopped = self._run(
            2, [0, 1], lambda i: f"ok-{i}", max_starts=10,
        )

        assert started == [0, 1]
        assert stopped is False
        assert scheduler.budget_exhausted is False
        assert [outcomes[k].result() for k in (0, 1)] == ["ok-0", "ok-1"]

    def test_failing_cells_still_count_against_the_cap(self):
        def boom(item):
            raise RuntimeError(f"cell {item} exploded")

        _, started, outcomes, stopped = self._run(2, [0, 1, 2, 3], boom, max_starts=2)

        assert len(started) == 2
        assert stopped is True
        for item in started:  # exceptions surface through their futures
            with pytest.raises(RuntimeError):
                outcomes[item].result()

    def test_invalid_max_starts_rejected(self):
        with pytest.raises(ValueError):
            Scheduler(1).run([0], lambda i: i, lambda i, f: None, max_starts=0)

    def test_empty_items_with_cap_completes_cleanly(self):
        scheduler, started, _, stopped = self._run(2, [], lambda i: i, max_starts=1)

        assert started == []
        assert stopped is False
        assert scheduler.budget_exhausted is False
