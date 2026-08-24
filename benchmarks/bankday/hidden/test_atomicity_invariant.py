"""Hidden behavioral checks for ledger atomicity."""

from __future__ import annotations

import pytest

from bankday.ledger import Entry, Ledger, ValidationError


def test_self_transfer_rejected_and_stateless():
    ledger = Ledger()
    ledger.deposit("a", 10)
    with pytest.raises(ValidationError):
        ledger.transfer("a", "a", 5)
    assert ledger.balances["a"] == 10
    assert len(ledger.history) == 1


def test_transfer_to_unknown_target_creates_it_only_on_success():
    ledger = Ledger()
    ledger.deposit("a", 20)
    with pytest.raises(ValidationError):
        ledger.transfer("a", "x", 999)
    assert "x" not in ledger.balances
    ledger.transfer("a", "x", 5)
    assert ledger.balances["x"] == 5


def test_empty_batch_is_a_noop():
    ledger = Ledger()
    ledger.execute_batch([])
    assert ledger.balances == {} and ledger.history == []


def test_large_batch_rolls_back_to_exact_snapshot():
    ledger = Ledger()
    for name in ("u1", "u2", "u3"):
        ledger.deposit(name, 100)
    snapshot = dict(ledger.balances)
    entries = [Entry("transfer", "u1", "u2", 1), Entry("transfer", "u2", "u3", 1),
               Entry("transfer", "u3", "u1", 1)]
    entries.append(Entry("transfer", "nobody", "u1", 1))
    with pytest.raises(ValidationError):
        ledger.execute_batch(entries)
    assert ledger.balances == snapshot


def test_history_records_only_committed_transfers():
    ledger = Ledger()
    ledger.deposit("a", 30)
    ok = [Entry("transfer", "a", "b", 10)]
    bad = [Entry("transfer", "a", "c", 10), Entry("transfer", "missing", "z", 1)]
    ledger.execute_batch(ok)
    with pytest.raises(ValidationError):
        ledger.execute_batch(bad)
    transfers = [e for e in ledger.history if e.kind == "transfer"]
    assert len(transfers) == 1
    assert (transfers[0].source, transfers[0].target) == ("a", "b")


def test_fuzz_invariant_total_preserved():
    import itertools
    ledger = Ledger()
    ledger.deposit("p", 77)
    ledger.deposit("q", 13)
    baseline = ledger.total()
    amounts = (1, 5, 50, 500)
    for a, b in itertools.product(("p", "q"), repeat=2):
        for amount in amounts:
            try:
                ledger.transfer(a, b, amount)
            except ValidationError:
                pass
            assert ledger.total() == baseline
