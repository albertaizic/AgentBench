"""Hidden checks for the settings versioning contract."""

from __future__ import annotations

import pytest

from prefsfile.settings import load_settings


def test_v1_string_true_coerces_to_bool(tmp_path):
    cfg = tmp_path / "s.json"
    cfg.write_text('{"notifications": "true"}', encoding="utf-8")

    assert load_settings(cfg)["notifications"] is True


def test_unknown_versions_rejected(tmp_path):
    cfg = tmp_path / "s.json"
    cfg.write_text('{"schema": 99}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_settings(cfg)


def test_partial_v1_files_never_crash(tmp_path):
    cfg = tmp_path / "s.json"
    cfg.write_text("{}", encoding="utf-8")

    settings = load_settings(cfg)
    assert settings == {"theme": "light", "notifications": True, "retries": 3}
