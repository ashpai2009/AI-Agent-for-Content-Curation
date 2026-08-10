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
    assert reports.validation_report["issues_resolved"] == 1


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
