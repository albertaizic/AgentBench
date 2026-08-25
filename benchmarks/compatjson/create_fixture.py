"""Deterministic generator for the compatjson fixture (legacy codec bugfix)."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, pin_commit  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

CODEC_BROKEN = '''"""Ledger archive codec. v2 is current; v1 archives are still read."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

SUPPORTED_VERSION = 2


class UnsupportedVersion(ValueError):
    pass


class Entry:
    def __init__(self, account: str, amount: Decimal, memo: str = "") -> None:
        self.account = account
        self.amount = amount
        self.memo = memo

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Entry)
            and self.account == other.account
            and self.amount == other.amount
            and self.memo == other.memo
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Entry({self.account!r}, {self.amount}, {self.memo!r})"


def encode(entries: list[Entry]) -> dict[str, Any]:
    return {
        "version": 2,
        "entries": [
            {
                "account": e.account,
                "amount": str(e.amount),
                "memo": e.memo,
            }
            for e in entries
        ],
    }


def decode(payload: dict[str, Any]) -> list[Entry]:
    version = payload.get("version", 1)
    # BUG: no guard against versions we do not understand; a v3 archive from
    # the future silently decodes into nonsense instead of failing loudly.
    entries = []
    for raw in payload.get("entries", []):
        amount = raw["amount"]
        if isinstance(amount, float):
            # BUG: v1 wrote amounts as JSON numbers; routing them through
            # binary float corrupts cents (0.1 + 0.2 problems).
            amount = Decimal(str(amount))
        else:
            amount = Decimal(str(amount))
        memo = raw.get("memo") or ""
        entries.append(Entry(raw["account"], amount, memo))
    return entries
'''

CODEC_FIXED = CODEC_BROKEN.replace(
    """def decode(payload: dict[str, Any]) -> list[Entry]:
    version = payload.get("version", 1)
    # BUG: no guard against versions we do not understand; a v3 archive from
    # the future silently decodes into nonsense instead of failing loudly.
    entries = []
    for raw in payload.get("entries", []):
        amount = raw["amount"]
        if isinstance(amount, float):
            # BUG: v1 wrote amounts as JSON numbers; routing them through
            # binary float corrupts cents (0.1 + 0.2 problems).
            amount = Decimal(str(amount))
        else:
            amount = Decimal(str(amount))
        memo = raw.get("memo") or ""
        entries.append(Entry(raw["account"], amount, memo))
    return entries
""",
    '''def decode(payload: dict[str, Any]) -> list[Entry]:
    version = int(payload.get("version", 1))
    if version > SUPPORTED_VERSION:
        raise UnsupportedVersion(
            f"archive version {version} is newer than supported "
            f"{SUPPORTED_VERSION}"
        )
    entries = []
    for raw in payload.get("entries", []):
        raw_amount = raw["amount"]
        if isinstance(raw_amount, bool):
            raise ValueError("bool is not an amount")
        if isinstance(raw_amount, int):
            amount = Decimal(raw_amount)
        elif isinstance(raw_amount, float) and version == 1:
            # v1 stored amounts as JSON numbers: integral values arrive as
            # e.g. 5.0 and must normalize to an integer-scale Decimal.
            amount = _decimal_from_v1_float(raw_amount)
        else:
            amount = Decimal(str(raw_amount))
        memo = raw.get("memo") or ""
        entries.append(Entry(raw["account"], amount, memo))
    return entries


def _decimal_from_v1_float(value: float) -> Decimal:
    amount = Decimal(repr(value))
    if amount == amount.to_integral_value():
        return amount.quantize(Decimal(1))
    return amount
''',
)

PUBLIC_TESTS = '''"""Public tests for the ledger codec."""

from decimal import Decimal

import pytest

from ledger.codec import UnsupportedVersion, decode, encode
from ledger.codec import Entry


def test_round_trip_v2():
    entries = [Entry("cash", Decimal("10.50")), Entry("fees", Decimal("0.25"), memo="wire")]
    restored = decode(encode(entries))
    assert restored == [
        Entry("cash", Decimal("10.50")),
        Entry("fees", Decimal("0.25"), memo="wire"),
    ]


def test_integer_amounts_stay_integral():
    decoded = decode({"version": 2, "entries": [{"account": "a", "amount": 3}]})
    assert decoded[0].amount == 3
    assert decoded[0].amount.as_tuple().exponent >= 0 or decoded[
        0
    ].amount == decoded[0].amount.to_integral_value()


def test_newer_version_raises():
    with pytest.raises(UnsupportedVersion):
        decode({"version": 3, "entries": []})


def test_missing_memo_defaults_to_empty():
    decoded = decode({"version": 1, "entries": [{"account": "a", "amount": "1"}]})
    assert decoded[0].memo == ""
'''

HIDDEN_TESTS = '''"""Hidden contract: legacy v1 archives decode exactly."""

from decimal import Decimal

from ledger.codec import Entry, UnsupportedVersion, decode


def test_v1_fractional_float_keeps_exact_shortest_value():
    payload = {"version": 1, "entries": [{"account": "cash", "amount": 10.10}]}
    decoded = decode(payload)
    assert decoded[0].amount == Decimal("10.1")


def test_v1_integral_float_normalizes_to_integer_scale():
    # JSON gives 5.0 for an amount the writer meant as 5; the exponent must
    # reflect an integer, not binary-float bookkeeping.
    payload = {"version": 1, "entries": [{"account": "x", "amount": 5.0}]}
    decoded = decode(payload)
    assert decoded[0].amount == 5
    assert decoded[0].amount.as_tuple().exponent == 0


def test_v2_integral_stays_untouched_by_v1_rules():
    payload = {"version": 2, "entries": [{"account": "x", "amount": "5.0"}]}
    decoded = decode(payload)
    assert decoded[0].amount.as_tuple().exponent == -1  # exact string wins


def test_v1_string_amounts_pass_through_exactly():
    payload = {"version": 1, "entries": [{"account": "x", "amount": "12.340"}]}
    decoded = decode(payload)
    assert decoded[0].amount == Decimal("12.340")
    assert decoded[0].amount.as_tuple().exponent == -3


def test_v1_entries_without_memo_and_multiple_rows():
    payload = {
        "version": 1,
        "entries": [
            {"account": "a", "amount": "5"},
            {"account": "b", "amount": "7.5"},
        ],
    }
    decoded = decode(payload)
    assert decoded == [Entry("a", 5), Entry("b", Decimal("7.5"))]


def test_far_future_version_raises_with_helpful_message():
    try:
        decode({"version": 99, "entries": []})
    except UnsupportedVersion as exc:
        assert "99" in str(exc)
    else:
        raise AssertionError("expected UnsupportedVersion")
'''


def main() -> int:
    files = {
        ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
        "pyproject.toml": '[project]\nname = "ledger"\nversion = "2.0.0"\n',
        "ledger/__init__.py": "",
        "ledger/codec.py": CODEC_BROKEN,
        "tests/test_codec.py": PUBLIC_TESTS,
    }
    sha = create_fixture_repo(FIXTURE_DIR, files, "ledger: v2 codec")
    patch_dir = Path(__file__).parent / "reference"
    patch_dir.mkdir(exist_ok=True)
    diff = difflib.unified_diff(
        CODEC_BROKEN.splitlines(keepends=True),
        CODEC_FIXED.splitlines(keepends=True),
        fromfile="a/ledger/codec.py",
        tofile="b/ledger/codec.py",
    )
    (patch_dir / "fix.patch").write_text("".join(diff), encoding="utf-8")
    print(f"fixture repository created at {FIXTURE_DIR}")
    print(f"commit: {sha}")
    pin_commit(YAML_PATH, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
