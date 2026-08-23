"""Tests for the failure taxonomy (agentbench.taxonomy)."""

from __future__ import annotations

from agentbench.taxonomy import (
    AGENT_FAILED,
    AGENT_TIMEOUT,
    EVALUATION_FAILED,
    INVALID_RESULT,
    PASSED,
    PROTECTED_PATH_VIOLATION,
    classify_run,
)


def classify(**overrides):
    defaults = dict(
        agent_timed_out=False,
        agent_exit_code=0,
        evaluations_passed=False,
        has_evaluation_results=True,
        protected_violation=False,
    )
    defaults.update(overrides)
    return classify_run(**defaults)


class TestClassificationPrecedence:
    def test_all_passing_evaluations_pass(self):
        assert classify(evaluations_passed=True).status == PASSED

    def test_failing_evaluations_are_evaluation_failed(self):
        result = classify(agent_exit_code=0)

        assert result.status == EVALUATION_FAILED
        assert "evaluations failed" in result.reason

    def test_timeout_outranks_everything(self):
        # Even passing evaluations cannot make a timed-out run a pass.
        assert classify(
            agent_timed_out=True, evaluations_passed=True
        ).status == AGENT_TIMEOUT

    def test_agent_failure_explains_failed_evaluations(self):
        result = classify(agent_exit_code=3)

        assert result.status == AGENT_FAILED
        assert "3" in result.reason

    def test_agent_nonzero_exit_alone_does_not_fail_a_passing_run(self):
        assert classify(agent_exit_code=1, evaluations_passed=True).status == PASSED

    def test_protected_violation_outranks_passing_evaluations(self):
        assert classify(
            evaluations_passed=True, protected_violation=True
        ).status == PROTECTED_PATH_VIOLATION

    def test_no_evaluation_results_is_invalid_result(self):
        result = classify(has_evaluation_results=False)

        assert result.status == INVALID_RESULT

    def test_setup_failed_status_exists_for_cli_level_errors(self):
        from agentbench.taxonomy import SETUP_FAILED  # noqa: F401

        assert True
