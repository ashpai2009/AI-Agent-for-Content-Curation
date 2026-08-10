"""What each row type is required to carry, and forbidden from carrying."""

from __future__ import annotations

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
