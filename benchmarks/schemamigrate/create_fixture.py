"""Deterministic generator for the schemamigrate fixture (config schema migration)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

INIT_PY = '''"""Config schema migration toolkit (v1 flat <-> v2 nested)."""

from .compat import flatten_document, nest_payload, upgrade_document
from .model import ConfigDocument, KNOWN_SECTIONS
from .parser import detect_version, load, loads
from .serializer import dumps

__all__ = [
    "ConfigDocument",
    "KNOWN_SECTIONS",
    "flatten_document",
    "nest_payload",
    "upgrade_document",
    "detect_version",
    "load",
    "loads",
    "dumps",
]
'''

MODEL_PY = '''"""In-memory representation of a configuration document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Section names the current schema knows about. Anything else is "extra".
KNOWN_SECTIONS = ("database", "logging", "cache")


@dataclass
class ConfigDocument:
    """A parsed configuration document.

    ``format_version`` records which on-disk layout the document uses (1 =
    flat dotted keys, no version marker; 2 = ``"version": 2`` plus nested
    sections). ``sections`` holds known-section settings; ``extras`` holds
    everything the schema does not model, which must survive round trips.
    """

    format_version: int
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def setting(self, section: str, key: str, default: Any = None) -> Any:
        return self.sections.get(section, {}).get(key, default)
'''

COMPAT_PY = '''"""Structural converters between the v1 flat layout and the v2 nested layout."""

from __future__ import annotations

from typing import Any

from .model import KNOWN_SECTIONS, ConfigDocument


def split_flat_key(key: str) -> tuple[str | None, str]:
    """Split ``"section.key"`` into ``(section, key)`` for known sections.

    Returns ``(None, key)`` when the key does not belong to a known section
    (including keys without a dot at all).
    """
    section, sep, rest = key.partition(".")
    if sep and section in KNOWN_SECTIONS and rest:
        return section, rest
    return None, key


def flatten_document(document: ConfigDocument) -> dict[str, Any]:
    """Project a document onto the flat v1 mapping (``section.key`` -> value)."""
    flat: dict[str, Any] = {}
    for section, values in document.sections.items():
        for key, value in values.items():
            flat[f"{section}.{key}"] = value
    flat.update(document.extras)
    return flat


def nest_payload(flat: dict[str, Any]) -> dict[str, Any]:
    """Build a v2-style payload (version marker + nested sections) from flat keys."""
    payload: dict[str, Any] = {"version": 2}
    for key, value in flat.items():
        section, rest = split_flat_key(key)
        if section is None:
            payload[key] = value
        else:
            payload.setdefault(section, {})[rest] = value
    return payload


def upgrade_document(document: ConfigDocument) -> ConfigDocument:
    """Explicit opt-in v1 -> v2 upgrade; extras are carried over verbatim."""
    upgraded = ConfigDocument(format_version=2)
    upgraded.sections = {name: dict(values) for name, values in document.sections.items()}
    upgraded.extras = dict(document.extras)
    return upgraded
'''

PARSER_PY = '''"""Load configuration text into a ConfigDocument, auto-detecting the layout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .compat import split_flat_key
from .model import KNOWN_SECTIONS, ConfigDocument


def detect_version(payload: object) -> int:
    """A payload is v2 iff it is a mapping whose ``version`` entry is exactly 2."""
    if isinstance(payload, dict) and payload.get("version") == 2:
        return 2
    return 1


def _parse_v2(raw: dict[str, Any]) -> ConfigDocument:
    document = ConfigDocument(format_version=2)
    for key, value in raw.items():
        if key == "version":
            continue
        if key in KNOWN_SECTIONS and isinstance(value, dict):
            document.sections[key] = dict(value)
        else:
            document.extras[key] = value
    return document


def _parse_v1(raw: dict[str, Any]) -> ConfigDocument:
    document = ConfigDocument(format_version=1)
    for key, value in raw.items():
        section, rest = split_flat_key(key)
        if section is None:
            document.extras[key] = value
        else:
            document.sections.setdefault(section, {})[rest] = value
    return document


def loads(text: str) -> ConfigDocument:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("configuration root must be a JSON object")
    if detect_version(payload) == 2:
        return _parse_v2(payload)
    return _parse_v1(payload)


def load(path: str | Path) -> ConfigDocument:
    return loads(Path(path).read_text(encoding="utf-8"))
'''

SERIALIZER_PY = '''"""Serialize a ConfigDocument back to configuration text."""

from __future__ import annotations

import json
from typing import Any

from .compat import nest_payload
from .model import ConfigDocument


def dumps(document: ConfigDocument) -> str:
    # BUG: the write path ignores document.format_version entirely - every
    # document is re-emitted in the v2 nested layout (silently upgrading v1
    # inputs), and the unmodelled settings held in ``extras`` are dropped
    # from the payload altogether.
    payload: dict[str, Any] = nest_payload(
        {
            f"{section}.{key}": value
            for section, values in document.sections.items()
            for key, value in values.items()
        }
    )
    return json.dumps(payload, indent=2, sort_keys=True) + "\\n"
'''

TEST_PARSER_PY = '''"""Public tests for layout auto-detection."""

from __future__ import annotations

import json

import pytest

from schemamigrate.parser import detect_version, loads

V1_TEXT = json.dumps(
    {
        "database.host": "db01.internal",
        "database.port": 5432,
        "logging.level": "info",
        "search.index": "main",
    }
)

V2_TEXT = json.dumps(
    {
        "version": 2,
        "database": {"host": "db02.internal", "port": 6543},
        "logging": {"level": "debug"},
        "feature_flags": {"new_ui": True},
    }
)


def test_detects_v1_flat_document():
    doc = loads(V1_TEXT)
    assert doc.format_version == 1
    assert doc.setting("database", "host") == "db01.internal"
    assert doc.setting("logging", "level") == "info"


def test_detects_v2_nested_document():
    doc = loads(V2_TEXT)
    assert doc.format_version == 2
    assert doc.setting("database", "port") == 6543
    assert doc.extras == {"feature_flags": {"new_ui": True}}


def test_missing_version_marker_means_v1():
    assert detect_version({"database.host": "x"}) == 1


def test_unknown_section_key_lands_in_extras():
    doc = loads('{"queue.driver": "redis"}')
    assert doc.extras == {"queue.driver": "redis"}
    assert doc.sections == {}


def test_non_object_root_is_rejected():
    with pytest.raises(ValueError):
        loads('[1, 2, 3]')
'''

TEST_ROUNDTRIP_PY = '''"""Public tests for lossless load/save round trips."""

from __future__ import annotations

import json

from schemamigrate.compat import upgrade_document
from schemamigrate.parser import loads
from schemamigrate.serializer import dumps

V1_TEXT = json.dumps(
    {
        "database.host": "db01.internal",
        "database.port": 5432,
        "logging.level": "info",
        "search.index": "main",
    }
)

V2_TEXT = json.dumps(
    {
        "version": 2,
        "database": {"host": "db02.internal", "port": 6543},
        "logging": {"level": "debug"},
        "feature_flags": {"new_ui": True},
    }
)


def test_v2_round_trip_preserves_everything():
    doc = loads(V2_TEXT)
    assert loads(dumps(doc)) == doc


def test_v1_documents_stay_v1_after_round_trip():
    doc = loads(V1_TEXT)
    reloaded = loads(dumps(doc))
    assert reloaded.format_version == 1
    assert reloaded == doc


def test_v1_round_trip_preserves_unknown_settings():
    doc = loads(V1_TEXT)
    reloaded = loads(dumps(doc))
    assert reloaded.extras == {"search.index": "main"}
    assert reloaded.setting("database", "host") == "db01.internal"


def test_round_trip_is_idempotent_for_both_layouts():
    for text in (V1_TEXT, V2_TEXT):
        original = loads(text)
        assert loads(dumps(original)) == original
        twice = loads(dumps(loads(dumps(original))))
        assert twice == original


def test_explicit_upgrade_is_the_only_v1_to_v2_path():
    upgraded = upgrade_document(loads(V1_TEXT))
    assert upgraded.format_version == 2
    reloaded = loads(dumps(upgraded))
    assert reloaded.format_version == 2
    assert reloaded.setting("database", "host") == "db01.internal"
    assert reloaded.extras == {"search.index": "main"}
'''

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "schemamigrate"\nversion = "0.3.0"\n',
    "schemamigrate/__init__.py": INIT_PY,
    "schemamigrate/model.py": MODEL_PY,
    "schemamigrate/compat.py": COMPAT_PY,
    "schemamigrate/parser.py": PARSER_PY,
    "schemamigrate/serializer.py": SERIALIZER_PY,
    "tests/test_parser.py": TEST_PARSER_PY,
    "tests/test_roundtrip.py": TEST_ROUNDTRIP_PY,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "schemamigrate: config schema v1/v2 toolkit", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
