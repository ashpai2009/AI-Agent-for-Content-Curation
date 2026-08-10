"""The final deterministic gate. Emits findings; never edits.

Two questions are asked here and they are deliberately kept apart.

**Integrity** -- is the output file a faithful, fully accounted-for descendant of the
source? The source hash still matches, the output reopens as a workbook, every
difference traces to an accepted change, and every accepted change is actually present.
Integrity failing means the *system* misbehaved, and no amount of content review makes
that acceptable.

**Content** -- what is still wrong with the mathematics? These findings are routed back
to a reviewer, not treated as a gate: a workbook that arrived with warnings does not
become a failure because it still has some.

Conflating the two is how a system reports success on a damaged file, so `passed` is
integrity only and content findings are returned alongside for the orchestrator to route.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..models import (
    ChangeRecord,
    FindingScope,
    Severity,
    SourcePath,
    ValidationFinding,
)
from ..workbook.diff import (
    WorkbookDifference,
    compare_workbooks,
    net_changes,
    reconcile,
)
from ..workbook.reader import read_workbook
from ..workbook.writer import sha256_of
from .rules import run_rules


class GateCode:
    """Integrity failures. Separate from rule codes because these are not about content."""

    SOURCE_MODIFIED = "SOURCE_MODIFIED"
    OUTPUT_UNREADABLE = "OUTPUT_UNREADABLE"
    UNEXPLAINED_DIFFERENCE = "UNEXPLAINED_DIFFERENCE"
    RECORDED_CHANGE_NOT_PRESENT = "RECORDED_CHANGE_NOT_PRESENT"


@dataclass(frozen=True)
class GateResult:
    """What the gate found. `passed` is integrity, never content."""

    integrity_findings: tuple[ValidationFinding, ...] = ()
    content_findings: tuple[ValidationFinding, ...] = ()
    unexplained: tuple[WorkbookDifference, ...] = ()
    changes_not_present: tuple[ChangeRecord, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.integrity_findings

    @property
    def blocking_content(self) -> tuple[ValidationFinding, ...]:
        return tuple(
            f for f in self.content_findings if f.severity is Severity.BLOCKING
        )

    def summary(self) -> str:
        if not self.passed:
            return (
                f"integrity gate failed: {len(self.integrity_findings)} problem(s) with "
                "the output file itself"
            )
        blocking = len(self.blocking_content)
        return (
            f"integrity gate passed; {len(self.content_findings)} content finding(s) "
            f"remain, {blocking} blocking"
        )


def run_final_gate(
    *,
    source: SourcePath,
    source_sha256: str,
    output: Path,
    changes: Sequence[ChangeRecord],
    sheet_name: str | None = None,
) -> GateResult:
    """Run every deterministic check over the finished workbook.

    Order matters. The source hash is checked first: if the curator's original moved,
    every comparison below it is against the wrong baseline and reporting its results
    would be worse than reporting nothing.
    """
    integrity: list[ValidationFinding] = []

    source_path = Path(source)
    actual = sha256_of(source_path)
    if actual != source_sha256:
        integrity.append(
            ValidationFinding(
                code=GateCode.SOURCE_MODIFIED,
                severity=Severity.BLOCKING,
                scope=FindingScope.WORKBOOK,
                repairable=False,
                message=(
                    "the source workbook changed on disk during the job, so the output "
                    "cannot be compared against what was submitted"
                ),
                detail={"expected": source_sha256, "actual": actual},
            )
        )
        return GateResult(integrity_findings=tuple(integrity))

    try:
        parsed = read_workbook(output)
    except Exception as error:  # noqa: BLE001
        # Deliberately broad. A corrupt archive, a truncated save and an unparseable
        # workbook all mean the same thing here: the file we are about to hand a
        # curator cannot be opened, and the reason matters less than the refusal.
        integrity.append(
            ValidationFinding(
                code=GateCode.OUTPUT_UNREADABLE,
                severity=Severity.BLOCKING,
                scope=FindingScope.WORKBOOK,
                repairable=False,
                message=f"the corrected workbook could not be reopened: {error}",
            )
        )
        return GateResult(integrity_findings=tuple(integrity))

    curated_sheet = sheet_name or parsed.sheet_name
    differences = compare_workbooks(source_path, output)
    unexplained = reconcile(differences, changes, sheet_name=curated_sheet)
    for difference in unexplained:
        integrity.append(
            ValidationFinding(
                code=GateCode.UNEXPLAINED_DIFFERENCE,
                severity=Severity.BLOCKING,
                scope=FindingScope.CELL
                if difference.row and difference.column
                else FindingScope.WORKBOOK,
                row=difference.row,
                column=difference.column,
                repairable=False,
                message=(
                    "the output differs from the source in a way no accepted change "
                    f"accounts for: {difference.describe()}"
                ),
                detail={"dimension": str(difference.dimension)},
            )
        )

    missing = _changes_not_present(differences, changes, curated_sheet)
    for change in missing:
        integrity.append(
            ValidationFinding(
                code=GateCode.RECORDED_CHANGE_NOT_PRESENT,
                severity=Severity.BLOCKING,
                scope=FindingScope.CELL,
                row=change.row,
                column=change.column,
                column_key=change.column_key,
                repairable=False,
                message=(
                    "the change log records an edit that is not present in the output; "
                    "the ledger and the workbook have diverged"
                ),
                detail={"before": change.before, "after": change.after},
            )
        )

    return GateResult(
        integrity_findings=tuple(integrity),
        content_findings=tuple(run_rules(parsed)) + parsed.all_findings,
        unexplained=unexplained,
        changes_not_present=missing,
    )


def _changes_not_present(
    differences: Iterable[WorkbookDifference],
    changes: Sequence[ChangeRecord],
    sheet_name: str,
) -> tuple[ChangeRecord, ...]:
    """Find ledger entries the output does not actually contain.

    The reconciliation in `diff.reconcile` only asks whether each *difference* is
    authorised. This asks the opposite question -- whether each authorised change
    happened -- and it is the one that catches a lost write. A job that reported an edit
    it never made would otherwise hand a curator a report describing a workbook that
    does not exist.
    """
    changed_cells = {
        (d.row, d.column)
        for d in differences
        if d.sheet == sheet_name and d.row is not None and d.column is not None
    }
    # Compared per cell rather than per record, for the same reason `reconcile` is: a
    # cell edited twice has two records and one net difference. A net effect of "no
    # change" -- a value written and then written back -- correctly expects no difference.
    return tuple(
        change
        for cell, change in net_changes(changes).items()
        if cell not in changed_cells and change.before != change.after
    )
