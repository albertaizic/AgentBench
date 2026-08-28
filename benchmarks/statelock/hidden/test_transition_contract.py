"""Hidden behavioral checks for statelock (different request sequences)."""

from __future__ import annotations

import pytest

from statelock.effects import EffectJournal
from statelock.guards import GuardViolation
from statelock.machine import MachineError, StateMachine


def test_interrupted_commit_retry_finalizes_once():
    machine = StateMachine()
    machine.process("reserve", "op-10")
    machine.interrupt("commit", "op-11")
    assert machine.state == "committing"
    outcome = machine.retry("op-11")
    assert outcome.state == "committed"
    assert machine.journal.times_run("finalize") == 1
    assert machine.journal.ran("finalize", "op-11")


def test_repeated_replays_never_add_effects():
    machine = StateMachine()
    machine.process("reserve", "op-20")
    machine.process("commit", "op-21")
    baseline = (
        machine.journal.times_run("allocate_stock"),
        machine.journal.times_run("finalize"),
    )
    for _ in range(3):
        machine.retry("op-20")
        machine.process("reserve", "op-20")
        machine.retry("op-21")
    assert (
        machine.journal.times_run("allocate_stock"),
        machine.journal.times_run("finalize"),
    ) == baseline == (1, 1)


def test_retry_unknown_request_raises_without_mutation():
    machine = StateMachine()
    with pytest.raises(MachineError):
        machine.retry("ghost-1")
    assert machine.state == "idle"
    assert machine.journal.outcome_for("ghost-1") is None


def test_guard_refusal_leaves_journal_untouched():
    machine = StateMachine(state="reserved")
    with pytest.raises(GuardViolation):
        machine.process("commit", "op-30")
    assert machine.journal.times_run("finalize") == 0
    assert machine.journal.outcome_for("op-30") is None
    assert machine.journal.effects_for("op-30") == ()
    assert machine.state == "reserved"


def test_independent_machines_do_not_share_evidence():
    isolated = StateMachine(state="reserved", journal=EffectJournal())
    with pytest.raises(GuardViolation):
        isolated.process("commit", "op-40")

    witness = StateMachine()
    witness.process("reserve", "op-41")
    # The other machine's allocation must not satisfy this one's guard.
    fresh = StateMachine(state="reserved", journal=EffectJournal())
    with pytest.raises(GuardViolation):
        fresh.process("commit", "op-42")


def test_process_commit_during_reserving_is_illegal():
    machine = StateMachine()
    machine.interrupt("reserve", "op-50")
    with pytest.raises(MachineError):
        machine.process("commit", "op-51")
    assert machine.state == "reserving"
    assert machine.journal.times_run("finalize") == 0


def test_recovery_chain_with_multiple_interruptions():
    machine = StateMachine()
    machine.interrupt("reserve", "op-60")
    machine.retry("op-60")
    machine.interrupt("commit", "op-61")
    machine.retry("op-61")
    machine.retry("op-61")  # extra replay after completion
    assert machine.state == "committed"
    assert machine.journal.times_run("allocate_stock") == 1
    assert machine.journal.times_run("finalize") == 1
    first = machine.retry("op-60")
    assert first.state == "reserved"
    assert first == machine.journal.outcome_for("op-60")


def test_outcomes_are_frozen_records():
    machine = StateMachine()
    outcome = machine.process("reserve", "op-70")
    with pytest.raises(Exception):
        outcome.state = "idle"
