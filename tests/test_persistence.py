"""Persistence tests.

The interesting assertions are not "a row round-trips" but the ones where a database
constraint *is* a design guarantee: the fingerprint index that stops the validation loop
cycling, the epoch fence that neutralises a zombie worker, and the queue predicate that
keeps two issues from racing on one block.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from oatutor_council.models import (
    ArtifactKind,
    AttemptOutcome,
    CellEdit,
    ChangeRecord,
    ColumnKey,
    CurationJob,
    FailureReason,
    Issue,
    IssueCategory,
    IssueSource,
    IssueState,
    JobState,
    Patch,
    RepairAttempt,
    ReviewDecision,
    ReviewerRole,
    ReviewVerdict,
    Severity,
)
from oatutor_council.persistence import (
    ConcurrencyError,
    Database,
    acquire_lease,
    claimable_jobs,
    count_issues_in_states,
    create_job,
    get_issue,
    get_job,
    heartbeat,
    increment_counters,
    insert_attempt,
    insert_issue,
    insert_patch,
    insert_verdict,
    list_artifacts,
    list_changes,
    list_events,
    load_ledger,
    next_issue_for_phase,
    open_apply_intent,
    open_apply_intents,
    open_attempts,
    record_artifact,
    record_changes,
    save_issue,
    settle_apply_intent,
    settle_attempt,
    transition_job,
)
from oatutor_council.state_machine import IllegalTransition


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "council.db")


@pytest.fixture
def job(db) -> CurationJob:
    return create_job(
        db,
        CurationJob(
            job_id="job-1", source_filename="workbook.xlsx", source_sha256="abc123"
        ),
    )


def make_issue(issue_id: str = "issue-1", **kwargs) -> Issue:
    defaults = dict(
        issue_id=issue_id,
        job_id="job-1",
        block_id="block-0000",
        source=IssueSource.INITIAL_AUDITOR,
        category=IssueCategory.MATHEMATICS,
        severity=Severity.ERROR,
        title="t",
        description="d",
        fingerprint=f"fp-{issue_id}",
    )
    return Issue(**{**defaults, **kwargs})


# --------------------------------------------------------------------------------------
# Pragmas
# --------------------------------------------------------------------------------------


def test_the_durability_pragmas_are_actually_set(db):
    """`synchronous=FULL` is not a preference. The intent-then-write-then-commit design
    assumes a commit is durable; NORMAL can lose the last transactions on power loss,
    which would make an apply intent a suggestion rather than a record."""
    connection = db.connection
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_foreign_key_violation_is_refused(db, job):
    """An orphaned attempt should be impossible, not merely unlikely."""
    with pytest.raises(sqlite3.IntegrityError):
        insert_attempt(
            db, RepairAttempt(attempt_id="a1", issue_id="no-such-issue", attempt_no=1)
        )


# --------------------------------------------------------------------------------------
# Jobs and transitions
# --------------------------------------------------------------------------------------


def test_a_job_round_trips(db, job):
    stored = get_job(db, "job-1")
    assert stored.source_sha256 == "abc123"
    assert stored.state is JobState.CREATED


def test_an_illegal_transition_is_refused_at_the_database(db, job):
    """The legality check reads the current state inside the write transaction, so two
    workers cannot both see the same state and both advance it."""
    with pytest.raises(IllegalTransition):
        transition_job(db, "job-1", JobState.FINALIZING, run_epoch=0)
    assert get_job(db, "job-1").state is JobState.CREATED


def test_a_transition_from_a_stale_epoch_is_refused(db, job):
    acquire_lease(db, "job-1", "worker-a", lease_seconds=60)  # epoch 1
    with pytest.raises(ConcurrencyError, match="must stop"):
        transition_job(db, "job-1", JobState.INGESTING, run_epoch=0)


def test_transitions_are_recorded_as_events(db, job):
    transition_job(db, "job-1", JobState.INGESTING, run_epoch=0)
    kinds = [event["kind"] for event in list_events(db, "job-1")]
    assert kinds == ["created", "transition"]


# --------------------------------------------------------------------------------------
# Leases and fencing
# --------------------------------------------------------------------------------------


def test_a_live_lease_cannot_be_stolen(db, job):
    acquire_lease(db, "job-1", "worker-a", lease_seconds=60)
    with pytest.raises(ConcurrencyError, match="leased by worker-a"):
        acquire_lease(db, "job-1", "worker-b", lease_seconds=60)


def test_an_expired_lease_can_be_stolen_and_bumps_the_epoch(db, job):
    acquire_lease(db, "job-1", "worker-a", lease_seconds=-1)
    stolen = acquire_lease(db, "job-1", "worker-b", lease_seconds=60)
    assert stolen.lease_owner == "worker-b"
    assert stolen.run_epoch == 2


def test_a_zombie_worker_writes_zero_rows_and_learns_it_lost(db, job):
    """The whole point of the fence. The old worker may still be alive and mid-step;
    every write it makes carries the old epoch and must match nothing."""
    first = acquire_lease(db, "job-1", "worker-a", lease_seconds=-1)
    acquire_lease(db, "job-1", "worker-b", lease_seconds=60)

    with pytest.raises(ConcurrencyError):
        heartbeat(db, "job-1", "worker-a", run_epoch=first.run_epoch, lease_seconds=60)
    with pytest.raises(ConcurrencyError):
        increment_counters(db, "job-1", run_epoch=first.run_epoch, steps=1)
    with pytest.raises(ConcurrencyError):
        transition_job(db, "job-1", JobState.INGESTING, run_epoch=first.run_epoch)

    assert get_job(db, "job-1").steps_used == 0


def test_the_durable_queue_offers_unleased_unfinished_jobs(db, job):
    """A submission lost to a crash reappears here with no coordination, because nothing
    about claimability is held in memory."""
    assert [j.job_id for j in claimable_jobs(db)] == ["job-1"]

    acquire_lease(db, "job-1", "worker-a", lease_seconds=60)
    assert claimable_jobs(db) == ()

    acquire_lease(db, "job-1", "worker-a", lease_seconds=-1)
    assert [j.job_id for j in claimable_jobs(db)] == ["job-1"]


def test_a_finished_job_is_never_offered(db, job):
    for target in (
        JobState.INGESTING,
        JobState.AUDITING,
        JobState.REPAIRING_KNOWN,
        JobState.INDEPENDENT_REVIEW,
        JobState.FINAL_VALIDATION,
        JobState.FINALIZING,
        JobState.SUCCEEDED,
    ):
        transition_job(db, "job-1", target, run_epoch=0)
    assert claimable_jobs(db) == ()


def test_a_failure_reason_is_persisted(db, job):
    transition_job(
        db, "job-1", JobState.FAILED, run_epoch=0, failure_reason=FailureReason.CORRUPTION
    )
    assert get_job(db, "job-1").failure_reason is FailureReason.CORRUPTION


# --------------------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------------------


def test_a_duplicate_fingerprint_is_refused_rather_than_inserted(db, job):
    """The termination guarantee as a constraint rather than a convention. A
    rediscovered defect cannot open a second issue with a second attempt budget, however
    many code paths try."""
    assert insert_issue(db, make_issue("issue-1")) is not None
    assert insert_issue(db, make_issue("issue-2", fingerprint="fp-issue-1")) is None
    assert len(load_ledger(db, "job-1").issues) == 1


def test_issue_state_survives_a_round_trip(db, job):
    insert_issue(db, make_issue())
    issue = get_issue(db, "issue-1")
    save_issue(db, issue.model_copy(update={"state": IssueState.AWAITING_PATCH, "attempts_used": 2}))
    reloaded = get_issue(db, "issue-1")
    assert reloaded.state is IssueState.AWAITING_PATCH
    assert reloaded.attempts_used == 2


def test_the_phase_queue_returns_only_matching_issues(db, job):
    insert_issue(db, make_issue("a", state=IssueState.OPEN, block_id="block-1"))
    insert_issue(db, make_issue("b", state=IssueState.ACCEPTED, block_id="block-2"))
    found = next_issue_for_phase(db, "job-1", [IssueState.OPEN])
    assert found.issue_id == "a"


def test_only_one_issue_per_block_is_ever_in_flight(db, job):
    """Two issues on one block would race on the same cell and burn an attempt on a
    confusing BEFORE_MISMATCH."""
    insert_issue(db, make_issue("busy", state=IssueState.AWAITING_REVIEW, block_id="block-1"))
    insert_issue(db, make_issue("waiting", state=IssueState.OPEN, block_id="block-1"))
    insert_issue(db, make_issue("elsewhere", state=IssueState.OPEN, block_id="block-2"))

    found = next_issue_for_phase(db, "job-1", [IssueState.OPEN])
    assert found.issue_id == "elsewhere"


def test_the_phase_queue_can_filter_by_reviewer_role(db, job):
    """Stage-5 repairs route to either reviewer without needing their own phase."""
    insert_issue(
        db,
        make_issue("k", state=IssueState.OPEN, block_id="b1",
                   reviewer_role=ReviewerRole.KNOWN_ISSUE_REVIEWER),
    )
    insert_issue(
        db,
        make_issue("i", state=IssueState.OPEN, block_id="b2",
                   reviewer_role=ReviewerRole.INDEPENDENT_REVIEWER),
    )
    found = next_issue_for_phase(
        db, "job-1", [IssueState.OPEN], reviewer_role="independent_reviewer"
    )
    assert found.issue_id == "i"


def test_a_drained_phase_returns_nothing(db, job):
    """A phase advances when its predicate returns zero rows -- there is no cursor to
    get out of step with the data."""
    insert_issue(db, make_issue("a", state=IssueState.ACCEPTED))
    assert next_issue_for_phase(db, "job-1", [IssueState.OPEN]) is None
    assert count_issues_in_states(db, "job-1", [IssueState.OPEN]) == 0


# --------------------------------------------------------------------------------------
# Attempts, patches, verdicts
# --------------------------------------------------------------------------------------


def test_an_unfinished_attempt_is_visible_to_recovery(db, job):
    """An attempt that started and never finished is the signature of a crash mid-call,
    and recovery has to be able to find it."""
    insert_issue(db, make_issue())
    insert_attempt(db, RepairAttempt(attempt_id="a1", issue_id="issue-1", attempt_no=1))
    assert [a.attempt_id for a in open_attempts(db, "job-1")] == ["a1"]

    attempt = open_attempts(db, "job-1")[0]
    settle_attempt(
        db,
        attempt.model_copy(
            update={
                "outcome": AttemptOutcome.PATCH_ACCEPTED,
                "finished_at": datetime.now(timezone.utc),
            }
        ),
    )
    assert open_attempts(db, "job-1") == ()


def test_two_attempts_cannot_share_a_number(db, job):
    insert_issue(db, make_issue())
    insert_attempt(db, RepairAttempt(attempt_id="a1", issue_id="issue-1", attempt_no=1))
    with pytest.raises(sqlite3.IntegrityError):
        insert_attempt(db, RepairAttempt(attempt_id="a2", issue_id="issue-1", attempt_no=1))


def test_a_patch_stores_its_edits_separately_for_querying(db, job):
    insert_issue(db, make_issue())
    patch = Patch(
        patch_id="p1",
        issue_id="issue-1",
        attempt_no=1,
        edits=(
            CellEdit(row=3, column=5, column_key=ColumnKey.ANSWER, before="1/2", after="1/3"),
        ),
        reason="private writer rationale",
    )
    insert_patch(db, patch)
    rows = db.connection.execute("SELECT * FROM cell_edits WHERE patch_id = 'p1'").fetchall()
    assert [(r["row_index"], r["before_text"], r["after_text"]) for r in rows] == [
        (3, "1/2", "1/3")
    ]


def test_a_verdict_round_trips(db, job):
    insert_issue(db, make_issue())
    insert_verdict(
        db,
        ReviewVerdict(
            verdict_id="v1",
            issue_id="issue-1",
            reviewer_role=ReviewerRole.KNOWN_ISSUE_REVIEWER,
            attempt_no=1,
            decision=ReviewDecision.REVISE,
            feedback="the exponent is still wrong",
        ),
    )
    row = db.connection.execute("SELECT decision FROM review_verdicts").fetchone()
    assert row["decision"] == "revise"


# --------------------------------------------------------------------------------------
# Changes and apply intents
# --------------------------------------------------------------------------------------


def test_changes_round_trip_with_their_before_values(db, job):
    record_changes(
        db,
        "job-1",
        [
            ChangeRecord(
                change_id="c1",
                issue_id=None,
                patch_id=None,
                block_id="block-0000",
                row=3,
                column=5,
                column_key=ColumnKey.ANSWER,
                before="2026-01-02 00:00:00",
                after="1/2",
                applied_at=datetime.now(timezone.utc),
            )
        ],
    )
    change = list_changes(db, "job-1")[0]
    assert change.before == "2026-01-02 00:00:00"
    assert change.column_key is ColumnKey.ANSWER


def test_an_apply_intent_is_durable_before_the_write_and_settles_after(db, job):
    """Step one of intent-then-file-then-commit. If the process dies after the file
    lands but before the changes commit, this row is the only evidence the write was
    ever authorised."""
    intent_id = open_apply_intent(
        db,
        job_id="job-1",
        issue_id=None,
        patch_id=None,
        run_epoch=0,
        edits=[{"row": 3, "column": 5, "before": "1/2", "after": "1/3"}],
    )
    pending = open_apply_intents(db, "job-1")
    assert len(pending) == 1
    assert pending[0]["edits"][0]["after"] == "1/3"

    settle_apply_intent(db, intent_id, "applied")
    assert open_apply_intents(db, "job-1") == ()


# --------------------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------------------


def test_recording_an_artifact_twice_updates_it(db, job):
    record_artifact(db, "job-1", ArtifactKind.CORRECTED_WORKBOOK, "out/v1.xlsx")
    record_artifact(db, "job-1", ArtifactKind.CORRECTED_WORKBOOK, "out/v2.xlsx")
    assert list_artifacts(db, "job-1")[ArtifactKind.CORRECTED_WORKBOOK] == "out/v2.xlsx"


# --------------------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------------------


def test_concurrent_writers_from_several_threads_do_not_corrupt_the_counters(db, job):
    """One connection per thread plus an in-process write lock. Without them this either
    raises about threads or loses increments."""
    errors: list[Exception] = []

    def bump() -> None:
        try:
            for _ in range(20):
                increment_counters(db, "job-1", run_epoch=0, steps=1)
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert get_job(db, "job-1").steps_used == 80
