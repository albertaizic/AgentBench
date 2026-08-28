"""Hidden behavioral checks for schemamigrate version/round-trip contract.

Uses different data than the public tests on purpose.
"""

from __future__ import annotations

import json

from schemamigrate.compat import upgrade_document
from schemamigrate.parser import detect_version, loads
from schemamigrate.serializer import dumps

V1_B = json.dumps(
    {
        "cache.ttl": 900,
        "database.url": "postgresql://analytics.internal/prod",
        "telemetry.endpoint": "https://telemetry.invalid/ingest",
    }
)

V2_B = json.dumps(
    {
        "version": 2,
        "cache": {"ttl": 1200},
        "logging": {"level": "warning", "format": "json"},
        "rate_limits": {"api_per_minute": 60},
    }
)


def test_string_version_marker_is_not_v2():
    doc = loads(json.dumps({"version": "2", "database.host": "h"}))
    assert doc.format_version == 1


def test_detection_rejects_non_mappings():
    assert detect_version(None) == 1
    assert detect_version(["version"]) == 1
    assert detect_version({"version": 2}) == 2


def test_empty_document_round_trips_as_v1():
    doc = loads("{}")
    assert doc.format_version == 1
    assert loads(dumps(doc)) == doc


def test_null_and_complex_values_survive_v1_round_trip():
    doc = loads(
        json.dumps(
            {
                "database.pool": None,
                "workers.backends": ["redis", "memcached"],
                "retry.jitter": False,
                "alert.threshold": 0.75,
            }
        )
    )
    restored = loads(dumps(doc))
    assert restored == doc
    assert restored.extras["workers.backends"] == ["redis", "memcached"]
    assert restored.setting("database", "pool") is None


def test_v2_unknown_section_with_nesting_preserved():
    doc = loads(V2_B)
    restored = loads(dumps(doc))
    assert restored == doc
    assert restored.extras["rate_limits"] == {"api_per_minute": 60}
    assert restored.setting("logging", "format") == "json"


def test_repeated_round_trips_are_stable_for_both_layouts():
    for text in (V1_B, V2_B):
        original = loads(text)
        current = original
        for _ in range(3):
            current = loads(dumps(current))
        assert current == original


def test_explicit_upgrade_keeps_extras_and_writes_v2():
    doc = loads(V1_B)
    upgraded = upgrade_document(doc)
    assert upgraded.format_version == 2
    reloaded = loads(dumps(upgraded))
    assert reloaded.format_version == 2
    assert reloaded.extras == doc.extras
    assert reloaded.setting("database", "url") == "postgresql://analytics.internal/prod"


def test_known_section_with_scalar_value_in_v2_goes_to_extras():
    # A v2 payload whose "database" entry is not a mapping cannot populate a
    # section; it must be kept verbatim so the round trip stays lossless.
    text = json.dumps({"version": 2, "database": "legacy-flat"})
    doc = loads(text)
    assert doc.sections == {}
    assert loads(dumps(doc)) == doc
