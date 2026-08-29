"""Crash-safety and recovery tests.

Every test here simulates a crash at a specific point in intent-then-file-then-commit and
asserts that recovery reaches the right conclusion. The three readings of the working
copy are exhaustive by construction, so the tests are too: all-after, all-before, and the
mixture that can only mean corruption.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import load_workbook

from conftest import problem, step
from oatutor_council.models import (
    AttemptOutcome,
    CellEdit,
    ColumnKey,
    CurationJob,
    FailureReason,
    Issue,
    IssueCategory,
    IssueSource,
    IssueState,
    Patch,
    RepairAttempt,
    Severity,
    SourcePath,
)
from oatutor_council.orchestrator import (
    JobCorrupted,
    apply_patch,
    failure_for,
    recover_apply_intents,
    recover_job,
    RecoveryReport,
)
from oatutor_council.persistence import (
    Database,
    create_job,
    get_issue,
    insert_attempt,
    insert_issue,
    list_changes,
    list_events,
    open_apply_intent,
    open_apply_intents,
    open_attempts,
)
from oatutor_council.state_machine import IssueMachine
from oatutor_council.workbook.reader import read_workbook
from oatutor_council.workbook.writer import EditRejected, create_working_copy

MACHINE = IssueMachine(max_attempts=3, interrupted_retry_budget=2)


@pytest.fixture
def source(make_workbook) -> Path:
    return make_workbook(
        [
            problem("angles1", title="Convert", oer_src="src", license="CC-BY"),
            step("angles1", answer="1/2", answer_type="numeric"),
            step("angles1", answer="3", answer_type="numeric"),
        ]
    )


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "council.db")


@pytest.fixture
def copy(source, tmp_path):
    return create_working_copy(SourcePath(str(source)), tmp_path / "job")


@pytest.fixture
def job(db, copy) -> CurationJob:
    return create_job(
        db,
        CurationJob(
            job_id="job-1",
            source_filename="workbook.xlsx",
            source_sha256=copy.source_sha256,
        ),
    )


def make_issue(**kwargs) -> Issue:
    defaults = dict(
        issue_id="issue-1",
        job_id="job-1",
        block_id="block-0000",
        source=IssueSource.INITIAL_AUDITOR,
        category=IssueCategory.NOTATION,
        severity=Severity.ERROR,
        title="t",
        description="d",
        fingerprint="fp-1",
        state=IssueState.APPLYING,
    )
    return Issue(**{**defaults, **kwargs})


def make_patch(**kwargs) -> Patch:
    defaults = dict(
        patch_id="patch-1",
        issue_id="issue-1",
        attempt_no=1,
        edits=(
            CellEdit(
                row=3, column=5, column_key=ColumnKey.ANSWER, before="1/2", after="1/3"
            ),
        ),
    )
    return Patch(**{**defaults, **kwargs})


def answer_at(copy, row: int) -> str:
    return read_workbook(copy.path).blocks[0].rows[row - 2].get(ColumnKey.ANSWER)


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_a_patch_applies_and_settles_its_intent(db, job, copy):
    changes = apply_patch(db, job, copy, make_patch(), block_id="block-0000")
    assert [(c.row, c.after) for c in changes] == [(3, "1/3")]
    assert answer_at(copy, 3) == "1/3"
    assert open_apply_intents(db, "job-1") == ()
    assert len(list_changes(db, "job-1")) == 1


def test_a_rejected_patch_writes_nothing_and_leaves_no_open_intent(db, job, copy):
    """`apply_edits` verifies the whole batch before touching the file, so a stale patch
    cannot leave a partial write behind for recovery to puzzle over."""
    with pytest.raises(EditRejected):
        apply_patch(db, job, copy, make_patch(edits=(
            CellEdit(row=3, column=5, before="wrong", after="1/3"),
        )))
    assert answer_at(copy, 3) == "1/2"
    assert open_apply_intents(db, "job-1") == ()
    assert list_changes(db, "job-1") == ()


# --------------------------------------------------------------------------------------
# Crash between the file write and the commit
# --------------------------------------------------------------------------------------


def test_a_crash_after_the_write_rolls_forward(db, job, copy):
    """The file landed and only the change records are missing. Recovery reads the cells,
    sees the new values, and commits the records -- which is what makes the ledger and
    the workbook agree again."""
    patch = make_patch()
    open_apply_intent(
        db,
        job_id="job-1",
        issue_id=patch.issue_id,
        patch_id=patch.patch_id,
        run_epoch=job.run_epoch,
        edits=[{"row": 3, "column": 5, "column_key": "answer", "before": "1/2", "after": "1/3"}],
        block_id="block-0000",
    )
    # Simulate the write having landed without its commit.
    workbook = load_workbook(copy.path)
    workbook.active.cell(row=3, column=5, value="1/3")
    workbook.save(copy.path)
    workbook.close()

    report = RecoveryReport()
    recover_apply_intents(db, job, copy, report)

    assert len(report.rolled_forward) == 1
    assert [(c.row, c.before, c.after) for c in list_changes(db, "job-1")] == [
        (3, "1/2", "1/3")
    ]
    assert list_changes(db, "job-1")[0].block_id == "block-0000"
    assert open_apply_intents(db, "job-1") == ()


def test_a_crash_before_the_write_marks_the_intent_for_re_application(db, job, copy):
    """Every cell still holds its `before`, so nothing happened and re-applying is safe.
    The edit is not lost -- the patch is still on record for the phase to retry."""
    open_apply_intent(
        db,
        job_id="job-1",
        issue_id="issue-1",
        patch_id="patch-1",
        run_epoch=job.run_epoch,
        edits=[{"row": 3, "column": 5, "column_key": "answer", "before": "1/2", "after": "1/3"}],
    )
    report = RecoveryReport()
    recover_apply_intents(db, job, copy, report)

    assert len(report.reapplied) == 1
    assert list_changes(db, "job-1") == ()
    assert answer_at(copy, 3) == "1/2"


def test_a_partially_applied_patch_is_corruption_not_a_case_to_handle(db, job, copy):
    """`os.replace` swaps the whole file or nothing, so a mixture cannot be something
    this system produced. Guessing which half to trust would be building on an unknown
    state."""
    open_apply_intent(
        db,
        job_id="job-1",
        issue_id="issue-1",
        patch_id="patch-1",
        run_epoch=job.run_epoch,
        edits=[
            {"row": 3, "column": 5, "column_key": "answer", "before": "1/2", "after": "1/3"},
            {"row": 4, "column": 5, "column_key": "answer", "before": "3", "after": "4"},
        ],
    )
    workbook = load_workbook(copy.path)
    workbook.active.cell(row=3, column=5, value="1/3")  # first edit only
    workbook.save(copy.path)
    workbook.close()

    with pytest.raises(JobCorrupted, match="partially patched"):
        recover_apply_intents(db, job, copy, RecoveryReport())


def test_corruption_maps_to_a_non_resumable_failure():
    """Retrying a job whose file is in an unknown state repeats the damage."""
    from oatutor_council.models import NON_RESUMABLE_FAILURES

    reason = failure_for(JobCorrupted("partially patched"))
    assert reason is FailureReason.CORRUPTION
    assert reason in NON_RESUMABLE_FAILURES


# --------------------------------------------------------------------------------------
# Interrupted attempts
# --------------------------------------------------------------------------------------


def test_an_attempt_with_no_patch_is_refunded(db, job, copy):
    """No patch means the Writer call never returned -- infrastructure failure, which is
    exactly what the bounded refund budget exists for."""
    issue = make_issue(attempts_used=1)
    insert_issue(db, issue)
    insert_attempt(db, RepairAttempt(attempt_id="a1", issue_id="issue-1", attempt_no=1))

    report = recover_job(db, job, copy, MACHINE, [issue])

    assert report.attempts_refunded == ["a1"]
    reloaded = get_issue(db, "issue-1")
    assert reloaded.attempts_used == 0
    assert reloaded.interrupted_retries_used == 1


def test_an_attempt_that_produced_a_patch_stays_spent(db, job, copy):
    """The Writer call completed, so the attempt was genuinely used. Refunding it would
    hand back real work and make the three-attempt cap meaningless."""
    issue = make_issue(attempts_used=1)
    insert_issue(db, issue)
    insert_attempt(
        db,
        RepairAttempt(
            attempt_id="a1", issue_id="issue-1", attempt_no=1, patch_id="patch-1"
        ),
    )

    report = recover_job(db, job, copy, MACHINE, [issue])

    assert report.attempts_refunded == []
    # It stays open until the reviewer records whether the completed proposal was
    # accepted, revised or escalated. "Interrupted" would be a false outcome.
    assert report.attempts_closed == []
    assert [attempt.attempt_id for attempt in open_attempts(db, "job-1")] == ["a1"]
    assert get_issue(db, "issue-1").attempts_used == 1


def test_every_open_attempt_is_closed_so_recovery_is_idempotent(db, job, copy):
    issue = make_issue(attempts_used=1)
    insert_issue(db, issue)
    insert_attempt(db, RepairAttempt(attempt_id="a1", issue_id="issue-1", attempt_no=1))

    recover_job(db, job, copy, MACHINE, [issue])
    assert open_attempts(db, "job-1") == ()

    second = recover_job(db, job, copy, MACHINE, [get_issue(db, "issue-1")])
    assert second.attempts_closed == []
    assert get_issue(db, "issue-1").attempts_used == 0


def test_the_closed_attempt_records_that_it_was_interrupted(db, job, copy):
    from oatutor_council.persistence import list_attempts

    issue = make_issue(attempts_used=1)
    insert_issue(db, issue)
    insert_attempt(db, RepairAttempt(attempt_id="a1", issue_id="issue-1", attempt_no=1))
    recover_job(db, job, copy, MACHINE, [issue])

    assert list_attempts(db, "job-1")[0].outcome is AttemptOutcome.INTERRUPTED


# --------------------------------------------------------------------------------------
# Whole-job recovery
# --------------------------------------------------------------------------------------


def test_recovery_checks_the_source_hash_before_anything_else(db, job, copy, source):
    """Everything below compares the working copy against the source. If the original
    moved, every conclusion after that point is against the wrong baseline."""
    source.chmod(0o644)
    source.write_bytes(source.read_bytes() + b"tampered")
    with pytest.raises(JobCorrupted, match="changed on disk"):
        recover_job(db, job, copy, MACHINE, [])


def test_recovery_sweeps_orphan_temp_files(db, job, copy):
    (copy.tmp_dir / "working.abc.tmp.xlsx").write_bytes(b"junk")
    report = recover_job(db, job, copy, MACHINE, [])
    assert report.temp_files_swept == ["working.abc.tmp.xlsx"]
    assert list(copy.tmp_dir.glob("*.tmp.xlsx")) == []


def test_recovery_records_what_it_did(db, job, copy):
    """`job_events` should say what recovery did rather than leaving it to be inferred
    from the state afterwards."""
    (copy.tmp_dir / "working.abc.tmp.xlsx").write_bytes(b"junk")
    recover_job(db, job, copy, MACHINE, [])
    events = [e for e in list_events(db, "job-1") if e["kind"] == "recovered"]
    assert len(events) == 1
    assert "swept 1 temp file" in events[0]["detail"]


def test_a_clean_job_records_no_recovery_event(db, job, copy):
    """Recovery on an untouched job must be a no-op, or every resume would litter the
    audit trail with events describing nothing."""
    report = recover_job(db, job, copy, MACHINE, [])
    assert not report.did_anything
    assert [e for e in list_events(db, "job-1") if e["kind"] == "recovered"] == []


def test_recovery_then_re_apply_leaves_exactly_one_change(db, job, copy):
    """The end-to-end property: kill the worker between the file write and the commit,
    recover, and confirm no edit is lost and none is applied twice."""
    patch = make_patch()
    open_apply_intent(
        db,
        job_id="job-1",
        issue_id=patch.issue_id,
        patch_id=patch.patch_id,
        run_epoch=job.run_epoch,
        edits=[{"row": 3, "column": 5, "column_key": "answer", "before": "1/2", "after": "1/3"}],
    )
    recover_job(db, job, copy, MACHINE, [])  # nothing landed, so it is re-appliable

    apply_patch(db, job, copy, patch, block_id="block-0000")

    assert len(list_changes(db, "job-1")) == 1
    assert answer_at(copy, 3) == "1/3"
    assert open_apply_intents(db, "job-1") == ()
