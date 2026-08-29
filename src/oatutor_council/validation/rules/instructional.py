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
