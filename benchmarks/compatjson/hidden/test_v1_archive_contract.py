"""Hidden contract: legacy v1 archives decode exactly."""
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
