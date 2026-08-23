"""Deterministic generator for the configschema fixture (cross-file API change)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "configschema"\nversion = "0.2.0"\n',
    # BUG: strict mode exists in the loader but defaults to False and callers
    # were never updated; unknown keys silently pass through.
    "configschema/loader.py": (
        '"""JSON config loading with optional strict key checking."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import json\n'
        'from pathlib import Path\n\n\n'
        'ALLOWED_KEYS = {"host", "port", "retries"}\n\n\n'
        'def load_config(path: str | Path, *, strict: bool = False) -> dict:\n'
        '    """Load config JSON. In strict mode, unknown keys raise ValueError."""\n'
        '    data = json.loads(Path(path).read_text(encoding="utf-8"))\n'
        '    if not isinstance(data, dict):\n'
        '        raise ValueError("config must be a JSON object")\n'
        '    if strict:\n'
        '        unknown = set(data) - ALLOWED_KEYS\n'
        '        if unknown:\n'
        '            raise ValueError(f"unknown config keys: {sorted(unknown)}")\n'
        '    return data\n'
    ),
    # BUG: server.py still uses non-strict load, so typos like "prot" apply nothing.
    "configschema/server.py": (
        '"""HTTP server settings derived from the config file."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from configschema.loader import load_config\n\n\n'
        'DEFAULTS = {"host": "127.0.0.1", "port": 8000, "retries": 3}\n\n\n'
        'class ServerSettings:\n'
        '    def __init__(self, values: dict) -> None:\n'
        '        merged = {**DEFAULTS, **values}\n'
        '        self.host = merged["host"]\n'
        '        self.port = int(merged["port"])\n'
        '        self.retries = int(merged["retries"])\n\n\n'
        'def settings_from(path) -> ServerSettings:\n'
        '    return ServerSettings(load_config(path))\n'
    ),
    # BUG: cli.py likewise loads non-strictly.
    "configschema/cli.py": (
        '"""Command line entry point for the demo service."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from configschema.loader import load_config\n\n\n'
        'def describe(path) -> str:\n'
        '    data = load_config(path)\n'
        '    host = data.get("host", "127.0.0.1")\n'
        '    port = data.get("port", 8000)\n'
        '    return f"serving on {host}:{port}"\n'
    ),
    "tests/test_loader.py": (
        'import pytest\n\n'
        'from configschema.loader import load_config\n\n\n'
        'def test_strict_mode_rejects_unknown_keys(tmp_path):\n'
        '    cfg = tmp_path / "c.json"\n'
        '    cfg.write_text(\'{"host": "h", "prot": 80}\')\n'
        '    with pytest.raises(ValueError):\n'
        '        load_config(cfg, strict=True)\n\n'
        'def test_explicit_lenient_mode_tolerates_unknown_keys(tmp_path):\n'
        '    cfg = tmp_path / "c.json"\n'
        '    cfg.write_text(\'{"host": "h", "prot": 80}\')\n'
        '    assert load_config(cfg, strict=False)["host"] == "h"\n'
    ),
    "tests/test_callers.py": (
        '"""Public tests: every caller must use strict mode."""\n\n'
        'import pytest\n\n'
        'from configschema.cli import describe\n'
        'from configschema.server import settings_from\n\n\n'
        'def _bad_config(tmp_path):\n'
        '    cfg = tmp_path / "c.json"\n'
        '    cfg.write_text(\'{"prot": 9999}\')\n'
        '    return cfg\n\n\n'
        'def test_settings_from_is_strict(tmp_path):\n'
        '    with pytest.raises(ValueError):\n'
        '        settings_from(_bad_config(tmp_path))\n\n'
        'def test_describe_is_strict(tmp_path):\n'
        '    with pytest.raises(ValueError):\n'
        '        describe(_bad_config(tmp_path))\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "configschema: introduce strict loading", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
