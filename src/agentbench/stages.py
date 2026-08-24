"""Failure stages and per-stage timings for a run.

The status taxonomy (``agentbench.taxonomy``) answers *what* happened;
stages answer *where* it happened. Keeping them separate means the
top-level status set stays small and stable while evidence still pinpoints
the phase a failure occurred in — "setup_failed" at stage ``source`` is a
missing commit, the same status at ``backend_prepare`` is an unavailable
Docker image.

Stage vocabulary (closed set; new phases require a schema-version bump):

* ``load``            – manifest/config interpretation before any identity exists
* ``source``          – obtaining repository objects (cache fetch, remote clone)
* ``workspace``       – clone/checkout of the pinned commit into a temp workspace
* ``backend_prepare`` – making the execution environment ready (adapter binary,
                        Docker image, container start)
* ``agent``           – the agent process itself
* ``evaluation``      – public + hidden evaluators
* ``evidence``        – diff capture, output parsing, classification inputs
* ``persistence``     – writing result.json/sidecars/index
* ``cleanup``         – backend and workspace teardown

:class:`StageTimer` collects wall-clock durations for these phases; they are
persisted as ``stage_timings`` so model latency can be separated from
AgentBench overhead.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

STAGE_LOAD = "load"
STAGE_SOURCE = "source"
STAGE_WORKSPACE = "workspace"
STAGE_BACKEND_PREPARE = "backend_prepare"
STAGE_AGENT = "agent"
STAGE_EVALUATION = "evaluation"
STAGE_EVIDENCE = "evidence"
STAGE_PERSISTENCE = "persistence"
STAGE_CLEANUP = "cleanup"

ALL_STAGES = (
    STAGE_LOAD,
    STAGE_SOURCE,
    STAGE_WORKSPACE,
    STAGE_BACKEND_PREPARE,
    STAGE_AGENT,
    STAGE_EVALUATION,
    STAGE_EVIDENCE,
    STAGE_PERSISTENCE,
    STAGE_CLEANUP,
)


class StageTimer:
    """Accumulates wall-clock seconds per stage; safe to re-enter stages."""

    def __init__(self) -> None:
        self._durations: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            self._durations[name] = self._durations.get(name, 0.0) + elapsed

    def record(self, name: str, seconds: float) -> None:
        self._durations[name] = self._durations.get(name, 0.0) + float(seconds)

    def snapshot(self) -> dict[str, float]:
        return {stage: round(seconds, 3) for stage, seconds in sorted(self._durations.items())}


__all__ = [
    "ALL_STAGES",
    "STAGE_AGENT",
    "STAGE_BACKEND_PREPARE",
    "STAGE_CLEANUP",
    "STAGE_EVIDENCE",
    "STAGE_EVALUATION",
    "STAGE_LOAD",
    "STAGE_PERSISTENCE",
    "STAGE_SOURCE",
    "STAGE_WORKSPACE",
    "StageTimer",
]
