"""Deterministic generator for the logroll fixture (handler-leak bugfix)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from _corpus_common import create_fixture_repo, main_for  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixture"
YAML_PATH = Path(__file__).parent / "benchmark.yaml"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "pyproject.toml": '[project]\nname = "logroll"\nversion = "0.1.0"\n',
    # BUG: every call appends another handler to the same logger; old file
    # handlers are never closed. Repeated init duplicates every line.
    "logroll/setup.py": (
        '"""Application logging setup."""\n'
        '\nfrom __future__ import annotations\n\n'
        'import logging\n'
        'import sys\n\n\n'
        'LOGGER_NAME = "logroll.app"\n\n\n'
        'def configure_logger(level: int = logging.INFO,\n'
        '                     stream=None) -> logging.Logger:\n'
        '    logger = logging.getLogger(LOGGER_NAME)\n'
        '    logger.setLevel(level)\n'
        '    handler = logging.StreamHandler(stream or sys.stdout)\n'
        '    handler.setFormatter(\n'
        '        logging.Formatter("%(levelname)s %(name)s: %(message)s")\n'
        '    )\n'
        '    # BUG: appends unconditionally - duplicates on every reload and\n'
        '    # leaks previously attached (possibly file) handlers.\n'
        '    logger.addHandler(handler)\n'
        '    return logger\n'
    ),
    "tests/test_setup.py": (
        '"""Public tests for logger configuration hygiene."""\n\n'
        'import io\n'
        'import logging\n\n'
        'from logroll.setup import configure_logger, LOGGER_NAME\n\n\n'
        'def fresh_logger() -> logging.Logger:\n'
        '    logger = logging.getLogger(LOGGER_NAME)\n'
        '    for handler in list(logger.handlers):\n'
        '        logger.removeHandler(handler)\n'
        '    return logger\n\n\n'
        'def test_repeated_configuration_is_idempotent():\n'
        '    logger = fresh_logger()\n'
        '    configure_logger()\n'
        '    configure_logger()\n'
        '    configure_logger()\n'
        '    assert len(logger.handlers) == 1\n\n'
        'def test_message_appears_once():\n'
        '    logger = fresh_logger()\n'
        '    buffer = io.StringIO()\n'
        '    configured = configure_logger(stream=buffer)\n'
        '    configured.info("hello")\n'
        '    assert buffer.getvalue().count("hello") == 1\n\n'
        'def test_changing_stream_replaces_handler():\n'
        '    logger = fresh_logger()\n'
        '    first = io.StringIO()\n'
        '    second = io.StringIO()\n'
        '    configure_logger(stream=first)\n'
        '    configure_logger(stream=second)\n'
        '    assert len(logger.handlers) == 1\n'
        '    logging.getLogger(LOGGER_NAME).info("again")\n'
        '    assert "again" in second.getvalue()\n'
        '    assert "again" not in first.getvalue()\n\n'
        'def test_level_change_applies():\n'
        '    logger = fresh_logger()\n'
        '    configure_logger(level=logging.WARNING)\n'
        '    assert logger.level == logging.WARNING\n'
    ),
}


def main() -> int:
    return main_for(FIXTURE_DIR, FILES, "logroll: logging setup", YAML_PATH)


if __name__ == "__main__":
    sys.exit(main())
