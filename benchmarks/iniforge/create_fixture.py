"""Deterministic generator for the iniforge fixture (config parsing bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "iniforge"\nversion = "0.1.0"\n',
    # BUGS: duplicate keys keep the first value (dict.setdefault-style guard),
    # and boolean coercion treats any non-empty string other than the known
    # false-words as True, so junk silently parses.
    "iniforge/loader.py": (
        '"""INI settings loading with typed coercion."""\n'
        '\nfrom __future__ import annotations\n\n'
        'FALSE_WORDS = {"false", "no", "off", "0"}\n'
        'TRUE_WORDS = {"true", "yes", "on", "1"}\n\n\n'
        'class Settings:\n'
        '    def __init__(self) -> None:\n'
        '        self._sections: dict[str, dict[str, object]] = {}\n\n'
        '    def load(self, text: str) -> None:\n'
        '        section = "_root"\n'
        '        for raw_line in text.splitlines():\n'
        '            line = raw_line.strip()\n'
        '            if not line or line.startswith((";", "#")):\n'
        '                continue\n'
        '            if line.startswith("[") and line.endswith("]"):\n'
        '                section = line[1:-1].strip()\n'
        '                self._sections.setdefault(section, {})\n'
        '                continue\n'
        '            key, _, value = line.partition("=")\n'
        '            key = key.strip()\n'
        '            value = value.strip()\n'
        '            bucket = self._sections.setdefault(section, {})\n'
        '            # BUG: first value wins; duplicates are ignored.\n'
        '            if key not in bucket:\n'
        '                bucket[key] = self._coerce(key, value)\n\n'
        '    @staticmethod\n'
        '    def _coerce(key: str, value: str) -> object:\n'
        '        # BUG: loose truthiness - any non-false word becomes True,\n'
        '        # so strings like "maybe" silently parse as boolean True and\n'
        '        # real integers collapse to booleans too.\n'
        '        lowered = value.lower()\n'
        '        if lowered in FALSE_WORDS:\n'
        '            return False\n'
        '        return True\n\n'
        '    def get(self, section: str, key: str, default: object = None) -> object:\n'
        '        return self._sections.get(section, {}).get(key, default)\n'
    ),
    "tests/test_loader.py": (
        '"""Public tests for INI parsing semantics."""\n\n'
        'import pytest\n\n'
        'from iniforge.loader import Settings\n\n\n'
        'def test_last_duplicate_key_wins():\n'
        '    s = Settings()\n'
        '    s.load("[core]\\nmode = off\\nmode = on\\n")\n'
        '    assert s.get("core", "mode") is True\n\n'
        'def test_boolean_true_words_case_insensitive():\n'
        '    s = Settings()\n'
        '    s.load("[flags]\\na = TRUE\\nb = Yes\\nc = On\\nd = 1\\n")\n'
        '    assert all(\n'
        '        s.get("flags", k) is True for k in ("a", "b", "c", "d")\n'
        '    )\n\n'
        'def test_boolean_false_words():\n'
        '    s = Settings()\n'
        '    s.load("[flags]\\na = false\\nb = NO\\nc = Off\\nd = 0\\n")\n'
        '    assert all(\n'
        '        s.get("flags", k) is False for k in ("a", "b", "c", "d")\n'
        '    )\n\n'
        'def test_surrounding_whitespace_tolerated():\n'
        '    s = Settings()\n'
        '    s.load("[flags]\\na =  true \\nb =\\tNo\\n")\n'
        '    assert s.get("flags", "a") is True\n'
        '    assert s.get("flags", "b") is False\n\n'
        'def test_empty_value_raises_value_error_naming_the_key():\n'
        '    s = Settings()\n'
        '    with pytest.raises(ValueError) as excinfo:\n'
        '        s.load("[flags]\\nverbose =\\n")\n'
        '    assert "verbose" in str(excinfo.value)\n\n'
        'def test_plain_strings_pass_through():\n'
        '    s = Settings()\n'
        '    s.load("[ui]\\nname = alpha\\ncity = New York\\n")\n'
        '    assert s.get("ui", "name") == "alpha"\n'
        '    assert s.get("ui", "city") == "New York"\n\n'
        'def test_integers_parse_as_int():\n'
        '    s = Settings()\n'
        '    s.load("[core]\\ntimeout = 30\\nretries = 0\\nneg = -7\\n")\n'
        '    assert s.get("core", "timeout") == 30\n'
        '    assert isinstance(s.get("core", "timeout"), int)\n'
        '    assert s.get("core", "neg") == -7\n'
        '    # "0" is a boolean word AND an integer: booleans win by contract.\n'
        '    assert s.get("core", "retries") is False\n\n'
        'def test_free_form_text_passes_through():\n'
        '    s = Settings()\n'
        '    s.load("[ui]\\nname = alpha prime\\ntitle = red-hot!\\n")\n'
        '    assert s.get("ui", "name") == "alpha prime"\n'
        '    assert s.get("ui", "title") == "red-hot!"\n\n'
        'def test_sections_are_independent():\n'
        '    s = Settings()\n'
        '    s.load("[a]\\nflag = on\\n[b]\\nflag = off\\n")\n'
        '    assert s.get("a", "flag") is True\n'
        '    assert s.get("b", "flag") is False\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "iniforge: INI settings loader", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
