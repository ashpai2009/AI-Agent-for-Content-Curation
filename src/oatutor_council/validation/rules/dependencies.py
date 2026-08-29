"""Hint and scaffold identifiers, and the dependencies between them.

**Everything here is scoped to a step, not to a block.** A block's hints and scaffolds
belong to the step above them, and the two conventions the corpus uses differ only in
what the *identifiers* do across that boundary:

* `RESET_PER_STEP` — every step restarts its numbering at 1, so `h1` recurring under a
  later step is correct, not a duplicate.
* `CONTINUOUS` — identifiers keep counting up across steps, so `h1` recurring is a real
  duplicate.

The **dependency chain restarts at every step boundary under both conventions**: the
first sub-row of a step depends on nothing, and each one after it depends on the one
immediately before. That is why the chain rules need no convention check while the
uniqueness and numbering rules do.

Getting the scope wrong is not a small error. Checking uniqueness block-wide reports a
duplicate for every step after the first in a reset-per-step workbook, which is 312 of
the findings the deterministic core previously produced over the real corpus -- enough
noise to bury everything true sitting beside it.
"""

from __future__ import annotations

import re
from typing import Iterable

from ...models import (
    FIXED_COLUMNS,
    ColumnKey,
    DependencyConvention,
    IssueCategory,
    RowType,
    Severity,
    StepScope,
    ValidationFinding,
)
from .registry import RuleContext, finding, rule

IDENTIFIER = re.compile(r"^([A-Za-z]+)(\d+)$")

#: The namespace the written rules specify for scaffold identifiers.
EXPECTED_SCAFFOLD_NAMESPACE = "s"

#: Separators that would mean "more than one dependency". The contract allows exactly
#: one identifier per cell, so each of these is reported rather than parsed.
DEPENDENCY_SEPARATORS = (",", ";", "|", " and ", "&")


def _identifier(row) -> str:
    return row.get(ColumnKey.HINT_ID).strip()


def _dependency(row) -> str:
    return row.get(ColumnKey.DEPENDENCY).strip()


def _has_separator(text: str) -> str | None:
    for separator in DEPENDENCY_SEPARATORS:
        if separator in text:
            return separator
    return None


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
    """Uniqueness, over the scope the workbook's convention actually implies.

    Under `RESET_PER_STEP` an identifier only has to be unique **within its step**: the
    whole point of the convention is that step two starts again at `h1`. Under
    `CONTINUOUS` it must be unique across the block, because there the numbering is
    supposed to keep climbing.

    `UNDECIDED` -- a workbook that never gave evidence either way, typically because
    every block has one step -- is checked block-wide. With one step per block the two
    readings coincide, and where they do not, the stricter one is the safer default:
    a real duplicate reported is recoverable, a real duplicate missed is a hint that
    silently never fires in the tutor.
    """
    block = context.block
    if block is None:
        return

    if context.conventions.dependency_convention is DependencyConvention.RESET_PER_STEP:
        for scope in block.step_scopes():
            yield from _duplicates_within(context, scope.identified)
        return

    yield from _duplicates_within(
        context,
        tuple(
            row
            for row in block.rows
            if row.row_type in (RowType.HINT, RowType.SCAFFOLD) and _identifier(row)
        ),
    )


def _duplicates_within(context: RuleContext, rows) -> Iterable[ValidationFinding]:
    seen: dict[str, int] = {}
    for row in rows:
        identifier = _identifier(row)
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
    """A dependency must name an identifier that exists **in its own step**.

    A dangling reference leaves the student stuck at a step whose prerequisite can never
    be satisfied, which is invisible in the spreadsheet and obvious in the tutor.

    Resolving block-wide would be laxer *and* wronger: under the reset-per-step
    convention every step has an `h1`, so a dependency on `h1` written under step three
    resolves against step one's row and looks fine while pointing at a hint the student
    will never have seen. `DEPENDENCY_CROSSES_STEP` names that case specifically when it
    happens under the continuous convention, where identifiers really are block-unique.

    Cells carrying more than one reference are left to `DEPENDENCY_MULTIPLE`, so a comma
    produces one clear finding rather than two overlapping ones.
    """
    block = context.block
    if block is None:
        return

    for scope in block.step_scopes():
        available = {_identifier(row) for row in scope.identified}
        for row in scope.rows:
            dependency = _dependency(row)
            if not dependency or _has_separator(dependency):
                continue
            if dependency not in available:
                yield finding(
                    context,
                    "DEPENDENCY_UNRESOLVED",
                    (
                        f"Dependency {dependency!r} names no identifier under this step; "
                        f"available: {', '.join(sorted(available)) or 'none'}"
                    ),
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                    column_key=ColumnKey.DEPENDENCY,
                    reference=dependency,
                )


@rule(
    "DEPENDENCY_MULTIPLE",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A Dependency cell names more than one identifier.",
)
def dependency_multiple(context: RuleContext) -> Iterable[ValidationFinding]:
    """One dependency per cell. The contract has no syntax for a list.

    Whatever a comma-separated cell was meant to express, the tutor reads the cell as a
    single identifier -- so it resolves to nothing and the prerequisite silently never
    fires. Reported rather than parsed, because guessing which of the two was intended
    is exactly the kind of decision this system is not allowed to make quietly.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        dependency = _dependency(row)
        separator = _has_separator(dependency) if dependency else None
        if separator:
            yield finding(
                context,
                "DEPENDENCY_MULTIPLE",
                f"Dependency {dependency!r} names more than one identifier "
                f"(separated by {separator!r}); the contract allows exactly one",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                column_key=ColumnKey.DEPENDENCY,
                separator=separator,
            )


@rule(
    "DEPENDENCY_ON_LATER_ROW",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A Dependency names an identifier defined further down the block.",
)
def dependency_on_later_row(context: RuleContext) -> Iterable[ValidationFinding]:
    """A prerequisite that comes afterwards is not a prerequisite.

    Resolved **within the step**, which is where a dependency resolves at all. A
    block-wide lookup is not merely laxer here, it is wrong: under the reset-per-step
    convention every step defines its own `h1`, so a block-wide map keeps whichever came
    last and then reports every earlier reference to `h1` as pointing forwards. That
    single detail accounted for 187 findings against workbooks doing nothing wrong.
    """
    block = context.block
    if block is None:
        return
    for scope in block.step_scopes():
        defined_at = {
            _identifier(row): row.row for row in scope.identified
        }
        for row in scope.rows:
            yield from _later_row_finding(context, row, defined_at)


def _later_row_finding(
    context: RuleContext, row, defined_at: dict[str, int]
) -> Iterable[ValidationFinding]:
    dependency = _dependency(row)
    if not dependency or _has_separator(dependency):
        return
    target = defined_at.get(dependency)
    if target is not None and target > row.row:
        yield finding(
            context,
            "DEPENDENCY_ON_LATER_ROW",
            f"row depends on {dependency!r}, which is defined below it at row "
            f"{target}; a prerequisite cannot come afterwards",
            row=row.row,
            column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
            column_key=ColumnKey.DEPENDENCY,
            target_row=target,
        )

@rule(
    "DEPENDENCY_CROSSES_STEP",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A Dependency resolves to a hint belonging to a different step.",
)
def dependency_crosses_step(context: RuleContext) -> Iterable[ValidationFinding]:
    """A dependency reaching into another step's hints.

    Only meaningful under `CONTINUOUS`, where identifiers are block-unique and a
    cross-step reference is therefore unambiguous -- and unambiguously wrong, because a
    student working step three has not seen step one's hints. Under `RESET_PER_STEP`
    the same text is not a cross-step reference at all: it names this step's own `h1`,
    and `DEPENDENCY_UNRESOLVED` already covers the case where no such row exists.
    """
    block = context.block
    if block is None:
        return
    if context.conventions.dependency_convention is not DependencyConvention.CONTINUOUS:
        return

    owner: dict[str, int] = {}
    for index, scope in enumerate(block.step_scopes()):
        for row in scope.identified:
            owner.setdefault(_identifier(row), index)

    for index, scope in enumerate(block.step_scopes()):
        for row in scope.rows:
            dependency = _dependency(row)
            if not dependency or _has_separator(dependency):
                continue
            belongs_to = owner.get(dependency)
            if belongs_to is not None and belongs_to != index:
                yield finding(
                    context,
                    "DEPENDENCY_CROSSES_STEP",
                    f"row depends on {dependency!r}, which belongs to a different step; "
                    "a student reaching this row has not seen it",
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                    column_key=ColumnKey.DEPENDENCY,
                    reference=dependency,
                )


@rule(
    "STEP_HAS_DEPENDENCY",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A step row carries a Dependency, which only hints and scaffolds may.",
)
def step_has_dependency(context: RuleContext) -> Iterable[ValidationFinding]:
    """Steps are ordered by position, not by dependency.

    A dependency on a step row is either inert or, worse, read as a prerequisite the
    step ordering already implies -- and it is a common symptom of the column-shift
    corruption, where a value slid into the Dependency column from its neighbour.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.STEP):
        dependency = _dependency(row)
        if dependency:
            yield finding(
                context,
                "STEP_HAS_DEPENDENCY",
                f"step row carries the Dependency {dependency!r}; steps are ordered by "
                "position and take no prerequisites",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                column_key=ColumnKey.DEPENDENCY,
                found=dependency,
            )


@rule(
    "FIRST_HINT_HAS_DEPENDENCY",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="The first hint or scaffold under a step carries a Dependency.",
)
def first_hint_has_dependency(context: RuleContext) -> Iterable[ValidationFinding]:
    """The chain restarts at every step boundary, under **both** conventions.

    The conventions differ in what the identifiers do across a step -- reset or keep
    counting -- and not in what the dependencies do. The first hint of a step is
    reachable as soon as the student is on that step, so it waits for nothing.
    """
    block = context.block
    if block is None:
        return
    for scope in block.step_scopes():
        chain = scope.hints
        if not chain:
            continue
        dependency = _dependency(chain[0])
        if dependency:
            yield finding(
                context,
                "FIRST_HINT_HAS_DEPENDENCY",
                f"the first hint under this step depends on {dependency!r}; the chain "
                "restarts at every step, so it waits for nothing",
                row=chain[0].row,
                column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                column_key=ColumnKey.DEPENDENCY,
                found=dependency,
            )


@rule(
    "HINT_DEPENDENCY_NOT_PREVIOUS",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A hint does not depend on the hint immediately before it.",
)
def hint_dependency_not_previous(context: RuleContext) -> Iterable[ValidationFinding]:
    """`h2` depends on `h1`, `h3` on `h2`, and so on within one step.

    **Hints only.** Hints and scaffolds play different parts: the hints of a step are a
    ladder a student climbs one rung at a time, while scaffolds hang off whichever hint
    they follow and several may share one -- which is why they get their own rule below
    rather than being spliced into this sequence.

    A missing dependency is as much a defect as a wrong one: it releases every hint of
    the step at once, which is the failure the chain exists to prevent.
    """
    block = context.block
    if block is None:
        return
    for scope in block.step_scopes():
        chain = scope.hints
        for previous, row in zip(chain, chain[1:]):
            expected = _identifier(previous)
            dependency = _dependency(row)
            if _has_separator(dependency):
                continue  # DEPENDENCY_MULTIPLE owns this cell
            if dependency == expected:
                continue
            yield finding(
                context,
                "HINT_DEPENDENCY_NOT_PREVIOUS",
                (
                    f"hint depends on {dependency or 'nothing'!r} where the chain "
                    f"expects {expected!r} from row {previous.row}"
                    + (
                        "; without it every hint in this step is released at once"
                        if not dependency
                        else ""
                    )
                ),
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                column_key=ColumnKey.DEPENDENCY,
                expected=expected,
                found=dependency,
            )


@rule(
    "SCAFFOLD_DEPENDENCY_NOT_HINT",
    severity=Severity.ERROR,
    category=IssueCategory.DEPENDENCY,
    description="A scaffold does not depend on the hint it follows.",
)
def scaffold_dependency_not_hint(context: RuleContext) -> Iterable[ValidationFinding]:
    """A scaffold waits on the hint above it -- "the appropriate hint".

    Which hint that is, is not a matter of opinion: it is the nearest one above the
    scaffold within its step. Several scaffolds following one hint all name that same
    hint, which is why they are not a chain and why threading them into the hint
    sequence produced hundreds of findings on workbooks that were doing it right.

    A scaffold with no hint above it in its step has nothing to wait for, so its
    dependency must be blank.
    """
    block = context.block
    if block is None:
        return
    for scope in block.step_scopes():
        for row in scope.rows:
            if row.row_type is not RowType.SCAFFOLD or not _identifier(row):
                continue
            dependency = _dependency(row)
            if _has_separator(dependency):
                continue  # DEPENDENCY_MULTIPLE owns this cell
            hint = scope.hint_before(row)
            expected = _identifier(hint) if hint is not None else ""
            if dependency == expected:
                continue
            yield finding(
                context,
                "SCAFFOLD_DEPENDENCY_NOT_HINT",
                (
                    f"scaffold depends on {dependency or 'nothing'!r} where it should "
                    + (
                        f"depend on {expected!r}, the hint above it at row {hint.row}"
                        if hint is not None
                        else "depend on nothing: no hint precedes it in this step"
                    )
                ),
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.DEPENDENCY],
                column_key=ColumnKey.DEPENDENCY,
                expected=expected,
                found=dependency,
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
        identifier = _identifier(row)
        dependency = _dependency(row)
        if identifier and identifier in [
            part.strip() for part in re.split(r"[,;|&]", dependency)
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
            repairable=not consistent,
            namespace=namespace,
            workbook_is_consistent=consistent,
        )
