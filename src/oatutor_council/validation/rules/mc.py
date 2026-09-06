"""Multiple-choice structure and the relationship between answer and distractors."""

from __future__ import annotations

from typing import Iterable

from ...models import (
    FIXED_COLUMNS,
    MAX_MC_CHOICES,
    MC_CHOICE_DELIMITER,
    MIN_MC_CHOICES,
    AnswerType,
    ColumnKey,
    IssueCategory,
    Severity,
    ValidationFinding,
)
from ..mathematics import MathVerdict, equations_equivalent
from .registry import RuleContext, finding, rule


def split_choices(text: str) -> list[str]:
    return [part.strip() for part in text.split(MC_CHOICE_DELIMITER)]


def _mc_rows(context: RuleContext):
    block = context.block
    if block is None:
        return
    for row in block.rows:
        choices_text = row.get(ColumnKey.MC_CHOICES)
        if choices_text.strip():
            yield row, choices_text


@rule(
    "MC_CHOICES_ON_NON_MC_ROW",
    severity=Severity.ERROR,
    category=IssueCategory.MULTIPLE_CHOICE,
    description="mcChoices is populated on a row whose answerType is not mc.",
)
def mc_choices_on_non_mc_row(context: RuleContext) -> Iterable[ValidationFinding]:
    for row, choices_text in _mc_rows(context):
        if row.answer_type is not AnswerType.MC:
            found = row.get(ColumnKey.ANSWER_TYPE).strip() or "empty"
            choices = split_choices(choices_text)
            answer = row.get(ColumnKey.ANSWER).strip()
            # A valid list containing the exact Answer once is affirmative evidence of
            # the intended interaction. Point at the cell that must change and preserve
            # the authored choices. When that evidence is absent, keep pointing at the
            # list: choosing between repairing/removing it remains a semantic decision.
            preserve_interaction = (
                MIN_MC_CHOICES <= len(choices) <= MAX_MC_CHOICES
                and bool(answer)
                and choices.count(answer) == 1
                and all(choices)
            )
            column_key = (
                ColumnKey.ANSWER_TYPE if preserve_interaction else ColumnKey.MC_CHOICES
            )
            yield finding(
                context,
                "MC_CHOICES_ON_NON_MC_ROW",
                f"mcChoices is populated but answerType is {found}",
                row=row.row,
                column=FIXED_COLUMNS[column_key],
                column_key=column_key,
                answer_type=found,
                expected=(AnswerType.MC.value if preserve_interaction else ""),
                preserve_interaction=preserve_interaction,
            )


@rule(
    "MC_CHOICE_COUNT",
    severity=Severity.ERROR,
    category=IssueCategory.MULTIPLE_CHOICE,
    description=f"A choice list has fewer than {MIN_MC_CHOICES} or more than {MAX_MC_CHOICES} choices.",
)
def mc_choice_count(context: RuleContext) -> Iterable[ValidationFinding]:
    for row, text in _mc_rows(context):
        count = len(split_choices(text))
        if not MIN_MC_CHOICES <= count <= MAX_MC_CHOICES:
            yield finding(
                context,
                "MC_CHOICE_COUNT",
                (
                    f"choice list has {count} choices; the rules allow "
                    f"{MIN_MC_CHOICES} to {MAX_MC_CHOICES}"
                ),
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                column_key=ColumnKey.MC_CHOICES,
                count=count,
            )


@rule(
    "MC_EMPTY_CHOICE",
    severity=Severity.ERROR,
    category=IssueCategory.MULTIPLE_CHOICE,
    description="A choice list contains an empty choice.",
)
def mc_empty_choice(context: RuleContext) -> Iterable[ValidationFinding]:
    """Usually a stray or doubled pipe, which is also how the `\\middle|` corruption
    begins -- so this reports the symptom and leaves the cause to the council."""
    for row, text in _mc_rows(context):
        empties = [index for index, choice in enumerate(split_choices(text)) if not choice]
        if empties:
            yield finding(
                context,
                "MC_EMPTY_CHOICE",
                f"choice list has empty choices at positions {empties}",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                column_key=ColumnKey.MC_CHOICES,
                positions=empties,
            )


@rule(
    "MC_DUPLICATE_CHOICE",
    severity=Severity.ERROR,
    category=IssueCategory.MULTIPLE_CHOICE,
    description="Two choices are textually identical.",
)
def mc_duplicate_choice(context: RuleContext) -> Iterable[ValidationFinding]:
    for row, text in _mc_rows(context):
        choices = split_choices(text)
        seen: dict[str, int] = {}
        for index, choice in enumerate(choices):
            if not choice:
                continue
            if choice in seen:
                yield finding(
                    context,
                    "MC_DUPLICATE_CHOICE",
                    f"choice {index} repeats choice {seen[choice]}: {choice!r}",
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                    column_key=ColumnKey.MC_CHOICES,
                    choice=choice,
                )
            else:
                seen[choice] = index


@rule(
    "MC_ANSWER_NOT_IN_CHOICES",
    severity=Severity.BLOCKING,
    category=IssueCategory.MULTIPLE_CHOICE,
    description="The Answer does not exactly match any choice.",
)
def mc_answer_not_in_choices(context: RuleContext) -> Iterable[ValidationFinding]:
    """The match must be exact, character for character -- that is how grading works.

    When a choice is mathematically equal but written differently the finding says so,
    because the repair is completely different: one is a missing choice, the other is a
    formatting mismatch, and confusing them produces a wrong fix that still passes a
    casual read.
    """
    for row, text in _mc_rows(context):
        answer = row.get(ColumnKey.ANSWER).strip()
        if not answer:
            continue
        choices = split_choices(text)
        if answer in choices:
            continue

        equivalent_at = [
            index
            for index, choice in enumerate(choices)
            if choice and equations_equivalent(answer, choice) is MathVerdict.EQUIVALENT
        ]
        detail = (
            f"; choice {equivalent_at[0]} is mathematically equal but written differently"
            if equivalent_at
            else ""
        )
        yield finding(
            context,
            "MC_ANSWER_NOT_IN_CHOICES",
            f"Answer {answer!r} matches no choice exactly{detail}",
            row=row.row,
            column=FIXED_COLUMNS[ColumnKey.ANSWER],
            column_key=ColumnKey.ANSWER,
            answer=answer,
            choices=choices,
            equivalent_choice_indexes=equivalent_at,
        )


@rule(
    "MC_DISTRACTOR_EQUALS_ANSWER",
    severity=Severity.ERROR,
    category=IssueCategory.MATHEMATICS,
    description="A distractor is mathematically equivalent to the answer.",
)
def mc_distractor_equals_answer(context: RuleContext) -> Iterable[ValidationFinding]:
    """A distractor equal to the answer makes the question unanswerable.

    Only fires on a definite `EQUIVALENT`. An `UNKNOWN` -- an expression the gate could
    not parse -- is not evidence of anything, and reporting one would spend a repair
    attempt on a problem that may be perfectly correct.
    """
    for row, text in _mc_rows(context):
        answer = row.get(ColumnKey.ANSWER).strip()
        if not answer:
            continue
        choices = split_choices(text)
        for index, choice in enumerate(choices):
            if not choice or choice == answer:
                continue
            if equations_equivalent(answer, choice) is MathVerdict.EQUIVALENT:
                yield finding(
                    context,
                    "MC_DISTRACTOR_EQUALS_ANSWER",
                    (
                        f"distractor {index} ({choice!r}) is mathematically equal to the "
                        f"answer {answer!r}, so the question has two correct choices"
                    ),
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                    column_key=ColumnKey.MC_CHOICES,
                    choice_index=index,
                    choice=choice,
                )


@rule(
    "MC_ANSWER_IS_FIRST_CHOICE",
    severity=Severity.OBSERVATION,
    category=IssueCategory.MULTIPLE_CHOICE,
    description="The correct answer is the first choice.",
    repairable=False,
)
def mc_answer_is_first_choice(context: RuleContext) -> Iterable[ValidationFinding]:
    """Reported, never corrected.

    Answer-first is common in some real workbooks and rare in others, so it is not a
    convention. More to the point, the written rules require the answer to match a choice
    exactly and never require shuffling -- reordering choices would be this system
    inventing a requirement nobody stated.
    """
    for row, text in _mc_rows(context):
        answer = row.get(ColumnKey.ANSWER).strip()
        choices = split_choices(text)
        if answer and len(choices) > 1 and choices[0] == answer:
            yield finding(
                context,
                "MC_ANSWER_IS_FIRST_CHOICE",
                "the correct answer is the first choice (observation only, not corrected)",
                row=row.row,
                column=FIXED_COLUMNS[ColumnKey.MC_CHOICES],
                column_key=ColumnKey.MC_CHOICES,
            )
