"""Deterministic generator for the csvroll fixture (CSV round-trip bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "csvroll"\nversion = "0.2.0"\n',
    # BUGS: to_csv joins with bare commas (no quoting); from_csv splits on
    # commas and cannot handle quoted fields; header detection is absent so
    # legacy v1 header-less archives fail.
    "csvroll/records.py": (
        '"""Record sets persisted as CSV (v2 with header, v1 without)."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import io\n\n'
        'HEADER = ["id", "label", "amount"]\n\n\n'
        'class RecordSet:\n'
        '    def __init__(self) -> None:\n'
        '        self.rows: list[tuple[str, str, str]] = []\n\n'
        '    def add(self, record_id: str, label: str, amount: str) -> None:\n'
        '        self.rows.append((record_id, label, amount))\n\n'
        '    def to_csv(self) -> str:\n'
        '        # BUG: no quoting - commas/quotes in fields corrupt the file.\n'
        '        lines = [",".join(HEADER)]\n'
        '        lines.extend(",".join(row) for row in self.rows)\n'
        '        return "\\n".join(lines) + "\\n"\n\n'
        '    @classmethod\n'
        '    def from_csv(cls, text: str) -> "RecordSet":\n'
        '        # BUG: naive split cannot parse quoted fields, and v1 files\n'
        '        # (no header row) are misread as data starting at the header.\n'
        '        lines = [line for line in text.splitlines() if line]\n'
        '        rows = []\n'
        '        for line in lines[1:]:\n'
        '            parts = line.split(",")\n'
        '            rows.append((parts[0], parts[1], parts[2]))\n'
        '        record_set = cls()\n'
        '        record_set.rows = rows\n'
        '        return record_set\n'
    ),
    "tests/test_records.py": (
        '"""Public tests for CSV round-tripping."""\n\n'
        'from csvroll.records import RecordSet\n\n\n'
        'def test_plain_round_trip():\n'
        '    rs = RecordSet()\n'
        '    rs.add("1", "widget", "9.99")\n'
        '    rs.add("2", "gadget", "12.50")\n'
        '    restored = RecordSet.from_csv(rs.to_csv())\n'
        '    assert restored.rows == rs.rows\n\n'
        'def test_commas_in_fields_survive():\n'
        '    rs = RecordSet()\n'
        '    rs.add("1", "bolt, M8 x 40", "3.00")\n'
        '    restored = RecordSet.from_csv(rs.to_csv())\n'
        '    assert restored.rows == [("1", "bolt, M8 x 40", "3.00")]\n\n'
        'def test_quotes_in_fields_survive():\n'
        '    rs = RecordSet()\n'
        '    rs.add("7", \'the "deluxe" kit\', "20.00")\n'
        '    restored = RecordSet.from_csv(rs.to_csv())\n'
        '    assert restored.rows == [("7", \'the "deluxe" kit\', "20.00")]\n\n'
        'def test_header_present_in_export():\n'
        '    rs = RecordSet()\n'
        '    rs.add("1", "a", "1")\n'
        '    first_line = rs.to_csv().splitlines()[0]\n'
        '    assert first_line == "id,label,amount"\n\n'
        'def test_legacy_v1_without_header_loads():\n'
        '    text = "1,widget,9.99\\n2,gadget,12.50\\n"\n'
        '    restored = RecordSet.from_csv(text)\n'
        '    assert len(restored.rows) == 2\n'
        '    assert restored.rows[0] == ("1", "widget", "9.99")\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "csvroll: CSV record archive", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
