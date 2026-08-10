"""The deterministic gate every patch passes before a byte is written.

Nothing here asks a model anything. The Writer decided *what* to change; this decides
whether that change is permitted, and it does so with checks that are exact, cheap, and
reproducible.

The structural rules deserve their own explanation. `Problem Name`, `Row Type`,
`answerType`, `HintID` and `Dependency` define what a block *is*, and a blanket refusal to
touch them would be the safe-looking choice. It is also the wrong one: the column-shift
corruption in the real corpus lives entirely in those columns, and refusing them outright
would make it permanently unrepairable while still routing it to the council for a repair
that could never be applied. So they pass a stricter gate instead, and every condition has
to hold.

The gate simulates the whole resulting block and re-runs the rules over it. A patch that
fixes one thing and breaks another is rejected before it lands, which is cheaper for
everyone than discovering it in review.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from ..models import (
    MC_CHOICE_DELIMITER,
    STRUCTURAL_COLUMNS,
    CellEdit,
    ColumnKey,
    Issue,
    IssueCategory,
    ParsedWorkbook,
    Patch,
    PatchRejection,
    ProblemBlock,
    RejectionCode,
    Severity,
    ValidationFinding,
    WorkbookRow,
)
from .mathematics import MathVerdict, equations_equivalent
from .rules import REGISTRY, run_rules

#: Columns describing provenance rather than content. An edit here under a mathematics
#: issue is out of scope, however tempting.
METADATA_COLUMNS = frozenset(
    {
        ColumnKey.PARENT,
        ColumnKey.OER_SRC,
        ColumnKey.OPENSTAX_KC,
        ColumnKey.KC,
        ColumnKey.TAXONOMY,
        ColumnKey.LICENSE,
    }
)

#: Categories under which a metadata edit is the point rather than a digression.
METADATA_CATEGORIES = frozenset({IssueCategory.METADATA, IssueCategory.STRUCTURE})

#: Cells whose contents are mathematics, and which therefore need a stated derivation.
MATHEMATICAL_COLUMNS = frozenset({ColumnKey.ANSWER, ColumnKey.MC_CHOICES})

#: Findings this severe must not be *introduced* by a repair.
REGRESSION_SEVERITIES = frozenset({Severity.BLOCKING, Severity.ERROR})

#: Issue categories about how a value is *written* rather than what it is. A repair under
#: one of these must leave the mathematics alone: rewriting `sqrt(2)/2` as `0.7` is a
#: notation fix that quietly changed the answer, and nothing downstream would notice.
VALUE_PRESERVING_CATEGORIES = frozenset(
    {
        IssueCategory.NOTATION,
        IssueCategory.LATEX,
        IssueCategory.FORMATTING,
        IssueCategory.APPEARANCE,
    }
)


@dataclass(frozen=True)
class GateResult:
    rejection: PatchRejection | None = None
    #: Findings the patch would introduce. Empty on acceptance.
    regressions: tuple[ValidationFinding, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.rejection is None


def _reject(code: RejectionCode, message: str, **kwargs) -> GateResult:
    return GateResult(rejection=PatchRejection(code=code, message=message, **kwargs))


# --------------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------------


def simulate_block(block: ProblemBlock, edits: Sequence[CellEdit]) -> ProblemBlock:
    """The block as it would be after the patch, without writing anything.

    Simulation is what makes condition 5 of the structural gate possible: the complete
    resulting block is checked against every invariant *before* a byte is written, rather
    than the workbook being patched and inspected afterwards.
    """
    by_row: dict[int, dict[ColumnKey, str]] = {}
    for edit in edits:
        if edit.column_key is not None:
            by_row.setdefault(edit.row, {})[edit.column_key] = edit.after

    rows = []
    for row in block.rows:
        changes = by_row.get(row.row)
        if not changes:
            rows.append(row)
            continue
        values = dict(row.values)
        values.update(changes)
        raw = dict(row.raw)
        raw.update(changes)
        rows.append(
            WorkbookRow(
                row=row.row,
                values=values,
                raw=raw,
                is_blank=not any(v.strip() for v in values.values()),
                wrap_text_columns=row.wrap_text_columns,
            )
        )

    return block.model_copy(update={"rows": tuple(rows)})


def _findings_for(parsed: ParsedWorkbook, block: ProblemBlock) -> tuple[ValidationFinding, ...]:
    single = parsed.model_copy(update={"blocks": (block,)})
    return tuple(run_rules(single)) + block.findings


def regressions_introduced(
    parsed: ParsedWorkbook, block: ProblemBlock, patched: ProblemBlock
) -> tuple[ValidationFinding, ...]:
    """Findings present after the patch that were not present before.

    Compared by `(code, row, column)` rather than by message, so a rule whose wording
    changed does not read as a regression. Pre-existing findings are not the patch's
    fault and must not block it -- a block usually has several, and only one is being
    repaired.
    """
    before = {(f.code, f.row, f.column) for f in _findings_for(parsed, block)}
    return tuple(
        f
        for f in _findings_for(parsed, patched)
        if f.severity in REGRESSION_SEVERITIES
        and (f.code, f.row, f.column) not in before
    )


# --------------------------------------------------------------------------------------
# Does the patch fix what it was asked to fix?
# --------------------------------------------------------------------------------------


def target_findings(
    issue: Issue, parsed: ParsedWorkbook, block: ProblemBlock
) -> tuple[ValidationFinding, ...] | None:
    """The findings this issue was opened for, as they stand in `block`.

    `None` -- distinct from an empty tuple -- means the claim is **not mechanically
    checkable**. Two cases produce it, and conflating either with "resolved" would be a
    serious mistake:

    * an agent-discovered issue carries no registered rule code, because the defect is a
      semantic one only a reviewer can judge;
    * a structural finding the *reader* emitted while parsing is not re-derivable from a
      simulated block, since simulation edits cell values and does not re-parse the sheet.

    In both cases the gate falls back on the checks it can make exactly -- invariants,
    conservation, regressions -- and leaves sufficiency to the assigned reviewer.
    """
    codes = tuple(code for code in issue.rule_codes if code in REGISTRY)
    if not codes:
        return None

    single = parsed.model_copy(update={"blocks": (block,)})
    found = run_rules(single, only=codes)
    cells = set(issue.cells)
    if not cells:
        return found
    # An issue that named its cells is only claiming a defect *there*. The same rule
    # firing elsewhere in the block is somebody else's issue, and treating it as this
    # one's would make a correct repair look like a failed one.
    return tuple(f for f in found if (f.row, f.column) in cells)


def _still_present(
    issue: Issue, parsed: ParsedWorkbook, patched: ProblemBlock
) -> tuple[ValidationFinding, ...]:
    remaining = target_findings(issue, parsed, patched)
    return remaining or ()


def _unnecessary_edit(
    issue: Issue,
    parsed: ParsedWorkbook,
    block: ProblemBlock,
    edits: Sequence[CellEdit],
    extra: Sequence[CellEdit],
) -> CellEdit | None:
    """Find an edit outside the issue's cells that the repair did not actually need.

    "Necessary" sounds like a judgment call, and stated as a question about intent it is
    one. Stated as a question about consequences it is exactly decidable: drop the edit,
    simulate what is left, and ask whether the issue is still resolved and nothing new is
    broken. If both hold, the repair worked without that edit, so it was an unrelated
    improvement travelling under the issue's authority.

    Only run when the issue is mechanically checkable -- otherwise "still resolved" is
    vacuously true and every extra edit would look unnecessary.
    """
    for candidate in extra:
        reduced = [edit for edit in edits if edit is not candidate]
        if not reduced:
            # Dropping it leaves nothing, so it carried the whole repair.
            continue
        without = simulate_block(block, reduced)
        if _still_present(issue, parsed, without):
            continue
        if _block_invariants_broken(without) or regressions_introduced(
            parsed, block, without
        ):
            continue
        return candidate
    return None


def _value_changed(issue: Issue, edits: Sequence[CellEdit]) -> tuple[CellEdit, str] | None:
    """Catch a presentation repair that changed the mathematics.

    SymPy is a gate here, never a proof. `EQUIVALENT` passes and `DIFFERENT` is refused,
    but anything it cannot decide yields `UNKNOWN` and is **allowed through to the
    reviewer** -- that is the whole point of having a reviewer, and refusing every
    expression the parser does not understand would reject most correct LaTeX repairs.
    """
    if issue.category not in VALUE_PRESERVING_CATEGORIES:
        return None

    for edit in edits:
        if edit.column_key not in MATHEMATICAL_COLUMNS:
            continue
        if edit.column_key is ColumnKey.MC_CHOICES:
            failure = _choices_changed(edit)
            if failure:
                return edit, failure
            continue
        if not edit.before.strip() or not edit.after.strip():
            # Adding or clearing a value is not a rewriting of one, so there is nothing
            # to compare and no claim to check.
            continue
        if equations_equivalent(edit.before, edit.after) is MathVerdict.DIFFERENT:
            return edit, (
                f"{edit.before!r} and {edit.after!r} are not the same value"
            )
    return None


def _choices_changed(edit: CellEdit) -> str | None:
    before = [part.strip() for part in edit.before.split(MC_CHOICE_DELIMITER)]
    after = [part.strip() for part in edit.after.split(MC_CHOICE_DELIMITER)]
    if len(before) != len(after):
        return (
            f"the choice list goes from {len(before)} to {len(after)} choices, which is "
            "a change of content rather than of notation"
        )
    for old, new in zip(before, after):
        if old == new or not old or not new:
            continue
        if equations_equivalent(old, new) is MathVerdict.DIFFERENT:
            return f"choice {old!r} would become {new!r}, which is a different value"
    return None


# --------------------------------------------------------------------------------------
# Structural conservation
# --------------------------------------------------------------------------------------


def _conservation_failure(edits: Sequence[CellEdit]) -> str | None:
    """Check that a shift repair moves content rather than deleting it.

    Condition 6 of the structural gate. A patch that empties the source without filling
    any destination silently deletes a curator's work, and it looks perfectly reasonable
    cell by cell -- nothing else in the system notices a value that simply stopped
    existing.

    The mirror-image concern, a patch that *duplicates* content by filling the
    destination without emptying the source, is deliberately **not** checked here. It is
    covered exactly by the uniqueness invariants on the simulated block -- duplicate
    identifiers, unresolvable dependencies, disagreeing problem names -- and those are
    precise where a value-counting check is not. Repeated values are ordinary: writing
    `numeric` into an `answerType` cell is not duplication merely because another row is
    also `numeric`, and a check that said so would reject almost every real repair.
    """
    vacated = Counter(
        edit.before.strip()
        for edit in edits
        if not edit.after.strip() and edit.before.strip()
    )
    written = Counter(edit.after.strip() for edit in edits if edit.after.strip())

    # A patch that only clears cells is a *deletion*, not a move, and deletion is
    # sometimes the correct repair -- removing a dependency that references an identifier
    # the block does not contain is the obvious example. Requiring a destination there
    # would make a dangling reference permanently unfixable. Whether the deletion leaves
    # the block valid is decided by the invariant and regression checks, which answer
    # that question properly.
    if not written:
        return None

    for value, count in vacated.items():
        if written.get(value, 0) < count:
            return (
                f"the value {value!r} is removed from {count} cell(s) and written to "
                f"{written.get(value, 0)}; a shift repair must move content, not drop it"
            )
    return None


def _block_invariants_broken(patched: ProblemBlock) -> str | None:
    """Condition 5: the resulting block must still be a block.

    These are the invariants a shift repair is most likely to get subtly wrong, and each
    is cheap to state exactly -- which is the whole reason they are checked here rather
    than left to a reviewer's judgment.
    """
    from ..models import RowType

    if patched.problem_row.row_type is not RowType.PROBLEM:
        return "the first row of the block is no longer a problem row"

    name = patched.problem_row.get(ColumnKey.PROBLEM_NAME).strip()
    if not name:
        return "the problem row would be left without a Problem Name"

    for row in patched.rows[1:]:
        if row.is_blank:
            continue
        if row.row_type is RowType.PROBLEM:
            return f"row {row.row} would become a second problem row, splitting the block"
        row_name = row.get(ColumnKey.PROBLEM_NAME).strip()
        if row_name and row_name != name:
            return (
                f"row {row.row} would be named {row_name!r} inside the block declared "
                f"{name!r}"
            )

    identifiers = [
        row.get(ColumnKey.HINT_ID).strip()
        for row in patched.rows
        if row.get(ColumnKey.HINT_ID).strip()
    ]
    duplicates = [value for value, count in Counter(identifiers).items() if count > 1]
    if duplicates:
        return f"the patch would leave duplicate identifiers: {', '.join(duplicates)}"

    available = set(identifiers)
    for row in patched.rows:
        dependency = row.get(ColumnKey.DEPENDENCY).strip()
        for reference in (part.strip() for part in dependency.split(",")):
            if reference and reference not in available:
                return (
                    f"row {row.row} would depend on {reference!r}, which does not exist "
                    "in the resulting block"
                )
    return None


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def validate_patch(
    patch: Patch,
    *,
    issue: Issue,
    block: ProblemBlock,
    parsed: ParsedWorkbook,
) -> GateResult:
    """Decide whether this patch may be applied.

    `before` values are **not** verified here -- that happens against the live file in
    `workbook.writer`, which is the only place that can know what a cell currently holds.
    Checking a stale copy here would give a false pass.
    """
    if patch.needs_human_review:
        # Not a rejection: an escalation is a valid outcome, and it carries no edits.
        return GateResult()

    if not patch.edits:
        return _reject(RejectionCode.NO_OP, "the patch contains no edits")

    seen: set[tuple[int, int]] = set()
    for edit in patch.edits:
        if (edit.row, edit.column) in seen:
            return _reject(
                RejectionCode.DUPLICATE_CELL_EDIT,
                "two edits target the same cell",
                row=edit.row,
                column=edit.column,
            )
        seen.add((edit.row, edit.column))

        if not block.contains_row(edit.row):
            return _reject(
                RejectionCode.OUT_OF_BLOCK_SCOPE,
                f"row {edit.row} is outside the block being repaired "
                f"(rows {block.start_row}-{block.end_row})",
                row=edit.row,
                column=edit.column,
            )

        actual_key = parsed.column_map.key_at(edit.column)
        if actual_key is None:
            return _reject(
                RejectionCode.CELL_NOT_FOUND,
                f"column {edit.column} is not part of the workbook contract",
                row=edit.row,
                column=edit.column,
            )

        # The patch says both "column 5" and "answer", and everything downstream trusts
        # one or the other: the file writer addresses the cell by index, while the scope
        # and structural checks read the key. If they disagree the patch is checked
        # against one cell and written to a different one.
        if edit.column_key is not None and edit.column_key is not actual_key:
            return _reject(
                RejectionCode.COLUMN_KEY_MISMATCH,
                f"the edit names column {edit.column_key.value} but column "
                f"{edit.column} is {actual_key.value} in this workbook",
                row=edit.row,
                column=edit.column,
            )

        if (
            edit.column_key in METADATA_COLUMNS
            and issue.category not in METADATA_CATEGORIES
        ):
            return _reject(
                RejectionCode.UNRELATED_CELL,
                f"the issue is a {issue.category.value} issue, so it does not authorise "
                f"editing the {edit.column_key.value} metadata column",
                row=edit.row,
                column=edit.column,
            )

    # A mathematical correction with no stated derivation cannot be checked by anyone.
    # The derivation is visible to this gate and deliberately not to reviewers.
    if any(edit.column_key in MATHEMATICAL_COLUMNS for edit in patch.edits):
        if not patch.derivation.strip():
            return _reject(
                RejectionCode.MISSING_MATH_VERIFICATION,
                "the patch changes a mathematical cell without stating how the new "
                "value was derived",
            )

    # Scope. The issue names the cells it is about; anything else has to be justified,
    # and the justification is checked for substance further down by dropping the edit
    # and seeing whether the repair still works.
    #
    # An issue that names *no* cells -- a block-scoped finding, a defect the auditor
    # described without pinning to one cell -- is a different situation, not a stricter
    # one. There is nothing to have deviated from, so demanding an explanation for the
    # deviation asks the Writer to justify a patch against an empty baseline. Those edits
    # still face the necessity test below; they are just not required to argue first.
    named = set(issue.cells)
    extra = [edit for edit in patch.edits if (edit.row, edit.column) not in named]
    if named and extra and not patch.related_edits_reason.strip():
        first = extra[0]
        named = (
            ", ".join(f"row {row} column {column}" for row, column in issue.cells)
            or "no cell in particular"
        )
        return _reject(
            RejectionCode.UNRELATED_CELL,
            f"the patch edits row {first.row} column {first.column}, which the issue "
            f"does not name (it concerns {named}), without saying why that cell is part "
            "of the same repair",
            row=first.row,
            column=first.column,
        )

    structural = [edit for edit in patch.edits if edit.is_structural]
    if structural and not issue.is_structural:
        # Condition 1, checked before anything expensive: an unauthorised structural edit
        # is refused regardless of whether it would have been valid.
        columns = ", ".join(
            sorted({e.column_key.value for e in structural if e.column_key})
        )
        return _reject(
            RejectionCode.STRUCTURAL_COLUMN_UNAUTHORIZED,
            f"the patch edits the structural column(s) {columns}, but the open issue "
            "does not identify a structural defect",
            row=structural[0].row,
            column=structural[0].column,
        )

    patched = simulate_block(block, patch.edits)

    # Invariants first: they describe precisely what the resulting block would get wrong,
    # where a content count can only say that something moved. When both would fire, the
    # more specific message is the one worth giving the Writer.
    broken = _block_invariants_broken(patched)
    if broken:
        return _reject(
            RejectionCode.STRUCTURAL_BLOCK_INVARIANT_BROKEN,
            f"the resulting block would be invalid: {broken}",
        )

    if structural:
        failure = _conservation_failure(patch.edits)
        if failure:
            return _reject(
                RejectionCode.STRUCTURAL_CONTENT_NOT_CONSERVED,
                f"the structural patch does not conserve content: {failure}",
            )

    changed = _value_changed(issue, patch.edits)
    if changed:
        edit, why = changed
        return _reject(
            RejectionCode.MATH_NOT_EQUIVALENT,
            f"the issue is a {issue.category.value} issue -- about how the value is "
            f"written, not what it is -- but {why}",
            row=edit.row,
            column=edit.column,
        )

    regressions = regressions_introduced(parsed, block, patched)
    if regressions:
        worst = regressions[0]
        return GateResult(
            rejection=PatchRejection(
                code=RejectionCode.RULE_VIOLATION,
                message=(
                    f"the patch would introduce {len(regressions)} new finding(s), "
                    f"starting with {worst.code} at row {worst.row}: {worst.message}"
                ),
                row=worst.row,
                column=worst.column,
                detail={"codes": [f.code for f in regressions]},
            ),
            regressions=regressions,
        )

    # Sufficiency, last, because every check above describes damage and this one only
    # describes absence. A patch that breaks the block deserves to hear about that first.
    before = target_findings(issue, parsed, block)
    if before:
        remaining = _still_present(issue, parsed, patched)
        if remaining:
            worst = remaining[0]
            return _reject(
                RejectionCode.ISSUE_NOT_RESOLVED,
                f"the patch does not resolve the issue: {worst.code} is still raised at "
                f"row {worst.row} after the edits are applied",
                row=worst.row,
                column=worst.column,
                detail={"codes": [f.code for f in remaining]},
            )

        unnecessary = _unnecessary_edit(issue, parsed, block, patch.edits, extra)
        if unnecessary is not None:
            return _reject(
                RejectionCode.UNRELATED_CELL,
                f"the edit at row {unnecessary.row} column {unnecessary.column} was not "
                "needed: the issue is resolved without it, so it is an unrelated change "
                "travelling under this issue's authority",
                row=unnecessary.row,
                column=unnecessary.column,
            )

    return GateResult()


def rejection_consumes_attempt(rejection: PatchRejection) -> bool:
    """Whether a rejected patch spends one of the three attempts.

    It does, with one exception. The Writer call was made and the loop-forming resource
    was consumed, so a patch rejected for being wrong costs an attempt exactly as a
    patch rejected in review does. `STALE_BEFORE` is exempt because the patch was written
    against a block that changed underneath it -- that is the system's scheduling, not
    the Writer's mistake.
    """
    return rejection.code is not RejectionCode.STALE_BEFORE
