"""Deterministic generator for the cfgmerge fixture (precedence bugfix)."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, pin_commit  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

SETTINGS_BROKEN = '''"""Layered settings: CLI args beat env vars beat the config file."""

from __future__ import annotations


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(file_layer: dict, env_layer: dict, cli_layer: dict) -> dict:
    """Merge layers; later arguments win.

    Layers may be None (absent). Within nested dicts a layer only overrides
    the keys it explicitly provides.
    """
    merged: dict = {}
    for layer in (file_layer, env_layer, cli_layer):
        if not layer:
            continue
        # BUG: plain dict.update clobbers whole sub-dicts, so a file section
        # like {"db": {"host": "a", "port": 1}} is wiped by an env override
        # {"db": {"port": 2}} that should only replace port.
        merged.update(layer)
    return merged
'''

SETTINGS_FIXED = SETTINGS_BROKEN.replace(
    """        # BUG: plain dict.update clobbers whole sub-dicts, so a file section
        # like {"db": {"host": "a", "port": 1}} is wiped by an env override
        # {"db": {"port": 2}} that should only replace port.
        merged.update(layer)
""",
    """        merged = _deep_merge(merged, layer)
""",
)

PUBLIC_TESTS = '''"""Public tests for layered settings."""

from appconfig.settings import load_settings


def test_cli_beats_env_beats_file():
    settings = load_settings(
        {"log_level": "info"},
        {"log_level": "debug"},
        {"log_level": "trace"},
    )
    assert settings["log_level"] == "trace"


def test_missing_layers_are_skipped():
    settings = load_settings(None, {"retries": 3}, None)
    assert settings == {"retries": 3}


def test_file_only():
    assert load_settings({"region": "eu"}, {}, {}) == {"region": "eu"}
'''

HIDDEN_TESTS = '''"""Hidden contract: nested sections merge key-by-key across layers."""

from appconfig.settings import load_settings


def test_nested_sections_keep_unspecified_keys_from_lower_layers():
    settings = load_settings(
        {"db": {"host": "db1", "port": 5432, "ssl": True}},
        {"db": {"port": 6543}},
        {},
    )
    assert settings["db"] == {"host": "db1", "port": 6543, "ssl": True}


def test_cli_partial_section_still_wins_over_env_and_file_per_key():
    settings = load_settings(
        {"cache": {"ttl": 60, "backend": "memory"}},
        {"cache": {"ttl": 120}},
        {"cache": {"backend": "redis"}},
    )
    assert settings["cache"] == {"ttl": 120, "backend": "redis"}


def test_three_levels_deep():
    settings = load_settings(
        {"a": {"b": {"c": 1, "d": 2}}},
        {"a": {"b": {"d": 3}}},
        {"a": {"e": 4}},
    )
    assert settings == {"a": {"b": {"c": 1, "d": 3}, "e": 4}}


def test_empty_dicts_do_not_shadow():
    assert load_settings({"x": {"y": 1}}, {"x": {}}, {}) == {"x": {"y": 1}}
'''


def unified(broken: str, fixed: str) -> str:
    diff = difflib.unified_diff(
        broken.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile="a/appconfig/settings.py",
        tofile="b/appconfig/settings.py",
    )
    return "".join(diff)


def main() -> int:
    files = {
        ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
        "pyproject.toml": '[project]\nname = "appconfig"\nversion = "0.3.0"\n',
        "appconfig/__init__.py": "",
        "appconfig/settings.py": SETTINGS_BROKEN,
        "tests/test_settings.py": PUBLIC_TESTS,
    }
    sha = create_fixture_repo(FIXTURE_DIR, files, "appconfig: layered settings")
    patch_dir = Path(__file__).parent / "reference"
    patch_dir.mkdir(exist_ok=True)
    (patch_dir / "fix.patch").write_text(unified(SETTINGS_BROKEN, SETTINGS_FIXED), encoding="utf-8")
    print(f"fixture repository created at {FIXTURE_DIR}")
    print(f"commit: {sha}")
    pin_commit(YAML_PATH, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
