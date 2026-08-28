"""Provider/infra failure-classifier fixtures (v0.6 hardening, mission XII).

Recorded provider-error strings (real shapes observed from OpenRouter,
Anthropic, and local CLIs) are pushed through ``classify_validity`` to pin
the conservative policy:

* Hard outages invalidate a cell ONLY when the agent produced no tokens
  (it never reached a model);
* Auth failures and ordinary agent-produced HTTP errors stay graded —
  misclassifying them would silently excuse real failures;
* Unknown failures default to ``valid`` (never confidently-wrong invalid).
"""

from __future__ import annotations

import pytest

from agentbench.taxonomy import classify_validity

RECORDED_ERRORS = {
    # -- hard outages: invalidate only at zero tokens -------------------------
    "http_429": "OpenRouter error: 429: rate limit exceeded, retry later",
    "http_408": "gateway timeout: HTTP 408 from api.openai.com",
    "http_500": "provider returned error: HTTP 500 Internal Server Error",
    "http_502": "upstream connect error: HTTP 502 Bad Gateway",
    "http_503": "HTTP 503 Service Unavailable: overloaded_error",
    "insufficient_quota": "openai.APIStatusError: insufficient_quota — "
                          "You exceeded your current quota",
    "session_limit": "hermes: session limit reached for this key",
    "retry_exhausted": "API call failed after 5 attempts: connection reset",
    # -- must NEVER invalidate (agent-visible errors / config problems) ------
    "http_401_auth": "request failed: HTTP 401 Unauthorized (bad key)",
    "http_403_forbidden": "HTTP 403 Forbidden: model access denied",
    "model_not_found": "404 - model openai/gpt-nonexistent not found",
    "agent_test_failure": "assert response.status_code == 429  # task logic",
    "plain_failure": "evaluator failed: expected True",
}


@pytest.mark.parametrize("label,text", sorted(RECORDED_ERRORS.items()))
def test_zero_token_runs_classify_by_outage_evidence(label, text):
    verdict = classify_validity(agent_stdout=text, agent_stderr="",
                                total_tokens=None)
    never_invalidates = label in (
        "http_401_auth",       # config/auth problem, not a transient outage
        "http_403_forbidden",
        "model_not_found",     # deterministic bad request, retry cannot fix
        "agent_test_failure",  # the AGENT hit this while doing its job
        "plain_failure",
    )
    if never_invalidates:
        assert verdict == "valid", label
    else:
        assert verdict == "infra_invalid", label


def test_real_work_before_outage_stays_graded():
    # The agent produced 152k tokens before the outage: the cell measured
    # genuine partial capability and remains a graded failure.
    assert classify_validity(
        agent_stdout="...working...\nHTTP 503 overloaded_error",
        agent_stderr="",
        total_tokens=152000,
    ) == "valid"


def test_stderr_evidence_counts_and_is_case_insensitive():
    assert classify_validity(agent_stdout=None,
                             agent_stderr="RATE LIMIT EXCEEDED",
                             total_tokens=0) == "infra_invalid"


def test_unknown_failure_defaults_to_valid():
    # Conservative policy: prefer a possibly-harsh graded failure over a
    # confidently-wrong infrastructure excuse.
    assert classify_validity(agent_stdout="weird custom crash xyzzy",
                             agent_stderr="", total_tokens=0) == "valid"
