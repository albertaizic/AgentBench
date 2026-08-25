"""Deterministic generator for the statediag fixture (stale-state bugfix)."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, pin_commit  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

TAPE_BROKEN = '''"""Word recognizers over a mutable alphabet."""

from __future__ import annotations


class Tape:
    """A tiny FSM: transitions keyed by (state, symbol)."""

    def __init__(self, alphabet: frozenset[str] | None = None) -> None:
        self.alphabet = frozenset(alphabet or ())
        self.accepting: set[str] = set()
        self._transitions: dict[tuple[str, str], str] = {}

    def set_alphabet(self, alphabet: frozenset[str]) -> None:
        # BUG: callers holding a compiled view keep the OLD symbols.
        self.alphabet = frozenset(alphabet)

    def add_transition(self, state: str, symbol: str, nxt: str) -> None:
        if symbol not in self.alphabet:
            raise ValueError(f"symbol {symbol!r} not in alphabet")
        self._transitions[(state, symbol)] = nxt

    def mark_accepting(self, *states: str) -> None:
        self.accepting.update(states)

    def accepts(self, word: str) -> bool:
        state = "start"
        for ch in word:
            nxt = self._transitions.get((state, ch))
            if nxt is None:
                return False
            state = nxt
        return state in self.accepting
'''

TAPE_FIXED = TAPE_BROKEN.replace(
    "    def set_alphabet(self, alphabet: frozenset[str]) -> None:\n"
    "        # BUG: callers holding a compiled view keep the OLD symbols.\n"
    "        self.alphabet = frozenset(alphabet)\n",
    "    def set_alphabet(self, alphabet: frozenset[str]) -> None:\n"
    "        self.alphabet = frozenset(alphabet)\n",
)

CONTROLLER_BROKEN = '''"""Named pattern controllers built on top of Tape recognizers."""

from __future__ import annotations

from .tape import Tape


class UnknownPattern(KeyError):
    pass


class PatternController:
    """Validates words against named patterns backed by a shared Tape."""

    def __init__(self, tape: Tape) -> None:
        self.tape = tape
        self.patterns: dict[str, str] = {}  # name -> accepting state

    def register(self, name: str, accepting_state: str) -> None:
        # BUG: the allowed-symbol list is snapshotted here; later
        # tape.set_alphabet() calls silently leave this stale, so the
        # controller rejects (or accepts) symbols that no longer match the
        # machine the user actually configured.
        self.patterns[name] = accepting_state
        self.allowed_symbols = sorted(tape.alphabet)

    def validate(self, name: str, word: str) -> bool:
        if name not in self.patterns:
            raise UnknownPattern(name)
        if any(ch not in self.allowed_symbols for ch in word):
            return False
        return self.tape.accepts(word)
'''

CONTROLLER_FIXED = CONTROLLER_BROKEN.replace(
    "    def register(self, name: str, accepting_state: str) -> None:\n"
    "        # BUG: the allowed-symbol list is snapshotted here; later\n"
    "        # tape.set_alphabet() calls silently leave this stale, so the\n"
    "        # controller rejects (or accepts) symbols that no longer match the\n"
    "        # machine the user actually configured.\n"
    "        self.patterns[name] = accepting_state\n"
    "        self.allowed_symbols = sorted(tape.alphabet)\n",
    "    def register(self, name: str, accepting_state: str) -> None:\n"
    "        self.patterns[name] = accepting_state\n",
).replace(
    "        if any(ch not in self.allowed_symbols for ch in word):\n",
    "        if any(ch not in self.tape.alphabet for ch in word):\n",
)

PUBLIC_TESTS = '''"""Public tests for pattern controllers."""

import pytest

from machines.controller import PatternController, UnknownPattern
from machines.tape import Tape


@pytest.fixture
def controller():
    tape = Tape(frozenset("ab"))
    tape.add_transition("start", "a", "seen_a")
    tape.mark_accepting("seen_a")
    controller = PatternController(tape)
    controller.register("one-a", "seen_a")
    return controller


def test_registered_pattern_accepts(controller):
    assert controller.validate("one-a", "a")


def test_rejects_word_outside_pattern(controller):
    assert not controller.validate("one-a", "b")
    assert not controller.validate("one-a", "aa")


def test_unknown_pattern_raises(controller):
    with pytest.raises(UnknownPattern):
        controller.validate("nope", "a")


def test_alphabet_reconfiguration_takes_effect(controller):
    controller.tape.set_alphabet(frozenset("abc"))
    assert controller.validate("one-a", "c") is False
'''

HIDDEN_TESTS = '''"""Hidden contract: live alphabet view across modules."""

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
'''


def unified(broken: str, fixed: str, name: str) -> str:
    diff = difflib.unified_diff(
        broken.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"a/machines/{name}",
        tofile=f"b/machines/{name}",
    )
    return "".join(diff)


def main() -> int:
    files = {
        ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
        "pyproject.toml": '[project]\nname = "statediag"\nversion = "0.1.0"\n',
        "machines/__init__.py": "",
        "machines/tape.py": TAPE_BROKEN,
        "machines/controller.py": CONTROLLER_BROKEN,
        "tests/test_controller.py": PUBLIC_TESTS,
    }
    sha = create_fixture_repo(FIXTURE_DIR, files, "statediag: recognizer + controller")
    patch_dir = Path(__file__).parent / "reference"
    patch_dir.mkdir(exist_ok=True)
    patch = (
        unified(TAPE_BROKEN, TAPE_FIXED, "tape.py")
        + unified(CONTROLLER_BROKEN, CONTROLLER_FIXED, "controller.py")
    )
    (patch_dir / "fix.patch").write_text(patch, encoding="utf-8")
    print(f"fixture repository created at {FIXTURE_DIR}")
    print(f"commit: {sha}")
    pin_commit(YAML_PATH, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
