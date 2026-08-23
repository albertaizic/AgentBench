"""Hidden behavioral acceptance checks for the stockflow reservation flow.

This file lives OUTSIDE the fixture repository and is never copied into the
agent-visible workspace. It runs after the agent finishes, from this
directory, with the agent's workspace on PYTHONPATH.
"""

from __future__ import annotations

import pytest

from stockflow.catalog import ProductCatalog
from stockflow.feed import StockFeed
from stockflow.reservations import ReservationRegistry


def make_registry(stock: int = 10) -> tuple[ReservationRegistry, StockFeed]:
    feed = StockFeed(stock={"WIDGET": stock})
    return ReservationRegistry(feed, ProductCatalog(feed)), feed


def test_repeated_order_via_casing_reserves_only_once():
    registry, feed = make_registry(stock=10)

    first = registry.reserve("ORD-1", "WIDGET", 6)
    second = registry.reserve("  ord-1", "WIDGET", 6)

    assert second == first
    assert feed.current_stock("WIDGET") == 4


def test_no_oversell_after_reservations():
    registry, feed = make_registry(stock=10)

    registry.reserve("A", "WIDGET", 4)

    with pytest.raises(ValueError):
        registry.reserve("B", "WIDGET", 7)

    assert feed.current_stock("WIDGET") == 6


def test_catalog_stays_consistent_through_a_sequence():
    registry, feed = make_registry(stock=10)

    seen = []
    for index, order in enumerate(("O1", "o2", "O3")):
        registry.reserve(order, "WIDGET", 2)
        seen.append(registry._catalog.available("WIDGET"))
        del index

    assert seen == [8, 6, 4]
    assert feed.current_stock("WIDGET") == 4


@pytest.mark.parametrize("quantity", [0, -1, -25])
def test_invalid_quantities_leave_inventory_untouched(quantity):
    registry, feed = make_registry(stock=10)

    with pytest.raises(ValueError):
        registry.reserve(f"ORD-{abs(quantity)}", "WIDGET", quantity)

    assert feed.current_stock("WIDGET") == 10
    assert registry._catalog.available("WIDGET") == 10
