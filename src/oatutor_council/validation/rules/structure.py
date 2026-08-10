"""Block shape and metadata placement."""

from __future__ import annotations

from typing import Iterable

from ...models import (
    FIXED_COLUMNS,
    ColumnKey,
    FindingScope,
    IssueCategory,
    RowType,
    Severity,
    ValidationFinding,
)
from .registry import RuleContext, finding, rule

#: Metadata belongs on the problem row, which is where every real workbook puts it.
METADATA_COLUMNS = (
    ColumnKey.OER_SRC,
    ColumnKey.OPENSTAX_KC,
    ColumnKey.KC,
    ColumnKey.TAXONOMY,
    ColumnKey.LICENSE,
)


@rule(
    "DUPLICATE_PROBLEM_NAME",
    severity=Severity.ERROR,
    category=IssueCategory.STRUCTURE,
    scope=FindingScope.BLOCK,
    per_block=False,
    description="Two problem blocks share a Problem Name.",
)
def duplicate_problem_name(context: RuleContext) -> Iterable[ValidationFinding]:
    """Names identify problems downstream, so two blocks cannot share one.

    Reported per duplicate block rather than once for the workbook: each block needs its
    own repair, and a single workbook-scoped finding would give the Writer nowhere to
    apply one.
    """
    seen: dict[str, int] = {}
    for block in context.parsed.blocks:
        if not block.problem_name:
            continue
        first = seen.get(block.problem_name)
        if first is None:
            seen[block.problem_name] = block.start_row
            continue
        yield ValidationFinding(
            code="DUPLICATE_PROBLEM_NAME",
            severity=Severity.ERROR,
            scope=FindingScope.BLOCK,
            block_id=block.block_id,
            problem_name=block.problem_name,
            message=(
                f"Problem Name {block.problem_name!r} is already used by the block "
                f"starting at row {first}"
            ),
            detail={"first_use_row": first, "duplicate_row": block.start_row},
        )


@rule(
    "BLOCK_HAS_NO_STEP",
    severity=Severity.ERROR,
    category=IssueCategory.STRUCTURE,
    scope=FindingScope.BLOCK,
    description="A problem block contains no step rows.",
)
def block_has_no_step(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block and not block.rows_of_type(RowType.STEP):
        yield finding(
            context,
            "BLOCK_HAS_NO_STEP",
            "problem block has no step rows, so it asks the student nothing",
            scope=FindingScope.BLOCK,
        )


@rule(
    "PROBLEM_ROW_MISSING_TITLE",
    severity=Severity.WARNING,
    category=IssueCategory.STRUCTURE,
    description="A problem row has no Title.",
)
def problem_row_missing_title(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block and not block.problem_row.get(ColumnKey.TITLE).strip():
        yield finding(
            context,
            "PROBLEM_ROW_MISSING_TITLE",
            "problem row has no Title",
            row=block.start_row,
            column=FIXED_COLUMNS[ColumnKey.TITLE],
            column_key=ColumnKey.TITLE,
        )


@rule(
    "PROBLEM_METADATA_MISSING",
    severity=Severity.WARNING,
    category=IssueCategory.METADATA,
    description="A problem row is missing OER source or licence metadata.",
)
def problem_metadata_missing(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for key in (ColumnKey.OER_SRC, ColumnKey.LICENSE):
        if not block.problem_row.get(key).strip():
            yield finding(
                context,
                "PROBLEM_METADATA_MISSING",
                f"problem row has no {key.value.replace('_', ' ')}",
                row=block.start_row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )


@rule(
    "METADATA_ON_NON_PROBLEM_ROW",
    severity=Severity.WARNING,
    category=IssueCategory.METADATA,
    description="Metadata appears on a row other than the problem row.",
)
def metadata_on_non_problem_row(context: RuleContext) -> Iterable[ValidationFinding]:
    """Metadata below the problem row is usually displaced content.

    It is also the signature a shift leaves behind, which is why this is reported rather
    than tidied away: the useful repair is often to the shift, not to the stray cell.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows[1:]:
        for key in METADATA_COLUMNS:
            if row.get(key).strip():
                yield finding(
                    context,
                    "METADATA_ON_NON_PROBLEM_ROW",
                    (
                        f"{key.value.replace('_', ' ')} is populated on a "
                        f"{row.get(ColumnKey.ROW_TYPE) or 'blank'} row"
                    ),
                    row=row.row,
                    column=FIXED_COLUMNS[key],
                    column_key=key,
                )
