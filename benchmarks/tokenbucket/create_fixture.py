"""Deterministic generator for the tokenbucket fixture (rate limiter bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "tokenbucket"\nversion = "0.1.0"\n',
    # BUGS: (1) spend is applied before any clamp, so balances go negative
    # and later refills repay the debt; (2) refill uses int(elapsed) so
    # sub-second credit is discarded.
    "tokenbucket/limiter.py": (
        '"""Token-bucket rate limiting for outbound API calls."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import time\n\n\n'
        'class TokenBucket:\n'
        '    def __init__(self, capacity: float, refill_per_second: float,\n'
        '                 clock=None) -> None:\n'
        '        self.capacity = float(capacity)\n'
        '        self.refill_rate = float(refill_per_second)\n'
        '        self._clock = clock or time.monotonic\n'
        '        self._tokens = float(capacity)\n'
        '        self._last = self._clock()\n\n'
        '    def _refill(self) -> None:\n'
        '        now = self._clock()\n'
        '        elapsed = now - self._last\n'
        '        # BUG: whole seconds only - fractional credit is lost.\n'
        '        gained = self.refill_rate * float(int(elapsed))\n'
        '        self._tokens = min(self.capacity, self._tokens + gained)\n'
        '        self._last = now\n\n'
        '    def try_take(self, amount: float) -> bool:\n'
        '        self._refill()\n'
        '        # BUG: no clamp - balance may go negative and the debt is\n'
        '        # repaid by future refills instead of blocking the caller.\n'
        '        if amount <= self._tokens + self.refill_rate:\n'
        '            self._tokens -= amount\n'
        '            return True\n'
        '        return False\n\n'
        '    @property\n'
        '    def available(self) -> float:\n'
        '        self._refill()\n'
        '        return self._tokens\n'
    ),
    "tests/test_limiter.py": (
        '"""Public tests for the token bucket contract."""\n\n'
        'import pytest\n\n'
        'from tokenbucket.limiter import TokenBucket\n\n\nclass FakeClock:\n'
        '    def __init__(self) -> None:\n'
        '        self.now = 1000.0\n\n'
        '    def __call__(self) -> float:\n'
        '        return self.now\n\n'
        '    def advance(self, seconds: float) -> None:\n'
        '        self.now += seconds\n\n\n'
        'def test_initial_balance_is_full_capacity():\n'
        '    clock = FakeClock()\n'
        '    bucket = TokenBucket(10, 2, clock=clock)\n'
        '    assert bucket.available == 10\n\n'
        'def test_spend_depletes_and_refills_fractionally():\n'
        '    clock = FakeClock()\n'
        '    bucket = TokenBucket(10, 2, clock=clock)\n'
        '    assert bucket.try_take(10)\n'
        '    clock.advance(1.5)          # 3 tokens of credit, fractionally\n'
        '    assert bucket.available == pytest.approx(3.0)\n'
        '    assert bucket.try_take(3)\n'
        '    assert not bucket.try_take(0.5)\n\n'
        'def test_balance_never_goes_negative():\n'
        '    clock = FakeClock()\n'
        '    bucket = TokenBucket(10, 2, clock=clock)\n'
        '    assert not bucket.try_take(25)\n'
        '    assert bucket.available == pytest.approx(10)  # untouched full bucket\n\n'
        'def test_refill_clamps_at_capacity():\n'
        '    clock = FakeClock()\n'
        '    bucket = TokenBucket(10, 2, clock=clock)\n'
        '    bucket.try_take(4)\n'
        '    clock.advance(600)\n'
        '    assert bucket.available == 10\n\n'
        'def test_fractional_refill_accrues_exactly():\n'
        '    clock = FakeClock()\n'
        '    bucket = TokenBucket(4, 2, clock=clock)\n'
        '    assert bucket.try_take(4)   # drain completely\n'
        '    clock.advance(1.5)          # exactly 3.0 tokens of fractional credit\n'
        '    assert bucket.available == pytest.approx(3.0)\n'
        '    assert bucket.try_take(3)\n'
        '    assert not bucket.try_take(0.5)\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "tokenbucket: API rate limiter", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
