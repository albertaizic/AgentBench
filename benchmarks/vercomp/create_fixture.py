"""Deterministic generator for the vercomp fixture (version ordering bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "vercomp"\nversion = "0.1.0"\n',
    # BUGS: string comparison of numeric parts ("1.10" < "1.9"); pre-release
    # suffix ignored entirely.
    "vercomp/versions.py": (
        '"""Version ordering for the release tool."""\n'
        '\nfrom __future__ import annotations\n\n\n'
        'def compare(a: str, b: str) -> int:\n'
        '    # BUG: lexicographic component comparison.\n'
        '    if a == b:\n'
        '        return 0\n'
        '    return -1 if a < b else 1\n\n'
        'def sort_versions(versions: list[str]) -> list[str]:\n'
        '    return sorted(versions, key=_sort_key)\n\n'
        'def _sort_key(version: str):\n'
        '    # BUG: pre-release suffix is never separated out.\n'
        '    return tuple(version.split("."))\n'
    ),
    "tests/test_versions.py": (
        '"""Public tests for version ordering."""\n\n'
        'from vercomp.versions import compare, sort_versions\n\n\n'
        'def test_numeric_components_compare_numerically():\n'
        '    assert compare("1.10.0", "1.9.0") == 1\n'
        '    assert compare("1.9.0", "1.10.0") == -1\n\n'
        'def test_two_part_versions_still_work():\n'
        '    assert compare("2.3", "2.11") == -1\n'
        '    assert compare("2.3", "2.3") == 0\n\n'
        'def test_prerelease_sorts_before_release():\n'
        '    assert compare("1.2.0-rc.1", "1.2.0") == -1\n\n'
        'def test_prereleases_order_among_themselves():\n'
        '    versions = ["1.0.0-beta", "1.0.0-alpha.2", "1.0.0-alpha.10"]\n'
        '    ordered = sort_versions(versions)\n'
        '    assert ordered[0] == "1.0.0-alpha.2"\n\n'
        'def test_sort_mixed_list():\n'
        '    versions = ["1.9.0", "1.10.0", "1.10.0-rc.1", "2.0.0"]\n'
        '    assert sort_versions(versions) == [\n'
        '        "1.9.0", "1.10.0-rc.1", "1.10.0", "2.0.0",\n'
        '    ]\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "vercomp: release version ordering", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
