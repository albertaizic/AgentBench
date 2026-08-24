"""Deterministic generator for the retryloop fixture (retry semantics bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "retryloop"\nversion = "0.1.0"\n',
    # BUGS: retries every exception type; returns None when attempts are
    # exhausted instead of re-raising the last RetryableError.
    "retryloop/core.py": (
        '"""Retry execution for flaky outbound calls."""\n'
        '\nfrom __future__ import annotations\n\n\n'
        'class RetryableError(Exception):\n'
        '    """Transient failure: safe to attempt again."""\n\n\n'
        'class FatalError(Exception):\n'
        '    """Permanent failure: retrying cannot help."""\n\n\n'
        'def run_with_retry(operation, attempts: int = 3, on_retry=None):\n'
        '    last_error = None\n'
        '    for attempt in range(attempts):\n'
        '        try:\n'
        '            return operation()\n'
        '        except Exception as exc:\n'
        '            # BUG: retries FatalError too.\n'
        '            last_error = exc\n'
        '            if attempt < attempts - 1 and on_retry is not None:\n'
        '                on_retry(exc, attempt)\n'
        '    # BUG: swallows the final error - callers get None.\n'
        '    return None\n'
    ),
    "tests/test_core.py": (
        '"""Public tests for retry semantics."""\n\n'
        'import pytest\n\n'
        'from retryloop.core import FatalError, RetryableError, run_with_retry\n\n\n'
        'def test_success_on_first_attempt():\n'
        '    calls = []\n'
        '    def op():\n'
        '        calls.append(1)\n'
        '        return "ok"\n'
        '    assert run_with_retry(op) == "ok"\n'
        '    assert len(calls) == 1\n\n'
        'def test_retries_retryable_then_succeeds():\n'
        '    state = {"n": 0}\n'
        '    def flaky():\n'
        '        state["n"] += 1\n'
        '        if state["n"] < 3:\n'
        '            raise RetryableError("boom")\n'
        '        return "recovered"\n'
        '    assert run_with_retry(flaky, attempts=5) == "recovered"\n\n'
        'def test_fatal_error_propagates_immediately():\n'
        '    calls = []\n'
        '    def broken():\n'
        '        calls.append(1)\n'
        '        raise FatalError("no point")\n'
        '    with pytest.raises(FatalError):\n'
        '        run_with_retry(broken, attempts=4)\n'
        '    assert len(calls) == 1\n\n'
        'def test_exhaustion_reraises_last_retryable():\n'
        '    def always():\n'
        '        raise RetryableError("still down")\n'
        '    with pytest.raises(RetryableError, match="still down"):\n'
        '        run_with_retry(always, attempts=3)\n\n'
        'def test_on_retry_fires_between_attempts():\n'
        '    seen = []\n'
        '    state = {"n": 0}\n'
        '    def twice():\n'
        '        state["n"] += 1\n'
        '        if state["n"] <= 2:\n'
        '            raise RetryableError("x")\n'
        '        return 7\n'
        '    assert run_with_retry(twice, attempts=4,\n'
        '                          on_retry=lambda e, i: seen.append(i)) == 7\n'
        '    assert seen == [0, 1]\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "retryloop: retry helper", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
