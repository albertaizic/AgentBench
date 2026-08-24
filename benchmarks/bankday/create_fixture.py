"""Deterministic generator for the bankday fixture (ledger atomicity task)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "bankday"\nversion = "0.1.0"\n',
    # BUGS: debit-before-validate in transfer(); no rollback in batches;
    # history records failed operations.
    "bankday/ledger.py": (
        '"""In-memory double-entry ledger."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from dataclasses import dataclass, field\n\n\n'
        'class LedgerError(Exception):\n'
        '    pass\n\n\n'
        'class ValidationError(LedgerError):\n'
        '    pass\n\n\n'
        '@dataclass\nclass Entry:\n'
        '    kind: str          # "deposit" | "transfer"\n'
        '    source: str | None\n'
        '    target: str | None\n'
        '    amount: int\n\n'
        '@dataclass\nclass Batch:\n'
        '    entries: list[Entry] = field(default_factory=list)\n'
        '    committed: bool = False\n\n\n'
        'class Ledger:\n'
        '    def __init__(self) -> None:\n'
        '        self.balances: dict[str, int] = {}\n'
        '        self.history: list[Entry] = []\n\n'
        '    def deposit(self, account: str, amount: int) -> None:\n'
        '        if amount <= 0:\n'
        '            raise ValidationError("amount must be positive")\n'
        '        self.balances[account] = self.balances.get(account, 0) + amount\n'
        '        self.history.append(Entry("deposit", None, account, amount))\n\n'
        '    def transfer(self, source: str, target: str, amount: int) -> None:\n'
        '        # BUG: debits BEFORE validating; failures never undo the debit.\n'
        '        if amount <= 0:\n'
        '            raise ValidationError("amount must be positive")\n'
        '        self.balances[source] = self.balances.get(source, 0) - amount\n'
        '        if target == source:\n'
        '            raise ValidationError("source and target must differ")\n'
        '        if self.balances.get(source, 0) < 0:\n'
        '            raise ValidationError("insufficient funds")\n'
        '        self.balances[target] = self.balances.get(target, 0) + amount\n'
        '        self.history.append(Entry("transfer", source, target, amount))\n\n'
        '    def execute_batch(self, entries: list[Entry]) -> None:\n'
        '        # BUG: no rollback - a mid-batch failure leaves earlier steps.\n'
        '        for entry in entries:\n'
        '            if entry.kind == "deposit":\n'
        '                self.deposit(entry.target, entry.amount)\n'
        '            else:\n'
        '                self.transfer(entry.source, entry.target, entry.amount)\n\n'
        '    def total(self) -> int:\n'
        '        return sum(self.balances.values())\n'
    ),
    "tests/test_ledger.py": (
        '"""Public tests for ledger correctness."""\n\n'
        'import pytest\n\n'
        'from bankday.ledger import (Batch, Entry, Ledger,\n'
        '                            ValidationError)\n\n\n'
        'def test_deposit_then_transfer_moves_money():\n'
        '    ledger = Ledger()\n'
        '    ledger.deposit("alice", 100)\n'
        '    ledger.transfer("alice", "bob", 40)\n'
        '    assert ledger.balances["alice"] == 60\n'
        '    assert ledger.balances["bob"] == 40\n\n'
        'def test_failed_transfer_keeps_source_intact():\n'
        '    ledger = Ledger()\n'
        '    ledger.deposit("alice", 30)\n'
        '    with pytest.raises(ValidationError):\n'
        '        ledger.transfer("alice", "bob", 999)\n'
        '    assert ledger.balances["alice"] == 30\n'
        '    assert "bob" not in ledger.balances or ledger.balances["bob"] == 0\n\n'
        'def test_batch_is_atomic_on_failure():\n'
        '    ledger = Ledger()\n'
        '    ledger.deposit("alice", 100)\n'
        '    batch = Batch(entries=[\n'
        '        Entry("transfer", "alice", "bob", 10),\n'
        '        Entry("deposit", None, "carol", -5),   # invalid: negative\n'
        '        Entry("transfer", "alice", "dave", 10),\n'
        '    ])\n'
        '    with pytest.raises(ValidationError):\n'
        '        ledger.execute_batch(batch.entries)\n'
        '    assert ledger.balances["alice"] == 100\n'
        '    assert "bob" not in ledger.balances or ledger.balances["bob"] == 0\n\n'
        'def test_rolled_back_batch_leaves_no_history():\n'
        '    ledger = Ledger()\n'
        '    ledger.deposit("alice", 50)\n'
        '    entries = [Entry("transfer", "alice", "bob", 5),\n'
        '               Entry("transfer", "ghost", "bob", 5)]\n'
        '    with pytest.raises(ValidationError):\n'
        '        ledger.execute_batch(entries)\n'
        '    assert len(ledger.history) == 1      # only the initial deposit\n\n'
        'def test_total_invariant_after_mixed_failures():\n'
        '    ledger = Ledger()\n'
        '    ledger.deposit("a", 60)\n'
        '    ledger.deposit("b", 20)\n'
        '    baseline = ledger.total()\n'
        '    for entries in (\n'
        '        [Entry("transfer", "a", "b", 500)],\n'
        '        [Entry("transfer", "a", "b", 1), Entry("transfer", "zz", "a", 1)],\n'
        '    ):\n'
        '        try:\n'
        '            ledger.execute_batch(entries)\n'
        '        except ValidationError:\n'
        '            pass\n'
        '    assert ledger.total() == baseline\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "bankday: double-entry ledger", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
