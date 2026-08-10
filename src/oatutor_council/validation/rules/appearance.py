"""Appearance rules.

These describe what a curated workbook should look like, and they are the only rules
whose subject is presentation rather than content.

**Decided: reported everywhere, corrected only on rows the council edits.** The written
guide asks for wrap off and height 15 throughout, and this system deliberately does not
do that. Three reasons, in order of weight:

1. **It would require weakening the diff gate.** Every difference between source and
   output must trace to an accepted `CellEdit` or to the edited-row appearance rule.
   Normalising hundreds of untouched rows produces hundreds of differences nothing
   authorises, and the only way to let them through is a blanket allowlist -- the exact
   "openpyxl probably did this" category that would also conceal real damage.

2. **It buries the curation in the change log.** A report showing five corrections and
   238 formatting changes is a report nobody audits, and the audit trail is most of what
   this system is for.

3. **Nobody asked for it.** A curator submitted a workbook to have its mathematics
   checked. Restyling every row is a bulk edit performed on their file for this system's
   own tidiness.

So both rules are `OBSERVATION` and **not repairable**: they never open an issue, never
consume a repair attempt, and never block success. They appear in the report, where a
curator who does want a workbook-wide restyle can see exactly which rows it would touch
and do it themselves in one action -- which is cheaper and safer than having an agent do
it a cell at a time.
"""

from __future__ import annotations

from typing import Iterable

from ...models import IssueCategory, Severity, ValidationFinding
from ..rules.registry import RuleContext, finding, rule
from ...workbook.styles import EDITED_ROW_HEIGHT


@rule(
    "WRAP_TEXT_ENABLED",
    severity=Severity.OBSERVATION,
    category=IssueCategory.APPEARANCE,
    description="A row has wrap text enabled.",
    repairable=False,
)
def wrap_text_enabled(context: RuleContext) -> Iterable[ValidationFinding]:
    """The appearance contract turns wrap off on rows an edit touches.

    Reported everywhere else as an observation so a curator can see it, but never
    corrected on a row nothing else changed -- that would be this system rewriting a
    workbook's appearance for its own convenience.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.wrap_text_columns:
            yield finding(
                context,
                "WRAP_TEXT_ENABLED",
                (
                    f"wrap text is enabled on {len(row.wrap_text_columns)} cell(s); "
                    "the appearance contract turns it off on edited rows"
                ),
                row=row.row,
                column=row.wrap_text_columns[0],
                columns=list(row.wrap_text_columns),
            )


@rule(
    "ROW_HEIGHT_NOT_STANDARD",
    severity=Severity.OBSERVATION,
    category=IssueCategory.APPEARANCE,
    description=f"A row has an explicit height other than {EDITED_ROW_HEIGHT}.",
    repairable=False,
)
def row_height_not_standard(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows:
        height = context.parsed.row_heights.get(row.row)
        if height is not None and height != EDITED_ROW_HEIGHT:
            yield finding(
                context,
                "ROW_HEIGHT_NOT_STANDARD",
                (
                    f"row has an explicit height of {height}; edited rows are set to "
                    f"{EDITED_ROW_HEIGHT}"
                ),
                row=row.row,
                column=1,
                height=height,
            )
