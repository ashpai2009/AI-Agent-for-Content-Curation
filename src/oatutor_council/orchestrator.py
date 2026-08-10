"""Crash-safe application of patches, and recovery after a crash.

The apply sequence is **intent, then file, then commit**:

1. Commit an `apply_intents` row before any byte is written. This is the only durable
   evidence that a write was authorised, and it must exist before the write rather than
   after, or a crash in between leaves a changed file nobody can account for.
2. Write the workbook atomically -- temp file inside the job directory, fsync, reopen as
   a parse check, `os.replace`.
3. Commit the applied state and the change records.

A crash can land between any two of those. Recovery resolves it by **reading the target
cells**, never by hashing the file: openpyxl output is not byte-reproducible, so a hash
of the expected result could never be computed in advance. The cells tell the truth.

The three possible readings are exhaustive. Every cell holds its `after` -- the write
landed, so roll forward and commit the records. Every cell holds its `before` -- the
write never landed, so re-apply. A *mixture* is impossible under `os.replace`, which
swaps the whole file or nothing, so it can only mean something outside this system
modified the working copy: that is corruption, and the job fails rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from openpyxl import load_workbook

from .models import (
    CellEdit,
    ChangeRecord,
    ColumnKey,
    CurationJob,
    FailureReason,
    Issue,
    IssueState,
    Patch,
)
from .persistence import (
    Database,
    open_apply_intent,
    open_apply_intents,
    open_attempts,
    record_changes,
    record_event,
    save_issue,
    settle_apply_intent,
    settle_attempt,
)
from .state_machine import IssueMachine
from .workbook.reader import render_cell
from .workbook.writer import (
    EditRejected,
    WorkbookWriteError,
    WorkingCopy,
    apply_edits,
    sweep_orphan_temp_files,
)


class JobCorrupted(Exception):
    """The working copy is in a state this system cannot have produced.

    Always terminal. The whole recovery argument rests on `os.replace` being atomic, so
    a half-applied patch means something else wrote to the file -- and continuing would
    be building on an unknown state.
    """

    def __init__(self, message: str, *, job_id: str = "") -> None:
        super().__init__(message)
        self.job_id = job_id


@dataclass
class RecoveryReport:
    """What recovery did, so `job_events` can record it rather than infer it."""

    rolled_forward: list[str] = field(default_factory=list)
    reapplied: list[str] = field(default_factory=list)
    attempts_closed: list[str] = field(default_factory=list)
    attempts_refunded: list[str] = field(default_factory=list)
    temp_files_swept: list[str] = field(default_factory=list)

    @property
    def did_anything(self) -> bool:
        return bool(
            self.rolled_forward
            or self.reapplied
            or self.attempts_closed
            or self.temp_files_swept
        )

    def describe(self) -> str:
        return (
            f"rolled forward {len(self.rolled_forward)}, "
            f"re-applied {len(self.reapplied)}, "
            f"closed {len(self.attempts_closed)} attempt(s), "
            f"refunded {len(self.attempts_refunded)}, "
            f"swept {len(self.temp_files_swept)} temp file(s)"
        )


# --------------------------------------------------------------------------------------
# Applying a patch
# --------------------------------------------------------------------------------------


def _edit_payload(edits: Sequence[CellEdit]) -> list[dict[str, Any]]:
    return [
        {
            "row": edit.row,
            "column": edit.column,
            "column_key": edit.column_key.value if edit.column_key else None,
            "before": edit.before,
            "after": edit.after,
        }
        for edit in edits
    ]


def _edits_from_payload(payload: Sequence[dict[str, Any]]) -> tuple[CellEdit, ...]:
    return tuple(
        CellEdit(
            row=item["row"],
            column=item["column"],
            column_key=ColumnKey(item["column_key"]) if item["column_key"] else None,
            before=item["before"],
            after=item["after"],
        )
        for item in payload
    )


def apply_patch(
    db: Database,
    job: CurationJob,
    copy: WorkingCopy,
    patch: Patch,
    *,
    block_id: str | None = None,
) -> tuple[ChangeRecord, ...]:
    """Apply one patch under the intent-then-file-then-commit protocol.

    The intent is committed first and settled last. Between those two commits the system
    can crash at any point and recovery will still be able to tell what happened, because
    the intent names exactly which cells should hold which values.
    """
    intent_id = open_apply_intent(
        db,
        job_id=job.job_id,
        issue_id=patch.issue_id,
        patch_id=patch.patch_id,
        run_epoch=job.run_epoch,
        edits=_edit_payload(patch.edits),
    )

    try:
        changes = apply_edits(
            copy,
            patch.edits,
            issue_id=patch.issue_id,
            patch_id=patch.patch_id,
            block_id=block_id,
        )
    except (EditRejected, WorkbookWriteError):
        # Nothing was written -- `apply_edits` verifies the whole batch before touching
        # the file -- so the intent is settled as abandoned and the caller decides
        # whether the attempt was consumed.
        settle_apply_intent(db, intent_id, "rejected")
        raise

    record_changes(db, job.job_id, changes)
    settle_apply_intent(db, intent_id, "applied")
    return changes


# --------------------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------------------


def _read_cells(copy: WorkingCopy, edits: Sequence[CellEdit]) -> list[str]:
    workbook = load_workbook(copy.path, data_only=False)
    try:
        sheet = workbook.active
        return [
            render_cell(sheet.cell(row=edit.row, column=edit.column).value)
            for edit in edits
        ]
    finally:
        workbook.close()


def recover_apply_intents(
    db: Database, job: CurationJob, copy: WorkingCopy, report: RecoveryReport
) -> None:
    """Settle every apply intent left open by a crash.

    Reads the cells rather than the file. See the module docstring for why a hash cannot
    serve here, and why a mixed result is corruption rather than a case to handle.
    """
    for intent in open_apply_intents(db, job.job_id):
        edits = _edits_from_payload(intent["edits"])
        if not edits:
            settle_apply_intent(db, intent["intent_id"], "empty")
            continue

        actual = _read_cells(copy, edits)
        all_after = all(value == edit.after for value, edit in zip(actual, edits))
        all_before = all(value == edit.before for value, edit in zip(actual, edits))

        if all_after:
            # The file landed; only the change records are missing. Committing them now
            # is what makes the ledger and the workbook agree again.
            changes = _changes_from(edits, intent, job)
            record_changes(db, job.job_id, changes)
            settle_apply_intent(db, intent["intent_id"], "applied")
            report.rolled_forward.append(intent["intent_id"])
        elif all_before:
            settle_apply_intent(db, intent["intent_id"], "not_applied")
            report.reapplied.append(intent["intent_id"])
        else:
            raise JobCorrupted(
                f"working copy is partially patched for intent {intent['intent_id']}; "
                "os.replace cannot produce this, so the file was modified outside the job",
                job_id=job.job_id,
            )


def _changes_from(
    edits: Sequence[CellEdit], intent: dict[str, Any], job: CurationJob
) -> list[ChangeRecord]:
    applied_at = datetime.now(timezone.utc)
    return [
        ChangeRecord(
            change_id=uuid4().hex,
            issue_id=intent["issue_id"],
            patch_id=intent["patch_id"],
            block_id=None,
            row=edit.row,
            column=edit.column,
            column_key=edit.column_key,
            before=edit.before,
            after=edit.after,
            applied_at=applied_at,
        )
        for edit in edits
    ]


def recover_attempts(
    db: Database,
    job: CurationJob,
    machine: IssueMachine,
    issues: dict[str, Issue],
    report: RecoveryReport,
) -> None:
    """Close attempts that started and never finished.

    An attempt is refunded **only if no patch was recorded against it**. A patch means
    the Writer call completed and the attempt was legitimately spent; refunding it would
    hand back an attempt the system genuinely used, and three such refunds would make the
    cap meaningless. No patch means the call never returned, which is infrastructure
    failure and exactly what the bounded refund budget is for.
    """
    from .models import AttemptOutcome

    for attempt in open_attempts(db, job.job_id):
        settle_attempt(
            db,
            attempt.model_copy(
                update={
                    "outcome": AttemptOutcome.INTERRUPTED,
                    "finished_at": datetime.now(timezone.utc),
                }
            ),
        )
        report.attempts_closed.append(attempt.attempt_id)

        issue = issues.get(attempt.issue_id)
        if issue is None or attempt.patch_id is not None:
            continue

        refunded = machine.refund_interrupted(issue)
        if refunded.attempts_used != issue.attempts_used:
            save_issue(db, refunded)
            issues[issue.issue_id] = refunded
            report.attempts_refunded.append(attempt.attempt_id)


def recover_job(
    db: Database,
    job: CurationJob,
    copy: WorkingCopy,
    machine: IssueMachine,
    issues: Sequence[Issue] = (),
) -> RecoveryReport:
    """Bring a crashed job back to a state the phase loop can drain.

    The source hash is verified **first**. Everything below it compares the working copy
    against the source, so if the curator's original moved, every conclusion drawn after
    that point would be against the wrong baseline.
    """
    report = RecoveryReport()

    try:
        copy.verify_source_unchanged()
    except WorkbookWriteError as error:
        raise JobCorrupted(str(error), job_id=job.job_id) from error

    recover_apply_intents(db, job, copy, report)
    recover_attempts(db, job, machine, {i.issue_id: i for i in issues}, report)

    # A leftover temp file is unambiguously garbage: `os.replace` either happened or it
    # did not, so nothing that matters can be lost by deleting one.
    report.temp_files_swept = list(sweep_orphan_temp_files(copy.tmp_dir))

    if report.did_anything:
        record_event(db, job.job_id, "recovered", report.describe())
    return report


def failure_for(error: Exception) -> FailureReason:
    """Map an exception to the reason recorded on the job.

    Corruption is deliberately not resumable: retrying a job whose file is in an unknown
    state repeats the damage rather than recovering from it.
    """
    if isinstance(error, JobCorrupted):
        return FailureReason.CORRUPTION
    if isinstance(error, WorkbookWriteError):
        return FailureReason.CORRUPTION
    return FailureReason.INTERNAL


def issue_is_retryable(issue: Issue) -> bool:
    """Whether the phase loop can pick this issue up again after a crash.

    Nothing here changes the issue's state. An interrupted attempt leaves the issue where
    it was, and each phase predicate already covers the state it owns -- so re-running
    the step is the recovery, and there is no reconciliation pass that could put an issue
    somewhere no phase looks.
    """
    return issue.state in {
        IssueState.OPEN,
        IssueState.AWAITING_PATCH,
        IssueState.PATCH_PROPOSED,
        IssueState.APPLYING,
        IssueState.PATCH_APPLIED,
        IssueState.AWAITING_REVIEW,
        IssueState.REVISION_REQUESTED,
        IssueState.PATCH_REJECTED,
    }
