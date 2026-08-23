"""Tests for protected-path detection (agentbench.protected)."""

from __future__ import annotations

from agentbench.protected import find_violations


class TestGlobMatching:
    def test_exact_path_matches(self):
        violations = find_violations(["pyproject.toml"], ["pyproject.toml"])

        assert len(violations) == 1
        assert violations[0].path == "pyproject.toml"

    def test_doublestar_matches_nested_paths(self):
        violations = find_violations(
            ["tests/test_deep/nested/test_x.py", "src/app.py"], ["tests/**"]
        )

        assert [v.path for v in violations] == ["tests/test_deep/nested/test_x.py"]

    def test_single_star_crosses_directories_per_fnmatch_semantics(self):
        # fnmatch's '*' matches '/' too (documented stdlib behavior), so
        # 'tests/*.py' reaches nested paths as long as the name ends .py.
        violations = find_violations(["tests/nested/test_a.py"], ["tests/*.py"])

        assert len(violations) == 1

    def test_unmodified_paths_never_match(self):
        assert find_violations(["src/app.py", "README.md"], ["tests/**", "pyproject.toml"]) == []

    def test_windows_style_separators_are_normalized(self):
        # Defensive: git reports POSIX separators, but if a path ever arrives
        # with backslashes it must still match.
        violations = find_violations(["tests\\test_x.py"], ["tests/**"])

        assert len(violations) == 1
        assert violations[0].path == "tests/test_x.py"

    def test_multiple_patterns_each_reported(self):
        violations = find_violations(["tests/test_a.py"], ["tests/**", "tests/test_*.py"])

        assert {(v.path, v.pattern) for v in violations} == {
            ("tests/test_a.py", "tests/**"),
            ("tests/test_a.py", "tests/test_*.py"),
        }

    def test_matching_is_case_sensitive(self):
        # Git paths are authoritative and case-sensitive on the record.
        assert find_violations(["Tests/test_a.py"], ["tests/**"]) == []
