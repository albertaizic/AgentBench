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
    classify_validity,
    VALIDITY_INFRA_INVALID,
    VALIDITY_VALID,
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


class TestValidityClassification:
    """Outcome and validity are orthogonal (P41): a provider outage that
    starves the agent of any model call must be visible as infra_invalid
    while staying out of the failure-taxonomy overload."""

    def test_zero_token_outage_is_infra_invalid(self):
        assert classify_validity(
            agent_stdout="API call failed after 3 retries: HTTP 429: Provider returned error",
            agent_stderr="",
            total_tokens=None,
        ) == VALIDITY_INFRA_INVALID

    def test_rate_limit_with_real_work_stays_valid(self):
        # The agent did reach a model and produced evidence; its outcome
        # already reflects demonstrated capability.
        assert classify_validity(
            agent_stdout="hit session limit near the end", agent_stderr="",
            total_tokens=152000,
        ) == VALIDITY_VALID

    def test_clean_run_is_valid(self):
        assert classify_validity(
            agent_stdout="done", agent_stderr="", total_tokens=5000
        ) == VALIDITY_VALID

    def test_patterns_are_case_insensitive_and_cover_stderr(self):
        assert classify_validity(
            agent_stdout="", agent_stderr="Error: QUOTA exceeded for project",
            total_tokens=0,
        ) == VALIDITY_INFRA_INVALID
