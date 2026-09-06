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
    JobState.INDEPENDENT_REVIEW: frozenset({JobState.FINAL_SEMANTIC}),
    # Final semantic verification runs its own repair loop in place rather than handing
    # off to a repair state, because a repair *invalidates the verification that preceded
    # it*: the block has to be solved again against the file as it now stands. A separate
    # repair state would have to hand control back, and the edge that does that is the one
    # somebody eventually removes as redundant.
    JobState.FINAL_SEMANTIC: frozenset({JobState.FINAL_VALIDATION}),
    # The second cycle in the job machine, and bounded twice over: `max_validation_rounds`
    # limits how often the gate may send work back, and `final_semantic_rounds` limits how
    # often any one block may be verified.
    # The only cyclic edge in the machine, and the reason `max_validation_rounds`
    # exists. Everything else moves strictly forward.
    JobState.FINAL_VALIDATION: frozenset(
        {JobState.REPAIRING_VALIDATION, JobState.FINALIZING}
    ),
    # Back to semantic verification, not straight to the gate: these repairs cleared the
    # markers of the blocks they touched, so the verifier re-checks those and no others.
    JobState.REPAIRING_VALIDATION: frozenset({JobState.FINAL_SEMANTIC}),
    JobState.FINALIZING: frozenset(
        {JobState.SUCCEEDED, JobState.NEEDS_HUMAN_ATTENTION}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.NEEDS_HUMAN_ATTENTION: frozenset(),
    # A resumable failure -- an outage, a run that overran its ceiling -- re-enters at
    # `INGESTING` rather than at whatever state it failed in. Every phase is a queue
    # predicate over durable rows, so ingestion is idempotent: recovery settles what the
    # crash left open, audited blocks are already recorded as done, and deduplication by
    # fingerprint means re-derived findings reopen nothing. Restoring the exact state
    # would need a second record of where it got to, and a job's position is meant to be
    # derivable from its rows rather than remembered alongside them.
    #
    # Which failures may take this edge is `is_resumable`'s decision, not this table's:
    # corruption, an isolation violation and a misconfiguration must never re-enter.
    JobState.FAILED: frozenset({JobState.INGESTING}),
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

#: Where a resolved issue may go when final validation finds its defect again. Back to
#: the Writer if attempts remain, to a person if they do not -- and nowhere else, so a
#: rediscovery can never quietly re-enter the pipeline at an earlier stage.
_REOPENABLE: frozenset[IssueState] = frozenset(
    {IssueState.REVISION_REQUESTED, IssueState.NEEDS_HUMAN_REVIEW}
)

#: `SUPERSEDED` is reachable from every non-terminal state: a later issue can subsume an
#: earlier one at any point, and forcing it through the repair loop first would spend
#: attempts on a defect that no longer exists independently.
_ISSUE_FLOW: dict[IssueState, frozenset[IssueState]] = {
    IssueState.OPEN: frozenset(
        {IssueState.AWAITING_PATCH, IssueState.REFUTED, IssueState.UNCONFIRMED}
    ),
    IssueState.AWAITING_PATCH: frozenset(
        {IssueState.PATCH_PROPOSED, IssueState.PATCH_REJECTED, IssueState.REFUTED}
    ),
    IssueState.PATCH_PROPOSED: frozenset(
        {IssueState.AWAITING_REVIEW, IssueState.PATCH_REJECTED}
    ),
    IssueState.AWAITING_REVIEW: frozenset(
        {IssueState.PATCH_APPROVED, IssueState.REVISION_REQUESTED}
    ),
    IssueState.PATCH_APPROVED: frozenset(
        {IssueState.APPLYING, IssueState.PATCH_REJECTED}
    ),
    IssueState.APPLYING: frozenset(
        {IssueState.PATCH_APPLIED, IssueState.PATCH_REJECTED}
    ),
    # `PATCH_APPLIED -> AWAITING_REVIEW` remains for jobs created before simulated review
    # was introduced. New jobs reach PATCH_APPLIED only after approval and go directly to
    # ACCEPTED; old in-flight jobs can still finish or roll back safely.
    IssueState.PATCH_APPLIED: frozenset(
        {IssueState.ACCEPTED, IssueState.AWAITING_REVIEW}
    ),
    IssueState.REVISION_REQUESTED: frozenset({IssueState.AWAITING_PATCH}),
    IssueState.PATCH_REJECTED: frozenset({IssueState.AWAITING_PATCH}),
    # A resolved issue is reopenable, and only in these two directions. Final validation
    # can rediscover the very defect an issue was closed for -- the repair looked right
    # to a reviewer and did not hold, or a later edit in the same block undid it. Leaving
    # those states with no way out is what forces the choice between silently absorbing
    # the rediscovery and reporting success over it, and both of those are dishonest.
    #
    # This is the second cycle in the issue machine, and it is bounded by the same
    # counter as the first: reopening never resets `attempts_used`, and the caller only
    # reopens while attempts remain. `max_validation_rounds` bounds it again from outside.
    IssueState.ACCEPTED: _REOPENABLE,
    IssueState.REFUTED: _REOPENABLE,
    IssueState.SUPERSEDED: _REOPENABLE,
    # An unsettled disagreement is the state most worth reopening: a later deterministic
    # rediscovery at the same cells is precisely the independent evidence the second audit
    # failed to supply, so it belongs back with the Writer rather than left as a question.
    # It takes the same two exits as every other resolved state and no third one -- an
    # A model-only claim carries no rule code, but a later sibling repair can now settle
    # its exact expected value and final semantic verification can examine the resulting
    # block. That narrow evidence also permits SUPERSEDED; unresolved disagreements about
    # mathematically equivalent but differently requested forms remain UNCONFIRMED.
    IssueState.UNCONFIRMED: _REOPENABLE | {IssueState.SUPERSEDED},
    # A later accepted sibling repair can make an escalated deterministic finding cease
    # to exist. `SUPERSEDED` is the only honest transition then: retaining the stale
    # escalation tells a curator to inspect a defect the final workbook does not have.
    # It cannot return to the repair loop or become accepted/refuted.
    IssueState.NEEDS_HUMAN_REVIEW: frozenset({IssueState.SUPERSEDED}),
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

    def reopen(self, issue: Issue) -> Issue:
        """Take a resolved issue back, because its defect is still there.

        Back to the Writer while attempts remain, to a person once they do not. The
        attempt count is deliberately **not** reset: a reopened issue is the same issue,
        and giving it a fresh budget is how the validation loop stops terminating.
        """
        target = (
            IssueState.REVISION_REQUESTED
            if self.can_attempt(issue)
            else IssueState.NEEDS_HUMAN_REVIEW
        )
        return advance_issue(issue, target)


def advance_issue(issue: Issue, target: IssueState) -> Issue:
    assert_issue_transition(issue.state, target)
    return issue.model_copy(update={"state": target})
