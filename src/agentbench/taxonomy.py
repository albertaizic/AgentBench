"""Failure taxonomy: a small, well-defined set of run outcomes.

Benchmark failure is distinct from AgentBench/setup error; persisted
results explain which of these happened and why.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentbench.stages import (
    STAGE_AGENT,
    STAGE_EVIDENCE,
    STAGE_EVALUATION,
)

PASSED = "passed"
EVALUATION_FAILED = "evaluation_failed"
AGENT_FAILED = "agent_failed"
AGENT_TIMEOUT = "agent_timeout"
SETUP_FAILED = "setup_failed"
INVALID_RESULT = "invalid_result"
PROTECTED_PATH_VIOLATION = "protected_path_violation"

ALL_STATUSES = (
    PASSED,
    EVALUATION_FAILED,
    AGENT_FAILED,
    AGENT_TIMEOUT,
    SETUP_FAILED,
    INVALID_RESULT,
    PROTECTED_PATH_VIOLATION,
)


@dataclass(frozen=True)
class Classification:
    status: str
    reason: str | None
    # WHERE it happened (see agentbench.stages); None for passed runs.
    stage: str | None = None


def classify_run(
    *,
    agent_timed_out: bool,
    agent_exit_code: int | None,
    evaluations_passed: bool,
    has_evaluation_results: bool,
    protected_violation: bool,
) -> Classification:
    """Decide the run outcome from evidence, in precedence order.

    1. ``agent_timeout``      — the run was cut off; eval results are moot.
    2. ``protected_path_violation`` — cheating concerns outrank test results.
    3. ``passed``             — every evaluation passed (v0.1 semantics kept:
                                 the agent's own exit code does not decide).
    4. ``agent_failed``       — evaluations failed *and* the agent process
                               itself exited nonzero, which explains why.
    5. ``evaluation_failed``  — evaluations failed while the agent finished
                               cleanly.
    """
    if agent_timed_out:
        return Classification(
            AGENT_TIMEOUT, "agent process exceeded the timeout", stage=STAGE_AGENT
        )
    if protected_violation:
        return Classification(
            PROTECTED_PATH_VIOLATION,
            "agent modified protected paths",
            stage=STAGE_EVIDENCE,
        )
    if not has_evaluation_results:
        return Classification(
            INVALID_RESULT, "no evaluation results were produced", stage=STAGE_EVALUATION
        )
    if evaluations_passed:
        return Classification(PASSED, None)
    if agent_exit_code not in (0, None):
        return Classification(
            AGENT_FAILED,
            f"agent exited with code {agent_exit_code} and evaluations failed",
            stage=STAGE_AGENT,
        )
    return Classification(EVALUATION_FAILED, "one or more evaluations failed", stage=STAGE_EVALUATION)


# Outcome (did the task pass?) and validity (may the evidence be graded?)
# are orthogonal. A provider outage yields outcome=failed/agent_failed with
# validity=infra_invalid: the cell happened, but it measures infrastructure,
# not capability, and must be excluded from capability denominators while
# staying visible in reports.
VALIDITY_VALID = "valid"
VALIDITY_INFRA_INVALID = "infra_invalid"
VALIDITY_INTEGRITY_WARNING = "integrity_warning"
VALIDITY_INVALID = "invalid"
ALL_VALIDITIES = (
    VALIDITY_VALID,
    VALIDITY_INFRA_INVALID,
    VALIDITY_INTEGRITY_WARNING,
    VALIDITY_INVALID,
)

# Deterministic provider-outage evidence. Matched case-insensitively against
# captured agent output; every pattern was observed in real v0.5 run logs.
_INFRA_EVIDENCE_PATTERNS = (
    "http 429",
    "429: ",
    "http 408",
    "408: ",
    "http 500",
    "500: ",
    "http 502",
    "502: ",
    "http 503",
    "503: ",
    "rate limit",
    "rate-limit",
    "session limit",
    "quota",
    "api call failed after",
    "insufficient_quota",
    "overloaded_error",
    "provider returned error",
    # Transport-level failures observed between harness and provider.
    "bad gateway",
    "service unavailable",
    "connection reset",
    "connection refused",
    "name resolution",
    "getaddrinfo",
)


def classify_validity(
    *,
    agent_stdout: str | None,
    agent_stderr: str | None,
    total_tokens: int | None,
) -> str:
    """Grade whether a finished run's evidence measures agent capability.

    Conservative by design: an outage signal only invalidates a cell when
    the agent produced no tokens at all — i.e. it never reached a model.
    Runs that did real work before hitting an outage keep ``valid``; their
    outcome already reflects whatever partial capability they demonstrated.
    """
    combined = f"{agent_stdout or ''}\n{agent_stderr or ''}".lower()
    infra_hit = any(pattern in combined for pattern in _INFRA_EVIDENCE_PATTERNS)
    if infra_hit and total_tokens in (None, 0):
        return VALIDITY_INFRA_INVALID
    return VALIDITY_VALID
