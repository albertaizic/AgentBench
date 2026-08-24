"""Deterministic generator for the testforge fixture (write-the-tests task)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

SLUG_PY = (
    '"""Slugifier: turn titles into URL-safe slugs.\n'
    '\n'
    'Contract (slugify(title, *, sep="-", max_length=None)):\n'
    '\n'
    '* the result is lowercased;\n'
    '* every maximal run of characters that are not ASCII letters, digits,\n'
    '  or the separator collapses into ONE separator character;\n'
    '* leading and trailing separators are trimmed;\n'
    '* sep may only be "-" or "_" - anything else raises ValueError before\n'
    '  any other work happens;\n'
    '* max_length (when given) truncates the slug to at most that many\n'
    '  characters, never leaving a trailing separator; it is applied after\n'
    '  all other rules and must be a positive integer, else ValueError;\n'
    '* a title with no usable characters yields "".\n'
    '"""\n'
    '\n'
    'from __future__ import annotations\n\n'
    'import string\n\n'
    '_OK = set(string.ascii_letters + string.digits)\n\n\n'
    'def slugify(title: str, *, sep: str = "-", max_length: int | None = None) -> str:\n'
    '    if sep not in ("-", "_"):\n'
    '        raise ValueError(f"sep must be \'-\' or \'_\', got {sep!r}")\n'
    '    if max_length is not None and (not isinstance(max_length, int)\n'
    '                                   or isinstance(max_length, bool)\n'
    '                                   or max_length <= 0):\n'
    '        raise ValueError("max_length must be a positive integer")\n'
    '    out = []\n'
    '    pending_sep = False\n'
    '    for char in title.lower():\n'
    '        if char in _OK:\n'
    '            if pending_sep and out:\n'
    '                out.append(sep)\n'
    '            pending_sep = False\n'
    '            out.append(char)\n'
    '        else:\n'
    '            pending_sep = pending_sep or bool(out)\n'
    '    while out and out[-1] == sep:\n'
    '        out.pop()\n'
    '    result = "".join(out)\n'
    '    if max_length is not None and len(result) > max_length:\n'
    '        result = result[:max_length].rstrip(sep)\n'
    '    return result\n'
)

CHECKER = '''"""Mutation-based grading for tests/test_slug.py (deterministic).

Exits 0 only when the agent-written suite passes on the real implementation
AND kills every seeded mutant. The implementation itself must never be
edited by hand here beyond the exact mutations below.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
REAL = (REPO / "textforge" / "slug.py").read_text(encoding="utf-8")

# Each mutant: (description, old_snippet, new_snippet). Snippets are exact,
# unique substrings of the canonical source.
MUTANTS = [
    ("lowercasing dropped",
     "for char in title.lower():",
     "for char in title:"),
    ("separator runs not collapsed",
     "pending_sep = pending_sep or bool(out)",
     "pending_sep = True"),
    ("trailing separator not trimmed",
     "while out and out[-1] == sep:",
     "while False:"),
    ("max_length boundary mishandled",
     "result = result[:max_length].rstrip(sep)",
     "result = result[:max_length]"),
    ("separator validation skipped",
     'if sep not in ("-", "_"):',
     "if False:"),
]


def run_pytests(root: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_slug.py"],
        cwd=root, capture_output=True, text=True,
    ).returncode


def main() -> int:
    tests_file = REPO / "tests" / "test_slug.py"
    if not tests_file.exists():
        print("FAIL: tests/test_slug.py does not exist")
        return 1

    with tempfile.TemporaryDirectory(prefix="tf-mut-") as tmp:
        base = Path(tmp)
        shutil.copytree(REPO, base / "real",
                        ignore=shutil.ignore_patterns(".git"))
        if run_pytests(base / "real") != 0:
            print("FAIL: suite does not pass on the real implementation")
            return 1

        for index, (why, old, new) in enumerate(MUTANTS):
            assert REAL.count(old) == 1, f"mutation {index} anchor not unique"
            root = base / f"mut{index}"
            shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(".git"))
            target = root / "textforge" / "slug.py"
            target.write_text(REAL.replace(old, new), encoding="utf-8")
            code = run_pytests(root)
            if code == 0:
                print(f"FAIL: mutant survived: {why}")
                return 1

    print("PASS: suite passes on real code and kills all "
          f"{len(MUTANTS)} mutants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "textforge"\nversion = "0.1.0"\n',
    "textforge/slug.py": SLUG_PY,
    "textforge/__init__.py": "",
    "check_tests.py": CHECKER,
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "textforge: write the missing test suite", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
