"""Deterministic generator for the cleanroom-ratelimit fixture.

Cleanroom task: agents implement ratelimit.py purely from docs/api.md.
The shipped ratelimit.py is a stub raising NotImplementedError, so the
baseline is broken by construction. No reference implementation is exposed
to agents; reference/fix.patch exists solely for `benchmark validate`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

API_DOC = """\
# ratelimit — token-bucket rate limiter (binding contract)

The module `ratelimit.py` at the repository root must expose exactly one
public factory:

```python
make_limiter(capacity, refill_rate, *, clock=time.monotonic)
```

* `capacity`, `refill_rate`: numbers (int or float).
* `clock`: zero-argument callable returning a float number of seconds;
  defaults to `time.monotonic`. ALL timing derives from `clock()`. The
  implementation MUST NOT call `time.monotonic`, `time.time`, or
  `time.sleep` itself — tests inject a controllable clock and never sleep.
* `capacity <= 0` → raise `ValueError`.
* `refill_rate < 0` → raise `ValueError`. `refill_rate == 0` is legal:
  the bucket never refills.
* The limiter starts FULL: immediately after construction
  `available() == capacity` for the clock value observed at construction.

`make_limiter` returns an object exposing exactly these three methods:

## available() -> float

Applies any pending refill (see *Refill*) and returns the current balance,
which always lies in `[0, capacity]`.

## try_acquire(tokens=1.0) -> bool

1. Raise `ValueError` if `tokens <= 0` or `tokens > capacity`.
2. Apply any pending refill.
3. If balance >= tokens: subtract tokens and return `True`.
4. Otherwise return `False`; the balance is unchanged.

## retry_after(tokens=1.0) -> float

1. Raise `ValueError` under the same conditions as `try_acquire`
   (`tokens <= 0` or `tokens > capacity`).
2. Apply any pending refill.
3. If balance >= tokens: return `0.0`.
4. Else if `refill_rate > 0`: return `(tokens - balance) / refill_rate`
   — the seconds of clock time after which, absent other calls,
   `try_acquire(tokens)` would first succeed.
5. Else (`refill_rate == 0`): return `math.inf`.

## Refill

Refill is lazy and exact. On each call that observes time (`available`,
`try_acquire`, `retry_after`) the balance gains
`(now - last_refill_time) * refill_rate` tokens, clamped at capacity, and
`last_refill_time` advances to `now`. Fractional elapsed time and fractional
token balances are honored; a balance may be fractional but never negative
and never above capacity. Calling `retry_after` or `available` never spends
tokens.

## Independence

Every call to `make_limiter` returns a fully independent limiter; no state
is shared between instances.

## Determinism note

Tests always inject a fake clock; nothing here depends on wall time.
"""

STUB = '''\
"""Token-bucket rate limiter.

The binding behavioral contract lives in docs/api.md. Implement it here.
"""

from __future__ import annotations

import time


def make_limiter(capacity, refill_rate, *, clock=time.monotonic):
    """Build an independent token-bucket limiter (see docs/api.md)."""
    raise NotImplementedError("ratelimit.make_limiter is not implemented yet")
'''

PUBLIC_TESTS = '''\
"""Public black-box tests for the documented token-bucket contract."""
from __future__ import annotations

import math

import pytest

from ratelimit import make_limiter


class FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_starts_full():
    clock = FakeClock()
    limiter = make_limiter(7, 2, clock=clock)
    assert limiter.available() == 7


def test_invalid_construction():
    with pytest.raises(ValueError):
        make_limiter(0, 1)
    with pytest.raises(ValueError):
        make_limiter(-3, 1)
    with pytest.raises(ValueError):
        make_limiter(10, -1)


def test_acquire_drains_balance():
    clock = FakeClock()
    limiter = make_limiter(3, 1, clock=clock)
    assert limiter.try_acquire() is True
    assert limiter.available() == 2
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.available() == 0


def test_insufficient_balance_returns_false_without_spending():
    clock = FakeClock()
    limiter = make_limiter(2, 1, clock=clock)
    assert limiter.try_acquire(2) is True
    assert limiter.try_acquire(1) is False
    assert limiter.available() == 0


def test_refund_via_elapsed_time():
    clock = FakeClock()
    limiter = make_limiter(4, 2, clock=clock)
    limiter.try_acquire(4)
    assert limiter.available() == 0
    clock.advance(1.5)
    assert limiter.available() == 3


def test_fractional_refill_is_honored():
    clock = FakeClock()
    limiter = make_limiter(5, 10, clock=clock)
    limiter.try_acquire(5)
    clock.advance(0.25)
    assert limiter.available() == pytest.approx(2.5)


def test_balance_never_exceeds_capacity():
    clock = FakeClock()
    limiter = make_limiter(5, 1, clock=clock)
    clock.advance(3600)
    assert limiter.available() == 5


def test_retry_after_zero_when_affordable_and_positive_when_not():
    clock = FakeClock()
    limiter = make_limiter(6, 2, clock=clock)
    assert limiter.retry_after(6) == 0.0
    limiter.try_acquire(6)
    clock.advance(1)
    assert limiter.retry_after(6) == pytest.approx((6 - 2) / 2)


def test_retry_after_infinite_when_bucket_never_refills():
    clock = FakeClock()
    limiter = make_limiter(2, 0, clock=clock)
    limiter.try_acquire(2)
    assert math.isinf(limiter.retry_after(1))


def test_try_acquire_rejects_bad_token_counts():
    limiter = make_limiter(3, 1, clock=FakeClock())
    with pytest.raises(ValueError):
        limiter.try_acquire(0)
    with pytest.raises(ValueError):
        limiter.try_acquire(-1)
    with pytest.raises(ValueError):
        limiter.try_acquire(3.5)
    with pytest.raises(ValueError):
        limiter.retry_after(0)


def test_limiters_are_independent():
    clock_a = FakeClock()
    a = make_limiter(2, 1, clock=clock_a)
    b = make_limiter(9, 1, clock=FakeClock(start=0.0))
    a.try_acquire(2)
    assert a.available() == 0
    assert b.available() == 9


def test_exact_boundary_succeeds():
    clock = FakeClock()
    limiter = make_limiter(5, 1, clock=clock)
    assert limiter.try_acquire(5) is True
'''

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "ratelimit"\nversion = "0.1.0"\n',
    "docs/api.md": API_DOC,
    "ratelimit.py": STUB,
    "tests/test_ratelimit_public.py": PUBLIC_TESTS,
}


def main() -> int:
    return main_for(
        FIXTURE_DIR,
        FILES,
        "cleanroom-ratelimit: token-bucket contract from executable spec",
        YAML_PATH,
    )


if __name__ == "__main__":
    sys.exit(main())
