"""Hidden behavioral checks for strict-mode propagation."""

from __future__ import annotations

import json

import pytest

from gate.cli import main
from gate.config import GateConfig
from gate.loader import load_config
from gate.policy import cache_config, evaluate
from gate.remote import fetch_and_load


def _write(tmp_path, payload):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_error_names_first_unknown_key_sorted(tmp_path):
    path = _write(tmp_path, {"retries": 1, "aaa_unknown": 1, "zzz_unknown": 2})
    with pytest.raises(KeyError) as excinfo:
        load_config(path, strict=True)
    assert "aaa_unknown" in str(excinfo.value)


def test_lenient_paths_unchanged(tmp_path):
    path = _write(tmp_path, {"endpoint": "e", "wat": 5})
    cfg = load_config(path)
    assert cfg.extra == {"wat": 5}
    assert cfg.retries == 1 or cfg.retries == 2


def test_remote_strict_clean_payload_ok():
    cfg = fetch_and_load(json.dumps({"retries": 4}), strict=True)
    assert cfg.retries == 4
    assert cfg.strict is True


def test_policy_cache_roundtrip_keeps_strictness():
    cache_config("a", GateConfig(strict=True))
    cache_config("b", GateConfig(strict=False))
    assert evaluate("b", ["endpoint", "nope"]) == ["endpoint"]
    with pytest.raises(KeyError):
        evaluate("a", ["endpoint", "nope"])


def test_cli_remote_path_honors_strict():
    payload = json.dumps({"endpoint": "x", "ghost": True})
    with pytest.raises(KeyError):
        main(["--payload", payload, "--strict"])
