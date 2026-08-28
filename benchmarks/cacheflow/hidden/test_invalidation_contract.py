"""Hidden behavioral checks for cacheflow cache-invalidation contract.

Uses a different dataset and longer mutation sequences than the public tests.
"""

from __future__ import annotations

import pytest

from cacheflow.cache import NamespaceCache
from cacheflow.repository import RecordRepository
from cacheflow.service import RecordService

SEQUENCE = [
    ("t-501", "dana", "open", "alpha"),
    ("t-502", "dana", "open", "beta"),
    ("t-503", "eli", "closed", "gamma"),
    ("t-504", "eli", "open", "delta"),
    ("t-505", "finn", "closed", "epsilon"),
]

OWNERS = ["dana", "eli", "finn"]
STATUSES = ["open", "closed"]


@pytest.fixture()
def env():
    repo = RecordRepository()
    for record_id, owner, status, payload in SEQUENCE:
        repo.add(record_id=record_id, owner=owner, status=status, payload=payload)
    return RecordService(repo, NamespaceCache()), repo


def test_multi_hop_owner_transfers_stay_coherent(env):
    svc, _repo = env
    assert svc.list_by_owner("dana") == ["t-501", "t-502"]
    svc.update_record("t-501", owner="eli")
    assert svc.list_by_owner("dana") == ["t-502"]
    assert svc.list_by_owner("eli") == ["t-501", "t-503", "t-504"]  # warmed stale-prone
    svc.update_record("t-501", owner="finn")
    assert svc.list_by_owner("eli") == ["t-503", "t-504"]
    assert svc.list_by_owner("finn") == ["t-501", "t-505"]
    assert svc.list_by_owner("dana") == ["t-502"]


def test_status_flips_in_both_directions(env):
    svc, _repo = env
    assert svc.list_by_status("open") == ["t-501", "t-502", "t-504"]
    svc.update_record("t-501", status="closed")
    assert svc.list_by_status("open") == ["t-502", "t-504"]
    assert svc.list_by_status("closed") == ["t-501", "t-503", "t-505"]
    svc.update_record("t-503", status="open")
    assert svc.list_by_status("closed") == ["t-501", "t-505"]
    assert svc.list_by_status("open") == ["t-502", "t-503", "t-504"]


def test_delete_then_recreate_same_id(env):
    svc, repo = env
    svc.list_by_owner("dana")
    svc.list_by_status("open")
    svc.delete_record("t-502")
    assert svc.list_by_owner("dana") == ["t-501"]
    assert svc.list_by_status("open") == ["t-501", "t-504"]
    svc.create_record(record_id="t-502", owner="finn", status="open", payload="zeta")
    assert svc.list_by_owner("finn") == ["t-505", "t-502"]
    assert svc.list_by_status("open") == ["t-501", "t-504", "t-502"]
    assert svc.get_record("t-502").payload == "zeta"
    assert repo.get("t-502").owner == "finn"


def test_noop_update_keeps_every_view_correct(env):
    svc, _repo = env
    # warm every view
    for owner in OWNERS:
        svc.list_by_owner(owner)
    for status in STATUSES:
        svc.list_by_status(status)
    svc.update_record("t-503")  # changes nothing
    assert svc.list_by_owner("eli") == ["t-503", "t-504"]
    assert svc.list_by_status("closed") == ["t-503", "t-505"]


def test_views_track_repository_truth_through_scripted_sequence(env):
    svc, repo = env

    def truth():
        return (
            {o: [r.record_id for r in repo.by_owner(o)] for o in OWNERS},
            {s: [r.record_id for r in repo.by_status(s)] for s in STATUSES},
        )

    ops = [
        (svc.update_record, "t-501", {"status": "closed"}),
        (svc.delete_record, "t-504", {}),
        (svc.create_record, None, {"record_id": "t-506", "owner": "dana", "status": "open", "payload": "eta"}),
        (svc.update_record, "t-503", {"owner": "dana"}),
        (svc.update_record, "t-502", {"owner": "finn", "status": "closed"}),
        (svc.delete_record, "t-501", {}),
        (svc.create_record, None, {"record_id": "t-504", "owner": "eli", "status": "open", "payload": "theta"}),
    ]
    for fn, rid, kwargs in ops:
        if rid is None:
            fn(**kwargs)
        else:
            fn(rid, **kwargs)
        owners_truth, statuses_truth = truth()
        for owner, expected in owners_truth.items():
            assert svc.list_by_owner(owner) == expected, f"owner {owner} stale after {fn.__name__}"
        for status, expected in statuses_truth.items():
            assert svc.list_by_status(status) == expected, f"status {status} stale after {fn.__name__}"


def test_untouched_views_remain_cached(env):
    svc, repo = env
    svc.list_by_owner("dana")
    svc.list_by_status("closed")
    keys_before = len(svc._cache.cached_keys())
    svc.update_record("t-504", payload="delta2")  # touches neither dana nor closed views' membership
    assert svc._cache.get("owner_records", "dana") is not None
    assert svc._cache.get("status_records", "closed") is not None
    assert svc.list_by_owner("dana") == ["t-501", "t-502"]
    assert len(svc._cache.cached_keys()) >= keys_before - 0
