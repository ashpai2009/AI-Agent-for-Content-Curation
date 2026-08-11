"""State machine tests.

Both transition tables are data, so they can be asserted over exhaustively rather than
sampled. The attempt-accounting tests are the ones that matter most: the cap is what
stands between a crash loop and unbounded spend.
"""

from __future__ import annotations

import pytest

from oatutor_council.models import (
    TERMINAL_ISSUE_STATES,
    TERMINAL_JOB_STATES,
    FailureReason,
    Issue,
    IssueCategory,
    IssueSource,
    IssueState,
    JobState,
    Severity,
)
from oatutor_council.state_machine import (
    LEGAL_ISSUE_TRANSITIONS,
    LEGAL_JOB_TRANSITIONS,
    AttemptsExhausted,
    IllegalTransition,
    IssueMachine,
    advance_issue,
    assert_issue_transition,
    assert_job_transition,
    is_resumable,
)


def make_issue(**kwargs) -> Issue:
    defaults = dict(
        issue_id="issue-1",
        job_id="job-1",
        source=IssueSource.INITIAL_AUDITOR,
        category=IssueCategory.MATHEMATICS,
        severity=Severity.ERROR,
        title="t",
        description="d",
    )
    return Issue(**{**defaults, **kwargs})


MACHINE = IssueMachine(max_attempts=3, interrupted_retry_budget=2)


# --------------------------------------------------------------------------------------
# Table shape
# --------------------------------------------------------------------------------------


def test_every_job_state_appears_in_the_table():
    assert set(LEGAL_JOB_TRANSITIONS) == set(JobState)


def test_every_issue_state_appears_in_the_table():
    assert set(LEGAL_ISSUE_TRANSITIONS) == set(IssueState)


@pytest.mark.parametrize(
    "state", sorted(TERMINAL_JOB_STATES - {JobState.FAILED}, key=str)
)
def test_finished_job_states_go_nowhere(state):
    """Including to CANCELLED. A finished job cannot be un-finished."""
    assert LEGAL_JOB_TRANSITIONS[state] == frozenset()


def test_a_failed_job_can_only_re_enter_at_the_beginning():
    """The one terminal state with a way out, because some failures are worth another
    run. It re-enters at `INGESTING` rather than where it failed: every phase is a queue
    predicate over durable rows, so ingestion is idempotent, and restoring the exact
    state would need a second record of a position that is meant to be derivable.

    The table permits the edge for every failure; which failures may take it is
    `is_resumable`'s decision, tested below.
    """
    assert LEGAL_JOB_TRANSITIONS[JobState.FAILED] == frozenset({JobState.INGESTING})



def test_an_escalated_issue_goes_nowhere():
    """The one true dead end. Nothing the council does next can un-escalate an issue a
    person has been asked to look at."""
    assert LEGAL_ISSUE_TRANSITIONS[IssueState.NEEDS_HUMAN_REVIEW] == frozenset()


@pytest.mark.parametrize(
    "state",
    sorted(TERMINAL_ISSUE_STATES - {IssueState.NEEDS_HUMAN_REVIEW}, key=str),
)
def test_a_resolved_issue_reopens_only_backwards_or_to_a_person(state):
    """Final validation can rediscover the defect an issue was closed for. It has to be
    able to say so -- absorbing the rediscovery silently is how a job reports success
    over a workbook it never fixed -- but only in these two directions, so a rediscovery
    can never re-enter the pipeline at an earlier stage."""
    assert LEGAL_ISSUE_TRANSITIONS[state] == frozenset(
        {IssueState.REVISION_REQUESTED, IssueState.NEEDS_HUMAN_REVIEW}
    )


def test_every_non_terminal_job_state_can_fail_or_be_cancelled():
    for state, targets in LEGAL_JOB_TRANSITIONS.items():
        if state in TERMINAL_JOB_STATES:
            continue
        assert JobState.FAILED in targets
        assert JobState.CANCELLED in targets


def test_the_only_cycle_is_the_validation_repair_edge():
    """Every other edge moves strictly forward, which is why `max_validation_rounds` is
    the only budget guarding a loop in the job machine."""
    cycles = [
        (source, target)
        for source, targets in LEGAL_JOB_TRANSITIONS.items()
        for target in targets
        if source in LEGAL_JOB_TRANSITIONS.get(target, frozenset())
        and target not in TERMINAL_JOB_STATES
        # `FAILED -> INGESTING` closes a loop on paper only. Nothing takes it
        # automatically: `claimable_jobs` excludes failed jobs, so a failed job re-enters
        # the pipeline when a person asks it to and at no other time.
        and source is not JobState.FAILED
    ]
    assert set(cycles) == {
        (JobState.FINAL_VALIDATION, JobState.REPAIRING_VALIDATION),
        (JobState.REPAIRING_VALIDATION, JobState.FINAL_VALIDATION),
    }


def test_success_is_reachable_only_from_finalizing():
    """`SUCCEEDED` is set in exactly one place, guarded by every gate. A second inbound
    edge would be a second place to report success without checking."""
    sources = [
        state
        for state, targets in LEGAL_JOB_TRANSITIONS.items()
        if JobState.SUCCEEDED in targets
    ]
    assert sources == [JobState.FINALIZING]


# --------------------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------------------


def test_the_happy_path_is_legal():
    path = [
        JobState.CREATED,
        JobState.INGESTING,
        JobState.AUDITING,
        JobState.REPAIRING_KNOWN,
        JobState.INDEPENDENT_REVIEW,
        JobState.FINAL_VALIDATION,
        JobState.FINALIZING,
        JobState.SUCCEEDED,
    ]
    for current, target in zip(path, path[1:]):
        assert_job_transition(current, target)


def test_skipping_a_phase_is_refused():
    with pytest.raises(IllegalTransition):
        assert_job_transition(JobState.CREATED, JobState.FINALIZING)


def test_going_backwards_is_refused():
    with pytest.raises(IllegalTransition):
        assert_job_transition(JobState.FINALIZING, JobState.AUDITING)


def test_an_issue_can_be_superseded_from_any_live_state():
    """A later issue can subsume an earlier one at any point; forcing it through the
    repair loop first would spend attempts on a defect that no longer exists alone."""
    for state in IssueState:
        if state in TERMINAL_ISSUE_STATES:
            continue
        assert_issue_transition(state, IssueState.SUPERSEDED)


def test_an_issue_cannot_be_reopened_once_accepted():
    with pytest.raises(IllegalTransition):
        advance_issue(make_issue(state=IssueState.ACCEPTED), IssueState.AWAITING_PATCH)


def test_a_rejected_patch_returns_the_issue_for_another_attempt():
    issue = make_issue(state=IssueState.PATCH_REJECTED)
    assert advance_issue(issue, IssueState.AWAITING_PATCH).state is IssueState.AWAITING_PATCH


# --------------------------------------------------------------------------------------
# Resumability
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [FailureReason.CORRUPTION, FailureReason.ISOLATION_VIOLATION, FailureReason.CONFIG],
)
def test_some_failures_are_never_resumed(reason):
    """The job's premises are broken. Retrying repeats the damage rather than
    recovering from it."""
    assert not is_resumable(JobState.FAILED, reason)


def test_a_provider_failure_is_resumable():
    assert is_resumable(JobState.FAILED, FailureReason.PROVIDER)


def test_a_finished_job_is_not_resumable():
    assert not is_resumable(JobState.SUCCEEDED, None)
    assert not is_resumable(JobState.CANCELLED, None)


def test_every_live_state_is_resumable():
    """Including FINALIZING, which writes outputs before evaluating gates and is
    therefore idempotent to re-run."""
    for state in JobState:
        if state not in TERMINAL_JOB_STATES:
            assert is_resumable(state, None)


# --------------------------------------------------------------------------------------
# Attempt accounting
# --------------------------------------------------------------------------------------


def test_an_attempt_is_spent_by_reserving_it_not_by_finishing_it():
    """The ordering the whole budget rests on. Incrementing after the call means a
    process that dies mid-call leaves no evidence, so a crash loop burns unbounded spend
    against a counter that never moves."""
    issue = make_issue()
    assert MACHINE.reserve_attempt(issue).attempts_used == 1


def test_the_cap_is_three_attempts():
    issue = make_issue()
    for expected in (1, 2, 3):
        issue = MACHINE.reserve_attempt(issue)
        assert issue.attempts_used == expected
    with pytest.raises(AttemptsExhausted):
        MACHINE.reserve_attempt(issue)


def test_an_exhausted_issue_goes_to_human_review():
    issue = make_issue(state=IssueState.AWAITING_PATCH, attempts_used=3)
    assert not MACHINE.can_attempt(issue)
    assert MACHINE.exhausted(issue).state is IssueState.NEEDS_HUMAN_REVIEW


def test_an_attempt_lost_to_infrastructure_is_refunded():
    issue = MACHINE.reserve_attempt(make_issue())
    refunded = MACHINE.refund_interrupted(issue)
    assert refunded.attempts_used == 0
    assert refunded.interrupted_retries_used == 1


def test_the_refund_budget_is_bounded_so_neither_counter_can_loop():
    """Repeated infrastructure failure on one issue is itself a reason to involve a
    person, not to keep paying for retries."""
    issue = make_issue(attempts_used=1)
    for _ in range(2):
        issue = MACHINE.refund_interrupted(make_issue(
            attempts_used=1, interrupted_retries_used=issue.interrupted_retries_used
        ))
    assert issue.interrupted_retries_used == 2

    spent = make_issue(attempts_used=1, interrupted_retries_used=2)
    assert MACHINE.refund_interrupted(spent).attempts_used == 1


def test_a_refund_cannot_create_an_attempt_that_was_never_spent():
    assert MACHINE.refund_interrupted(make_issue(attempts_used=0)).attempts_used == 0


# --------------------------------------------------------------------------------------
# Reopening
# --------------------------------------------------------------------------------------


def test_a_rediscovered_defect_reopens_the_issue_it_belongs_to():
    issue = make_issue(state=IssueState.ACCEPTED, attempts_used=1)
    reopened = MACHINE.reopen(issue)
    assert reopened.state is IssueState.REVISION_REQUESTED


def test_reopening_does_not_refill_the_attempt_budget():
    """The bound on the reopen cycle. A reopened issue is the same issue; a fresh budget
    is exactly how the validation loop stops terminating."""
    issue = make_issue(state=IssueState.ACCEPTED, attempts_used=2)
    assert MACHINE.reopen(issue).attempts_used == 2


def test_reopening_an_issue_with_no_attempts_left_sends_it_to_a_person():
    """Absorption stops the loop. It never launders the defect: the issue lands in a
    state that denies the job success rather than one that ignores the rediscovery."""
    issue = make_issue(state=IssueState.ACCEPTED, attempts_used=3)
    assert MACHINE.reopen(issue).state is IssueState.NEEDS_HUMAN_REVIEW
