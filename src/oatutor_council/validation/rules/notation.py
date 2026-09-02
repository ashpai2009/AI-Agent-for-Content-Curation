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
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "‑": "-",
    "…": "...",
    "\u00a0": " ",
    "\u202f": " ",
}

_CARET = re.compile(r"[A-Za-z0-9)\]]\s*\^")


def normalize_known_non_ascii(text: str) -> str | None:
    """Return the exact ASCII spelling when every non-ASCII glyph is known.

    Detection deliberately reports every non-ASCII codepoint, including ones this
    function does not know how to interpret.  Mechanical repair is narrower: it runs
    only when every glyph has a lossless, policy-defined replacement.  In particular,
    the degree sign needs context -- ``120°`` is ``120 degrees``, not ``120degrees``.
    The latter was measured in a live workbook after the model copied the replacement
    label from the rule message literally.
    """
    if any(ord(ch) > 127 and ch not in NON_ASCII_MATH for ch in text):
        return None

    pieces: list[str] = []
    for index, ch in enumerate(text):
        if ord(ch) <= 127:
            pieces.append(ch)
            continue
        replacement = NON_ASCII_MATH[ch]
        if ch == "°":
            if pieces and pieces[-1] and not pieces[-1][-1].isspace():
                pieces.append(" ")
            pieces.append(replacement)
            following = text[index + 1 : index + 2]
            if following and following.isalpha():
                pieces.append(" ")
        else:
            pieces.append(replacement)
    return "".join(pieces)


def normalize_irregular_whitespace(text: str) -> str:
    """Apply the written one-space rule without interpreting cell content."""
    return re.sub(r"\s+", " ", text).strip()


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
    """Any character outside ASCII, in either convention.

    Two deliberate widenings over the obvious implementation.

    **Every non-ASCII codepoint, not a glyph list.** A list catches the symbols someone
    thought of; the ones that actually reach a workbook are the ones nobody thought of --
    a non-breaking space pasted from a PDF, a Unicode minus that looks exactly like a
    hyphen, a smart quote. `NON_ASCII_MATH` still supplies the replacement where it knows
    one, so a curator gets `'θ' -> 'theta'` rather than a codepoint number, but the
    *detection* does not depend on having predicted the character.

    **In LaTeX workbooks too.** LaTeX renders `\\theta`; it does not render a literal θ
    any better than the ASCII convention does. Skipping the check there -- which this rule
    used to -- left the one LaTeX workbook in the corpus unexamined for exactly the defect
    it is most likely to contain, since its content was pasted from rendered output.
    """
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS + TEXT_COLUMNS):
        offenders = sorted({ch for ch in text if ord(ch) > 127})
        if not offenders:
            continue
        described = ", ".join(
            f"{ch!r} -> {NON_ASCII_MATH[ch]!r}"
            if ch in NON_ASCII_MATH
            else f"{ch!r} (U+{ord(ch):04X})"
            for ch in offenders
        )
        yield finding(
            context,
            "NON_ASCII_MATH",
            f"cell contains non-ASCII characters: {described}",
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


# --------------------------------------------------------------------------------------
# Spacing
# --------------------------------------------------------------------------------------

#: Binary operators, as they appear in the ASCII convention. `**` is matched before `*`
#: so a spaced power reports once rather than twice.
_SPACED_OPERATOR = re.compile(r"\s(\*\*|[-+*/=]|<=|>=|<|>)\s")

#: Whitespace that should never appear in a curated cell. A tab or a newline survives a
#: copy-paste invisibly and then breaks an exact-match comparison; a double space does
#: the same and is even harder to see.
_IRREGULAR_WHITESPACE = {
    "\t": "a tab",
    "\n": "a line break",
    "\r": "a carriage return",
    "\v": "a vertical tab",
    "\f": "a form feed",
}


@rule(
    "OPERATOR_SPACING",
    severity=Severity.WARNING,
    category=IssueCategory.NOTATION,
    description="An operator in Answer or mcChoices has spaces around it.",
)
def operator_spacing(context: RuleContext) -> Iterable[ValidationFinding]:
    """`x + 1` where the convention writes `x+1`.

    Confined to `Answer` and `mcChoices`, which are expressions. Title and Body Text are
    prose, where spaces around a minus sign are ordinary English rather than a defect.

    Warning rather than error because the mathematics is unaffected -- but not silence,
    because multiple-choice grading compares the answer to a choice character for
    character, so a space that changes nothing about the value still marks a correct
    answer wrong.
    """
    if context.conventions.notation is Notation.LATEX:
        return
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS):
        match = _SPACED_OPERATOR.search(text)
        if match:
            yield finding(
                context,
                "OPERATOR_SPACING",
                f"the operator {match.group(1)!r} is written with spaces around it; the "
                "ASCII convention writes operators tight against their operands",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                operator=match.group(1),
            )


@rule(
    "IRREGULAR_WHITESPACE",
    severity=Severity.ERROR,
    category=IssueCategory.FORMATTING,
    description="A cell contains a tab, a line break, or a double space.",
)
def irregular_whitespace(context: RuleContext) -> Iterable[ValidationFinding]:
    """Whitespace that is invisible in the spreadsheet and fatal to an exact match.

    Separate from `WHITESPACE_PADDING`, which is about the *edges* of a value and has an
    exactly-known repair. This is about the middle, where what to do depends on what the
    character was doing there -- a line break inside body text may be deliberate
    formatting, a tab never is.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key, text in row.values.items():
            if not text:
                continue
            found = sorted({name for ch, name in _IRREGULAR_WHITESPACE.items() if ch in text})
            if "  " in text:
                found.append("a double space")
            if not found:
                continue
            column = context.parsed.column_map.index_of(key)
            if column is None:
                continue
            yield finding(
                context,
                "IRREGULAR_WHITESPACE",
                f"cell value contains {', '.join(found)}",
                row=row.row,
                column=column,
                column_key=key,
                found=found,
            )


# --------------------------------------------------------------------------------------
# Expected ASCII forms
# --------------------------------------------------------------------------------------

#: `sqrt` not followed by an opening parenthesis: `sqrt 2`, `sqrt2`, `sqrtx`.
_SQRT_WITHOUT_PARENTHESIS = re.compile(r"\bsqrt\s*(?![\s(]*\()")

#: `sin^-1`, `cos**-1`, `tan ^ -1` -- the inverse written as a power.
_INVERSE_AS_POWER = re.compile(
    r"\b(sin|cos|tan|sec|csc|cot)\s*(?:\^|\*\*)\s*\(?\s*-\s*1", re.IGNORECASE
)

#: `asin`, `acos`, `atan` -- the programming-language spelling.
_INVERSE_AS_A_PREFIX = re.compile(r"\ba(sin|cos|tan|sec|csc|cot)\b", re.IGNORECASE)

#: `=<` and `=>`, which are not operators in any convention.
_REVERSED_INEQUALITY = re.compile(r"=<|=>")


@rule(
    "SQRT_NOT_PARENTHESISED",
    severity=Severity.ERROR,
    category=IssueCategory.NOTATION,
    description="`sqrt` is not followed by a parenthesised argument.",
)
def sqrt_not_parenthesised(context: RuleContext) -> Iterable[ValidationFinding]:
    """`sqrt(2)`, never `sqrt 2`.

    Without the parentheses the extent of the root is a matter of opinion: `sqrt 2x`
    could be either factor under the radical, and the tutor and the curator need not
    agree about which.
    """
    if context.conventions.notation is Notation.LATEX:
        return
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS):
        if _SQRT_WITHOUT_PARENTHESIS.search(text):
            yield finding(
                context,
                "SQRT_NOT_PARENTHESISED",
                "`sqrt` is not followed by a parenthesised argument, so how far the "
                "root extends is ambiguous",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )


@rule(
    "INVERSE_TRIG_FORM",
    severity=Severity.ERROR,
    category=IssueCategory.NOTATION,
    description="An inverse trigonometric function is not written as arcsin/arccos/etc.",
)
def inverse_trig_form(context: RuleContext) -> Iterable[ValidationFinding]:
    """`arcsin(x)`, not `sin^-1(x)` and not `asin(x)`.

    `sin**-1` is the worse of the two: it is not merely the wrong spelling but a
    different expression, since a parser that takes it at face value returns the
    reciprocal of the sine rather than the inverse function.
    """
    if context.conventions.notation is Notation.LATEX:
        return
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS):
        power = _INVERSE_AS_POWER.search(text)
        prefix = _INVERSE_AS_A_PREFIX.search(text)
        if not power and not prefix:
            continue
        if power:
            name = power.group(1).lower()
            message = (
                f"the inverse is written as a power, {power.group(0)!r}; write "
                f"'arc{name}'. Read literally that is the reciprocal of {name}, which is "
                "a different function"
            )
        else:
            name = prefix.group(1).lower()
            message = f"the inverse is written 'a{name}'; the convention writes 'arc{name}'"
        yield finding(
            context,
            "INVERSE_TRIG_FORM",
            message,
            row=row.row,
            column=FIXED_COLUMNS[key],
            column_key=key,
            function=name,
        )


@rule(
    "INEQUALITY_FORM",
    severity=Severity.ERROR,
    category=IssueCategory.NOTATION,
    description="An inequality is written `=<` or `=>` rather than `<=` or `>=`.",
)
def inequality_form(context: RuleContext) -> Iterable[ValidationFinding]:
    """`<=` and `>=`, in that order.

    `=>` in particular is read by a parser as an implication or a syntax error rather
    than as the inequality the curator meant.
    """
    for row, key, text in _cells(context, MATHEMATICAL_COLUMNS):
        match = _REVERSED_INEQUALITY.search(text)
        if match:
            correct = {"=<": "<=", "=>": ">="}[match.group(0)]
            yield finding(
                context,
                "INEQUALITY_FORM",
                f"inequality written {match.group(0)!r}; the convention writes "
                f"{correct!r}",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                found=match.group(0),
                expected=correct,
            )
