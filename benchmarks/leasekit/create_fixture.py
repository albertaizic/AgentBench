"""Deterministic generator for the leasekit fixture (lease lifecycle bugfix)."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, pin_commit  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

POOL_BROKEN = '''"""Bounded connection leases with expiry."""

from __future__ import annotations

import itertools
import time


class PoolExhausted(RuntimeError):
    pass


class Lease:
    """A scoped lease; use as a context manager."""

    def __init__(self, pool: "Pool", lease_id: int, expires_at: float) -> None:
        self.pool = pool
        self.lease_id = lease_id
        self.expires_at = expires_at
        self.released = False

    def release(self) -> None:
        if self.released:
            raise RuntimeError(f"lease {self.lease_id} already released")
        self.released = True
        self.pool._active.pop(self.lease_id, None)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class Pool:
    """At most *size* leases may be live at once; leases expire after TTL."""

    def __init__(self, size: int = 2, ttl_seconds: float = 30.0) -> None:
        if size < 1:
            raise ValueError("size must be >= 1")
        self.size = size
        self.ttl_seconds = ttl_seconds
        self._ids = itertools.count(1)
        self._active: dict[int, float] = {}

    def acquire(self) -> Lease:
        now = time.monotonic()
        # BUG: expiry is checked here, but expired entries are only removed
        # inside acquire's guard — callers that simply ask active_count()
        # between expiries see stale data, and any accounting path that skips
        # this purge leaks capacity permanently.
        expired = [k for k, v in self._active.items() if v <= now]
        for k in expired:
            pass  # dropped on the floor instead of being released properly
        if len(self._active) >= self.size:
            raise PoolExhausted("all leases are in use")
        lease_id = next(self._ids)
        self._active[lease_id] = now + self.ttl_seconds
        return Lease(self, lease_id, self._active[lease_id])

    def active_count(self) -> int:
        return len(self._active)
'''

POOL_FIXED = '''"""Bounded connection leases with expiry."""

from __future__ import annotations

import itertools
import time


class PoolExhausted(RuntimeError):
    pass


class Lease:
    """A scoped lease; use as a context manager."""

    def __init__(self, pool: "Pool", lease_id: int, expires_at: float) -> None:
        self.pool = pool
        self.lease_id = lease_id
        self.expires_at = expires_at
        self.released = False

    def release(self) -> None:
        if self.released:
            raise RuntimeError(f"lease {self.lease_id} already released")
        self.released = True
        self.pool._discard(self.lease_id)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class Pool:
    """At most *size* leases may be live at once; leases expire after TTL."""

    def __init__(self, size: int = 2, ttl_seconds: float = 30.0) -> None:
        if size < 1:
            raise ValueError("size must be >= 1")
        self.size = size
        self.ttl_seconds = ttl_seconds
        self._ids = itertools.count(1)
        self._active: dict[int, float] = {}

    def _discard(self, lease_id: int) -> None:
        self._active.pop(lease_id, None)

    def _reap_expired(self) -> None:
        now = time.monotonic()
        for expired in [k for k, v in self._active.items() if v <= now]:
            self._discard(expired)

    def acquire(self) -> Lease:
        self._reap_expired()
        now = time.monotonic()
        if len(self._active) >= self.size:
            raise PoolExhausted("all leases are in use")
        lease_id = next(self._ids)
        self._active[lease_id] = now + self.ttl_seconds
        return Lease(self, lease_id, self._active[lease_id])

    def active_count(self) -> int:
        self._reap_expired()
        return len(self._active)
'''

PUBLIC_TESTS = '''"""Public tests for the connection pool."""

import time

import pytest

from leasekit.pool import Pool, PoolExhausted


def test_acquire_within_capacity():
    pool = Pool(size=2)
    with pool.acquire():
        with pool.acquire():
            assert pool.active_count() == 2


def test_exhaustion_raises():
    pool = Pool(size=1)
    lease = pool.acquire()
    with pytest.raises(PoolExhausted):
        pool.acquire()
    lease.release()
    pool.acquire()  # capacity freed


def test_release_is_guarded_against_double_release():
    pool = Pool(size=1)
    lease = pool.acquire()
    lease.release()
    with pytest.raises(RuntimeError):
        lease.release()


def test_context_manager_releases():
    pool = Pool(size=1)
    with pool.acquire():
        pass
    assert pool.active_count() == 0


def test_expired_capacity_is_reclaimed_without_release():
    pool = Pool(size=1, ttl_seconds=0.05)
    pool.acquire()
    time.sleep(0.08)  # TTL long past
    pool.acquire()  # must succeed: the old lease expired
'''

HIDDEN_TESTS = '''"""Hidden contract: lifecycle survives failure paths and expiry."""

import time

import pytest

from leasekit.pool import Pool, PoolExhausted


class Boom(Exception):
    pass


def test_exception_inside_lease_still_releases_capacity():
    pool = Pool(size=1)
    for _ in range(50):
        try:
            with pool.acquire():
                raise Boom
        except Boom:
            pass
    assert pool.active_count() == 0


def test_expired_leases_stop_consuming_capacity():
    pool = Pool(size=2, ttl_seconds=0.05)
    pool.acquire()
    pool.acquire()
    time.sleep(0.15)
    pool.acquire()   # expiry alone must have freed a slot
    pool.acquire()   # and the other one too
    with pytest.raises(PoolExhausted):
        pool.acquire()


def test_active_count_reflects_expiry_without_acquiring():
    pool = Pool(size=2, ttl_seconds=0.05)
    pool.acquire()
    time.sleep(0.15)
    assert pool.active_count() == 0


def test_ids_stay_unique_across_expiries():
    pool = Pool(size=1, ttl_seconds=0.05)
    seen = []
    for _ in range(5):
        lease = pool.acquire()
        seen.append(lease.lease_id)
        time.sleep(0.15)
    assert len(set(seen)) == len(seen)


def test_late_release_of_expired_lease_is_harmless():
    pool = Pool(size=1, ttl_seconds=0.05)
    stale = pool.acquire()
    time.sleep(0.15)
    assert pool.active_count() == 0
    stale.release()  # already gone: must be a no-op, not corruption
    fresh = pool.acquire()
    assert fresh.lease_id != stale.lease_id
'''


def main() -> int:
    files = {
        ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
        "pyproject.toml": '[project]\nname = "leasekit"\nversion = "0.1.4"\n',
        "leasekit/__init__.py": "",
        "leasekit/pool.py": POOL_BROKEN,
        "tests/test_pool.py": PUBLIC_TESTS,
    }
    sha = create_fixture_repo(FIXTURE_DIR, files, "leasekit: bounded leases")
    patch_dir = Path(__file__).parent / "reference"
    patch_dir.mkdir(exist_ok=True)
    diff = difflib.unified_diff(
        POOL_BROKEN.splitlines(keepends=True),
        POOL_FIXED.splitlines(keepends=True),
        fromfile="a/leasekit/pool.py",
        tofile="b/leasekit/pool.py",
    )
    (patch_dir / "fix.patch").write_text("".join(diff), encoding="utf-8")
    print(f"fixture repository created at {FIXTURE_DIR}")
    print(f"commit: {sha}")
    pin_commit(YAML_PATH, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
