"""Deterministic generator for the statelock fixture (guarded-transition bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {}
FILES[".gitignore"] = "__pycache__/\n*.pyc\n.pytest_cache/\n"
FILES["pyproject.toml"] = '[project]\nname = "statelock"\nversion = "0.7.0"\n'

FILES["statelock/__init__.py"] = ""

FILES["statelock/effects.py"] = '''\
"""Side-effect journal with per-request idempotency keys."""

from __future__ import annotations


class EffectJournal:
    """Records executed side effects and request outcomes, exactly once each."""

    def __init__(self) -> None:
        self._ran: dict[tuple[str, str], None] = {}
        self._intents: dict[str, str] = {}
        self._outcomes: dict[str, object] = {}

    def run(self, effect: str, request_id: str) -> bool:
        """Execute *effect* for *request_id* unless it already ran."""
        key = (effect, request_id)
        if key in self._ran:
            return False
        self._ran[key] = None
        return True

    def ran(self, effect: str, request_id: str) -> bool:
        return (effect, request_id) in self._ran

    def has_effect(self, effect: str) -> bool:
        return any(key[0] == effect for key in self._ran)

    def effects_for(self, request_id: str) -> tuple[str, ...]:
        return tuple(
            effect for effect, req in self._ran if req == request_id
        )

    def times_run(self, effect: str) -> int:
        return sum(1 for key in self._ran if key[0] == effect)

    def record_intent(self, request_id: str, action: str) -> None:
        self._intents[request_id] = action

    def intent_for(self, request_id: str) -> str | None:
        return self._intents.get(request_id)

    def record_outcome(self, request_id: str, outcome: object) -> None:
        self._outcomes[request_id] = outcome

    def outcome_for(self, request_id: str) -> object | None:
        return self._outcomes.get(request_id)
'''

FILES["statelock/guards.py"] = '''\
"""Transition guards verifying side-effect evidence before state changes."""

from __future__ import annotations


class GuardViolation(Exception):
    """A transition lacks the side-effect evidence required to complete."""


# Side effects that must already be journaled before the machine may enter
# a state. Entering "committed" is only legal if stock was actually
# allocated by a completed reservation transition.
REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "reserved": (),
    "committed": ("allocate_stock",),
}

# States from which each target may legitimately be entered (surface check).
ENTRY_STATES: dict[str, tuple[str, ...]] = {
    "reserved": ("reserving",),
    "committed": ("committing",),
}


class TransitionGuard:
    """Gates transitions on required side-effect evidence."""

    def __init__(
        self, requirements: dict[str, tuple[str, ...]] | None = None
    ) -> None:
        self._requirements = (
            dict(REQUIREMENTS) if requirements is None else dict(requirements)
        )

    def required_for(self, target_state: str) -> tuple[str, ...]:
        return self._requirements.get(target_state, ())

    # BUG: consults only the visible state label, never the effect journal,
    # so a machine whose intermediate side effects were skipped is waved
    # straight through to the terminal state.
    def check(self, target_state: str, current_state: str) -> None:
        allowed = ENTRY_STATES.get(target_state, ())
        if current_state not in allowed:
            raise GuardViolation(
                f"cannot enter {target_state} from {current_state}"
            )
'''

# BUG: retry() takes an unlocked fast-path that jumps straight to the target
# state. The intermediate state's required side effect never executes and
# the guard is never consulted, so an interrupted-and-retried reservation
# reaches "reserved" with no allocation evidence — and the weak guard then
# lets "commit" succeed anyway.
FILES["statelock/machine.py"] = '''\
"""Order-lock state machine with guarded transitions and retryable requests."""

from __future__ import annotations

from dataclasses import dataclass

from statelock.effects import EffectJournal
from statelock.guards import TransitionGuard


STATES = ("idle", "reserving", "reserved", "committing", "committed")

ACTION_START = {"reserve": "idle", "commit": "reserved"}
ACTION_INTERMEDIATE = {"reserve": "reserving", "commit": "committing"}
ACTION_TARGET = {"reserve": "reserved", "commit": "committed"}
ACTION_EFFECT = {"reserve": "allocate_stock", "commit": "finalize"}


class MachineError(Exception):
    """Raised for unknown actions or illegal state requests."""


@dataclass(frozen=True)
class Outcome:
    request_id: str
    action: str
    state: str


class StateMachine:
    """Processes reserve/commit requests with idempotent retries.

    ``interrupt()`` simulates a crash between states (used by recovery
    drills); ``retry()`` resumes such a request through the normal
    pipeline. Restored-from-snapshot instances may be constructed with a
    starting state and no journal history.
    """

    def __init__(
        self,
        guard: TransitionGuard | None = None,
        journal: EffectJournal | None = None,
        state: str = "idle",
    ) -> None:
        self._guard = guard if guard is not None else TransitionGuard()
        self._journal = journal if journal is not None else EffectJournal()
        if state not in STATES:
            raise MachineError(f"unknown state: {state}")
        self._state = state

    @property
    def state(self) -> str:
        return self._state

    @property
    def journal(self) -> EffectJournal:
        return self._journal

    def process(self, action: str, request_id: str) -> Outcome:
        replayed = self._journal.outcome_for(request_id)
        if replayed is not None:
            return replayed  # idempotent replay of a completed request
        return self._drive(action, request_id)

    def interrupt(self, action: str, request_id: str) -> None:
        """Crash-recovery seam: halt mid-transition like a killed worker."""
        if action not in ACTION_START:
            raise MachineError(f"unknown action: {action}")
        if self._state != ACTION_START[action]:
            raise MachineError(f"cannot interrupt {action} from {self._state}")
        self._journal.record_intent(request_id, action)
        self._state = ACTION_INTERMEDIATE[action]

    def retry(self, request_id: str) -> Outcome:
        replayed = self._journal.outcome_for(request_id)
        if replayed is not None:
            return replayed
        action = self._journal.intent_for(request_id)
        if action is None:
            raise MachineError(f"no interrupted request: {request_id}")
        # Unlocked fast-path: skip the pipeline entirely and land on the
        # target state, running neither its side effect nor its guard.
        self._state = ACTION_TARGET[action]
        outcome = Outcome(request_id=request_id, action=action,
                          state=self._state)
        self._journal.record_outcome(request_id, outcome)
        return outcome

    def _drive(self, action: str, request_id: str) -> Outcome:
        if action not in ACTION_START:
            raise MachineError(f"unknown action: {action}")
        if self._state != ACTION_START[action]:
            raise MachineError(f"cannot {action} from {self._state}")
        self._state = ACTION_INTERMEDIATE[action]
        self._guard.check(ACTION_TARGET[action], self._state)
        self._journal.run(ACTION_EFFECT[action], request_id)
        self._state = ACTION_TARGET[action]
        outcome = Outcome(request_id=request_id, action=action,
                          state=self._state)
        self._journal.record_outcome(request_id, outcome)
        return outcome
'''

FILES["tests/__init__.py"] = ""

FILES["tests/test_statelock.py"] = '''\
"""Public tests for guarded transitions and idempotent retries."""

import pytest

from statelock.effects import EffectJournal
from statelock.guards import GuardViolation
from statelock.machine import MachineError, StateMachine


def test_happy_path_runs_effects_once_each():
    machine = StateMachine()
    machine.process("reserve", "rq-1")
    machine.process("commit", "rq-2")
    assert machine.state == "committed"
    assert machine.journal.times_run("allocate_stock") == 1
    assert machine.journal.times_run("finalize") == 1
    assert machine.journal.ran("allocate_stock", "rq-1")
    assert machine.journal.ran("finalize", "rq-2")


def test_completed_request_replays_idempotently():
    machine = StateMachine()
    first = machine.process("reserve", "rq-1")
    second = machine.process("reserve", "rq-1")
    assert first == second
    assert machine.journal.times_run("allocate_stock") == 1
    assert machine.state == "reserved"


def test_retry_after_interrupt_still_allocates():
    machine = StateMachine()
    machine.interrupt("reserve", "rq-7")
    assert machine.state == "reserving"
    outcome = machine.retry("rq-7")
    assert outcome.state == "reserved"
    assert machine.journal.times_run("allocate_stock") == 1
    assert machine.journal.ran("allocate_stock", "rq-7")


def test_commit_requires_allocation_evidence():
    # Snapshot restored into "reserved" with NO journal history: committing
    # must be refused because allocation never demonstrably happened.
    machine = StateMachine(state="reserved")
    with pytest.raises(GuardViolation):
        machine.process("commit", "rq-9")
    assert machine.state == "reserved"
    assert machine.journal.times_run("finalize") == 0


def test_full_chain_after_recovery():
    machine = StateMachine()
    machine.interrupt("reserve", "rq-a")
    machine.retry("rq-a")
    machine.process("commit", "rq-b")
    assert machine.state == "committed"
    assert machine.journal.times_run("allocate_stock") == 1
    assert machine.journal.times_run("finalize") == 1


def test_unknown_action_rejected():
    machine = StateMachine()
    with pytest.raises(MachineError):
        machine.process("teleport", "rq-x")


def test_commit_from_idle_rejected():
    machine = StateMachine()
    with pytest.raises(MachineError):
        machine.process("commit", "rq-y")
    assert machine.state == "idle"


def test_interrupt_from_wrong_state_rejected():
    machine = StateMachine()
    with pytest.raises(MachineError):
        machine.interrupt("commit", "rq-z")
'''


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "statelock: guarded order-lock state machine", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
