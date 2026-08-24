"""Hidden behavioral checks for the token bucket."""

from __future__ import annotations

import pytest

from tokenbucket.limiter import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_no_debt_repayout_after_denied_spend():
    clock = FakeClock()
    bucket = TokenBucket(5, 1, clock=clock)
    assert not bucket.try_take(50)
    # No time passed: a denied spend must not have changed the balance.
    assert bucket.available == pytest.approx(5)


def test_partial_credit_blocks_oversized_single_spend():
    clock = FakeClock()
    bucket = TokenBucket(10, 10, clock=clock)
    bucket.try_take(10)
    clock.advance(0.9)              # 9 accrued
    assert not bucket.try_take(9.5)
    clock.advance(0.1)              # exactly 10 now
    assert bucket.try_take(10)


def test_available_is_idempotent_across_reads():
    clock = FakeClock()
    bucket = TokenBucket(4, 2, clock=clock)
    first = bucket.available
    second = bucket.available       # no time passed: identical
    assert first == pytest.approx(second)


def test_zero_amount_spend_is_trivially_granted():
    clock = FakeClock()
    bucket = TokenBucket(1, 1, clock=clock)
    assert bucket.try_take(0)
    assert bucket.available == pytest.approx(1)


def test_total_granted_respects_budget_invariant():
    clock = FakeClock()
    bucket = TokenBucket(10, 5, clock=clock)
    granted = 0
    while granted < 100 and bucket.try_take(1):
        granted += 1
        clock.advance(1)            # 5 tokens accrue per step
    # Invariant: total granted can never exceed capacity + refill over elapsed.
    assert granted <= 10 + 5 * 100
    assert granted >= 10            # sanity: the initial budget is usable
