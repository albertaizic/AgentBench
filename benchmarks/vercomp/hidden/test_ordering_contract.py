"""Hidden behavioral checks for version ordering."""

from __future__ import annotations

import pytest

from vercomp.versions import compare, sort_versions


@pytest.mark.parametrize("older,newer", [
    ("1.2", "1.10"),
    ("0.9.15", "0.10.1"),
    ("3.0.0", "3.0.1"),
    ("1.0.0-alpha.1", "1.0.0-alpha.2"),
    ("1.0.0-alpha.9", "1.0.0-alpha.10"),   # numeric identifiers, not lexical
    ("1.0.0-alpha", "1.0.0-alpha.1"),
    ("1.0.0-rc.1", "1.0.0"),               # any prerelease < release
])
def test_pairs_order_correctly(older, newer):
    assert compare(older, newer) == -1
    assert compare(newer, older) == 1
    assert compare(older, older) == 0


def test_numeric_identifier_beats_longer_alphanumeric():
    # Numeric identifiers compare numerically and bind tighter than lexical:
    # identifier [10] vs [alpha]: numeric < alphanumeric per SemVer-ish rule.
    assert compare("1.0.0-10", "1.0.0-alpha") == -1


def test_sort_is_stable_for_equal_versions():
    versions = ["1.5", "1.5.0", "1.5"]
    ordered = sort_versions(versions)
    assert ordered.count("1.5") == 2


def test_large_release_train_sorted():
    train = ["10.0.1", "9.99.99", "10.0.0-rc.2", "10.0.0-rc.10", "10.0.0"]
    assert sort_versions(train) == [
        "9.99.99", "10.0.0-rc.2", "10.0.0-rc.10", "10.0.0", "10.0.1",
    ]
