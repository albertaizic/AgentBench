"""Deterministic generator for the prefsfile fixture (serialization versioning)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "prefsfile"\nversion = "0.2.0"\n',
    # BUG: no v1 handling (missing "schema" crashes via KeyError on compare),
    # string booleans/ints are not coerced, unknown versions silently pass.
    "prefsfile/settings.py": (
        '"""Settings persistence with schema versioning."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import json\n'
        'from pathlib import Path\n\n\n'
        'SCHEMA_VERSION = 2\n\n'
        'DEFAULTS: dict = {"theme": "light", "notifications": True, "retries": 3}\n\n\n'
        'def load_settings(path) -> dict:\n'
        '    data = json.loads(Path(path).read_text(encoding="utf-8"))\n'
        '    version = data.get("schema")\n'
        '    if version is not None and version != SCHEMA_VERSION:\n'
        '        raise ValueError(f"unsupported settings schema: {version}")\n'
        '    settings = {**DEFAULTS}\n'
        '    for key, value in data.items():\n'
        '        if key != "schema":\n'
        '            settings[key] = value\n'
        '    return settings\n'
    ),
    "tests/test_settings.py": (
        '"""Public tests for settings loading."""\n\n'
        'import pytest\n\n'
        'from prefsfile.settings import load_settings\n\n\n'
        'def test_v2_file_loads_as_is(tmp_path):\n'
        '    cfg = tmp_path / "s.json"\n'
        '    cfg.write_text(\'{"schema": 2, "theme": "dark"}\')\n'
        '    assert load_settings(cfg)["theme"] == "dark"\n\n'
        'def test_missing_file_raises(tmp_path):\n'
        '    with pytest.raises(FileNotFoundError):\n'
        '        load_settings(tmp_path / "nope.json")\n\n'
        'def test_v1_string_boolean_becomes_bool(tmp_path):\n'
        '    cfg = tmp_path / "s.json"\n'
        '    cfg.write_text(\'{"notifications": "true"}\')\n'
        '    assert load_settings(cfg)["notifications"] is True\n\n'
        'def test_unknown_version_rejected(tmp_path):\n'
        '    cfg = tmp_path / "s.json"\n'
        '    cfg.write_text(\'{"schema": 99}\')\n'
        '    with pytest.raises(ValueError):\n'
        '        load_settings(cfg)\n\n'
        'def test_v1_defaults_fill_missing_keys(tmp_path):\n'
        '    cfg = tmp_path / "s.json"\n'
        '    cfg.write_text("{}")\n'
        '    assert load_settings(cfg)["retries"] == 3\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "prefsfile: schema-versioned settings", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
