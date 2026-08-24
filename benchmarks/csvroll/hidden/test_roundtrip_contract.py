"""Hidden behavioral checks for csvroll round-tripping and migration."""

from __future__ import annotations

from csvroll.records import RecordSet


def test_embedded_newline_in_quoted_field_survives():
    rs = RecordSet()
    rs.add("1", "line one\nline two", "5.00")
    restored = RecordSet.from_csv(rs.to_csv())
    assert restored.rows == [("1", "line one\nline two", "5.00")]


def test_empty_fields_preserved():
    rs = RecordSet()
    rs.add("3", "", "")
    restored = RecordSet.from_csv(rs.to_csv())
    assert restored.rows == [("3", "", "")]


def test_unicode_labels_round_trip():
    rs = RecordSet()
    rs.add("9", "café ☕, large", "4.20")
    restored = RecordSet.from_csv(rs.to_csv())
    assert restored.rows == [("9", "café ☕, large", "4.20")]


def test_v2_detection_is_exact_header_match():
    # A first line that merely CONTAINS header-like words is still data (v1):
    # detection is an exact match against the canonical header.
    text = 'idx,"label, with comma",5.00\n'
    restored = RecordSet.from_csv(text)
    assert restored.rows == [("idx", "label, with comma", "5.00")]


def test_many_rows_order_stable():
    rs = RecordSet()
    for index in range(25):
        rs.add(str(index), f"label {index}, extra", str(index) + ".00")
    restored = RecordSet.from_csv(rs.to_csv())
    assert restored.rows == rs.rows
