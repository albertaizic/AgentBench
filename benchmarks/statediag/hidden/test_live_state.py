"""Hidden contract: live alphabet view across modules."""
from machines.controller import PatternController
from machines.tape import Tape


def test_new_symbols_validate_after_reconfiguration():
    tape = Tape(frozenset("ab"))
    controller = PatternController(tape)
    controller.register("either", "ok")

    # Widen the alphabet first; transitions for new symbols come after.
    tape.set_alphabet(frozenset("abcd"))
    tape.add_transition("start", "a", "ok")
    tape.add_transition("start", "c", "ok")
    tape.mark_accepting("ok")

    assert controller.validate("either", "a")
    assert controller.validate("either", "c")


def test_removed_symbols_stop_validating_after_reconfiguration():
    tape = Tape(frozenset("ab"))
    tape.add_transition("start", "a", "ok")
    tape.mark_accepting("ok")
    controller = PatternController(tape)
    controller.register("only-a", "ok")

    controller.tape.set_alphabet(frozenset("aXYZ"))

    assert controller.validate("only-a", "a") is True
    assert controller.validate("only-a", "b") is False


def test_transitions_added_after_register_are_honored():
    tape = Tape(frozenset("ab"))
    controller = PatternController(tape)
    controller.register("grow", "done")
    tape.add_transition("start", "a", "mid")
    tape.add_transition("mid", "b", "done")
    tape.mark_accepting("done")

    assert controller.validate("grow", "ab") is True
