"""Deterministic generator for the htmlstrip fixture (mini-parser task)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "htmlstrip"\nversion = "0.1.0"\n',
    # BUGS: regex chain cannot handle > inside script/style bodies, comments
    # with tags/dashes, double-unescape of entities, and missing whitespace
    # separation between block elements.
    "htmlstrip/core.py": (
        '"""Convert small HTML fragments to plain text."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import html as _html\n'
        'import re\n\n'
        '_TAG = re.compile(r"<[^>]*>")\n'
        '_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)\n'
        '_BLOCKS = ("p", "div", "br", "li", "/p", "/div", "/li")\n\n\n'
        'def to_text(markup: str) -> str:\n'
        '    # BUG 1/2: comment and tag regexes stop at the first ">" so a\n'
        '    # script body like "if (a > b)" leaks into the output.\n'
        '    markup = _COMMENT.sub(" ", markup)\n'
        '    markup = _TAG.sub(" ", markup)\n'
        '    # BUG 3: unescape applied twice - "&amp;lt;" collapses to "<".\n'
        '    text = _html.unescape(_html.unescape(markup))\n'
        '    # BUG 4: no block-level whitespace handling beyond tag spaces.\n'
        '    return re.sub(r"\\s+", " ", text).strip()\n'
    ),
    "tests/test_core.py": (
        '"""Public tests for HTML-to-text conversion."""\n\n'
        'from htmlstrip.core import to_text\n\n\n'
        'def test_simple_paragraph():\n'
        '    assert to_text("<p>Hello world</p>") == "Hello world"\n\n'
        'def test_script_body_with_gt_is_removed_entirely():\n'
        '    markup = "<p>a</p><script>if (x > y) { grow(); }</script><p>b</p>"\n'
        '    assert to_text(markup) == "a b"\n\n'
        'def test_style_body_removed():\n'
        '    markup = "<style>p > div { color: red; }</style>visible"\n'
        '    assert to_text(markup) == "visible"\n\n'
        'def test_comment_containing_tags_is_dropped():\n'
        '    markup = "before<!-- <b>x</b> -- note -->after"\n'
        '    assert to_text(markup) == "before after"\n\n'
        'def test_single_unescape_only():\n'
        '    assert to_text("<p>&amp;lt;</p>") == "&lt;"\n'
        '    assert to_text("&lt;b&gt;") == "<b>"\n\n'
        'def test_block_tags_separate_words():\n'
        '    assert to_text("<div>alpha</div><div>beta</div>") == "alpha beta"\n'
        '    assert to_text("one<br>two") == "one two"\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "htmlstrip: HTML to text", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
