"""Potential contradictions between a hint's stated purpose and its instruction.

Most hint quality is semantic and belongs to the council. This module is deliberately
narrow: if a title says the hint is undoing one arithmetic operation and its body names
that same operation again, the pair deserves semantic verification. It is deliberately
not repair-authoritative: ``subtract -7`` can undo subtracting 7, and strings alone do not
carry enough algebraic context to decide every case safely.
"""

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


_UNDO = re.compile(
    r"\bundo(?:ing)?(?:\s+the)?\s+"
    r"(addition|subtraction|multiplication|division)\b",
    re.IGNORECASE,
)
_SAME_OPERATION = {
    "addition": re.compile(r"\badd(?:ing)?\b", re.IGNORECASE),
    "subtraction": re.compile(r"\bsubtract(?:ing)?\b", re.IGNORECASE),
    "multiplication": re.compile(r"\bmultiply(?:ing)?\b", re.IGNORECASE),
    "division": re.compile(r"\bdivid(?:e|ing)\b", re.IGNORECASE),
}
_INVERSE = {
    "addition": "subtract",
    "subtraction": "add",
    "multiplication": "divide",
    "division": "multiply",
}


def _instruction_key(value: str) -> str:
    """Compare instructional text without treating spacing or case as new content."""

    return " ".join(value.split()).casefold()


@rule(
    "DUPLICATE_HINT_BODY",
    severity=Severity.ERROR,
    category=IssueCategory.MATHEMATICS,
    description="Two hints for the same graded step repeat the same instruction.",
)
def duplicate_hint_body(context: RuleContext) -> Iterable[ValidationFinding]:
    """Report adjacent copies, scoped to one step rather than the whole problem.

    The real corpus legitimately restarts hint sequences under later steps. Comparing
    across a whole block would therefore turn a repeated reminder in a different graded
    question into a false positive. Even within one step, a useful identity can be repeated
    after intervening scaffolds. The mechanically certain case is narrower: consecutive
    hint rows repeating the same substantive instruction. Placeholder values used by old
    tooling (``hint`` and ``scaffold``) carry no instructional claim and stay out too.
    """

    block = context.block
    if block is None:
        return
    for scope in block.step_scopes():
        previous = None
        for row in scope.hints:
            body = row.get(ColumnKey.BODY_TEXT)
            normalized = _instruction_key(body)
            if (
                previous is not None
                and row.row == previous.row + 1
                and normalized not in {"", "hint", "scaffold"}
                and normalized
                == _instruction_key(previous.get(ColumnKey.BODY_TEXT))
            ):
                yield finding(
                    context,
                    "DUPLICATE_HINT_BODY",
                    f"hint repeats the same instruction as the adjacent hint at row "
                    f"{previous.row}; replace it with a distinct next hint",
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.BODY_TEXT],
                    column_key=ColumnKey.BODY_TEXT,
                    first_use_row=previous.row,
                    observed=body,
                )
            previous = row


@rule(
    "STEP_TITLE_DUPLICATES_BODY",
    severity=Severity.ERROR,
    category=IssueCategory.FORMATTING,
    description="A step repeats its title verbatim in Body Text.",
)
def step_title_duplicates_body(context: RuleContext) -> Iterable[ValidationFinding]:
    """A lossless row-shift residue: keep the required Title and clear its exact copy.

    This is intentionally limited to step rows. Hint and scaffold bodies are required
    instructional content, while a step Body Text is optional. The rule also requires
    exact text after boundary trimming; similar wording still belongs to semantic review.
    """

    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.STEP):
        title = row.get(ColumnKey.TITLE).strip()
        body = row.get(ColumnKey.BODY_TEXT).strip()
        if not title or title != body:
            continue
        yield finding(
            context,
            "STEP_TITLE_DUPLICATES_BODY",
            "step Body Text exactly duplicates its Title; clear the redundant Body Text",
            row=row.row,
            column=FIXED_COLUMNS[ColumnKey.BODY_TEXT],
            column_key=ColumnKey.BODY_TEXT,
            observed=row.get(ColumnKey.BODY_TEXT),
            expected="",
        )


@rule(
    "HINT_INVERSE_OPERATION_CONTRADICTION",
    severity=Severity.WARNING,
    category=IssueCategory.MATHEMATICS,
    description="A hint may repeat the operation its title says it is undoing.",
    repairable=False,
)
def hint_inverse_operation_contradiction(
    context: RuleContext,
) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.HINT):
        title = row.get(ColumnKey.TITLE)
        body = row.get(ColumnKey.BODY_TEXT)
        match = _UNDO.search(title)
        if match is None or not body.strip():
            continue
        operation = match.group(1).casefold()
        if not _SAME_OPERATION[operation].search(body):
            continue
        inverse = _INVERSE[operation]
        yield finding(
            context,
            "HINT_INVERSE_OPERATION_CONTRADICTION",
            f"the hint title says to undo {operation}, but its body tells the student "
            f"to perform {operation} again; verify from the equation whether it should "
            f"{inverse} the same quantity instead",
            row=row.row,
            column=FIXED_COLUMNS[ColumnKey.BODY_TEXT],
            column_key=ColumnKey.BODY_TEXT,
            operation=operation,
            expected_inverse=inverse,
        )
