"""Ledger and reporting tests.

Two things are under test. The fingerprint must give "the same defect in the same place"
a stable identity across rounds, because that is what stops the validation loop cycling.
And the reports must state an unfinished job's status honestly in the first line a
curator reads -- never a summary that reads like success.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oatutor_council.models import (
    CurationJob,
    AttemptOutcome,
    ChangeRecord,
    ColumnKey,
    FindingScope,
    IssueLedger,
    IssueSource,
    IssueState,
    JobState,
    PatchRejection,
    RejectionCode,
    RepairAttempt,
    ReviewDecision,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    ValidationFinding,
)
from oatutor_council.reporting.ledger import (
    actionable,
    dedupe,
    fingerprint,
    issue_from_finding,
)
from oatutor_council.reporting.reports import (
    build_reports,
    render_markdown,
    unresolved_summary,
)


def make_finding(**kwargs) -> ValidationFinding:
    defaults = dict(
        code="STEP_MISSING_ANSWER",
        severity=Severity.ERROR,
        scope=FindingScope.CELL,
        message="step row has no Answer",
        row=3,
        column=5,
        column_key=ColumnKey.ANSWER,
        block_id="block-0000",
        problem_name="angles1",
    )
    return ValidationFinding(**{**defaults, **kwargs})


def make_issue(**kwargs):
    issue = issue_from_finding(
        make_finding(), job_id="job-1", source=IssueSource.INITIAL_AUDITOR
    )
    return issue.model_copy(update=kwargs)


# --------------------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------------------


def test_the_same_defect_in_the_same_place_fingerprints_identically():
    """The property termination depends on. A repair that changes the cell's value but
    leaves the defect intact must land on the same fingerprint, or every validation round
    opens a fresh issue with a fresh attempt budget and the job never ends."""
    assert fingerprint(make_finding(message="one wording")) == fingerprint(
        make_finding(message="a completely different wording")
    )


def test_a_different_location_fingerprints_differently():
    assert fingerprint(make_finding(row=3)) != fingerprint(make_finding(row=4))
    assert fingerprint(make_finding(column=5)) != fingerprint(make_finding(column=6))


def test_a_different_rule_fingerprints_differently():
    """Several rules legitimately fire on one broken cell. They are separate defects
    with separate repairs and must not collapse into one issue."""
    assert fingerprint(make_finding(code="STEP_MISSING_ANSWER")) != fingerprint(
        make_finding(code="ANSWER_WITHOUT_TYPE")
    )


def test_dedupe_collapses_repeats_and_keeps_distinct_codes():
    findings = (
        make_finding(),
        make_finding(message="restated"),
        make_finding(code="ANSWER_WITHOUT_TYPE"),
    )
    assert len(dedupe(findings)) == 2


# --------------------------------------------------------------------------------------
# Promotion to issues
# --------------------------------------------------------------------------------------


def test_an_issue_inherits_its_category_from_the_rule_registry():
    """The rule's own declaration is the single source of truth about what it found."""
    issue = issue_from_finding(
        make_finding(), job_id="job-1", source=IssueSource.INITIAL_AUDITOR
    )
    assert issue.category.value == "row_type"
    assert issue.rule_codes == ("STEP_MISSING_ANSWER",)
    assert issue.cells == ((3, 5),)
    assert issue.state is IssueState.OPEN


def test_a_finding_in_a_structural_column_is_marked_structural():
    """Structural edits are not forbidden, but they pass a stricter gate -- so the issue
    has to carry the flag that routes them there."""
    issue = issue_from_finding(
        make_finding(code="COLUMN_SHIFT", column_key=ColumnKey.ANSWER_TYPE, column=6),
        job_id="job-1",
        source=IssueSource.INITIAL_AUDITOR,
    )
    assert issue.is_structural


def test_observations_and_unrepairable_findings_do_not_become_issues():
    """An observation the system will not act on would consume attempts and block
    success forever."""
    findings = (
        make_finding(),
        make_finding(code="MC_ANSWER_IS_FIRST_CHOICE", severity=Severity.OBSERVATION),
        make_finding(code="LATEX_BANNED_COMMAND", repairable=False),
    )
    assert [f.code for f in actionable(findings)] == ["STEP_MISSING_ANSWER"]


# --------------------------------------------------------------------------------------
# Ledger views
# --------------------------------------------------------------------------------------


def test_a_block_whose_only_claim_was_refuted_is_left_to_the_independent_sweep():
    """Otherwise a bogus claim buys a problem permanent immunity from any review."""
    ledger = IssueLedger(
        job_id="job-1",
        issues=(
            make_issue(issue_id="a", block_id="block-0001", state=IssueState.REFUTED),
            make_issue(issue_id="b", block_id="block-0002", state=IssueState.ACCEPTED),
        ),
    )
    assert ledger.blocks_with_ledger_entry == frozenset({"block-0002"})


def test_success_requires_every_issue_resolved_and_a_refutation_counts():
    """A correctly refuted claim is a correct outcome, not a blocker."""
    resolved = IssueLedger(
        job_id="job-1",
        issues=(
            make_issue(issue_id="a", state=IssueState.ACCEPTED),
            make_issue(issue_id="b", state=IssueState.REFUTED),
            make_issue(issue_id="c", state=IssueState.SUPERSEDED),
        ),
    )
    assert resolved.all_resolved

    needing = resolved.model_copy(
        update={
            "issues": resolved.issues
            + (make_issue(issue_id="d", state=IssueState.NEEDS_HUMAN_REVIEW),)
        }
    )
    assert not needing.all_resolved


# --------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------


@pytest.fixture
def reports():
    ledger = IssueLedger(
        job_id="job-1",
        issues=(
            make_issue(issue_id="a", state=IssueState.ACCEPTED, attempts_used=1),
            make_issue(
                issue_id="b",
                state=IssueState.NEEDS_HUMAN_REVIEW,
                attempts_used=3,
                problem_name="unitcirc2",
            ),
            make_issue(issue_id="c", state=IssueState.REFUTED),
        ),
    )
    change = ChangeRecord(
        change_id="c1",
        issue_id="a",
        patch_id="p1",
        block_id="block-0000",
        row=3,
        column=5,
        column_key=ColumnKey.ANSWER,
        before="2026-01-02 00:00:00",
        after="1/2",
        applied_at=datetime.now(timezone.utc),
    )
    verdict = ReviewVerdict(
        verdict_id="v1",
        issue_id="a",
        reviewer_role=ReviewerRole.KNOWN_ISSUE_REVIEWER,
        attempt_no=1,
        decision=ReviewDecision.ACCEPT,
    )
    attempt = RepairAttempt(
        attempt_id="t1",
        issue_id="a",
        attempt_no=1,
        outcome=AttemptOutcome.PATCH_ACCEPTED,
        patch_id="p1",
    )
    rejected = RepairAttempt(
        attempt_id="t2",
        issue_id="b",
        attempt_no=1,
        outcome=AttemptOutcome.PATCH_REJECTED,
        rejection=PatchRejection(
            code=RejectionCode.BEFORE_MISMATCH, message="cell holds something else"
        ),
    )
    return build_reports(
        job_id="job-1",
        state=JobState.NEEDS_HUMAN_ATTENTION,
        ledger=ledger,
        changes=[change],
        verdicts=[verdict],
        attempts=[attempt, rejected],
        findings=[make_finding(code="WHITESPACE_PADDING", severity=Severity.WARNING)],
    )


def test_the_change_log_keeps_the_before_value(reports):
    """So a curator can verify every edit against the original workbook without
    trusting anything the system says about itself."""
    change = reports.change_log["changes"][0]
    assert change["before"] == "2026-01-02 00:00:00"
    assert change["after"] == "1/2"
    assert change["column_key"] == "answer"


def test_the_review_history_groups_attempts_and_verdicts_by_issue(reports):
    by_id = {entry["issue_id"]: entry for entry in reports.review_history["issues"]}
    assert by_id["a"]["verdicts"][0]["decision"] == "accept"
    assert by_id["b"]["attempts"][0]["rejection"] == "BEFORE_MISMATCH"
    assert by_id["c"]["attempts"] == []


def test_the_validation_report_does_not_read_like_success(reports):
    """A job that needed a person must say so in the first line a curator reads."""
    validation = reports.validation_report
    assert validation["succeeded"] is False
    assert "need a person" in validation["unresolved_summary"]
    assert [i["problem_name"] for i in validation["issues_needing_a_person"]] == [
        "unitcirc2"
    ]


def test_the_report_still_hands_over_what_was_accepted(reports):
    """Finalisation writes outputs before evaluating gates, so an unfinished job still
    delivers the corrected workbook and an account of what it could not fix."""
    assert reports.validation_report["changes_applied"] == 1
    # `resolved` is every state compatible with success, and the split says how each one
    # got there. Counting only repairs made the summary contradict its own first line on
    # a job whose issues were refuted or resolved by a sibling repair.
    assert reports.validation_report["issues_resolved"] == 2
    assert reports.validation_report["issues_repaired"] == 1
    assert reports.validation_report["issues_refuted"] == 1
    assert reports.validation_report["issues_superseded"] == 0


def test_cells_changed_is_net_output_not_retry_and_rollback_operations():
    now = datetime.now(timezone.utc)
    forward = ChangeRecord(
        change_id="forward",
        issue_id="a",
        patch_id="p1",
        block_id="block-0000",
        row=3,
        column=5,
        column_key=ColumnKey.ANSWER,
        before="1/2",
        after="0.5",
        applied_at=now,
    )
    rollback = forward.model_copy(
        update={
            "change_id": "rollback",
            "patch_id": "rollback-v1",
            "before": "0.5",
            "after": "1/2",
        }
    )
    reports = build_reports(
        job_id="job-1",
        state=JobState.NEEDS_HUMAN_ATTENTION,
        ledger=IssueLedger(job_id="job-1", issues=()),
        changes=[forward, rollback],
        verdicts=(),
        attempts=(),
        findings=(),
    )

    assert reports.validation_report["changes_applied"] == 0
    assert reports.validation_report["edit_operations"] == 2
    assert reports.change_log["change_count"] == 0
    assert reports.change_log["edit_operation_count"] == 2


def test_an_integrity_failure_dominates_the_summary():
    """Content counts are irrelevant next to an output file that cannot be trusted."""
    ledger = IssueLedger(job_id="job-1", issues=())
    summary = unresolved_summary(
        JobState.FAILED,
        ledger,
        [
            ValidationFinding(
                code="UNEXPLAINED_DIFFERENCE",
                severity=Severity.BLOCKING,
                scope=FindingScope.WORKBOOK,
                message="unexplained change",
            )
        ],
    )
    assert "must not be used" in summary


def test_markdown_renders_the_outcome_and_the_changes(reports):
    text = render_markdown(reports)
    assert "needs_human_attention" in text
    assert "## Needs a person" in text
    assert "`2026-01-02 00:00:00`" in text
    assert "## Changes applied" in text


# --------------------------------------------------------------------------------------
# The curator's own claims
# --------------------------------------------------------------------------------------


def claim(index: int, text: str = "Problem 3 has the wrong answer"):
    return {"segment_index": index, "text": text, "provenance": f"line {index + 1}"}


def verdict(index: int, block: str, outcome: str, detail: str = ""):
    return {
        "segment_index": index,
        "block_id": block,
        "outcome": outcome,
        "detail": detail,
    }


def test_every_supplied_claim_appears_in_the_report():
    """Driven from the claims, not the results. Iterating the results would silently
    omit any claim nothing concluded about -- exactly the ones a curator needs to know
    were never reached."""
    from oatutor_council.reporting.reports import resolve_claims

    resolved = resolve_claims([claim(0), claim(1), claim(2)], [])
    assert [c["outcome"] for c in resolved] == ["unresolved"] * 3


def test_a_confirmation_anywhere_settles_a_claim():
    """A defect exists if any block has it. The thirty blocks that do not are not
    evidence against the one that does."""
    from oatutor_council.reporting.reports import resolve_claims

    resolved = resolve_claims(
        [claim(0)],
        [
            verdict(0, "block-1", "refuted", "not here"),
            verdict(0, "block-2", "confirmed", "the answer is 1/2, not 2"),
            verdict(0, "block-3", "refuted", "not here either"),
        ],
    )
    assert resolved[0]["outcome"] == "confirmed"
    assert resolved[0]["detail"] == "the answer is 1/2, not 2"
    assert resolved[0]["blocks_considered"] == 3


def test_refuted_and_unresolved_are_different_answers():
    """Refuted means a block looked and the defect was not there. Unresolved means
    nothing reached a conclusion. Collapsing them tells a curator their report was
    checked and dismissed when in fact it was never read."""
    from oatutor_council.reporting.reports import resolve_claims

    resolved = resolve_claims(
        [claim(0), claim(1)], [verdict(0, "block-1", "refuted", "checked, absent")]
    )
    assert [c["outcome"] for c in resolved] == ["refuted", "unresolved"]
    assert resolved[0]["blocks_considered"] == 1
    assert resolved[1]["blocks_considered"] == 0


def test_the_markdown_report_lists_every_claim_including_the_unread_ones():
    from oatutor_council.reporting.reports import build_reports, render_markdown

    reports = build_reports(
        job_id="job-1",
        state=JobState.SUCCEEDED,
        ledger=IssueLedger(job_id="job-1", issues=()),
        changes=(),
        verdicts=(),
        attempts=(),
        findings=(),
        claims=[claim(0, "Problem 3 is wrong"), claim(1, "Problem 9 is wrong")],
        claim_results=[verdict(0, "block-1", "confirmed", "found it")],
    )
    text = render_markdown(reports)
    assert "Problem 3 is wrong" in text
    assert "Problem 9 is wrong" in text
    assert "not reached" in text


def test_only_the_latest_round_of_findings_is_reported(tmp_path):
    """Every round re-runs the whole rule set over the whole workbook, so round two's
    findings are not additional to round one's -- they are what is left after the repairs
    round one asked for. Concatenating rounds would report defects fixed two rounds ago
    as still present, which is the success lie pointed the other way."""
    from oatutor_council.persistence import (
        Database,
        create_job,
        latest_findings,
        record_findings,
    )

    db = Database(tmp_path / "c.db")
    create_job(db, CurationJob(job_id="job-1", source_filename="w.xlsx"))

    first = _finding("MC_ANSWER_NOT_IN_CHOICES", row=4)
    second = _finding("SCAFFOLD_MISSING_ANSWER", row=7)
    record_findings(db, "job-1", 0, [first, second], ["a", "b"])
    record_findings(db, "job-1", 1, [second], ["b"])

    assert [f.code for f in latest_findings(db, "job-1")] == ["SCAFFOLD_MISSING_ANSWER"]


def test_content_and_integrity_findings_are_kept_apart(tmp_path):
    """They mean opposite things to a curator: one is work still to do, the other is a
    reason not to use the file at all."""
    from oatutor_council.persistence import (
        Database,
        create_job,
        latest_findings,
        record_findings,
    )

    db = Database(tmp_path / "c.db")
    create_job(db, CurationJob(job_id="job-1", source_filename="w.xlsx"))
    record_findings(db, "job-1", 1, [_finding("MC_CHOICE_COUNT", row=3)], ["a"])
    record_findings(
        db, "job-1", 1, [_finding("UNRECORDED_CHANGE", row=9)], ["b"], kind="integrity"
    )

    assert [f.code for f in latest_findings(db, "job-1", kind="content")] == [
        "MC_CHOICE_COUNT"
    ]
    assert [f.code for f in latest_findings(db, "job-1", kind="integrity")] == [
        "UNRECORDED_CHANGE"
    ]


def test_a_clean_final_round_reports_nothing_rather_than_the_previous_round(tmp_path):
    """The bug this exists for, and it was the worst-shaped one this system can have.

    Final validation of a repaired workbook records an empty list at round
    `FINAL_GATE_ROUND`. `record_findings` wrote no rows for it, so deriving "the latest
    round" from `MAX(round_no)` over the findings skipped the round entirely, landed on
    round 0 and returned the six defects the job had already repaired. The API told a
    curator their finished workbook still carried every fault it arrived with.

    A round that finds nothing is a result. It just is not a row, which is why the round
    itself has to be recorded and not inferred from its findings.
    """
    from oatutor_council.persistence import (
        Database,
        create_job,
        latest_findings,
        record_findings,
    )

    db = Database(tmp_path / "c.db")
    create_job(db, CurationJob(job_id="job-1", source_filename="w.xlsx"))

    record_findings(
        db,
        "job-1",
        0,
        [_finding("MC_ANSWER_NOT_IN_CHOICES", row=4), _finding("SCAFFOLD_MISSING_ANSWER", row=7)],
        ["a", "b"],
    )
    assert len(latest_findings(db, "job-1")) == 2

    # Everything was repaired; the final gate finds nothing.
    record_findings(db, "job-1", 1000, [], [])

    assert latest_findings(db, "job-1") == ()


def test_an_empty_round_is_recorded_per_kind(tmp_path):
    """A clean content round must not be read as a clean integrity round, or the reverse.

    Integrity is the more dangerous direction: it is the difference between "the output is
    an accounted-for descendant of your file" and "nobody has checked".
    """
    from oatutor_council.persistence import (
        Database,
        create_job,
        latest_findings,
        record_findings,
    )

    db = Database(tmp_path / "c.db")
    create_job(db, CurationJob(job_id="job-1", source_filename="w.xlsx"))
    record_findings(db, "job-1", 0, [_finding("MC_CHOICE_COUNT", row=3)], ["a"])
    record_findings(
        db, "job-1", 0, [_finding("UNRECORDED_CHANGE", row=9)], ["b"], kind="integrity"
    )

    # The content round comes back clean. Integrity was not re-run.
    record_findings(db, "job-1", 1000, [], [])

    assert latest_findings(db, "job-1", kind="content") == ()
    assert [f.code for f in latest_findings(db, "job-1", kind="integrity")] == [
        "UNRECORDED_CHANGE"
    ]


def test_re_recording_a_round_replaces_it_rather_than_doubling_it(tmp_path):
    """A round re-run after a crash must not report every finding twice."""
    from oatutor_council.persistence import (
        Database,
        create_job,
        latest_findings,
        record_findings,
    )

    db = Database(tmp_path / "c.db")
    create_job(db, CurationJob(job_id="job-1", source_filename="w.xlsx"))
    finding = _finding("MC_CHOICE_COUNT", row=3)
    record_findings(db, "job-1", 1, [finding], ["a"])
    record_findings(db, "job-1", 1, [finding], ["a"])

    assert len(latest_findings(db, "job-1")) == 1


def _finding(code: str, *, row: int) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        message=f"{code} at row {row}",
        severity=Severity.ERROR,
        scope=FindingScope.BLOCK,
        row=row,
        problem_name="angles1",
    )
