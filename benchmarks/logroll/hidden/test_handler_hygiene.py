"""Hidden behavioral checks for logroll handler hygiene."""

from __future__ import annotations

import io
import logging

from logroll.setup import configure_logger, LOGGER_NAME


def _fresh() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    return logger


def test_many_reloads_never_multiply_handlers():
    logger = _fresh()
    for _ in range(10):
        configure_logger(stream=io.StringIO())
    assert len(logger.handlers) == 1


def test_previous_handler_removed_after_reconfiguration():
    logger = _fresh()
    first = io.StringIO()
    second = io.StringIO()
    configure_logger(stream=first)
    old_handlers = list(logger.handlers)
    configure_logger(stream=second)
    assert not any(h in logger.handlers for h in old_handlers)


def test_same_logger_object_returned_each_time():
    _fresh()
    one = configure_logger()
    two = configure_logger(stream=io.StringIO())
    assert one is two
    assert one.name == LOGGER_NAME


def test_level_change_without_handler_duplication():
    logger = _fresh()
    for level in (logging.DEBUG, logging.INFO, logging.ERROR):
        configure_logger(level=level, stream=io.StringIO())
        assert len(logger.handlers) == 1
        assert logger.level == level


def test_propagation_stays_enabled():
    logger = _fresh()
    configure_logger()
    assert logger.propagate is True


def test_formatter_present_on_active_handler():
    logger = _fresh()
    buffer = io.StringIO()
    configured = configure_logger(stream=buffer)
    configured.warning("careful")
    output = buffer.getvalue()
    assert "WARNING" in output
    assert LOGGER_NAME in output
    assert "careful" in output
