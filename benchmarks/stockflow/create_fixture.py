"""Deterministic generator for the stockflow fixture repository.

The fixture is the single source of truth for the benchmark's code; the git
repository under ``fixture/`` is generated from it with pinned authorship and
timestamps so the resulting commit sha is identical on every machine. That is
what makes AgentBench's exact-commit model usable without a remote host.

Maintenance process (documented in README.md):
1. edit the file contents embedded below (or restructure the generator),
2. run ``.venv/Scripts/python.exe benchmarks/stockflow/create_fixture.py``
3. paste the printed commit sha into ``benchmarks/stockflow/benchmark.yaml``.

Regenerating with unchanged content reproduces the exact same sha.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixture"
AUTHOR = "AgentBench Fixture <fixture@agentbench.invalid>"
DATE = "2026-01-15T12:00:00+00:00"
MESSAGE = "stockflow: reservation service with catalog cache"

FILES: dict[str, str] = {
    ".gitignore": """\
__pycache__/
*.pyc
.pytest_cache/
""",
    "pyproject.toml": """\
[project]
name = "stockflow"
version = "0.1.0"
requires-python = ">=3.12"
""",
    "stockflow/__init__.py": '''\
"""Inventory reservation service used by the AgentBench fixture."""

from stockflow.catalog import ProductCatalog
from stockflow.feed import StockFeed
from stockflow.reservations import Reservation, ReservationRegistry

__all__ = ["ProductCatalog", "Reservation", "ReservationRegistry", "StockFeed"]
''',
    "stockflow/feed.py": '''\
"""Raw stock feed: the single source of truth for on-hand inventory.

Internal component. It deliberately performs no validation: quantity rules
are the reservation flow's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StockFeed:
    stock: dict[str, int] = field(default_factory=dict)

    def current_stock(self, sku: str) -> int:
        return self.stock.get(sku, 0)

    def consume(self, sku: str, quantity: int) -> None:
        """Subtract the given quantity; negative quantities restock."""
        self.stock[sku] = self.current_stock(sku) - quantity
''',
    "stockflow/catalog.py": '''\
"""Read-through TTL cache over the stock feed."""

from __future__ import annotations

import time
from typing import Callable

from stockflow.feed import StockFeed


class ProductCatalog:
    def __init__(
        self,
        feed: StockFeed,
        ttl_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._feed = feed
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, int]] = {}

    def available(self, sku: str) -> int:
        entry = self._cache.get(sku)
        if entry is not None and self._is_fresh(entry):
            return entry[1]
        level = self._feed.current_stock(sku)
        self._cache[sku] = (self._clock(), level)
        return level

    def invalidate(self, sku: str | None = None) -> None:
        if sku is None:
            self._cache.clear()
        else:
            self._cache.pop(sku, None)

    def _is_fresh(self, entry: tuple[float, int]) -> bool:
        return (self._clock() - entry[0]) < self._ttl
''',
    "stockflow/reservations.py": '''\
"""Customer-facing reservation flow."""

from __future__ import annotations

from dataclasses import dataclass

from stockflow.catalog import ProductCatalog
from stockflow.feed import StockFeed


@dataclass
class Reservation:
    order_id: str
    sku: str
    quantity: int


class ReservationRegistry:
    def __init__(self, feed: StockFeed, catalog: ProductCatalog) -> None:
        self._feed = feed
        self._catalog = catalog
        self._by_order: dict[str, Reservation] = {}

    def reserve(self, order_id: str, sku: str, quantity: int) -> Reservation:
        if order_id in self._by_order:
            return self._by_order[order_id]
        level = self._catalog.available(sku)
        if quantity > level:
            raise ValueError(f"insufficient stock for {sku}")
        self._feed.consume(sku, quantity)
        reservation = Reservation(order_id=order_id, sku=sku, quantity=quantity)
        self._by_order[order_id] = reservation
        return reservation
''',
    "tests/test_catalog.py": '''\
"""Public tests for the catalog cache."""

from stockflow.catalog import ProductCatalog


class CountingFeed:
    def __init__(self, levels: dict[str, int]) -> None:
        self.levels = levels
        self.reads = 0

    def current_stock(self, sku: str) -> int:
        self.reads += 1
        return self.levels.get(sku, 0)

    def consume(self, sku: str, quantity: int) -> None:
        self.levels[sku] = self.current_stock(sku) - quantity


def test_available_reads_through_the_feed():
    feed = CountingFeed({"WIDGET": 7})
    catalog = ProductCatalog(feed)

    assert catalog.available("WIDGET") == 7


def test_repeated_reads_are_cached():
    feed = CountingFeed({"WIDGET": 7})
    catalog = ProductCatalog(feed)

    catalog.available("WIDGET")
    catalog.available("WIDGET")
    catalog.available("WIDGET")

    assert feed.reads == 1


def test_invalidate_forces_a_fresh_read():
    feed = CountingFeed({"WIDGET": 7})
    catalog = ProductCatalog(feed)

    catalog.available("WIDGET")
    feed.consume("WIDGET", 2)
    catalog.invalidate("WIDGET")

    assert catalog.available("WIDGET") == 5
''',
    "tests/test_reservations.py": '''\
"""Public tests for the reservation flow.

Some of these currently FAIL: they describe the behavior customers expect.
"""

import pytest

from stockflow.catalog import ProductCatalog
from stockflow.feed import StockFeed
from stockflow.reservations import ReservationRegistry


def make_registry(stock: int = 10) -> tuple[ReservationRegistry, StockFeed]:
    feed = StockFeed(stock={"WIDGET": stock})
    return ReservationRegistry(feed, ProductCatalog(feed)), feed


def test_reserve_reduces_reported_stock():
    registry, feed = make_registry(stock=10)

    registry.reserve("ORD-1", "WIDGET", 4)

    assert feed.current_stock("WIDGET") == 6
    assert registry._catalog.available("WIDGET") == 6


def test_reserve_is_idempotent_for_identical_order_ids():
    registry, feed = make_registry(stock=10)

    first = registry.reserve("ORD-1", "WIDGET", 4)
    second = registry.reserve("ORD-1", "WIDGET", 4)

    assert second is first
    assert feed.current_stock("WIDGET") == 6


def test_reserve_is_idempotent_regardless_of_order_id_case():
    registry, feed = make_registry(stock=10)

    first = registry.reserve("ORD-1", "WIDGET", 4)
    second = registry.reserve("ord-1", "WIDGET", 4)

    assert second is first
    assert feed.current_stock("WIDGET") == 6


def test_rejects_zero_quantity():
    registry, feed = make_registry(stock=10)

    with pytest.raises(ValueError):
        registry.reserve("ORD-1", "WIDGET", 0)

    assert feed.current_stock("WIDGET") == 10


def test_rejects_negative_quantity_and_leaves_stock_untouched():
    registry, feed = make_registry(stock=10)

    with pytest.raises(ValueError):
        registry.reserve("ORD-1", "WIDGET", -3)

    assert feed.current_stock("WIDGET") == 10


def test_insufficient_stock_raises():
    registry, _ = make_registry(stock=2)

    with pytest.raises(ValueError):
        registry.reserve("ORD-1", "WIDGET", 3)
''',
}


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=FIXTURE_DIR,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "AgentBench Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@agentbench.invalid",
            "GIT_COMMITTER_NAME": "AgentBench Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@agentbench.invalid",
            "GIT_AUTHOR_DATE": DATE,
            "GIT_COMMITTER_DATE": DATE,
        },
    )
    return result.stdout.strip()


def _clear_contents(directory: Path) -> None:
    """Empty the directory without deleting it.

    The fixture's top-level directory is frequently pinned by a CWD-style
    handle (shells, file watchers), which makes removing or renaming the dir
    itself impossible on Windows while its children stay removable.
    """
    import shutil

    def _on_readonly(func, target, _exc):
        os.chmod(target, 0o200)
        func(target)

    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, onexc=_on_readonly)
        else:
            entry.unlink()


def create_fixture() -> str:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    _clear_contents(FIXTURE_DIR)
    run_git("init", "-q", "-b", "main")
    for relative, content in FILES.items():
        target = FIXTURE_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_git("add", "-A")
    run_git("commit", "-q", "-m", MESSAGE)
    return run_git("rev-parse", "HEAD")


if __name__ == "__main__":
    sha = create_fixture()
    print(f"fixture repository created at {FIXTURE_DIR}")
    print(f"commit: {sha}")
    print("pin this sha as 'commit' in benchmarks/stockflow/benchmark.yaml")
    sys.exit(0)
