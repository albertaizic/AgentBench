"""Hidden contract: nested sections merge key-by-key across layers."""
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
