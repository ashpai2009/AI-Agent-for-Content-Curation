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

import re
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
    IssueSource,
    ParsedWorkbook,
    Patch,
    PatchRejection,
    ProblemBlock,
    RejectionCode,
    RowType,
    Severity,
    ValidationFinding,
    WorkbookRow,
)
from .mathematics import MathVerdict, answers_equivalent, equations_equivalent
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


def _equivalent_mathematics_rewrite(
    issue: Issue, edits: Sequence[CellEdit]
) -> CellEdit | None:
    """A mathematics correction must correct mathematics, not merely restyle it.

    The live control `x=sqrt(4)` was changed to `x=2` after an auditor called the
    equivalent original "wrong". Representation issues have their own categories; under
    `mathematics`, a patch whose every mathematical change is provably equivalent has not
    repaired the alleged defect and must not modify a clean cell.
    """
    if issue.category is not IssueCategory.MATHEMATICS:
        return None
    candidates = [
        edit
        for edit in edits
        if edit.column_key in MATHEMATICAL_COLUMNS
        and edit.before.strip()
        and edit.after.strip()
    ]
    if not candidates:
        return None
    verdicts = [
        (
            answers_equivalent(edit.before, edit.after)
            if edit.column_key is ColumnKey.ANSWER
            else equations_equivalent(edit.before, edit.after)
        )
        for edit in candidates
    ]
    if all(verdict is MathVerdict.EQUIVALENT for verdict in verdicts):
        return candidates[0]
    return None


def _requested_form_violation(
    block: ProblemBlock, edits: Sequence[CellEdit]
) -> CellEdit | None:
    """Do not replace an exact/fraction answer with a decimal the question did not ask for.

    Form language is scoped to the edited row and its enclosing step, plus the problem
    stem. A multi-step block may ask for an exact fraction in step one and a decimal in
    step two; scanning every row would incorrectly let the first instruction govern the
    second answer.
    """
    decimal = re.compile(r"^[+-]?\d+\.\d+$")
    fraction = re.compile(r"^[+-]?\d+\s*/\s*\d+$")
    for edit in edits:
        if edit.column_key is not ColumnKey.ANSWER:
            continue
        edited_row = next((row for row in block.rows if row.row == edit.row), None)
        if edited_row is None:
            continue
        context_rows = [block.problem_row, edited_row]
        if edited_row.row_type not in {RowType.PROBLEM, RowType.STEP}:
            preceding_step = next(
                (
                    row
                    for row in reversed(block.rows)
                    if row.row < edited_row.row and row.row_type is RowType.STEP
                ),
                None,
            )
            if preceding_step is not None:
                context_rows.append(preceding_step)
        prompt_text = " ".join(
            value
            for row in context_rows
            for value in (
                row.get(ColumnKey.TITLE),
                row.get(ColumnKey.BODY_TEXT),
            )
        ).casefold()
        if "exact" not in prompt_text and "fraction" not in prompt_text:
            continue
        before = edit.before.strip().removeprefix("$$").removesuffix("$$").strip()
        after = edit.after.strip().removeprefix("$$").removesuffix("$$").strip()
        before_is_fraction = "\\frac" in before or bool(fraction.fullmatch(before))
        if before_is_fraction and decimal.fullmatch(after):
            return edit
    return None


def _unsupported_identifier_renumber(
    issue: Issue,
    parsed: ParsedWorkbook,
    block: ProblemBlock,
    edits: Sequence[CellEdit],
) -> CellEdit | None:
    """Reject a model-only rename that changes no dependency semantics.

    Identifiers are labels, not a sequence that has to be gap-free.  The live pilot
    renamed a valid ``s3`` to ``s2`` solely because the preceding label happened to be
    ``s1``.  A blanket requirement for deterministic evidence is too strong: displaced
    cells and malformed row types are exactly the structural defects for which the model
    is useful.  The safe mechanical boundary is narrower:

    * the finding is model-only;
    * every structural edit touches only ``HintID`` or ``Dependency``;
    * the edits are a consistent label substitution; and
    * no registered structural rule currently supports the cited row.

    Such a patch preserves the graph and merely renames its nodes, so it cannot repair a
    structural defect.  Real dependency repairs, row-type repairs, and column shifts do
    not match this shape and continue through the full simulation gate.
    """
    if target_findings(issue, parsed, block) is not None:
        return None

    structural = [edit for edit in edits if edit.is_structural]
    if not structural or any(
        edit.column_key not in {ColumnKey.HINT_ID, ColumnKey.DEPENDENCY}
        for edit in structural
    ):
        return None

    # A rename may touch one unreferenced identifier, or both its declaration and every
    # reference. Different substitutions are a substantive graph edit, not this case.
    substitutions = {
        (edit.before.strip(), edit.after.strip())
        for edit in structural
        if edit.before.strip() and edit.after.strip()
    }
    if len(substitutions) != 1 or any(
        not edit.before.strip() or not edit.after.strip() for edit in structural
    ):
        return None

    issue_cells = set(issue.cells)
    structural_categories = {
        IssueCategory.STRUCTURE,
        IssueCategory.ROW_TYPE,
        IssueCategory.DEPENDENCY,
    }
    for finding in _findings_for(parsed, block):
        registered = REGISTRY.get(finding.code)
        if finding.code in issue.rule_codes or (
            (finding.row, finding.column) in issue_cells
            and registered is not None
            and registered.category in structural_categories
        ):
            return None
    return structural[0]


def _unsupported_answer_type_relabel(
    issue: Issue,
    parsed: ParsedWorkbook,
    block: ProblemBlock,
    edits: Sequence[CellEdit],
) -> CellEdit | None:
    """Reject a model-invented standalone ``numeric``/``algebra`` policy.

    The curator's rules say those are valid answer types but do not define a complete
    classifier between them. A plain exact fraction is the concrete ambiguous case: real
    workbooks use both conventions, so a model preference is not authority to relabel an
    otherwise-correct cell.

    Three sources of authority remain:

    * a registered rule fires at the same cell (currently the high-confidence case of an
      explicit variable equation labelled numeric);
    * the curator's instruction document explicitly asks for the relabel; or
    * the same patch changes the Answer on that row, so answer and type are one coordinated
      semantic correction rather than a preference about an already-correct value.
    """
    if issue.source is IssueSource.INSTRUCTION_DOCUMENT:
        return None

    answer_rows = {
        edit.row for edit in edits if edit.column_key is ColumnKey.ANSWER
    }
    candidates = [
        edit
        for edit in edits
        if edit.column_key is ColumnKey.ANSWER_TYPE
        and {edit.before.strip(), edit.after.strip()} == {"numeric", "algebra"}
        and edit.row not in answer_rows
    ]
    if not candidates:
        return None

    findings = _findings_for(parsed, block)
    for edit in candidates:
        supported = any(
            finding.row == edit.row
            and finding.column == edit.column
            and finding.code in REGISTRY
            and REGISTRY[finding.code].category
            in {
                IssueCategory.STRUCTURE,
                IssueCategory.ROW_TYPE,
                IssueCategory.DEPENDENCY,
            }
            for finding in findings
        )
        if not supported:
            return edit
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
    """Condition 5: the resulting block must still be a **block**.

    Strictly the shape of the block: a problem row at the top, no second problem row
    splitting it, one `Problem Name` throughout. These are reader-level facts that
    `run_rules` cannot re-derive from a simulated block -- simulation edits cell values
    and does not re-parse the sheet -- so they are stated here, absolutely, and each is
    cheap to state exactly.

    **Identifier uniqueness and dependency resolution are deliberately not here**, and
    were, which cost a live run. They were checked block-wide, so every step after the
    first in a `RESET_PER_STEP` workbook looked like a duplicate `h1` -- the exact
    misreading `step_scopes()` exists to prevent, restated in a second place that never
    got the fix. Worse, they were checked *absolutely*: the condition was almost always
    pre-existing, so the gate refused patches for a defect the patch had not introduced
    and could not remove. Nearly every repair inside a multi-step problem was rejected
    three times over and the issue walked to `NEEDS_HUMAN_REVIEW` with its attempts spent.

    Both belong to `regressions_introduced`, which already derives them from the
    registered rules -- with the workbook's own convention, therefore with the right
    scope -- and compares before against after, so a patch is answerable only for what it
    breaks. A rule engine and a gate that both decide what a valid identifier is will
    disagree eventually, and the gate is the one nobody re-measures against the corpus.
    """
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

    `before` values are verified here against the current parsed block *and* again against
    the live file during application. The first check is required now that review happens
    on an in-memory simulation: simulating an edit whose `before` names a different cell
    would show the reviewer a candidate that can never be applied. The second check keeps
    the write safe if the file changes after this gate.
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

        current_row = next((row for row in block.rows if row.row == edit.row), None)
        actual_value = current_row.get(actual_key) if current_row is not None else None
        if actual_value is None:
            return _reject(
                RejectionCode.CELL_NOT_FOUND,
                f"row {edit.row} is not present in the current problem block",
                row=edit.row,
                column=edit.column,
            )
        if actual_value != edit.before:
            return _reject(
                RejectionCode.BEFORE_MISMATCH,
                f"row {edit.row} column {edit.column} currently holds "
                f"{actual_value!r}, but the patch copied {edit.before!r}; copy the exact "
                "value from the named cell",
                row=edit.row,
                column=edit.column,
                detail={"actual": actual_value},
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

    unsupported_type = _unsupported_answer_type_relabel(
        issue, parsed, block, patch.edits
    )
    if unsupported_type is not None:
        return _reject(
            RejectionCode.STRUCTURAL_EVIDENCE_MISSING,
            "the patch relabels an unchanged Answer between numeric and algebra, but "
            "the workbook rules do not define that relabel and no deterministic finding "
            "supports this cell; preserve the existing type unless the curator explicitly "
            "requires a different convention",
            row=unsupported_type.row,
            column=unsupported_type.column,
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

    form_violation = _requested_form_violation(block, patch.edits)
    if form_violation:
        return _reject(
            RejectionCode.REQUESTED_FORM_VIOLATION,
            "the question requests an exact value or fraction, so the repair may not "
            "replace the existing fractional Answer with a decimal; repair the matching "
            "choice list instead",
            row=form_violation.row,
            column=form_violation.column,
        )

    equivalent = _equivalent_mathematics_rewrite(issue, patch.edits)
    if equivalent:
        return _reject(
            RejectionCode.MATHEMATICALLY_EQUIVALENT_REWRITE,
            "the proposed mathematics edit is equivalent to the existing value; an "
            "equivalent simplification is not a correction unless the issue is explicitly "
            "about representation",
            row=equivalent.row,
            column=equivalent.column,
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

    unsupported_renumber = _unsupported_identifier_renumber(
        issue, parsed, block, patch.edits
    )
    if unsupported_renumber is not None:
        return _reject(
            RejectionCode.STRUCTURAL_EVIDENCE_MISSING,
            "the patch only renames an identifier without changing the dependency "
            "graph or resolving a deterministic defect; identifiers are labels and are "
            "not renumbered merely to close a numeric gap",
            row=unsupported_renumber.row,
            column=unsupported_renumber.column,
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
