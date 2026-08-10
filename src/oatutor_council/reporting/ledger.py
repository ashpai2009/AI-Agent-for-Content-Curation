"""The issue ledger: building issues from findings, and keeping them distinguishable.

`IssueLedger` itself is a derived view (see `models`) and is never stored -- a persisted
aggregate is somewhere for the database and reality to disagree after a crash. This
module owns the two operations that need care: turning a deterministic finding into a
tracked issue, and fingerprinting so the validation loop cannot cycle.

The fingerprint is load-bearing. Final validation can rediscover a defect the council
already tried and failed to repair; without a stable identity for "the same defect", each
round opens a fresh issue with a fresh attempt budget and the job never terminates. With
one, a finding matching an already-terminal issue escalates the job to human attention
instead -- which is the honest outcome, because three attempts have already failed.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from ..models import (
    Issue,
    IssueCategory,
    IssueLedger,
    IssueSource,
    ReviewerRole,
    Severity,
    ValidationFinding,
)
from ..validation.rules import REGISTRY

#: Findings this severe are worth a repair attempt. Observations are recorded in the
#: report and never enter the repair loop -- an observation the system is not willing to
#: act on would otherwise consume attempts and block success forever.
ACTIONABLE_SEVERITIES = frozenset(
    {Severity.BLOCKING, Severity.ERROR, Severity.WARNING}
)


def fingerprint(finding: ValidationFinding) -> str:
    """A stable identity for "the same defect in the same place".

    Built from the rule code and the location, deliberately **not** from the message or
    the cell's current contents. A repair that changes the value but leaves the defect
    intact must fingerprint identically, or the loop gets a fresh attempt budget every
    round and runs until a global fuse blows.
    """
    parts = (
        finding.code,
        finding.block_id or "",
        str(finding.row or ""),
        str(finding.column or ""),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def issue_from_finding(
    finding: ValidationFinding,
    *,
    job_id: str,
    source: IssueSource,
    reviewer_role: ReviewerRole = ReviewerRole.KNOWN_ISSUE_REVIEWER,
) -> Issue:
    """Promote a deterministic finding into a tracked issue.

    Category and repairability come from the rule registry rather than being restated
    here, so a rule's own declaration stays the single source of truth about what it
    found and whether anything can be done about it.
    """
    rule = REGISTRY.get(finding.code)
    return Issue(
        issue_id=uuid4().hex,
        job_id=job_id,
        block_id=finding.block_id,
        problem_name=finding.problem_name,
        source=source,
        category=rule.category if rule else IssueCategory.STRUCTURE,
        severity=finding.severity,
        title=f"{finding.code} at {_location(finding)}",
        description=finding.message,
        rule_codes=(finding.code,),
        cells=((finding.row, finding.column),)
        if finding.row is not None and finding.column is not None
        else (),
        is_structural=_is_structural(finding),
        reviewer_role=reviewer_role,
        fingerprint=fingerprint(finding),
    )


def _location(finding: ValidationFinding) -> str:
    if finding.row is not None and finding.column is not None:
        return f"row {finding.row}, column {finding.column}"
    if finding.row is not None:
        return f"row {finding.row}"
    return finding.block_id or "the workbook"


def _is_structural(finding: ValidationFinding) -> bool:
    from ..models import STRUCTURAL_COLUMNS, StructuralCode

    if finding.column_key in STRUCTURAL_COLUMNS:
        return True
    return finding.code in {
        StructuralCode.COLUMN_SHIFT,
        StructuralCode.ROW_SHIFT_RIGHT,
        StructuralCode.BLOCK_BOUNDARY_DISAGREEMENT,
        StructuralCode.PROBLEM_NAME_MISMATCH_IN_BLOCK,
        StructuralCode.MISSING_PROBLEM_NAME,
    }


def actionable(findings: tuple[ValidationFinding, ...]) -> tuple[ValidationFinding, ...]:
    """Findings worth opening an issue for."""
    return tuple(
        f
        for f in findings
        if f.severity in ACTIONABLE_SEVERITIES and f.repairable
    )


#: A finding this severe stands between the job and success whether or not anything can
#: be done about it. A blocking defect the council *cannot* repair is precisely the case
#: for `NEEDS_HUMAN_ATTENTION`, and reporting success over it would be the worst outcome
#: available: the curator is told a workbook is fixed when nobody ever looked at it.
UNRESOLVED_SEVERITIES = frozenset({Severity.BLOCKING, Severity.ERROR})


def unresolved(findings: tuple[ValidationFinding, ...]) -> tuple[ValidationFinding, ...]:
    """Findings that must prevent `SUCCEEDED`.

    Deliberately wider than `actionable`. An actionable finding still present at the end
    means the repair loop finished without fixing what it was opened for -- so every
    actionable finding is here. But so is every blocking or erroneous finding the loop
    never touched, because "no issue tracks it" is a statement about this system's
    coverage, not about the workbook being sound.

    Observations stay out: they are recorded in the report and were never claims that
    anything is wrong. A non-repairable *warning* also stays out -- it is neither serious
    enough to stop a job nor something the council was ever going to act on.
    """
    return tuple(
        f
        for f in findings
        if f.severity in UNRESOLVED_SEVERITIES
        or (f.repairable and f.severity in ACTIONABLE_SEVERITIES)
    )


def build_ledger(job_id: str, issues: tuple[Issue, ...]) -> IssueLedger:
    return IssueLedger(job_id=job_id, issues=issues)


def dedupe(findings: tuple[ValidationFinding, ...]) -> tuple[ValidationFinding, ...]:
    """Collapse findings that share a fingerprint.

    Several rules legitimately fire on one broken cell -- a shifted row trips the type
    rule, the choice rule and the answer rule at once. They keep distinct codes and so
    distinct fingerprints, but a rule that yields twice for the same cell would otherwise
    open two issues racing on one edit.
    """
    seen: set[str] = set()
    unique: list[ValidationFinding] = []
    for finding in findings:
        key = fingerprint(finding)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return tuple(unique)
