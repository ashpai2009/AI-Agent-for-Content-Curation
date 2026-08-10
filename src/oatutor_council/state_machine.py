"""The two state machines, as data.

Both transition tables are frozen module-level mappings rather than branches scattered
through the orchestrator. That is what makes "can a job go from FINALIZING back to
AUDITING?" a question with an answer you can read, and a test you can write, instead of
a property of whichever code path happens to run.

The job machine tracks **pipeline position only**. There is deliberately no `RUNNING`
state and no `*_INTERRUPTED` variants: liveness lives in the lease columns, so a crashed
job simply sits in the state it reached with an expired lease. Resume is then a pure
function of the state, and there is no reconciliation step where a crash could leave a
job in a position no code knows how to interpret.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    TERMINAL_ISSUE_STATES,
    TERMINAL_JOB_STATES,
    FailureReason,
    Issue,
    IssueState,
    JobState,
)

# --------------------------------------------------------------------------------------
# Job machine
# --------------------------------------------------------------------------------------

#: Cancellation is available from any non-terminal state, so it is added below rather
#: than repeated on every row.
_JOB_FLOW: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset({JobState.INGESTING}),
    JobState.INGESTING: frozenset({JobState.AUDITING}),
    JobState.AUDITING: frozenset({JobState.REPAIRING_KNOWN}),
    JobState.REPAIRING_KNOWN: frozenset({JobState.INDEPENDENT_REVIEW}),
    JobState.INDEPENDENT_REVIEW: frozenset({JobState.FINAL_VALIDATION}),
    # The only cyclic edge in the machine, and the reason `max_validation_rounds`
    # exists. Everything else moves strictly forward.
    JobState.FINAL_VALIDATION: frozenset(
        {JobState.REPAIRING_VALIDATION, JobState.FINALIZING}
    ),
    JobState.REPAIRING_VALIDATION: frozenset({JobState.FINAL_VALIDATION}),
    JobState.FINALIZING: frozenset(
        {JobState.SUCCEEDED, JobState.NEEDS_HUMAN_ATTENTION}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.NEEDS_HUMAN_ATTENTION: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}

LEGAL_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    state: (
        targets
        if state in TERMINAL_JOB_STATES
        else targets | {JobState.FAILED, JobState.CANCELLED}
    )
    for state, targets in _JOB_FLOW.items()
}

#: States from which a crashed job can be picked up again. `FINALIZING` is resumable
#: because it writes outputs before evaluating gates, so re-running it is idempotent.
RESUMABLE_JOB_STATES: frozenset[JobState] = frozenset(
    state for state in JobState if state not in TERMINAL_JOB_STATES
)


# --------------------------------------------------------------------------------------
# Issue machine
# --------------------------------------------------------------------------------------

#: `SUPERSEDED` is reachable from every non-terminal state: a later issue can subsume an
#: earlier one at any point, and forcing it through the repair loop first would spend
#: attempts on a defect that no longer exists independently.
_ISSUE_FLOW: dict[IssueState, frozenset[IssueState]] = {
    IssueState.OPEN: frozenset({IssueState.AWAITING_PATCH, IssueState.REFUTED}),
    IssueState.AWAITING_PATCH: frozenset(
        {IssueState.PATCH_PROPOSED, IssueState.PATCH_REJECTED, IssueState.REFUTED}
    ),
    IssueState.PATCH_PROPOSED: frozenset(
        {IssueState.APPLYING, IssueState.PATCH_REJECTED}
    ),
    IssueState.APPLYING: frozenset(
        {IssueState.PATCH_APPLIED, IssueState.PATCH_REJECTED}
    ),
    IssueState.PATCH_APPLIED: frozenset({IssueState.AWAITING_REVIEW}),
    IssueState.AWAITING_REVIEW: frozenset(
        {IssueState.ACCEPTED, IssueState.REVISION_REQUESTED}
    ),
    IssueState.REVISION_REQUESTED: frozenset({IssueState.AWAITING_PATCH}),
    IssueState.PATCH_REJECTED: frozenset({IssueState.AWAITING_PATCH}),
    IssueState.ACCEPTED: frozenset(),
    IssueState.REFUTED: frozenset(),
    IssueState.SUPERSEDED: frozenset(),
    IssueState.NEEDS_HUMAN_REVIEW: frozenset(),
}

LEGAL_ISSUE_TRANSITIONS: dict[IssueState, frozenset[IssueState]] = {
    state: (
        targets
        if state in TERMINAL_ISSUE_STATES
        else targets | {IssueState.NEEDS_HUMAN_REVIEW, IssueState.SUPERSEDED}
    )
    for state, targets in _ISSUE_FLOW.items()
}


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class IllegalTransition(Exception):
    """A transition the machine does not permit.

    Always a programming error, never a runtime condition, so it raises rather than
    returning a status: a job silently refusing to advance is far harder to diagnose than
    one that stops loudly at the line that tried.
    """


class AttemptsExhausted(Exception):
    """The three-attempt budget for one issue is spent."""

    def __init__(self, issue_id: str, attempts_used: int) -> None:
        super().__init__(
            f"issue {issue_id} has used all {attempts_used} repair attempts"
        )
        self.issue_id = issue_id
        self.attempts_used = attempts_used


def assert_job_transition(current: JobState, target: JobState) -> None:
    if target not in LEGAL_JOB_TRANSITIONS[current]:
        raise IllegalTransition(f"job cannot move from {current} to {target}")


def assert_issue_transition(current: IssueState, target: IssueState) -> None:
    if target not in LEGAL_ISSUE_TRANSITIONS[current]:
        raise IllegalTransition(f"issue cannot move from {current} to {target}")


def is_resumable(state: JobState, failure_reason: FailureReason | None) -> bool:
    """Whether a crashed job in this state may be picked up again.

    A job that failed on corruption, an isolation violation, or bad configuration is not
    resumable: its premises are broken, so retrying repeats the damage rather than
    recovering from it.
    """
    from .models import NON_RESUMABLE_FAILURES

    if state is JobState.FAILED:
        return failure_reason not in NON_RESUMABLE_FAILURES
    return state in RESUMABLE_JOB_STATES


# --------------------------------------------------------------------------------------
# Attempt accounting
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueMachine:
    """The single place the repair-attempt cap is enforced.

    One attempt is one Writer call, and it is **reserved before the call, never
    incremented after**. Increment-after means a process that dies mid-call leaves no
    durable evidence the attempt happened, so a crash loop burns unbounded spend against
    a counter that never moves.

    The interrupted-retry budget is what keeps that from being unfair: an attempt lost to
    infrastructure -- a timeout, a provider 500, a killed worker -- is refunded, up to a
    bounded number of times. Two bounded counters cannot loop; one unbounded refund
    would put the cap back to being decorative.
    """

    max_attempts: int
    interrupted_retry_budget: int

    def reserve_attempt(self, issue: Issue) -> Issue:
        """Return the issue with its attempt count already spent.

        The caller must persist this **before** making the model call. Everything about
        the budget rests on that ordering.
        """
        if issue.attempts_used >= self.max_attempts:
            raise AttemptsExhausted(issue.issue_id, issue.attempts_used)
        return issue.model_copy(update={"attempts_used": issue.attempts_used + 1})

    def can_attempt(self, issue: Issue) -> bool:
        return issue.attempts_used < self.max_attempts

    def refund_interrupted(self, issue: Issue) -> Issue:
        """Give back an attempt lost to infrastructure, within a bounded budget.

        When the budget is spent the attempt stays spent. That is deliberate: repeated
        infrastructure failure on one issue is itself a reason to stop and involve a
        person, not to keep paying for retries indefinitely.
        """
        if issue.interrupted_retries_used >= self.interrupted_retry_budget:
            return issue
        if issue.attempts_used <= 0:
            return issue
        return issue.model_copy(
            update={
                "attempts_used": issue.attempts_used - 1,
                "interrupted_retries_used": issue.interrupted_retries_used + 1,
            }
        )

    def exhausted(self, issue: Issue) -> Issue:
        """Move an issue that ran out of attempts to human review."""
        assert_issue_transition(issue.state, IssueState.NEEDS_HUMAN_REVIEW)
        return issue.model_copy(update={"state": IssueState.NEEDS_HUMAN_REVIEW})


def advance_issue(issue: Issue, target: IssueState) -> Issue:
    assert_issue_transition(issue.state, target)
    return issue.model_copy(update={"state": target})
