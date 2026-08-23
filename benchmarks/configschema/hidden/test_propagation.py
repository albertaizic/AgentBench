"""Hidden checks: strict mode is propagated by EVERY caller."""

from __future__ import annotations

import pytest

from configschema.cli import describe
from configschema.loader import load_config
from configschema.server import settings_from


@pytest.fixture(name="typo_config")
def typo_config_fixture(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"host": "ok", "prot": 1234}', encoding="utf-8")
    return cfg


def test_loader_default_becomes_strict(typo_config):
    with pytest.raises(ValueError):
        load_config(typo_config)


def test_settings_from_rejects_typo(typo_config):
    with pytest.raises(ValueError):
        settings_from(typo_config)


def test_describe_rejects_typo(typo_config):
    with pytest.raises(ValueError):
        describe(typo_config)


def test_valid_configs_still_work(tmp_path):
    cfg = tmp_path / "good.json"
    cfg.write_text('{"host": "0.0.0.0", "port": 9000}', encoding="utf-8")
    settings = settings_from(cfg)
    assert (settings.host, settings.port) == ("0.0.0.0", 9000)
    assert "0.0.0.0:9000" in describe(cfg)
