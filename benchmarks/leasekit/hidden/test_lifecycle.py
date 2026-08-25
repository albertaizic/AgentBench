"""Hidden contract: lifecycle survives failure paths and expiry."""
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
