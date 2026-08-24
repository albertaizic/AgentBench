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
