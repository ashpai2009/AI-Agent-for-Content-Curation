"""Mathematical notation, and values Excel has quietly rewritten."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from ...models import (
    DATETIME_EXEMPT_COLUMNS,
    FIXED_COLUMNS,
    ColumnKey,
    IssueCategory,
    Notation,
    Severity,
    ValidationFinding,
)
from .registry import RuleContext, finding, rule

#: Columns whose contents are mathematics rather than prose.
MATHEMATICAL_COLUMNS = (ColumnKey.ANSWER, ColumnKey.MC_CHOICES)

#: Columns a curator writes freely, checked only for the things that break rendering.
TEXT_COLUMNS = (ColumnKey.TITLE, ColumnKey.BODY_TEXT)

#: Unicode glyphs the ASCII convention spells out. The corpus contains a literal Unicode
#: theta sitting in the answerType column, which is both a misplaced value and this.
NON_ASCII_MATH = {
    "θ": "theta",
    "π": "pi",
    "α": "alpha",
    "β": "beta",
    "φ": "phi",
    "×": "*",
    "÷": "/",
    "−": "-",
    "≤": "<=",
    "≥": ">=",
    "√": "sqrt()",
    "°": "degrees",
    "²": "**2",
    "³": "**3",
}

_CARET = re.compile(r"[A-Za-z0-9)\]]\s*\^")


def _cells(context: RuleContext, columns):
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key in columns:
            text = row.get(key)
            if text.strip():
                yield row, key, text


@rule(
    "DATE_COERCION",
    severity=Severity.BLOCKING,
    category=IssueCategory.NOTATION,
    description="Excel turned a fraction into a date.",
)
def date_coercion(context: RuleContext) -> Iterable[ValidationFinding]:
    """`1/2` typed into a general-format cell becomes a datetime.

    The year is noise -- it records when the file was last edited, and the corpus carries
    two different ones -- so the suggested repair is built from month and day alone.
    `Time Last Checked` legitimately holds a datetime and is exempt.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key, value in row.raw.items():
            if key in DATETIME_EXEMPT_COLUMNS or not isinstance(value, datetime):
                continue
            column = context.parsed.column_map.index_of(key)
            if column is None:
                continue
            yield finding(
                context,
                "DATE_COERCION",
                (
                    f"cell holds the date {value.date().isoformat()}, which is Excel's "
                    f"coercion of the fraction {value.month}/{value.day}"
                ),
                row=row.row,
                column=column,
                column_key=key,
                suggested=f"{value.month}/{value.day}",
                stored=value.isoformat(sep=" "),
            )


@rule(
    "NON_ASCII_MATH",
    severity=Severity.ERROR,
    category=IssueCategory.NOTATION,
    description="A Unicode mathematical glyph appears where the ASCII convention applies.",
)
def non_ascii_math(context: RuleContext) -> Iterable[ValidationFinding]:
    """Only applied to workbooks written in ASCII.

    A LaTeX workbook renders its own symbols and has no business being told to spell out
    pi, so the convention detected by the reader decides whether this rule runs at all.
    """
    if context.conventions.notation is Notation.LATEX:
        return
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS + TEXT_COLUMNS):
        offenders = sorted({glyph for glyph in NON_ASCII_MATH if glyph in text})
        if offenders:
            replacements = ", ".join(f"{g!r} -> {NON_ASCII_MATH[g]!r}" for g in offenders)
            yield finding(
                context,
                "NON_ASCII_MATH",
                f"cell uses Unicode mathematical glyphs: {replacements}",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                glyphs=offenders,
            )


@rule(
    "CARET_EXPONENT",
    severity=Severity.ERROR,
    category=IssueCategory.NOTATION,
    description="`^` used for exponentiation where the convention requires `**`.",
)
def caret_exponent(context: RuleContext) -> Iterable[ValidationFinding]:
    if context.conventions.notation is Notation.LATEX:
        return
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS):
        if _CARET.search(text):
            yield finding(
                context,
                "CARET_EXPONENT",
                "exponent written with `^`; the ASCII convention uses `**`",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )


@rule(
    "WHITESPACE_PADDING",
    severity=Severity.WARNING,
    category=IssueCategory.FORMATTING,
    description="A cell value has leading or trailing whitespace.",
)
def whitespace_padding(context: RuleContext) -> Iterable[ValidationFinding]:
    """Invisible in the spreadsheet and fatal to an exact match.

    Multiple-choice grading compares the answer to a choice character for character, so a
    trailing space is a wrong answer that looks right to everyone who reads it.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key, text in row.values.items():
            if text and text != text.strip():
                column = context.parsed.column_map.index_of(key)
                if column is None:
                    continue
                yield finding(
                    context,
                    "WHITESPACE_PADDING",
                    "cell value has leading or trailing whitespace",
                    row=row.row,
                    column=column,
                    column_key=key,
                    stripped=text.strip(),
                )
