"""Deterministic generator for the typegate fixture (flag propagation task)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "gate"\nversion = "0.3.0"\n',
    # Cross-file propagation: strict exists on the dataclass and in ONE
    # loader path; remote, cli, and the policy cache all drop it on the floor.
    "gate/config.py": (
        '"""Gate configuration model."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from dataclasses import dataclass, field\n\n\n'
        '@dataclass\nclass GateConfig:\n'
        '    endpoint: str = "https://example.invalid"\n'
        '    retries: int = 2\n'
        '    strict: bool = False          # NEW flag - must be honored everywhere\n'
        '    extra: dict = field(default_factory=dict)\n'
    ),
    "gate/loader.py": (
        '"""Config file loading."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import json\n\n'
        'from gate.config import GateConfig\n\n\n'
        'def load_config(path, strict: bool = False) -> GateConfig:\n'
        '    payload = json.loads(open(path, encoding="utf-8").read())\n'
        '    known = {"endpoint", "retries", "strict"}\n'
        '    unknown = {k: v for k, v in payload.items() if k not in known}\n'
        '    if strict and unknown:\n'
        '        first_unknown = sorted(unknown)[0]\n'
        '        raise KeyError(f"unknown config key: {first_unknown}")\n'
        '    return GateConfig(\n'
        '        endpoint=payload.get("endpoint", "https://example.invalid"),\n'
        '        retries=payload.get("retries", 2),\n'
        '        strict=strict,\n'
        '        extra=unknown,\n'
        '    )\n'
    ),
    "gate/remote.py": (
        '"""Fetch configs from the settings service."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import json\n\n'
        'from gate.config import GateConfig\n\n\n'
        'def fetch_and_load(payload: str, strict: bool = False) -> GateConfig:\n'
        '    # BUG: strict is accepted then ignored.\n'
        '    data = json.loads(payload)\n'
        '    known = {"endpoint", "retries", "strict"}\n'
        '    unknown = {k: v for k, v in data.items() if k not in known}\n'
        '    return GateConfig(\n'
        '        endpoint=data.get("endpoint", "https://example.invalid"),\n'
        '        retries=data.get("retries", 2),\n'
        '        strict=False,\n'
        '        extra=unknown,\n'
        '    )\n'
    ),
    "gate/policy.py": (
        '"""Policy evaluation over a tiny config cache."""\n'
        '\nfrom __future__ import annotations\n\n'
        'from gate.config import GateConfig\n\n\n'
        '_cache: dict[str, GateConfig] = {}\n\n\n'
        'def cache_config(name: str, config: GateConfig) -> None:\n'
        '    _cache[name] = config\n\n\n'
        'def evaluate(name: str, payload_keys: list[str]) -> list[str]:\n'
        '    # BUG: cached strictness is never consulted.\n'
        '    config = _cache[name]\n'
        '    allowed = {"endpoint", "retries", "strict"}\n'
        '    return [k for k in payload_keys if k in allowed]\n'
    ),
    "gate/cli.py": (
        '"""Command line entry point."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import argparse\n\n'
        'from gate.loader import load_config\n'
        'from gate.remote import fetch_and_load\n\n\n'
        'def main(argv=None):\n'
        '    parser = argparse.ArgumentParser(prog="gate")\n'
        '    parser.add_argument("--file")\n'
        '    parser.add_argument("--payload")\n'
        '    parser.add_argument("--strict", action="store_true")\n'
        '    args = parser.parse_args(argv)\n'
        '    # BUG: --strict parsed but never forwarded.\n'
        '    if args.file:\n'
        '        return load_config(args.file)\n'
        '    return fetch_and_load(args.payload or "{}")\n'
    ),
    "tests/test_propagation.py": (
        '"""Public tests for strict-mode end-to-end behavior."""\n\n'
        'import json\n'
        'import pytest\n\n'
        'from gate.loader import load_config\n'
        'from gate.remote import fetch_and_load\n'
        'from gate.policy import cache_config, evaluate\n\n\n'
        'def test_remote_strict_rejects_unknown():\n'
        '    payload = json.dumps({"endpoint": "x", "wat": 1})\n'
        '    with pytest.raises(KeyError):\n'
        '        fetch_and_load(payload, strict=True)\n\n'
        'def test_remote_lenient_collects_extra():\n'
        '    cfg = fetch_and_load(json.dumps({"endpoint": "x", "wat": 1}))\n'
        '    assert cfg.extra == {"wat": 1}\n'
        '    assert cfg.strict is False\n\n'
        'def test_cli_strict_flag_reaches_loader(tmp_path):\n'
        '    from gate.cli import main\n'
        '    path = tmp_path / "cfg.json"\n'
        '    path.write_text(json.dumps({"endpoint": "e", "surprise": true_value()}), encoding="utf-8")\n'
        '    with pytest.raises(KeyError):\n'
        '        main(["--file", str(path), "--strict"])\n\n'
        'def true_value():\n'
        '    return True  # helper keeps the JSON literal readable\n\n\n'
        'def test_policy_respects_cached_strictness():\n'
        '    from gate.config import GateConfig\n'
        '    cache_config("s", GateConfig(strict=True))\n'
        '    with pytest.raises(KeyError):\n'
        '        evaluate("s", ["endpoint", "mystery"])\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "gate: strict-mode propagation", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
