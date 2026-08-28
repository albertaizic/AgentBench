"""Hidden behavioral checks for txnrollback batch atomicity (different data)."""

from __future__ import annotations

import pytest

from txnrollback.audit import AuditLog
from txnrollback.ledger import Ledger, deposit, transfer, withdraw
from txnrollback.notifier import NotificationOutbox
from txnrollback.transactions import BatchError, TransactionBatch


@pytest.fixture()
def payroll_env():
    ledger = Ledger()
    ledger.open_account("payroll-main", 500_000)
    ledger.open_account("tax-escrow", 120_000)
    ledger.open_account("vendor-float", 75_000)
    return ledger, AuditLog(), NotificationOutbox()


def make_batch(env, batch_id):
    ledger, audit, outbox = env
    return TransactionBatch(ledger, audit, outbox, batch_id)


def test_payroll_batch_audit_order_matches_operation_order(payroll_env):
    ledger, audit, outbox = payroll_env
    batch = make_batch(payroll_env, "pr-2026-06")
    batch.add(withdraw("payroll-main", 60_000))
    batch.add(deposit("tax-escrow", 15_000))
    batch.add(transfer("payroll-main", "vendor-float", 9_000))
    receipt = batch.commit()
    assert receipt.applied == 3
    details = [r.detail for r in audit.posts_for("pr-2026-06")]
    assert details == [
        "withdraw 60000c from payroll-main",
        "deposit 15000c into tax-escrow",
        "transfer 9000c from payroll-main to vendor-float",
    ]
    seqs = [r.seq for r in audit.records()]
    assert seqs == sorted(seqs)
    assert len(outbox.deliverable("pr-2026-06")) == 3


def test_unknown_counterparty_mid_batch_is_fully_aborted(payroll_env):
    ledger, audit, outbox = payroll_env
    batch = make_batch(payroll_env, "pr-2026-07")
    batch.add(transfer("payroll-main", "tax-escrow", 20_000))
    batch.add(transfer("tax-escrow", "nonexistent", 5_000))
    with pytest.raises(BatchError):
        batch.commit()
    assert ledger.balance("payroll-main") == 500_000
    assert ledger.balance("tax-escrow") == 120_000
    assert ledger.balance("vendor-float") == 75_000
    assert audit.posts_for("pr-2026-07") == []
    assert len(audit.reversals_for("pr-2026-07")) >= 1
    assert outbox.deliverable() == []


def test_failed_batch_does_not_poison_following_batches(payroll_env):
    ledger, audit, outbox = payroll_env

    bad = make_batch(payroll_env, "pr-bad")
    bad.add(withdraw("vendor-float", 10_000)).add(
        withdraw("payroll-main", 10 ** 12)
    )
    with pytest.raises(BatchError):
        bad.commit()

    good = make_batch(payroll_env, "pr-good")
    good.add(deposit("vendor-float", 25_000))
    good.commit()

    assert ledger.balance("vendor-float") == 100_000
    assert len(audit.posts_for("pr-good")) == 1
    assert audit.posts_for("pr-bad") == []
    assert [n.status for n in outbox.all() if n.batch_id == "pr-bad"].count(
        "queued"
    ) == 0


def test_double_commit_rejected_without_duplicate_records(payroll_env):
    ledger, audit, outbox = payroll_env
    batch = make_batch(payroll_env, "pr-twice")
    batch.add(withdraw("payroll-main", 1_000))
    batch.commit()
    with pytest.raises(BatchError):
        batch.commit()
    assert ledger.balance("payroll-main") == 499_000
    assert len(audit.posts_for("pr-twice")) == 1
    assert len(outbox.deliverable("pr-twice")) == 1


def test_self_transfer_aborts_cleanly(payroll_env):
    ledger, audit, outbox = payroll_env
    batch = make_batch(payroll_env, "pr-self")
    batch.add(transfer("tax-escrow", "tax-escrow", 1_000))
    with pytest.raises(BatchError):
        batch.commit()
    assert ledger.balance("tax-escrow") == 120_000
    assert audit.posts_for("pr-self") == []
    assert outbox.deliverable() == []


def test_intra_batch_funds_chain_respected(payroll_env):
    # The second withdrawal relies on the first deposit landing first; the
    # validator must simulate running balances, not check against stale ones.
    ledger, audit, outbox = payroll_env
    batch = make_batch(payroll_env, "pr-chain")
    batch.add(deposit("vendor-float", 50_000))
    batch.add(withdraw("vendor-float", 60_000))
    batch.commit()
    assert ledger.balance("vendor-float") == 65_000
