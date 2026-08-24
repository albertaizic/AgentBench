"""Hidden behavioral checks for the iniforge settings loader."""

from __future__ import annotations

import pytest

from iniforge.loader import Settings


def test_last_duplicate_wins_across_many_repeats():
    s = Settings()
    s.load("[t]\nx = on\nx = off\nx = on\n")
    assert s.get("t", "x") is True


def test_first_value_does_not_shadow_later_string():
    s = Settings()
    s.load("[t]\nname = first\nname = second\n")
    assert s.get("t", "name") == "second"


def test_boolean_words_full_matrix():
    true_words = ["true", "TRUE", "True", "yes", "YES", "on", "ON", "1"]
    false_words = ["false", "FALSE", "no", "No", "OFF", "off", "0"]
    for word in true_words:
        s = Settings()
        s.load(f"[m]\nk = {word}\n")
        assert s.get("m", "k") is True, word
    for word in false_words:
        s = Settings()
        s.load(f"[m]\nk = {word}\n")
        assert s.get("m", "k") is False, word


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_values_raise(blank):
    s = Settings()
    with pytest.raises(ValueError):
        s.load(f"[m]\nflag = {blank}\n")


def test_error_message_names_the_offending_key():
    s = Settings()
    with pytest.raises(ValueError) as excinfo:
        s.load("[m]\nspecial_flag =   \n")
    assert "special_flag" in str(excinfo.value)


def test_integers_beyond_bool_words_stay_int():
    s = Settings()
    s.load("[m]\na = 2\nb = -1\n")
    assert s.get("m", "a") == 2
    assert s.get("m", "b") == -1


def test_arbitrary_words_are_strings_not_booleans():
    s = Settings()
    s.load("[m]\nmode = maybe\nstate = enabled\n")
    assert s.get("m", "mode") == "maybe"
    assert s.get("m", "state") == "enabled"


def test_punctuation_bearing_values_are_strings():
    s = Settings()
    s.load("[m]\npath = /usr/local:bin\nnote = wait-listed, ok\n")
    assert s.get("m", "path") == "/usr/local:bin"
    assert s.get("m", "note") == "wait-listed, ok"


def test_comments_and_blank_lines_ignored():
    s = Settings()
    s.load("# header\n; legacy note\n\n[net]\nretry = yes\n")
    assert s.get("net", "retry") is True


def test_root_keys_before_any_section():
    s = Settings()
    s.load("global_flag = ON\n[other]\nk = off\n")
    assert s.get("_root", "global_flag") is True
