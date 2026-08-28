"""Alternative cacheflow solution: generation-stamp coherence.

Instead of enumerating and invalidating every derived composite key on each
mutation, mutations bump per-owner/per-status generation counters; cached
list views carry the generation they were built at and are recomputed when
the stamp has moved on. A structurally different (optimistic) strategy."""

HEADER = '''"""Cached service layer over the record repository (generation-stamp coherence)."""

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
        self._owner_generations: dict[str, int] = {}
        self._status_generations: dict[str, int] = {}

    # -- generations -------------------------------------------------------

    def _bump(self, *, owner: str, status: str) -> None:
        self._owner_generations[owner] = self._owner_generations.get(owner, 0) + 1
        self._status_generations[status] = self._status_generations.get(status, 0) + 1

    def _bump_owner(self, owner: str) -> None:
        self._owner_generations[owner] = self._owner_generations.get(owner, 0) + 1

    def _bump_status(self, status: str) -> None:
        self._status_generations[status] = self._status_generations.get(status, 0) + 1

    @staticmethod
    def _stale(generations: dict[str, int], key: str, stamp) -> bool:
        return generations.get(key, 0) != stamp

    # -- reads -------------------------------------------------------------

    def get_record(self, record_id: str) -> Record:
        hit = self._cache.get(RECORD_DETAIL, record_id, default=_MISS)
        if hit is _MISS:
            hit = self._repo.require(record_id)
            self._cache.set(RECORD_DETAIL, record_id, value=hit)
        return hit

    def list_by_owner(self, owner: str) -> list[str]:
        hit = self._cache.get(OWNER_LIST, owner, default=_MISS)
        if isinstance(hit, tuple) and len(hit) == 2 \\
                and not self._stale(self._owner_generations, owner, hit[0]):
            return list(hit[1])
        fresh = tuple(r.record_id for r in self._repo.by_owner(owner))
        self._cache.set(OWNER_LIST, owner,
                        value=(self._owner_generations.get(owner, 0), fresh))
        return list(fresh)

    def list_by_status(self, status: str) -> list[str]:
        hit = self._cache.get(STATUS_LIST, status, default=_MISS)
        if isinstance(hit, tuple) and len(hit) == 2 \\
                and not self._stale(self._status_generations, status, hit[0]):
            return list(hit[1])
        fresh = tuple(r.record_id for r in self._repo.by_status(status))
        self._cache.set(STATUS_LIST, status,
                        value=(self._status_generations.get(status, 0), fresh))
        return list(fresh)

    # -- mutations ---------------------------------------------------------

    def create_record(self, *, record_id: str, owner: str, status: str, payload: str = "") -> Record:
        record = self._repo.add(record_id=record_id, owner=owner, status=status, payload=payload)
        self._cache.set(RECORD_DETAIL, record_id, value=record)
        self._bump(owner=owner, status=status)
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
        self._cache.invalidate(RECORD_DETAIL, record_id)
        if updated.owner != before.owner:
            self._bump_owner(before.owner)
        self._bump_owner(updated.owner)
        if updated.status != before.status:
            self._bump_status(before.status)
        self._bump_status(updated.status)
        return updated

    def delete_record(self, record_id: str) -> Record:
        record = self._repo.delete(record_id)
        self._cache.invalidate(RECORD_DETAIL, record_id)
        self._bump_owner(record.owner)
        self._bump_status(record.status)
        return record
'''

def edits(files):
    return {"cacheflow/service.py": HEADER}
