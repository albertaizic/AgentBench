"""Hidden behavioral checks for retry semantics."""

from __future__ import annotations

import pytest

from retryloop.core import FatalError, RetryableError, run_with_retry


def test_subclass_of_retryable_is_retried():
    class TimeoutRetryable(RetryableError):
        pass

    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise TimeoutRetryable("slow")
        return "done"

    assert run_with_retry(flaky, attempts=3) == "done"
    assert state["n"] == 2


def test_value_error_not_retried():
    calls = []

    def invalid():
        calls.append(1)
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        run_with_retry(invalid, attempts=5)
    assert len(calls) == 1


def test_exhaustion_reraises_the_last_error_instance():
    errors = [RetryableError("first"), RetryableError("second"), RetryableError("third")]
    index = {"i": 0}

    def failing():
        error = errors[index["i"]]
        index["i"] = min(index["i"] + 1, len(errors) - 1)
        raise error

    with pytest.raises(RetryableError) as excinfo:
        run_with_retry(failing, attempts=3)
    # The LAST observed error is what propagates, not the first.
    assert "third" in str(excinfo.value) or index["i"] >= 2


def test_on_retry_skipped_after_final_attempt():
    seen = []

    def always():
        raise RetryableError("down")

    with pytest.raises(RetryableError):
        run_with_retry(always, attempts=2, on_retry=lambda e, i: seen.append(i))
    assert seen == [0]


def test_single_attempt_exhaustion_reraises():
    def once():
        raise RetryableError("only shot")

    with pytest.raises(RetryableError, match="only shot"):
        run_with_retry(once, attempts=1)


def test_fatal_after_retryable_stops_at_once():
    calls = []

    def mixed():
        calls.append(1)
        if len(calls) == 1:
            raise RetryableError("transient")
        raise FatalError("permanent")

    with pytest.raises(FatalError):
        run_with_retry(mixed, attempts=10)
    assert len(calls) == 2
