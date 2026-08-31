"""What each row type is required to carry, and forbidden from carrying."""

from __future__ import annotations

import re
from typing import Iterable

from ...models import (
    FIXED_COLUMNS,
    AnswerType,
    ColumnKey,
    IssueCategory,
    RowType,
    Severity,
    ValidationFinding,
)
from ..mathematics import UnparseableExpression, parse_expression
from .registry import RuleContext, finding, rule


@rule(
    "INVALID_ANSWER_TYPE",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="answerType is not numeric, algebra or mc.",
)
def invalid_answer_type(context: RuleContext) -> Iterable[ValidationFinding]:
    """`answerType` is a closed set of three values.

    Anything else is either a typo or displaced content -- the corpus contains both a
    literal `string` and a whole trigonometric identity sitting in this column -- and
    telling them apart is a judgment call for the council, not for this rule.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        text = row.get(ColumnKey.ANSWER_TYPE).strip()
        if text and row.answer_type is None:
            yield finding(
                context,
                "INVALID_ANSWER_TYPE",
                f"answerType {text!r} is not one of numeric, algebra, mc",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.ANSWER_TYPE],
                column_key=ColumnKey.ANSWER_TYPE,
                found=text,
            )


_VARIABLE_LEFT_HAND_SIDE = re.compile(
    r"(?:[A-Za-z]|\\(?:theta|alpha|beta|gamma|phi|lambda))"
)


@rule(
    "ANSWER_TYPE_MISMATCH",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="answerType is numeric although Answer contains a free variable.",
)
def answer_type_mismatch(context: RuleContext) -> Iterable[ValidationFinding]:
    """Catch the high-confidence semantic mismatch that cost the pilot a whole issue.

    This recognizes an explicit equation whose left side contains a variable, and a
    safely parsed expression with a genuine free symbol. It does not use a broad
    letters-means-algebra heuristic: fractions, radicals, constants and scientific
    notation can all be numeric, while LaTeX commands such as ``\\frac`` contain letters
    without containing variables.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.answer_type is not AnswerType.NUMERIC:
            continue
        answer = row.get(ColumnKey.ANSWER).strip().strip("$").strip()
        variable_equation = False
        if "=" in answer:
            left = answer.split("=", 1)[0]
            variable_equation = bool(_VARIABLE_LEFT_HAND_SIDE.search(left))

        free_variable_expression = False
        if not variable_equation:
            try:
                free_variable_expression = bool(parse_expression(answer).free_symbols)
            except UnparseableExpression:
                pass

        if not variable_equation and not free_variable_expression:
            continue
        yield finding(
            context,
            "ANSWER_TYPE_MISMATCH",
            "answerType is numeric but Answer contains a free variable",
            row=row.row,
            column=FIXED_COLUMNS[ColumnKey.ANSWER_TYPE],
            column_key=ColumnKey.ANSWER_TYPE,
            answer=row.get(ColumnKey.ANSWER),
            expected=AnswerType.ALGEBRA.value,
        )


@rule(
    "ANSWER_WITHOUT_TYPE",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A row carries an Answer but no answerType.",
)
def answer_without_type(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.row_type in (RowType.STEP, RowType.SCAFFOLD) and row.get(
            ColumnKey.ANSWER
        ).strip():
            if not row.get(ColumnKey.ANSWER_TYPE).strip():
                yield finding(
                    context,
                    "ANSWER_WITHOUT_TYPE",
                    "row has an Answer but no answerType, so it cannot be graded",
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.ANSWER_TYPE],
                    column_key=ColumnKey.ANSWER_TYPE,
                )


@rule(
    "STEP_MISSING_ANSWER",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A step row has no Answer.",
)
def step_missing_answer(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.STEP):
        if not row.get(ColumnKey.ANSWER).strip():
            yield finding(
                context,
                "STEP_MISSING_ANSWER",
                "step row has no Answer, so nothing can be marked correct",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.ANSWER],
                column_key=ColumnKey.ANSWER,
            )


@rule(
    "HINT_HAS_ANSWER",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A hint row carries an Answer, which only steps and scaffolds may.",
)
def hint_has_answer(context: RuleContext) -> Iterable[ValidationFinding]:
    """A hint explains; it does not grade.

    An answer on a hint row is frequently a scaffold mislabelled as a hint, so the
    finding names both readings rather than assuming the answer is the mistake.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.HINT):
        answer = row.get(ColumnKey.ANSWER).strip()
        if answer:
            yield finding(
                context,
                "HINT_HAS_ANSWER",
                (
                    f"hint row carries the Answer {answer!r}; either the answer does not "
                    "belong here or the row should be a scaffold"
                ),
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.ANSWER],
                column_key=ColumnKey.ANSWER,
                answer=answer,
            )


@rule(
    "HINT_MISSING_BODY",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A hint row has no Body Text.",
)
def hint_missing_body(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.HINT):
        if not row.get(ColumnKey.BODY_TEXT).strip():
            yield finding(
                context,
                "HINT_MISSING_BODY",
                "hint row has no Body Text, so it tells the student nothing",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.BODY_TEXT],
                column_key=ColumnKey.BODY_TEXT,
            )


@rule(
    "SCAFFOLD_MISSING_ANSWER",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A scaffold row has no Answer.",
)
def scaffold_missing_answer(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows_of_type(RowType.SCAFFOLD):
        if not row.get(ColumnKey.ANSWER).strip():
            yield finding(
                context,
                "SCAFFOLD_MISSING_ANSWER",
                "scaffold row has no Answer; a scaffold is a graded sub-question",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.ANSWER],
                column_key=ColumnKey.ANSWER,
            )


@rule(
    "PROBLEM_ROW_HAS_ANSWER",
    severity=Severity.WARNING,
    category=IssueCategory.ROW_TYPE,
    description="A problem row carries an Answer or answerType.",
)
def problem_row_has_answer(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for key in (ColumnKey.ANSWER, ColumnKey.ANSWER_TYPE):
        if block.problem_row.get(key).strip():
            yield finding(
                context,
                "PROBLEM_ROW_HAS_ANSWER",
                (
                    f"problem row carries {key.value.replace('_', ' ')}; the problem row "
                    "introduces the question and its steps hold the answers"
                ),
                row=block.start_row,
                column=FIXED_COLUMNS[key],
                column_key=key,
            )


@rule(
    "MC_TYPE_WITHOUT_CHOICES_COLUMN",
    severity=Severity.ERROR,
    category=IssueCategory.MULTIPLE_CHOICE,
    description="A row is typed mc but carries no mcChoices.",
)
def mc_type_without_choices(context: RuleContext) -> Iterable[ValidationFinding]:
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.answer_type is AnswerType.MC and not row.get(
            ColumnKey.MC_CHOICES
        ).strip():
            yield finding(
                context,
                "MC_TYPE_WITHOUT_CHOICES_COLUMN",
                "answerType is mc but mcChoices is empty",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                column_key=ColumnKey.MC_CHOICES,
            )


# --------------------------------------------------------------------------------------
# Required content, per row type
# --------------------------------------------------------------------------------------

#: What each row type must carry. Read from the written contract, and corroborated by
#: the corpus at 92-99% per field -- which is the useful check on a table like this: a
#: requirement the real workbooks satisfy almost always is one this system has read
#: correctly, while one they satisfy half the time means the reading is wrong and the
#: rule would bury every true finding beside it.
#:
#: A field with a rule of its own is deliberately absent here. `Body Text` on a hint is
#: `HINT_MISSING_BODY` and `Answer` on a step or scaffold has its own rule too; listing
#: them again would open two issues for one empty cell, which then race for the same
#: repair.
REQUIRED_CONTENT: dict[RowType, tuple[ColumnKey, ...]] = {
    RowType.STEP: (ColumnKey.TITLE,),
    RowType.HINT: (ColumnKey.TITLE,),
    RowType.SCAFFOLD: (ColumnKey.TITLE, ColumnKey.BODY_TEXT),
}

#: What each row type must **not** carry. A populated cell here is nearly always
#: displaced content rather than a deliberate addition -- which is why the messages say
#: what the value probably is, not merely that it should not be there.
#:
#: `mcChoices` is absent from every row here on purpose: `MC_CHOICES_ON_NON_MC_ROW`
#: already owns it, and owns it better, because it asks about `answerType` rather than
#: about the row type.
FORBIDDEN_CONTENT: dict[RowType, tuple[ColumnKey, ...]] = {
    RowType.PROBLEM: (ColumnKey.HINT_ID, ColumnKey.DEPENDENCY),
    RowType.STEP: (ColumnKey.HINT_ID,),
    RowType.HINT: (ColumnKey.ANSWER_TYPE,),
}

_WHAT_IT_IS_FOR = {
    ColumnKey.TITLE: "the question or heading a student reads",
    ColumnKey.BODY_TEXT: "the explanation a student reads",
}


@rule(
    "ROW_MISSING_REQUIRED_CONTENT",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A row is missing a field its row type requires.",
)
def row_missing_required_content(context: RuleContext) -> Iterable[ValidationFinding]:
    """A row that renders empty in the tutor.

    Every one of these is invisible in the spreadsheet -- an empty cell among hundreds
    of populated ones -- and unmissable to a student, who is shown a hint with nothing
    in it or a step with no question.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.is_blank or row.row_type is None:
            continue
        for key in REQUIRED_CONTENT.get(row.row_type, ()):
            if row.get(key).strip():
                continue
            yield finding(
                context,
                "ROW_MISSING_REQUIRED_CONTENT",
                f"{row.row_type.value} row has no {key.value.replace('_', ' ')}, "
                f"which is {_WHAT_IT_IS_FOR.get(key, 'required for this row type')}",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                row_type=row.row_type.value,
                missing=key.value,
            )


@rule(
    "ROW_HAS_FORBIDDEN_CONTENT",
    severity=Severity.ERROR,
    category=IssueCategory.ROW_TYPE,
    description="A row carries a field its row type must not have.",
)
def row_has_forbidden_content(context: RuleContext) -> Iterable[ValidationFinding]:
    """A populated cell where this row type has no use for one.

    Reported as a row-type defect rather than silently ignored, because the value is
    almost never harmless: an identifier on a step row, a dependency on a problem row
    and a choice list on a hint are all the shape the column-shift corruption takes, and
    a rule that shrugged at them would be the reason it stayed hidden.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows:
        if row.is_blank or row.row_type is None:
            continue
        for key in FORBIDDEN_CONTENT.get(row.row_type, ()):
            value = row.get(key).strip()
            if not value:
                continue
            yield finding(
                context,
                "ROW_HAS_FORBIDDEN_CONTENT",
                f"{row.row_type.value} row carries "
                f"{key.value.replace('_', ' ')} {value!r}, which belongs to another "
                "row type; displaced content is the usual cause",
                row=row.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                row_type=row.row_type.value,
                found=value,
            )


@rule(
    "ROW_MISSING_PROBLEM_NAME",
    severity=Severity.ERROR,
    category=IssueCategory.STRUCTURE,
    description="A populated row inside a block carries no Problem Name.",
)
def row_missing_problem_name(context: RuleContext) -> Iterable[ValidationFinding]:
    """Every populated row repeats its block's Problem Name.

    A row *disagreeing* with its block is the reader's business -- it bears on where the
    block boundaries are, so it is reported during parsing where that decision is made.
    A row that simply has none is this rule's: the block is unambiguous, one cell is
    empty, and the repair is exact.
    """
    block = context.block
    if block is None:
        return
    for row in block.rows[1:]:
        if row.is_blank or row.row_type is None:
            continue
        if not row.get(ColumnKey.PROBLEM_NAME).strip():
            yield finding(
                context,
                "ROW_MISSING_PROBLEM_NAME",
                f"row carries no Problem Name; the block is {block.problem_name!r}",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.PROBLEM_NAME],
                column_key=ColumnKey.PROBLEM_NAME,
                expected=block.problem_name,
            )
