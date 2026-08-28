"""Deterministic generator for the txnrollback fixture (batch atomicity bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {}
FILES[".gitignore"] = "__pycache__/\n*.pyc\n.pytest_cache/\n"
FILES["pyproject.toml"] = '[project]\nname = "txnrollback"\nversion = "1.3.0"\n'

FILES["txnrollback/__init__.py"] = ""

FILES["txnrollback/ledger.py"] = '''\
"""Cash-position ledger with reversible entries."""

from __future__ import annotations

from dataclasses import dataclass


class LedgerError(ValueError):
    """Raised when an operation violates ledger invariants."""


@dataclass(frozen=True)
class Operation:
    kind: str  # "deposit" | "withdraw" | "transfer"
    account: str
    destination: str | None
    amount_cents: int

    def describe(self) -> str:
        if self.kind == "deposit":
            return f"deposit {self.amount_cents}c into {self.account}"
        if self.kind == "withdraw":
            return f"withdraw {self.amount_cents}c from {self.account}"
        return (
            f"transfer {self.amount_cents}c from {self.account}"
            f" to {self.destination}"
        )


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    account: str
    destination: str | None
    amount_cents: int


def deposit(account: str, amount_cents: int) -> Operation:
    return Operation("deposit", account, None, amount_cents)


def withdraw(account: str, amount_cents: int) -> Operation:
    return Operation("withdraw", account, None, amount_cents)


def transfer(source: str, destination: str, amount_cents: int) -> Operation:
    return Operation("transfer", source, destination, amount_cents)


class Ledger:
    """Account balances mutated only through validated entries."""

    def __init__(self) -> None:
        self._balances: dict[str, int] = {}
        self._entries: list[Entry] = []
        self._next_seq = 1

    def open_account(self, name: str, opening_balance_cents: int = 0) -> None:
        if name in self._balances:
            raise LedgerError(f"account already open: {name}")
        if opening_balance_cents < 0:
            raise LedgerError("opening balance cannot be negative")
        self._balances[name] = opening_balance_cents

    def balance(self, name: str) -> int:
        if name not in self._balances:
            raise LedgerError(f"unknown account: {name}")
        return self._balances[name]

    def accounts(self) -> tuple[str, ...]:
        return tuple(sorted(self._balances))

    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def apply(self, op: Operation) -> Entry:
        if not isinstance(op.amount_cents, int) or isinstance(
            op.amount_cents, bool
        ) or op.amount_cents <= 0:
            raise LedgerError(f"{op.describe()}: amount must be a positive integer")
        if op.kind == "deposit":
            self._require(op.account)
            self._balances[op.account] += op.amount_cents
        elif op.kind == "withdraw":
            self._require_funds(op.account, op.amount_cents)
            self._balances[op.account] -= op.amount_cents
        elif op.kind == "transfer":
            if op.destination is None or op.destination == op.account:
                raise LedgerError("transfer needs a distinct destination account")
            self._require_funds(op.account, op.amount_cents)
            self._require(op.destination)
            self._balances[op.account] -= op.amount_cents
            self._balances[op.destination] += op.amount_cents
        else:
            raise LedgerError(f"unknown operation kind: {op.kind}")
        entry = Entry(
            self._next_seq, op.kind, op.account, op.destination, op.amount_cents
        )
        self._next_seq += 1
        self._entries.append(entry)
        return entry

    def reverse(self, entry: Entry) -> Entry:
        """Compensating mutation undoing a previously applied entry."""
        if entry.kind == "deposit":
            self._balances[entry.account] -= entry.amount_cents
        elif entry.kind == "withdraw":
            self._balances[entry.account] += entry.amount_cents
        elif entry.kind == "transfer":
            self._balances[entry.account] += entry.amount_cents
            self._balances[entry.destination] -= entry.amount_cents
        else:
            raise LedgerError(f"cannot reverse entry kind: {entry.kind}")
        mirrored = Entry(
            self._next_seq,
            "reverse:" + entry.kind,
            entry.account,
            entry.destination,
            entry.amount_cents,
        )
        self._next_seq += 1
        self._entries.append(mirrored)
        return mirrored

    def _require(self, name: str) -> None:
        if name not in self._balances:
            raise LedgerError(f"unknown account: {name}")

    def _require_funds(self, name: str, amount_cents: int) -> None:
        self._require(name)
        if self._balances[name] < amount_cents:
            raise LedgerError(
                f"insufficient funds in {name}: "
                f"{self._balances[name]} < {amount_cents}"
            )
'''

FILES["txnrollback/audit.py"] = '''\
"""Append-only audit trail with compensating reversal records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    batch_id: str
    kind: str  # "post" | "reverse"
    detail: str


class AuditLog:
    """Sequential audit trail; auditors reconcile posts against reversals."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._next_seq = 1

    def record_post(self, batch_id: str, detail: str) -> AuditRecord:
        rec = AuditRecord(self._next_seq, batch_id, "post", detail)
        self._next_seq += 1
        self._records.append(rec)
        return rec

    def record_reversal(self, batch_id: str, reason: str) -> AuditRecord:
        rec = AuditRecord(self._next_seq, batch_id, "reverse", reason)
        self._next_seq += 1
        self._records.append(rec)
        return rec

    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._records)

    def posts_for(self, batch_id: str) -> list[AuditRecord]:
        return [
            r for r in self._records
            if r.batch_id == batch_id and r.kind == "post"
        ]

    def reversals_for(self, batch_id: str) -> list[AuditRecord]:
        return [
            r for r in self._records
            if r.batch_id == batch_id and r.kind == "reverse"
        ]
'''

FILES["txnrollback/notifier.py"] = '''\
"""Customer notification outbox keyed by transaction batch."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Notification:
    batch_id: str
    message: str
    status: str  # "queued" | "cancelled"


class NotificationOutbox:
    """Notifications become customer-visible only while they stay queued."""

    def __init__(self) -> None:
        self._items: list[Notification] = []

    def enqueue(self, batch_id: str, message: str) -> Notification:
        item = Notification(batch_id=batch_id, message=message, status="queued")
        self._items.append(item)
        return item

    def cancel_batch(self, batch_id: str) -> int:
        cancelled = 0
        for index, item in enumerate(self._items):
            if item.batch_id == batch_id and item.status == "queued":
                self._items[index] = Notification(
                    batch_id=item.batch_id, message=item.message,
                    status="cancelled",
                )
                cancelled += 1
        return cancelled

    def deliverable(self, batch_id: str | None = None) -> list[Notification]:
        return [
            item for item in self._items
            if item.status == "queued"
            and (batch_id is None or item.batch_id == batch_id)
        ]

    def all(self) -> tuple[Notification, ...]:
        return tuple(self._items)
'''

# BUG: commit() applies each operation to the ledger immediately and writes
# its audit/outbox records BEFORE the remaining operations are validated.
# A failure halfway through rolls the ledger back but leaves posted audit
# rows and queued notifications describing work that never happened.
FILES["txnrollback/transactions.py"] = '''\
"""Batch transaction orchestration with all-or-nothing commit."""

from __future__ import annotations

from dataclasses import dataclass

from txnrollback.audit import AuditLog
from txnrollback.ledger import Ledger, LedgerError, Operation
from txnrollback.notifier import NotificationOutbox


class BatchError(Exception):
    """Raised when a batch cannot be committed atomically."""


@dataclass(frozen=True)
class Receipt:
    batch_id: str
    applied: int


class TransactionBatch:
    """A staged set of operations committed atomically to one ledger."""

    def __init__(
        self,
        ledger: Ledger,
        audit_log: AuditLog,
        outbox: NotificationOutbox,
        batch_id: str,
    ) -> None:
        self._ledger = ledger
        self._audit = audit_log
        self._outbox = outbox
        self.batch_id = batch_id
        self._ops: list[Operation] = []
        self.committed = False

    def add(self, op: Operation) -> "TransactionBatch":
        if self.committed:
            raise BatchError("batch already committed")
        self._ops.append(op)
        return self

    @property
    def size(self) -> int:
        return len(self._ops)

    def commit(self) -> Receipt:
        applied: list = []
        try:
            for op in self._ops:
                entry = self._ledger.apply(op)
                self._audit.record_post(self.batch_id, op.describe())
                self._outbox.enqueue(self.batch_id, op.describe())
                applied.append(entry)
        except LedgerError as exc:
            for entry in reversed(applied):
                self._ledger.reverse(entry)
            raise BatchError(str(exc)) from exc
        self.committed = True
        return Receipt(batch_id=self.batch_id, applied=len(applied))
'''

FILES["tests/__init__.py"] = ""

FILES["tests/test_batches.py"] = '''\
"""Public tests for batch atomicity across ledger, audit, and outbox."""

import pytest

from txnrollback.audit import AuditLog
from txnrollback.ledger import Ledger, deposit, transfer, withdraw
from txnrollback.notifier import NotificationOutbox
from txnrollback.transactions import BatchError, TransactionBatch


@pytest.fixture()
def env():
    ledger = Ledger()
    ledger.open_account("checking", 100_000)
    ledger.open_account("savings", 40_000)
    return ledger, AuditLog(), NotificationOutbox()


def make_batch(env, batch_id):
    ledger, audit, outbox = env
    return TransactionBatch(ledger, audit, outbox, batch_id)


def test_successful_batch_posts_audits_and_queues_notifications(env):
    ledger, audit, outbox = env
    batch = make_batch(env, "b-100")
    batch.add(withdraw("savings", 5_000)).add(deposit("checking", 2_500))
    receipt = batch.commit()
    assert receipt.applied == 2
    assert ledger.balance("checking") == 102_500
    assert ledger.balance("savings") == 35_000
    assert len(audit.posts_for("b-100")) == 2
    assert len(outbox.deliverable("b-100")) == 2


def test_failure_on_last_operation_leaves_balances_untouched(env):
    ledger, audit, outbox = env
    batch = make_batch(env, "b-101")
    batch.add(withdraw("savings", 10_000))
    batch.add(transfer("checking", "savings", 999_999))
    with pytest.raises(BatchError):
        batch.commit()
    assert ledger.balance("checking") == 100_000
    assert ledger.balance("savings") == 40_000


def test_aborted_batch_leaves_no_posted_audit_rows(env):
    ledger, audit, outbox = env
    batch = make_batch(env, "b-102")
    batch.add(deposit("checking", 1_000))
    batch.add(withdraw("savings", 10 ** 9))
    with pytest.raises(BatchError):
        batch.commit()
    assert audit.posts_for("b-102") == []
    assert len(audit.reversals_for("b-102")) >= 1


def test_aborted_batch_delivers_nothing(env):
    ledger, audit, outbox = env
    batch = make_batch(env, "b-103")
    batch.add(deposit("checking", 500)).add(withdraw("ghost", 10))
    with pytest.raises(BatchError):
        batch.commit()
    assert outbox.deliverable() == []


def test_invalid_amount_anywhere_aborts_before_mutation(env):
    ledger, audit, outbox = env
    batch = make_batch(env, "b-104")
    batch.add(deposit("checking", 700))
    batch.add(withdraw("savings", 0))
    with pytest.raises(BatchError):
        batch.commit()
    assert ledger.balance("checking") == 100_000
    assert ledger.balance("savings") == 40_000
    assert audit.posts_for("b-104") == []
    assert outbox.deliverable() == []


def test_empty_batch_commits_cleanly(env):
    ledger, audit, outbox = env
    batch = make_batch(env, "b-105")
    receipt = batch.commit()
    assert receipt.applied == 0
    assert audit.records() == ()
    assert outbox.all() == ()


def test_committed_batch_rejects_new_operations(env):
    batch = make_batch(env, "b-106")
    batch.add(deposit("checking", 100))
    batch.commit()
    with pytest.raises(BatchError):
        batch.add(deposit("checking", 200))
'''


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "txnrollback: batch transaction engine", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
