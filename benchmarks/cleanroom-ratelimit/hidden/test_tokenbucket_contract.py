"""Hidden black-box contract tests: token-bucket limiter per docs/api.md.

Everything here is driven through make_limiter + the documented methods with
DIFFERENT parameters and richer scenarios than the public suite.
"""
from __future__ import annotations

import math

import pytest

from ratelimit import make_limiter


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

def test_validation_boundaries_exact():
    with pytest.raises(ValueError):
        make_limiter(0, 1)
    with pytest.raises(ValueError):
        make_limiter(-12, 5)
    with pytest.raises(ValueError):
        make_limiter(12, -0.001)
    limiter = make_limiter(10, 3, clock=FakeClock(start=42.0))
    assert limiter.try_acquire(10) is True  # tokens == capacity is legal
    assert limiter.available() == 0
    clock = FakeClock()
    zero_refill = make_limiter(4, 0, clock=clock)  # refill_rate == 0 is legal
    assert zero_refill.available() == 4


def test_failed_acquire_leaves_balance_bit_for_bit():
    clock = FakeClock()
    limiter = make_limiter(8, 1, clock=clock)
    limiter.try_acquire(8)
    for _ in range(5):
        assert limiter.try_acquire(2.5) is False
        assert limiter.available() == 0


def test_retry_after_is_consistent_with_waiting_then_acquiring():
    rate = 7.0
    clock = FakeClock()
    limiter = make_limiter(9, rate, clock=clock)
    limiter.try_acquire(9)
    wait = limiter.retry_after(5)
    assert wait == pytest.approx(5 / rate)
    clock.advance(wait + rate * 1e-6)  # a hair past the quoted instant
    assert limiter.available() >= 5
    assert limiter.try_acquire(5) is True

def test_repeated_observation_does_not_spend_or_overshoot():
    clock = FakeClock()
    limiter = make_limiter(6, 2.5, clock=clock)
    limiter.try_acquire(6)
    clock.advance(0.8)  # would restore exactly 2.0
    for _ in range(4):
        # affordable: retry_after stays 0.0 and never spends
        assert limiter.retry_after(1) == 0.0
    # retry_after/available are pure observations apart from lazy refill:
    assert limiter.available() == pytest.approx(2.0)
    assert limiter.available() == pytest.approx(2.0)
    assert limiter.retry_after(6) == pytest.approx(4 / 2.5)


def test_long_idle_clamps_at_capacity():
    clock = FakeClock()
    limiter = make_limiter(3.25, 100.0, clock=clock)
    limiter.try_acquire(3.25)
    clock.advance(10_000)
    balance = limiter.available()
    assert balance == 3.25
    assert balance <= 3.25


def test_fractional_rates_and_balances():
    clock = FakeClock()
    limiter = make_limiter(1.5, 0.3, clock=clock)
    assert limiter.try_acquire(1.5) is True
    clock.advance(1)
    assert limiter.available() == pytest.approx(0.3)
    assert limiter.try_acquire(0.5) is False
    clock.advance(1)
    assert limiter.available() == pytest.approx(0.6)
    assert limiter.try_acquire(0.6) is True
    assert limiter.available() == pytest.approx(0.0)


def test_zero_refill_bucket_semantics():
    clock = FakeClock()
    limiter = make_limiter(5, 0, clock=clock)
    limiter.try_acquire(3)
    clock.advance(500)
    assert limiter.available() == 2
    assert math.isinf(limiter.retry_after(3))
    assert limiter.retry_after(2) == 0.0


def test_instances_are_fully_independent_under_stress():
    limiters = [make_limiter(i + 1, i * 0.5 + 0.25, clock=FakeClock(start=i * 100))
                for i in range(6)]
    caps = list(range(1, 7))
    # all start full
    assert [limiter.available() for limiter in limiters] == caps
    # draining one leaves every other untouched
    for index, limiter in enumerate(limiters):
        assert limiter.try_acquire(caps[index]) is True
        assert limiter.available() == 0
        for other_index, other in enumerate(limiters):
            expected = 0 if other_index <= index else caps[other_index]
            assert other.available() == expected
    # no time passed anywhere: nothing refilled
    assert [limiter.available() for limiter in limiters] == [0] * 6


def test_operation_trace_matches_reference_model():
    """A scripted trace must agree with an independent model of the spec."""
    capacity, rate = 12.0, 4.0
    clock = FakeClock(start=500.0)
    limiter = make_limiter(capacity, rate, clock=clock)

    model_balance = capacity
    model_time = 500.0

    def model_advance(delta):
        nonlocal model_balance, model_time
        model_time += delta
        model_balance = min(capacity, model_balance + delta * rate)

    ops = [
        ("acquire", 5), ("advance", 0.5), ("acquire", 5), ("advance", 2),
        ("acquire", 5), ("observe", None), ("advance", 0.75), ("acquire", 4),
        ("observe", None), ("advance", 3), ("acquire", 12), ("observe", None),
    ]
    for op, amount in ops:
        if op == "advance":
            clock.advance(amount)
            model_advance(amount)
        elif op == "observe":
            model_advance(0)
            assert limiter.available() == pytest.approx(model_balance)
        else:
            model_advance(0)
            if model_balance >= amount:
                model_balance -= amount
                expected = True
            else:
                expected = False
            assert limiter.try_acquire(amount) is expected


def test_identical_traces_on_offset_clocks_prove_injected_clock_drives_timing():
    def run_trace(clock_start):
        clock = FakeClock(start=clock_start)
        limiter = make_limiter(10, 2, clock=clock)
        trace = []
        for step in range(8):
            limiter.try_acquire(3)
            clock.advance(0.4)
            trace.append(limiter.available())
        return trace

    baseline = run_trace(0.0)
    assert run_trace(1e9) == pytest.approx(baseline, rel=1e-6, abs=1e-6)
