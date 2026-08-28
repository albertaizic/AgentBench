"""Deterministic generator for the cacheflow fixture (stale derived-cache bug)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

INIT_PY = '''"""Record store with a cached service layer."""

from .cache import NamespaceCache
from .repository import Record, RecordRepository
from .service import RecordService

__all__ = ["NamespaceCache", "Record", "RecordRepository", "RecordService"]
'''

CACHE_PY = '''"""Minimal namespace/partition cache with exact-key access only.

Entries are addressed by a namespace plus a composite key (any sequence of
hashable parts). The cache intentionally offers no prefix or wildcard
invalidation: callers must name exactly the composite keys they want to
drop.
"""

from __future__ import annotations

from typing import Any, Hashable

_MISSING = object()


class NamespaceCache:
    def __init__(self) -> None:
        self._store: dict[tuple[str, tuple[Hashable, ...]], Any] = {}

    def get(self, namespace: str, *parts: Hashable, default: Any = None) -> Any:
        return self._store.get((namespace, parts), default)

    def set(self, namespace: str, *parts: Hashable, value: Any) -> None:
        self._store[(namespace, parts)] = value

    def invalidate(self, namespace: str, *parts: Hashable) -> bool:
        return self._store.pop((namespace, parts), _MISSING) is not _MISSING

    def clear(self) -> None:
        self._store.clear()

    def cached_keys(self) -> frozenset[tuple[str, tuple[Hashable, ...]]]:
        """Snapshot of live composite keys (useful for tests/introspection)."""
        return frozenset(self._store)
'''

REPOSITORY_PY = '''"""In-memory record repository - the source of truth."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Record:
    record_id: str
    owner: str
    status: str
    payload: str = ""


class RecordRepository:
    def __init__(self) -> None:
        self._records: dict[str, Record] = {}

    def add(self, *, record_id: str, owner: str, status: str, payload: str = "") -> Record:
        if record_id in self._records:
            raise ValueError(f"duplicate record id: {record_id}")
        record = Record(record_id=record_id, owner=owner, status=status, payload=payload)
        self._records[record_id] = record
        return record

    def get(self, record_id: str) -> Record | None:
        return self._records.get(record_id)

    def require(self, record_id: str) -> Record:
        try:
            return self._records[record_id]
        except KeyError:
            raise KeyError(f"unknown record: {record_id}") from None

    def update(self, record_id: str, **changes) -> Record:
        current = self.require(record_id)
        allowed = {key: value for key, value in changes.items() if value is not None}
        updated = replace(current, **allowed)
        self._records[record_id] = updated
        return updated

    def delete(self, record_id: str) -> Record:
        record = self.require(record_id)
        del self._records[record_id]
        return record

    def by_owner(self, owner: str) -> list[Record]:
        return [r for r in self._records.values() if r.owner == owner]

    def by_status(self, status: str) -> list[Record]:
        return [r for r in self._records.values() if r.status == status]

    def all_ids(self) -> list[str]:
        return list(self._records)
'''

SERVICE_PY = '''"""Cached service layer over the record repository.

Reads are served through a NamespaceCache; every repository mutation must
keep the cache coherent with repository truth.
"""

from __future__ import annotations

from .cache import NamespaceCache
from .repository import Record, RecordRepository

RECORD_DETAIL = "record_detail"
OWNER_LIST = "owner_records"
STATUS_LIST = "status_records"

_MISS = object()


class RecordService:
    def __init__(self, repository: RecordRepository, cache: NamespaceCache) -> None:
        self._repo = repository
        self._cache = cache

    # -- reads -------------------------------------------------------------

    def get_record(self, record_id: str) -> Record:
        hit = self._cache.get(RECORD_DETAIL, record_id, default=_MISS)
        if hit is _MISS:
            hit = self._repo.require(record_id)
            self._cache.set(RECORD_DETAIL, record_id, value=hit)
        return hit

    def list_by_owner(self, owner: str) -> list[str]:
        hit = self._cache.get(OWNER_LIST, owner, default=_MISS)
        if hit is _MISS:
            hit = tuple(r.record_id for r in self._repo.by_owner(owner))
            self._cache.set(OWNER_LIST, owner, value=hit)
        return list(hit)

    def list_by_status(self, status: str) -> list[str]:
        hit = self._cache.get(STATUS_LIST, status, default=_MISS)
        if hit is _MISS:
            hit = tuple(r.record_id for r in self._repo.by_status(status))
            self._cache.set(STATUS_LIST, status, value=hit)
        return list(hit)

    # -- mutations ---------------------------------------------------------

    def create_record(self, *, record_id: str, owner: str, status: str, payload: str = "") -> Record:
        record = self._repo.add(record_id=record_id, owner=owner, status=status, payload=payload)
        # BUG: mutations refresh only their primary-key entry. The derived
        # owner/status list views keyed by composite keys keep serving
        # pre-mutation membership.
        self._cache.invalidate(RECORD_DETAIL, record_id)
        self._cache.set(RECORD_DETAIL, record_id, value=record)
        return record

    def update_record(
        self,
        record_id: str,
        *,
        owner: str | None = None,
        status: str | None = None,
        payload: str | None = None,
    ) -> Record:
        before = self._repo.require(record_id)
        updated = self._repo.update(record_id, owner=owner, status=status, payload=payload)
        # BUG: only the primary-key entry is invalidated here; owner/status
        # list caches keep serving stale snapshots.
        self._cache.invalidate(RECORD_DETAIL, record_id)
        return updated

    def delete_record(self, record_id: str) -> Record:
        record = self._repo.delete(record_id)
        # BUG: same as above - derived list views stay stale after deletion.
        self._cache.invalidate(RECORD_DETAIL, record_id)
        return record
'''

TEST_SERVICE_CACHE_PY = '''"""Public tests for cache coherence across service-layer mutations."""

from __future__ import annotations

import pytest

from cacheflow.cache import NamespaceCache
from cacheflow.repository import RecordRepository
from cacheflow.service import RecordService


@pytest.fixture()
def service():
    repo = RecordRepository()
    repo.add(record_id="r-101", owner="mia", status="open", payload="notes-a")
    repo.add(record_id="r-102", owner="mia", status="closed", payload="notes-b")
    repo.add(record_id="r-103", owner="noah", status="open", payload="notes-c")
    return RecordService(repo, NamespaceCache())


def test_owner_transfer_refreshes_both_owner_views(service):
    assert service.list_by_owner("mia") == ["r-101", "r-102"]
    service.update_record("r-102", owner="noah")
    assert service.list_by_owner("mia") == ["r-101"]
    assert service.list_by_owner("noah") == ["r-102", "r-103"]


def test_status_change_refreshes_status_view(service):
    assert service.list_by_status("open") == ["r-101", "r-103"]
    service.update_record("r-101", status="closed")
    assert service.list_by_status("open") == ["r-103"]
    assert service.list_by_status("closed") == ["r-101", "r-102"]


def test_detail_entry_reflects_payload_update(service):
    assert service.get_record("r-101").payload == "notes-a"
    service.update_record("r-101", payload="notes-a2")
    assert service.get_record("r-101").payload == "notes-a2"


def test_delete_clears_cached_views(service):
    assert service.list_by_owner("mia") == ["r-101", "r-102"]
    assert service.list_by_status("open") == ["r-101", "r-103"]
    service.delete_record("r-103")
    assert service.list_by_status("open") == ["r-101"]
    with pytest.raises(KeyError):
        service.get_record("r-103")


def test_create_refreshes_previously_materialized_empty_view():
    repo = RecordRepository()
    service = RecordService(repo, NamespaceCache())
    assert service.list_by_owner("ivy") == []  # warm an empty view
    service.create_record(record_id="r-201", owner="ivy", status="open", payload="fresh")
    assert service.list_by_owner("ivy") == ["r-201"]
    assert service.list_by_status("open") == ["r-201"]
'''

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "cacheflow"\nversion = "0.4.0"\n',
    "cacheflow/__init__.py": INIT_PY,
    "cacheflow/cache.py": CACHE_PY,
    "cacheflow/repository.py": REPOSITORY_PY,
    "cacheflow/service.py": SERVICE_PY,
    "tests/test_service_cache.py": TEST_SERVICE_CACHE_PY,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "cacheflow: cached record service", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
