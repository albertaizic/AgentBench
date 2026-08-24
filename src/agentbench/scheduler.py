"""Bounded-parallelism scheduler for experiment matrices.

Each cell is a subprocess-heavy AgentBench run (its own workspace, agent
process, possibly its own container), so plain threads around
:func:`agentbench.runner.run_benchmark` are the right concurrency unit — the
GIL is released while subprocesses run, and no shared mutable state exists
between cells.

Contract:

* at most *jobs* cells execute concurrently;
* ``max_starts`` (the --max-runs budget) is enforced at *submission* time:
  no more than that many cells are ever launched, regardless of worker
  availability or completion timing;
* one cell's exception never prevents other cells from completing;
* :meth:`Scheduler.run` returns ``True`` when a stop was requested
  (Ctrl+C or budget): queued-but-unstarted cells are cancelled, in-flight
  cells finish naturally (bounded by their own timeouts) so their evidence
  and cleanup still happen, and their results are reported like any other;
* ``on_completion`` is always invoked from the calling thread, so callers
  (manifest writes, SQLite indexing, console output) need no locking.
"""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Hard ceiling: more parallel benchmark processes than this is almost always
# a typo, and each cell may hold a workspace plus a Docker container.
MAX_JOBS = 16

#: Outcome delivered for items that were cancelled before their worker ran.
#: Callers treat them as never-started: no manifest record, so resume re-runs them.
CANCELLED = object()


class Scheduler:
    def __init__(self, jobs: int) -> None:
        if not 1 <= jobs <= MAX_JOBS:
            raise ValueError(f"jobs must be between 1 and {MAX_JOBS}, got {jobs}")
        self.jobs = jobs
        self._stop = threading.Event()
        self._budget_limited = False

    @property
    def budget_exhausted(self) -> bool:
        """A supplied start budget truncated the queue (clean, resumable stop)."""
        return self._budget_limited

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Stop scheduling new cells; in-flight cells run to completion."""
        self._stop.set()

    def run(
        self,
        items: list[T],
        worker: Callable[[T], Any],
        on_completion: Callable[[T, Any], None],
        *,
        should_stop: Callable[[], bool] | None = None,
        on_interrupt: Callable[[], None] | None = None,
        max_starts: int | None = None,
    ) -> bool:
        """Execute *items* through *worker*, reporting through *on_completion*.

        Returns True if the run ended early (stop requested / interrupt /
        start budget); False when every item completed. Exceptions raised by
        *worker* are delivered to *on_completion* as the outcome value, never
        re-raised — except KeyboardInterrupt, which ends scheduling
        immediately (and fires ``on_interrupt`` before returning).

        ``max_starts`` is a hard cap on how many items this call may start,
        enforced structurally before scheduling begins: at most that many
        items are ever handed to the pool, whatever worker availability or
        completion timing would allow. Items beyond the cap are never
        submitted (and never reported, like anything queued behind a stop);
        :attr:`budget_exhausted` reports the truncation.
        """
        if max_starts is not None and max_starts < 1:
            raise ValueError(f"max_starts must be >= 1, got {max_starts}")
        interrupted = False
        self._budget_limited = False
        # Structural cap: decide the full submission set up front. There is no
        # runtime counter to race and no stop/budget interplay — a budgeted
        # slot always belongs to a real cell.
        if max_starts is not None and len(items) > max_starts:
            self._budget_limited = True
            pending = list(items[:max_starts])
        else:
            pending = list(items)
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures: dict[Any, T] = {}

            def guarded(item: T) -> Any:
                # Closes the race between submission and execution: an item
                # already handed to the pool when a stop arrives reports as
                # CANCELLED instead of starting real work.
                if self._stop.is_set():
                    return CANCELLED
                return worker(item)

            def submit_while_allowed() -> None:
                # Keep at most *jobs* items outstanding: anything still in
                # `pending` was never started, so a stop request cancels it
                # outright instead of racing a pool-side queue.
                while pending and not self._stop.is_set() and len(futures) < self.jobs:
                    item = pending.pop(0)
                    futures[pool.submit(guarded, item)] = item

            try:
                submit_while_allowed()
                while futures:
                    done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        on_completion(futures.pop(future), future)
                    if should_stop is not None and should_stop():
                        self._stop.set()
                    if not self._stop.is_set():
                        submit_while_allowed()
            except KeyboardInterrupt:
                # Ctrl+C (surfaced here through a cell's future): cancel queued
                # work immediately; in-flight cells keep running inside the
                # executor and are joined on pool exit (their own timeouts
                # bound how long that can take). Fire the caller's handler
                # BEFORE anything else can observe the half-updated state.
                interrupted = True
                self._stop.set()
                if on_interrupt is not None:
                    try:
                        on_interrupt()
                    except Exception:  # noqa: BLE001 - handler must not mask exit
                        pass
            # Pool exit joins surviving workers; collect whatever finished.
            # Cancelled-before-start items surface here as CANCELLED results.
            for future, item in list(futures.items()):
                on_completion(item, future)
        if self._budget_limited:
            # Every budgeted item has finished; nothing is left in flight, so
            # raising the stop flag now cancels nothing and flags the early
            # end for callers (experiment marked incomplete, resumable).
            self._stop.set()
        # True when a stop was requested at any point
        # (Ctrl+C, should_stop, or the start budget).
        return interrupted or self._stop.is_set()
