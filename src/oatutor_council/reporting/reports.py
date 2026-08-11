"""The four artefacts a curator receives, as data and as prose.

Every report is built from durable records -- issues, change records, verdicts, attempts,
gate findings -- and never from anything held in memory during the run. That is what
makes them reproducible after a resume, and it is why nothing here takes a live object.

The reports are written to be read by someone who was not watching. In particular the
validation report says plainly what is *unresolved*: a job that needed a person must hand
over the corrected workbook **and** an honest account of what it could not fix, never a
summary that reads like success.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from ..models import (
    SUCCESSFUL_ISSUE_STATES,
    ArtifactKind,
    ClaimOutcome,
    ChangeRecord,
    Issue,
    IssueLedger,
    IssueState,
    JobState,
    RepairAttempt,
    ReviewVerdict,
    Severity,
    ValidationFinding,
)

#: Reading order for a report: worst first.
_SEVERITY_ORDER = {
    Severity.BLOCKING: 0,
    Severity.ERROR: 1,
    Severity.WARNING: 2,
    Severity.OBSERVATION: 3,
}


@dataclass(frozen=True)
class JobReports:
    issue_ledger: dict[str, Any]
    change_log: dict[str, Any]
    review_history: dict[str, Any]
    validation_report: dict[str, Any]

    def as_dict(self) -> dict[ArtifactKind, dict[str, Any]]:
        return {
            ArtifactKind.ISSUE_LEDGER: self.issue_ledger,
            ArtifactKind.CHANGE_LOG: self.change_log,
            ArtifactKind.REVIEW_HISTORY: self.review_history,
            ArtifactKind.VALIDATION_REPORT: self.validation_report,
        }


def build_reports(
    *,
    job_id: str,
    state: JobState,
    ledger: IssueLedger,
    changes: Sequence[ChangeRecord],
    verdicts: Sequence[ReviewVerdict],
    attempts: Sequence[RepairAttempt],
    findings: Sequence[ValidationFinding],
    integrity_findings: Sequence[ValidationFinding] = (),
    claims: Sequence[dict[str, Any]] = (),
    claim_results: Sequence[dict[str, Any]] = (),
    usage: dict[str, int] | None = None,
    artifacts: Sequence[dict[str, Any]] = (),
    rediscoveries: dict[str, int] | None = None,
) -> JobReports:
    return JobReports(
        issue_ledger=_issue_ledger(job_id, ledger),
        change_log=_change_log(job_id, changes),
        review_history=_review_history(job_id, ledger, verdicts, attempts),
        validation_report=_validation_report(
            job_id, state, ledger, findings, integrity_findings, changes,
            claims, claim_results, usage or {}, artifacts, rediscoveries or {},
        ),
    )


def resolve_claims(
    claims: Sequence[dict[str, Any]], results: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every claim the curator supplied, with what became of it.

    Driven from the *claims*, not from the results, which is the whole point: iterating
    the results would silently omit any claim nothing ever concluded about, and those are
    exactly the ones a curator needs to know were never reached. A claim with no verdict
    is `UNRESOLVED`, which is a different answer from `REFUTED` -- refuted means a block
    looked and the defect was not there.

    One confirmation anywhere settles a claim. A defect exists if any block has it, and
    the thirty blocks that do not are not evidence against the one that does.
    """
    by_index: dict[int, list[dict[str, Any]]] = {}
    for result in results:
        by_index.setdefault(int(result["segment_index"]), []).append(result)

    resolved = []
    for claim in claims:
        # Only errata are claims. A governing rule ("steps must not have dependencies")
        # is not something a block confirms or refutes, and listing it here as
        # `UNRESOLVED` would report policy the agents applied correctly as a question
        # nobody answered.
        if claim.get("purpose", "errata") != "errata":
            continue
        index = int(claim["segment_index"])
        verdicts = by_index.get(index, [])
        confirmations = [v for v in verdicts if v["outcome"] == ClaimOutcome.CONFIRMED]
        refutations = [v for v in verdicts if v["outcome"] == ClaimOutcome.REFUTED]
        if confirmations:
            outcome, evidence = ClaimOutcome.CONFIRMED, confirmations
        elif refutations:
            outcome, evidence = ClaimOutcome.REFUTED, refutations
        else:
            outcome, evidence = ClaimOutcome.UNRESOLVED, []
        resolved.append(
            {
                "segment_index": index,
                "text": claim["text"],
                "provenance": claim["provenance"],
                "outcome": outcome.value,
                "blocks_considered": len(verdicts),
                "detail": evidence[0]["detail"] if evidence else "",
            }
        )
    return resolved


# --------------------------------------------------------------------------------------
# Individual reports
# --------------------------------------------------------------------------------------


def _issue_ledger(job_id: str, ledger: IssueLedger) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "issue_count": len(ledger.issues),
        "by_state": _count(i.state for i in ledger.issues),
        "by_source": _count(i.source for i in ledger.issues),
        "by_severity": _count(i.severity for i in ledger.issues),
        "issues": [
            {
                "issue_id": issue.issue_id,
                "state": issue.state.value,
                "source": issue.source.value,
                "severity": issue.severity.value,
                "category": issue.category.value,
                "problem_name": issue.problem_name,
                "block_id": issue.block_id,
                "title": issue.title,
                "description": issue.description,
                "rule_codes": list(issue.rule_codes),
                "cells": [list(cell) for cell in issue.cells],
                "is_structural": issue.is_structural,
                "reviewer_role": issue.reviewer_role.value,
                "attempts_used": issue.attempts_used,
            }
            for issue in _ordered(ledger.issues)
        ],
    }


def _change_log(job_id: str, changes: Sequence[ChangeRecord]) -> dict[str, Any]:
    """Every cell this job wrote, with what was there before.

    The `before` value is kept so the log is independently checkable: a curator can take
    this file and the original workbook and verify every edit without trusting anything
    the system says about itself.
    """
    return {
        "job_id": job_id,
        "change_count": len(changes),
        "changes": [
            {
                "change_id": change.change_id,
                "issue_id": change.issue_id,
                "patch_id": change.patch_id,
                "block_id": change.block_id,
                "row": change.row,
                "column": change.column,
                "column_key": change.column_key.value if change.column_key else None,
                "before": change.before,
                "after": change.after,
                "applied_at": change.applied_at.isoformat(),
            }
            for change in sorted(changes, key=lambda c: (c.row, c.column))
        ],
    }


def _review_history(
    job_id: str,
    ledger: IssueLedger,
    verdicts: Sequence[ReviewVerdict],
    attempts: Sequence[RepairAttempt],
) -> dict[str, Any]:
    """Reviewer decisions and the repair loop, grouped by issue.

    The Writer's rationale is deliberately absent from what reviewers saw and is not
    reconstructed here either; it belongs in the change log, where it informs a human
    rather than a reviewer whose independence depends on not having it.
    """
    by_issue: dict[str, dict[str, Any]] = {
        issue.issue_id: {
            "issue_id": issue.issue_id,
            "problem_name": issue.problem_name,
            "reviewer_role": issue.reviewer_role.value,
            "final_state": issue.state.value,
            "attempts": [],
            "verdicts": [],
        }
        for issue in ledger.issues
    }

    for attempt in sorted(attempts, key=lambda a: (a.issue_id, a.attempt_no)):
        entry = by_issue.get(attempt.issue_id)
        if entry is None:
            continue
        entry["attempts"].append(
            {
                "attempt_no": attempt.attempt_no,
                "outcome": attempt.outcome.value if attempt.outcome else None,
                "rejection": attempt.rejection.code.value if attempt.rejection else None,
                "rejection_detail": attempt.rejection.message if attempt.rejection else None,
                "started_at": attempt.started_at.isoformat(),
            }
        )

    for verdict in sorted(verdicts, key=lambda v: (v.issue_id, v.attempt_no)):
        entry = by_issue.get(verdict.issue_id)
        if entry is None:
            continue
        entry["verdicts"].append(
            {
                "attempt_no": verdict.attempt_no,
                "reviewer_role": verdict.reviewer_role.value,
                "decision": verdict.decision.value,
                "feedback": verdict.feedback,
                "rule_codes": list(verdict.rule_codes),
                "decided_at": verdict.decided_at.isoformat(),
            }
        )

    return {"job_id": job_id, "issues": list(by_issue.values())}


def _validation_report(
    job_id: str,
    state: JobState,
    ledger: IssueLedger,
    findings: Sequence[ValidationFinding],
    integrity_findings: Sequence[ValidationFinding],
    changes: Sequence[ChangeRecord],
    claims: Sequence[dict[str, Any]] = (),
    claim_results: Sequence[dict[str, Any]] = (),
    usage: dict[str, int] | None = None,
    artifacts: Sequence[dict[str, Any]] = (),
    rediscoveries: dict[str, int] | None = None,
) -> dict[str, Any]:
    unresolved = [
        issue for issue in ledger.issues if issue.state is IssueState.NEEDS_HUMAN_REVIEW
    ]
    resolved_claims = resolve_claims(claims, claim_results)
    return {
        "job_id": job_id,
        "state": state.value,
        # Read from the state rather than recomputed, and that is not a shortcut:
        # `SUCCEEDED` is set in exactly one place under three conditions -- integrity
        # passed, every issue resolved, no unresolved findings. A second derivation here
        # would be a second definition of success, and the two would eventually disagree.
        "succeeded": state is JobState.SUCCEEDED,
        "integrity_passed": not integrity_findings,
        "integrity_findings": [_render_finding(f) for f in integrity_findings],
        "changes_applied": len(changes),
        # Every state that counts as resolved, not just `ACCEPTED`. A refuted claim was
        # checked and was not there, and a superseded one was fixed by another repair;
        # counting only accepted issues made the summary contradict its own first line.
        "issues_resolved": len(
            [i for i in ledger.issues if i.state in SUCCESSFUL_ISSUE_STATES]
        ),
        "issues_repaired": len(
            [i for i in ledger.issues if i.state is IssueState.ACCEPTED]
        ),
        "issues_superseded": len(ledger.by_state(IssueState.SUPERSEDED)),
        "issues_refuted": len(ledger.by_state(IssueState.REFUTED)),
        "issues_needing_a_person": [
            {
                "issue_id": issue.issue_id,
                "problem_name": issue.problem_name,
                "title": issue.title,
                "description": issue.description,
                "attempts_used": issue.attempts_used,
            }
            for issue in unresolved
        ],
        "remaining_findings": [
            _render_finding(f)
            for f in sorted(
                findings, key=lambda f: (_SEVERITY_ORDER[f.severity], f.row or 0)
            )
        ],
        # A defect that was repaired, came back at final validation, and was repaired
        # again ends in the same state as one that was simply repaired. The counts are
        # the only place the difference shows, and it is the part a curator most needs.
        "issues_reopened": (rediscoveries or {}).get("issue_reopened", 0),
        "findings_absorbed": (rediscoveries or {}).get("finding_absorbed", 0),
        "instruction_claims": resolved_claims,
        "instruction_claims_confirmed": _count_claims(
            resolved_claims, ClaimOutcome.CONFIRMED
        ),
        "instruction_claims_refuted": _count_claims(
            resolved_claims, ClaimOutcome.REFUTED
        ),
        "instruction_claims_unresolved": _count_claims(
            resolved_claims, ClaimOutcome.UNRESOLVED
        ),
        "model_usage": usage or {},
        "artifacts": list(artifacts),
        "unresolved_summary": unresolved_summary(state, ledger, integrity_findings),
    }


def _count_claims(resolved: Sequence[dict[str, Any]], outcome: ClaimOutcome) -> int:
    return len([claim for claim in resolved if claim["outcome"] == outcome])


def unresolved_summary(
    state: JobState,
    ledger: IssueLedger,
    integrity_findings: Sequence[ValidationFinding] = (),
) -> str:
    """One honest sentence about where the job actually got to.

    Written for the case that matters most: a job that could not finish must say so in
    the first line a curator reads, not bury it under a count of what did succeed.
    """
    if integrity_findings:
        return (
            f"The output workbook failed {len(integrity_findings)} integrity check(s). "
            "It must not be used until a person has reviewed the validation report."
        )
    if state is JobState.SUCCEEDED:
        return (
            f"All {len(ledger.issues)} issue(s) reached a resolved state and every "
            "deterministic check passed."
        )
    needing = ledger.by_state(IssueState.NEEDS_HUMAN_REVIEW)
    if needing:
        return (
            f"{len(needing)} issue(s) could not be resolved automatically and need a "
            "person. The corrected workbook contains every change that was accepted; "
            "the issues listed below were left untouched."
        )
    open_issues = ledger.open_issues
    if open_issues:
        return (
            f"The job stopped in state {state.value} with {len(open_issues)} issue(s) "
            "still open."
        )
    return f"The job ended in state {state.value}."


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------


def render_markdown(reports: JobReports) -> str:
    """A human-readable summary of all four reports."""
    validation = reports.validation_report
    lines = [
        f"# Curation report — job {validation['job_id']}",
        "",
        f"**Outcome:** {validation['state']}",
        "",
        validation["unresolved_summary"],
        "",
        "## Summary",
        "",
        f"- Issues tracked: {reports.issue_ledger['issue_count']}",
        f"- Issues resolved: {validation['issues_resolved']}"
        f" (repaired {validation['issues_repaired']},"
        f" refuted {validation['issues_refuted']},"
        f" resolved by another repair {validation['issues_superseded']})",
        f"- Cells changed: {validation['changes_applied']}",
        f"- Integrity checks: {'passed' if validation['integrity_passed'] else 'FAILED'}",
    ]

    # Said out loud rather than left to be inferred from a state. "Repaired, came back,
    # repaired again" and "repaired, came back, gave up" are different stories, and both
    # end in a row that looks like every other row.
    if validation.get("issues_reopened") or validation.get("findings_absorbed"):
        lines.append(
            f"- Defects that came back after being repaired: "
            f"{validation.get('issues_reopened', 0)} reopened for another attempt, "
            f"{validation.get('findings_absorbed', 0)} left for a person"
        )

    usage = validation.get("model_usage") or {}
    if usage.get("calls"):
        lines.append(
            f"- Model calls: {usage['calls']}"
            + (f" ({usage['failed_calls']} failed)" if usage.get("failed_calls") else "")
            + (f", {usage['total_tokens']} tokens" if usage.get("total_tokens") else "")
        )

    artifacts = validation.get("artifacts") or []
    if artifacts:
        lines += ["", "## Files produced", ""]
        # Hashes, not paths. A curator checks the file they downloaded is the file this
        # report is about; the storage layout is nobody's business.
        for artifact in artifacts:
            digest = artifact["sha256"][:16] or "not hashed"
            lines.append(f"- `{artifact['kind']}` — sha256 {digest}…")

    if validation["integrity_findings"]:
        lines += ["", "## Integrity failures", ""]
        lines += [f"- {f['message']}" for f in validation["integrity_findings"]]

    claims = validation.get("instruction_claims") or []
    if claims:
        lines += ["", "## The instructions you supplied", ""]
        # Every claim, including the ones nothing concluded about. A report that listed
        # only the confirmed ones would let a claim nobody read pass for a claim nobody
        # needed to act on.
        for claim in claims:
            summary = {
                "confirmed": "found",
                "refuted": "looked for, not present",
                "unresolved": "**not reached** -- no block concluded anything",
            }[claim["outcome"]]
            lines.append(
                f"- [{claim['provenance']}] {claim['text'][:120]} — {summary}"
                + (f" ({claim['detail'][:80]})" if claim["detail"] else "")
            )

    if validation["issues_needing_a_person"]:
        lines += ["", "## Needs a person", ""]
        for issue in validation["issues_needing_a_person"]:
            lines.append(
                f"- **{issue['problem_name'] or 'workbook'}** — {issue['description']} "
                f"({issue['attempts_used']} attempt(s) made)"
            )

    changes = reports.change_log["changes"]
    if changes:
        lines += ["", "## Changes applied", "", "| Row | Column | Before | After |", "| --- | --- | --- | --- |"]
        for change in changes:
            lines.append(
                f"| {change['row']} | {change['column_key'] or change['column']} "
                f"| `{change['before']}` | `{change['after']}` |"
            )

    remaining = validation["remaining_findings"]
    if remaining:
        lines += ["", "## Remaining findings", ""]
        for item in remaining[:100]:
            location = f"row {item['row']}" if item["row"] else "workbook"
            lines.append(f"- `{item['severity']}` {item['code']} ({location}) — {item['message']}")
        if len(remaining) > 100:
            lines.append(f"- …and {len(remaining) - 100} more")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _render_finding(finding: ValidationFinding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "severity": finding.severity.value,
        "scope": finding.scope.value,
        "message": finding.message,
        "row": finding.row,
        "column": finding.column,
        "block_id": finding.block_id,
        "problem_name": finding.problem_name,
        "repairable": finding.repairable,
    }


def _ordered(issues: Sequence[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda i: (_SEVERITY_ORDER[i.severity], i.block_id or "", i.title),
    )


def _count(values) -> dict[str, int]:
    return dict(sorted(Counter(str(v) for v in values).items()))
