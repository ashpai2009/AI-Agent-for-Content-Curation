"""Turning workbook content into the fenced data sections an agent sees.

Everything here produces `DataSection`s, never raw strings spliced into a prompt. That is
what keeps the untrusted-data guarantee in one place: an agent module cannot accidentally
interpolate a cell, because it never holds one as text destined for a prompt.

Blocks are rendered as a table with real row numbers down the side. The row numbers are
the whole point -- a Writer's patch has to name the spreadsheet row, and a model given
1-based positions within the block will confidently name the wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.context import DataSection
from ..models import (
    ColumnKey,
    Issue,
    ProblemBlock,
    ValidationFinding,
    WorkbookConventions,
)
from .isolation import assert_no_private_fields

#: The columns worth showing an agent. Metadata is included because misplaced metadata is
#: a real defect and the signature a column shift leaves behind.
DISPLAY_COLUMNS = (
    ColumnKey.PROBLEM_NAME,
    ColumnKey.ROW_TYPE,
    ColumnKey.TITLE,
    ColumnKey.BODY_TEXT,
    ColumnKey.ANSWER,
    ColumnKey.ANSWER_TYPE,
    ColumnKey.HINT_ID,
    ColumnKey.DEPENDENCY,
    ColumnKey.MC_CHOICES,
    ColumnKey.OER_SRC,
    ColumnKey.KC,
    ColumnKey.LICENSE,
)


def render_block(block: ProblemBlock) -> str:
    lines = ["row | " + " | ".join(key.value for key in DISPLAY_COLUMNS)]
    for row in block.rows:
        cells = [row.get(key).replace("\n", "\\n") for key in DISPLAY_COLUMNS]
        lines.append(f"{row.row} | " + " | ".join(cells))
    return "\n".join(lines)


def render_conventions(conventions: WorkbookConventions) -> str:
    """What this workbook consistently does, so an agent does not treat it as a defect.

    Six of eleven real workbooks use the `h` scaffold namespace where the written rules
    say `s`. An agent not told that will report every scaffold in the file.
    """
    return "\n".join(
        [
            f"notation: {conventions.notation.value}",
            f"dependency numbering: {conventions.dependency_convention.value}",
            f"scaffold id namespace: {conventions.dominant_scaffold_namespace or 'none'}"
            f" (consistent: {conventions.scaffold_namespace_is_consistent})",
            f"problem name stems: {', '.join(conventions.naming_stems) or 'none'}",
        ]
    )


def render_findings(findings: Sequence[ValidationFinding]) -> str:
    if not findings:
        return "none"
    return "\n".join(
        f"row {f.row or '-'}: [{f.severity.value}] {f.code} — {f.message}"
        for f in findings
    )


def render_issue(issue: Issue) -> str:
    """The issue as a reviewer and the Writer both see it.

    Note what is absent: no rationale, no confidence, nothing about previous attempts
    beyond the count. The claim and its location, and nothing that argues for it.
    """
    cells = ", ".join(f"row {row} column {column}" for row, column in issue.cells)
    return "\n".join(
        [
            f"severity: {issue.severity.value}",
            f"category: {issue.category.value}",
            f"structural: {issue.is_structural}",
            f"cells: {cells or 'not specified'}",
            f"claim: {issue.description}",
            f"expected: {issue.expected or 'not stated'}",
            f"rules cited: {', '.join(issue.rule_codes) or 'none'}",
        ]
    )


def render_block_diff(original: ProblemBlock, current: ProblemBlock) -> str:
    """The whole block, source to current -- not just this issue's edits.

    A reviewer judging one edit in isolation cannot see that a sibling edit broke it: a
    repaired answer that no longer matches its choice list looks perfect on its own line.
    Showing the complete block is what makes that visible.
    """
    before = {row.row: row for row in original.rows}
    after = {row.row: row for row in current.rows}

    lines: list[str] = []
    for row_number in sorted(set(before) | set(after)):
        old, new = before.get(row_number), after.get(row_number)
        if old is None:
            lines.append(f"row {row_number}: ADDED")
            continue
        if new is None:
            lines.append(f"row {row_number}: REMOVED")
            continue
        for key in DISPLAY_COLUMNS:
            if old.get(key) != new.get(key):
                lines.append(
                    f"row {row_number} {key.value}: {old.get(key)!r} -> {new.get(key)!r}"
                )
    return "\n".join(lines) if lines else "no changes"


# --------------------------------------------------------------------------------------
# Reviewer context
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerContext:
    """Everything a reviewer is given, and nothing else.

    Checked at import time by `assert_no_private_fields`: if a field is ever added whose
    type closure can reach `PrivateText` or a `PrivateModel`, this module fails to import
    rather than shipping a reviewer that can read the Writer's argument.
    """

    issue_summary: str
    original_block: str
    current_block: str
    block_diff: str
    conventions: str
    deterministic_findings: str
    rules_reminder: str = ""

    def sections(self) -> tuple[DataSection, ...]:
        return (
            DataSection("The issue under review", self.issue_summary),
            DataSection("The block as originally submitted", self.original_block),
            DataSection("The block as it stands now", self.current_block),
            DataSection("What changed, across the whole block", self.block_diff),
            DataSection("Conventions this workbook follows", self.conventions),
            DataSection("Deterministic findings still open", self.deterministic_findings),
        )


assert_no_private_fields(ReviewerContext)
