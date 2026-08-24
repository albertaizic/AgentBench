"""Hidden behavioral checks for the HTML-to-text mini-parser."""

from __future__ import annotations

from htmlstrip.core import to_text


def test_script_with_quoted_gt_and_strings():
    markup = "<script>var s = \"</scr\" + \"ipt>\"; if (a<b && c>d) run();</script>ok"
    # The parser must consume the real closing tag; a naive scanner that
    # ends the script at the first "</" would leak the rest.
    assert "run()" not in to_text(markup)
    assert "ok" in to_text(markup)


def test_uppercase_tags_and_mixed_case_entities():
    assert to_text("<P>Up</P>") == "Up"
    assert to_text("&AMP;") == "&"


def test_comment_at_end_without_trailing_content():
    assert to_text("text<!-- gone -->") == "text"


def test_adjacent_inline_tags_do_not_gain_spaces():
    assert to_text("<b>bold</b><i>slant</i>") == "boldslant"


def test_nested_blocks_flatten_with_single_space():
    markup = "<div><p>a</p><p>b</p></div><div>c</div>"
    assert to_text(markup) == "a b c"


def test_entity_in_attribute_is_not_leaked():
    assert to_text('<a href="?x=1&amp;y=2">link</a>') == "link"


def test_unknown_tags_treated_as_inline():
    assert to_text("<custom-x>data</custom-x>") == "data"


def test_empty_input_and_only_comments():
    assert to_text("") == ""
    assert to_text("<!-- nothing -->") == ""
