"""Hidden behavioral checks for ledgerpad validation."""

from __future__ import annotations

import pytest

from ledgerpad.tracker import ExpenseTracker


@pytest.mark.parametrize("amount", [0, -1, -9999])
def test_invalid_amounts_rejected_and_ignored(amount):
    tracker = ExpenseTracker()
    tracker.add_expense("coffee", 450)
    with pytest.raises(ValueError):
        tracker.add_expense("bad", amount)
    assert len(tracker.expenses) == 1
    assert tracker.total_cents() == 450


def test_unknown_currency_rejected_case_insensitively():
    tracker = ExpenseTracker()
    with pytest.raises(ValueError):
        tracker.add_expense("abroad", 100, currency="btc")
    with pytest.raises(ValueError):
        tracker.add_expense("abroad", 100, currency="Yen")


def test_lowercase_valid_currency_is_accepted_and_normalized():
    # Case-INSENSITIVE comparison means a valid code in any casing is
    # accepted (and normalized), not merely not-rejected. Catches
    # "fixes" that drop the case-folding entirely.
    tracker = ExpenseTracker()
    tracker.add_expense("abroad", 250, currency="eur")
    assert tracker.expenses[-1].currency == "EUR"
    assert tracker.total_cents("EUR") == 250

    mixed = ExpenseTracker()
    mixed.add_expense("cafe", 75, currency="Eur")
    assert mixed.expenses[-1].currency == "EUR"
