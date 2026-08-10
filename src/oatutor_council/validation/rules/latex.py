"""LaTeX delimiters, escaping, and the corruption that pipe-splitting causes."""

from __future__ import annotations

import re
from typing import Iterable

from ...models import (
    FIXED_COLUMNS,
    MC_CHOICE_DELIMITER,
    ColumnKey,
    IssueCategory,
    Notation,
    Severity,
    ValidationFinding,
)
from .registry import RuleContext, finding, rule

CHECKED_COLUMNS = (
    ColumnKey.TITLE,
    ColumnKey.BODY_TEXT,
    ColumnKey.ANSWER,
    ColumnKey.MC_CHOICES,
)

#: Commands that read files, execute, or reach outside the expression. A workbook cell is
#: untrusted input rendered into a page, so these are refused regardless of intent.
BANNED_COMMANDS = (
    r"\input",
    r"\include",
    r"\write",
    r"\def",
    r"\usepackage",
    r"\href",
    r"\url",
    r"\catcode",
    r"\csname",
    r"\openout",
)

#: A backslash that survived one round of escaping too many, e.g. `\\theta` in a cell
#: where `\theta` was meant. Matched only before a letter, so a genuine LaTeX line break
#: (`\\` at the end of a row) is not reported.
_DOUBLE_ESCAPED = re.compile(r"\\\\[A-Za-z]")


def _cells(context: RuleContext):
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key in CHECKED_COLUMNS:
            text = row.get(key)
            if text.strip():
                yield row, key, text


@rule(
    "LATEX_DELIMITER_UNBALANCED",
    severity=Severity.BLOCKING,
    category=IssueCategory.LATEX,
    description="A cell has an odd number of `$$` delimiters.",
)
def latex_delimiter_unbalanced(context: RuleContext) -> Iterable[ValidationFinding]:
    for row, key, text in _cells(context):
        if text.count("$$") % 2:
            yield finding(
                context,
                "LATEX_DELIMITER_UNBALANCED",
                f"cell contains {text.count('$$')} `$$` delimiters, which cannot pair up",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                delimiter_count=text.count("$$"),
            )


@rule(
    "MC_LATEX_PIPE_CORRUPTION",
    severity=Severity.BLOCKING,
    category=IssueCategory.LATEX,
    description="A choice list was split on a pipe that belonged to a LaTeX command.",
)
def mc_latex_pipe_corruption(context: RuleContext) -> Iterable[ValidationFinding]:
    """The defect that motivates checking each choice separately.

    A `\\middle|` inside a choice was split on its pipe, destroying both the choice
    boundaries and the `$$` pairing. The whole cell still has an **even** `$$` count, so
    a cell-level balance check sees nothing wrong -- the corruption is only visible when
    each pipe-separated part is balanced on its own.

    Counted in `$$` tokens, not `$` characters. On the real corrupted cell every part has
    an even number of individual dollars, so a character count reports nothing; the
    delimiter is `$$`, and it is the token that fails to pair.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        text = row.get(ColumnKey.MC_CHOICES)
        if "$$" not in text:
            continue
        broken = [
            index
            for index, part in enumerate(text.split(MC_CHOICE_DELIMITER))
            if part.count("$$") % 2
        ]
        if broken:
            yield finding(
                context,
                "MC_LATEX_PIPE_CORRUPTION",
                (
                    f"choices {broken} have unpaired `$$` delimiters while the whole "
                    "cell balances, the signature of a LaTeX command split on its pipe"
                ),
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                column_key=ColumnKey.MC_CHOICES,
                broken_choice_indexes=broken,
            )


@rule(
    "DOUBLE_ESCAPED_BACKSLASH",
    severity=Severity.ERROR,
    category=IssueCategory.LATEX,
    description="A LaTeX command carries a doubled backslash.",
)
def double_escaped_backslash(context: RuleContext) -> Iterable[ValidationFinding]:
    for row, key, text in _cells(context):
        matches = sorted({match.group(0) for match in _DOUBLE_ESCAPED.finditer(text)})
        if matches:
            yield finding(
                context,
                "DOUBLE_ESCAPED_BACKSLASH",
                (
                    f"cell contains doubled backslashes before a command: "
                    f"{', '.join(repr(m) for m in matches)}"
                ),
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                matches=matches,
            )


@rule(
    "LATEX_BANNED_COMMAND",
    severity=Severity.BLOCKING,
    category=IssueCategory.LATEX,
    description="A cell contains a LaTeX command that reads files or executes.",
    repairable=False,
)
def latex_banned_command(context: RuleContext) -> Iterable[ValidationFinding]:
    for row, key, text in _cells(context):
        found = sorted({command for command in BANNED_COMMANDS if command in text})
        if found:
            yield finding(
                context,
                "LATEX_BANNED_COMMAND",
                f"cell contains banned LaTeX commands: {', '.join(found)}",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                commands=found,
            )


@rule(
    "LATEX_IN_ASCII_WORKBOOK",
    severity=Severity.WARNING,
    category=IssueCategory.NOTATION,
    description="LaTeX markup appears in a workbook written in the ASCII convention.",
)
def latex_in_ascii_workbook(context: RuleContext) -> Iterable[ValidationFinding]:
    """Mixed notation within one workbook is a defect; across workbooks it is not.

    Both conventions exist in the corpus and each is internally consistent, so this
    compares against the notation the reader detected rather than against a preference.
    Fires on `MIXED` as well as `ASCII`: a workbook sitting between the two conventions
    is precisely one whose cells disagree, which is the defect this rule names.
    """
    if context.conventions.notation is Notation.LATEX:
        return
    for row, key, text in _cells(context):
        if "$$" in text or _DOUBLE_ESCAPED.search(text) or re.search(r"\\[A-Za-z]+", text):
            yield finding(
                context,
                "LATEX_IN_ASCII_WORKBOOK",
                "cell contains LaTeX markup but the workbook is written in ASCII",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )
