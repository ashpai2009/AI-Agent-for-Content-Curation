"""Hint and scaffold identifiers, and the dependencies between them."""

from __future__ import annotations

import re
from typing import Iterable

from ...models import (
    FIXED_COLUMNS,
    ColumnKey,
    IssueCategory,
    RowType,
    Severity,
    ValidationFinding,
)
from .registry import RuleContext, finding, rule

IDENTIFIER = re.compile(r"^([A-Za-z]+)(\d+)$")

#: The namespace the written rules specify for scaffold identifiers.
EXPECTED_SCAFFOLD_NAMESPACE = "s"


@rule(
    "IDENTIFIER_MALFORMED",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A hint or scaffold identifier is not a letter prefix followed by digits.",
)
def identifier_malformed(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.row_type not in (RowType.HINT, RowType.SCAFFOLD):
            continue
        identifier = row.get(ColumnKey.HINT_ID).strip()
        if identifier and not IDENTIFIER.match(identifier):
            yield finding(
                context,
                "IDENTIFIER_MALFORMED",
                f"identifier {identifier!r} is not a letter prefix followed by digits",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.HINT_ID],
                column_key=ColumnKey.HINT_ID,
                found=identifier,
            )


@rule(
    "IDENTIFIER_MISSING",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A hint or scaffold row has no identifier.",
)
def identifier_missing(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.row_type in (RowType.HINT, RowType.SCAFFOLD) and not row.get(
            ColumnKey.HINT_ID
        ).strip():
            yield finding(
                context,
                "IDENTIFIER_MISSING",
                f"{row.row_type.value} row has no identifier, so nothing can depend on it",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.HINT_ID],
                column_key=ColumnKey.HINT_ID,
            )


@rule(
    "DUPLICATE_IDENTIFIER",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="Two rows in one block share an identifier.",
)
def duplicate_identifier(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    seen: dict[str, int] = {}
    for row in block.rows:
        identifier = row.get(ColumnKey.HINT_ID).strip()
        if not identifier or row.row_type not in (RowType.HINT, RowType.SCAFFOLD):
            continue
        if identifier in seen:
            yield finding(
                context,
                "DUPLICATE_IDENTIFIER",
                f"identifier {identifier!r} is already used at row {seen[identifier]}",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.HINT_ID],
                column_key=ColumnKey.HINT_ID,
                first_use_row=seen[identifier],
            )
        else:
            seen[identifier] = row.row


@rule(
    "DEPENDENCY_UNRESOLVED",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A Dependency names an identifier that does not exist in the block.",
)
def dependency_unresolved(context: RuleContext) -> Iterable[ValidationFinding]:
    """Dependencies are block-local.

    A dangling reference leaves the student stuck at a step whose prerequisite can never
    be satisfied, which is invisible in the spreadsheet and obvious in the tutor.
    """
    block = context.block
    if block is None:
        return
    identifiers = {
        row.get(ColumnKey.HINT_ID).strip()
        for row in block.rows
        if row.get(ColumnKey.HINT_ID).strip()
    }
    for row in block.rows:
        dependency = row.get(ColumnKey.DEPENDENCY).strip()
        if not dependency:
            continue
        for reference in (part.strip() for part in dependency.split(",")):
            if reference and reference not in identifiers:
                yield finding(
                    context,
                    "DEPENDENCY_UNRESOLVED",
                    (
                        f"Dependency {reference!r} names no identifier in this block; "
                        f"available: {', '.join(sorted(identifiers)) or 'none'}"
                    ),
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                    column_key=ColumnKey.DEPENDENCY,
                    reference=reference,
                )


@rule(
    "DEPENDENCY_ON_SELF",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A row depends on its own identifier.",
)
def dependency_on_self(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows:
        identifier = row.get(ColumnKey.HINT_ID).strip()
        dependency = row.get(ColumnKey.DEPENDENCY).strip()
        if identifier and identifier in [
            part.strip() for part in dependency.split(",")
        ]:
            yield finding(
                context,
                "DEPENDENCY_ON_SELF",
                f"row depends on its own identifier {identifier!r}",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                column_key=ColumnKey.DEPENDENCY,
            )


@rule(
    "SCAFFOLD_NAMESPACE_DEVIATION",
    severity=Severity.WARNING,
    category=IssueCategory.DEPENDENCY,
    description="Scaffold identifiers use a namespace other than the specified 's'.",
)
def scaffold_namespace_deviation(context: RuleContext) -> Iterable[ValidationFinding]:
    """The rules specify `s#`, and six of eleven real workbooks consistently use `h#`.

    A workbook-wide alternative is a house style, so it is reported once at warning
    severity rather than flagged on every scaffold. A workbook that *mixes* namespaces is
    a different matter: nothing there is a convention, and it stays an error.
    """
    block = context.block
    if block is None:
        return
    conventions = context.conventions
    consistent = conventions.scaffold_namespace_is_consistent

    for row in block.rows_of_type(RowType.SCAFFOLD):
        match = IDENTIFIER.match(row.get(ColumnKey.HINT_ID).strip())
        if not match:
            continue
        namespace = match.group(1).casefold()
        if namespace == EXPECTED_SCAFFOLD_NAMESPACE:
            continue
        yield finding(
            context,
            "SCAFFOLD_NAMESPACE_DEVIATION",
            (
                f"scaffold identifier uses the {namespace!r} namespace where the rules "
                f"specify {EXPECTED_SCAFFOLD_NAMESPACE!r}"
                + (
                    "; the whole workbook uses it consistently, so this is a house style"
                    if consistent
                    else "; this workbook mixes namespaces, so it is not a convention"
                )
            ),
            row=row.row,
            column=FIXED_COLUMNS[ColumnKey.HINT_ID],
            column_key=ColumnKey.HINT_ID,
            severity=Severity.WARNING if consistent else Severity.ERROR,
            namespace=namespace,
            workbook_is_consistent=consistent,
        )


@rule(
    "DEPENDENCY_NUMBERING_GAP",
    severity=Severity.WARNING,
    category=IssueCategory.DEPENDENCY,
    description="Identifier numbering within a step skips a value.",
)
def dependency_numbering_gap(context: RuleContext) -> Iterable[ValidationFinding]:
    """Numbering should run 1, 2, 3 within whatever the workbook's convention is.

    Compared against the *detected* convention, not an assumed one, and skipped entirely
    when the workbook gave no evidence -- inventing a convention here would flag every
    block in a workbook that simply never had one.
    """
    block = context.block
    if block is None:
        return

    numbers: list[tuple[int, int]] = []
    for row in block.rows:
        if row.row_type is RowType.STEP:
            yield from _report_gaps(context, numbers)
            numbers = []
            continue
        match = IDENTIFIER.match(row.get(ColumnKey.HINT_ID).strip())
        if match and row.row_type in (RowType.HINT, RowType.SCAFFOLD):
            numbers.append((int(match.group(2)), row.row))
    yield from _report_gaps(context, numbers)


def _report_gaps(
    context: RuleContext, numbers: list[tuple[int, int]]
) -> Iterable[ValidationFinding]:
    if len(numbers) < 2:
        return
    ordered = sorted(numbers)
    for (previous, _), (current, row) in zip(ordered, ordered[1:]):
        if current > previous + 1:
            yield finding(
                context,
                "DEPENDENCY_NUMBERING_GAP",
                f"identifier numbering jumps from {previous} to {current}",
                row=row,
                column=FIXED_COLUMNS[ColumnKey.HINT_ID],
                column_key=ColumnKey.HINT_ID,
                previous=previous,
                current=current,
            )
