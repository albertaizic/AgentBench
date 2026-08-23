"""Deterministic generator for the ledgerpad fixture (validation bugfix task)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "ledgerpad"\nversion = "0.1.0"\n',
    "ledgerpad/__init__.py": (
        'from ledgerpad.tracker import ExpenseTracker, Expense\n\n'
        '__all__ = ["Expense", "ExpenseTracker"]\n'
    ),
    # BUG: add_expense accepts amount <= 0 and unknown currency codes.
    "ledgerpad/tracker.py": (
        '"""Expense tracking with per-currency totals."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from dataclasses import dataclass\n\n\n'
        'VALID_CURRENCIES = {"USD", "EUR", "GBP"}\n\n\n'
        '@dataclass\nclass Expense:\n    merchant: str\n    amount_cents: int\n    currency: str\n\n\n'
        'class ExpenseTracker:\n    def __init__(self) -> None:\n'
        '        self.expenses: list[Expense] = []\n\n'
        '    def add_expense(self, merchant: str, amount_cents: int, currency: str = "USD") -> Expense:\n'
        '        expense = Expense(merchant=merchant, amount_cents=amount_cents, currency=currency)\n'
        '        self.expenses.append(expense)\n'
        '        return expense\n\n'
        '    def total_cents(self, currency: str = "USD") -> int:\n'
        '        return sum(e.amount_cents for e in self.expenses if e.currency == currency)\n'
    ),
    "tests/test_tracker.py": (
        '"""Public tests for the expense tracker."""\n\n'
        'import pytest\n\n'
        'from ledgerpad.tracker import ExpenseTracker\n\n\n'
        'def test_add_and_total():\n'
        '    tracker = ExpenseTracker()\n'
        '    tracker.add_expense("coffee", 450)\n'
        '    tracker.add_expense("lunch", 1250)\n'
        '    assert tracker.total_cents() == 1700\n\n'
        'def test_totals_are_per_currency():\n'
        '    tracker = ExpenseTracker()\n'
        '    tracker.add_expense("coffee", 450)\n'
        '    tracker.add_expense("train", 3000, currency="EUR")\n'
        '    assert tracker.total_cents() == 450\n'
        '    assert tracker.total_cents("EUR") == 3000\n\n'
        'def test_rejects_non_positive_amount():\n'
        '    tracker = ExpenseTracker()\n'
        '    with pytest.raises(ValueError):\n'
        '        tracker.add_expense("refund-gone-wrong", 0)\n\n'
        'def test_rejects_negative_amount():\n'
        '    tracker = ExpenseTracker()\n'
        '    with pytest.raises(ValueError):\n'
        '        tracker.add_expense("typo", -500)\n\n'
        'def test_rejects_unknown_currency():\n'
        '    tracker = ExpenseTracker()\n'
        '    with pytest.raises(ValueError):\n'
        '        tracker.add_expense("abroad", 100, currency="BTC")\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "ledgerpad: expense tracker", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
