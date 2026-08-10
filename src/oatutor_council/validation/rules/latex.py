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


# --------------------------------------------------------------------------------------
# What belongs inside a container, and what belongs outside it
# --------------------------------------------------------------------------------------

#: One `$$...$$` region, non-greedy so adjacent containers stay separate.
_CONTAINER = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)

#: An empty or whitespace-only container. Renders as a gap the student cannot interpret.
_EMPTY_CONTAINER = re.compile(r"\$\$\s*\$\$")

#: Manual spacing commands. They encode how the expression looked in whatever document it
#: was copied from, and carry no mathematical content.
_SPACING_COMMANDS = (r"\quad", r"\qquad", r"\,", r"\;", r"\!", r"\:")

#: Two containers separated only by whitespace: one expression cut in half.
_ADJACENT_CONTAINERS = re.compile(r"\$\$\s*\$\$")

#: Three or more ordinary words in a row -- prose, not mathematics.
_PROSE_RUN = re.compile(r"(?:\b[A-Za-z]{3,}\b[ ]+){2,}\b[A-Za-z]{3,}\b")


def _latex_cells(context: RuleContext):
    """Cells worth checking for LaTeX defects.

    Not restricted to LaTeX workbooks: an ASCII workbook containing `$$` has LaTeX in it
    whatever its convention says, and the defects below are defects there too.
    `LATEX_IN_ASCII_WORKBOOK` separately reports that it should not be there at all.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key in CHECKED_COLUMNS:
            text = row.get(key)
            if "$" in text or "\\" in text:
                yield row, key, text


def _outside_containers(text: str) -> str:
    return _CONTAINER.sub(" ", text)


@rule(
    "LATEX_EMPTY_CONTAINER",
    severity=Severity.ERROR,
    category=IssueCategory.LATEX,
    description="A `$$...$$` container is empty.",
)
def latex_empty_container(context: RuleContext) -> Iterable[ValidationFinding]:
    """`$$$$` renders as nothing, in a place the student expects mathematics.

    Usually the residue of a deletion: the expression was removed and its delimiters
    were not.
    """
    for row, key, text in _latex_cells(context):
        if _EMPTY_CONTAINER.search(text):
            yield finding(
                context,
                "LATEX_EMPTY_CONTAINER",
                "an empty `$$...$$` container renders as a gap where mathematics "
                "should be",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )


@rule(
    "LATEX_DELIMITER_PADDING",
    severity=Severity.WARNING,
    category=IssueCategory.LATEX,
    description="A `$$...$$` container has whitespace immediately inside its delimiters.",
)
def latex_delimiter_padding(context: RuleContext) -> Iterable[ValidationFinding]:
    """`$$ x $$` rather than `$$x$$`.

    Harmless to the rendered mathematics and not harmless to an exact match, which is
    what multiple-choice grading performs on these cells.
    """
    for row, key, text in _latex_cells(context):
        for match in _CONTAINER.finditer(text):
            inner = match.group(1)
            if inner and inner != inner.strip():
                yield finding(
                    context,
                    "LATEX_DELIMITER_PADDING",
                    "whitespace sits immediately inside the `$$` delimiters",
                    row=row.row,
                    column=FIXED_COLUMNS[key],
                    column_key=key,
                )
                break


@rule(
    "LATEX_SPACING_COMMAND",
    severity=Severity.WARNING,
    category=IssueCategory.LATEX,
    description="A cell uses a manual LaTeX spacing command.",
)
def latex_spacing_command(context: RuleContext) -> Iterable[ValidationFinding]:
    r"""`\quad`, `\,`, `\;` and their relatives are refused.

    They encode how the expression was laid out in whatever document it was copied from
    and carry no mathematical content, so they survive into a context where the spacing
    they were compensating for no longer exists. One of them is also how the real corpus
    lost a set of `$$` delimiters: a `\\middle` next to a `\;` was split on its pipe.
    """
    for row, key, text in _latex_cells(context):
        found = sorted({command for command in _SPACING_COMMANDS if command in text})
        if found:
            yield finding(
                context,
                "LATEX_SPACING_COMMAND",
                f"cell uses manual spacing commands: {', '.join(found)}",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                commands=found,
            )


@rule(
    "LATEX_COMMAND_OUTSIDE_CONTAINER",
    severity=Severity.ERROR,
    category=IssueCategory.LATEX,
    description="A LaTeX command appears outside any `$$...$$` container.",
)
def latex_command_outside_container(context: RuleContext) -> Iterable[ValidationFinding]:
    """A command outside a container is printed literally.

    The student sees the characters `\\frac{1}{2}` where the author meant a fraction --
    and because the cell still *looks* like LaTeX to a curator skimming the spreadsheet,
    this is one of the defects most likely to survive a manual review.
    """
    for row, key, text in _latex_cells(context):
        outside = _outside_containers(text)
        commands = sorted(set(re.findall(r"\\[A-Za-z]+", outside)))
        if commands:
            yield finding(
                context,
                "LATEX_COMMAND_OUTSIDE_CONTAINER",
                f"the command(s) {', '.join(commands)} sit outside any `$$...$$` "
                "container and will be printed literally",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                commands=commands,
            )


@rule(
    "LATEX_SPLIT_EXPRESSION",
    severity=Severity.WARNING,
    category=IssueCategory.LATEX,
    description="One expression is split across two adjacent `$$...$$` containers.",
)
def latex_split_expression(context: RuleContext) -> Iterable[ValidationFinding]:
    """`$$a$$ $$= b$$` where `$$a = b$$` was meant.

    Two containers with nothing but whitespace between them are one expression that was
    cut in half, and the halves are typeset independently -- so the alignment and
    spacing the author was looking at do not survive.
    """
    for row, key, text in _latex_cells(context):
        if _ADJACENT_CONTAINERS.search(text) and not _EMPTY_CONTAINER.fullmatch(
            text.strip()
        ):
            yield finding(
                context,
                "LATEX_SPLIT_EXPRESSION",
                "two `$$...$$` containers sit next to each other; one expression split "
                "in half is typeset as two",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )


@rule(
    "LATEX_PROSE_INSIDE_CONTAINER",
    severity=Severity.WARNING,
    category=IssueCategory.LATEX,
    description="Explanatory prose appears inside a `$$...$$` container.",
)
def latex_prose_inside_container(context: RuleContext) -> Iterable[ValidationFinding]:
    """Sentences inside a math container are set in italics, letter by letter.

    Detected as a run of three or more ordinary words, which is deliberately
    conservative: `\\text{...}` is the correct way to put words inside mathematics and is
    excluded, and two words are not enough evidence -- `sin x` and `d theta` are
    mathematics that happens to be spelled with letters.
    """
    for row, key, text in _latex_cells(context):
        for match in _CONTAINER.finditer(text):
            inner = re.sub(r"\\text\{[^}]*\}", " ", match.group(1))
            inner = re.sub(r"\\[A-Za-z]+", " ", inner)
            run = _PROSE_RUN.search(inner)
            if run:
                yield finding(
                    context,
                    "LATEX_PROSE_INSIDE_CONTAINER",
                    f"the words {run.group(0)!r} sit inside a `$$...$$` container and "
                    "will be set as italic mathematics; use `\\text{...}` or move them "
                    "outside",
                    row=row.row,
                    column=FIXED_COLUMNS[key],
                    column_key=key,
                    prose=run.group(0),
                )
                break


@rule(
    "LATEX_UNNECESSARY_IN_ANSWER",
    severity=Severity.WARNING,
    category=IssueCategory.LATEX,
    description="An Answer is wrapped in `$$...$$` but contains no LaTeX.",
)
def latex_unnecessary_in_answer(context: RuleContext) -> Iterable[ValidationFinding]:
    """`$$2$$` where `2` was meant.

    Confined to `Answer` and `mcChoices`, which are compared character for character:
    delimiters that render identically still make a correct answer fail to match. A
    container holding an actual command is left alone -- there the delimiters are doing
    something.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        for key in (ColumnKey.ANSWER, ColumnKey.MC_CHOICES):
            text = row.get(key).strip()
            if not text or "$$" not in text:
                continue
            if "\\" in text or "^" in text or "_" in text:
                continue
            inner = " ".join(m.group(1) for m in _CONTAINER.finditer(text)).strip()
            if not inner:
                continue
            yield finding(
                context,
                "LATEX_UNNECESSARY_IN_ANSWER",
                f"the value {inner!r} is wrapped in `$$...$$` but contains no LaTeX; "
                "graded cells are compared character for character, so the delimiters "
                "make a correct answer fail to match",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                stripped=inner,
            )
