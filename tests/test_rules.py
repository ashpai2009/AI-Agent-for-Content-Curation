"""Rule engine tests.

Each test isolates one rule with `only=`, because almost any fixture trips several rules
at once and an assertion over the whole finding set would break every time an unrelated
rule was added.

The negative cases matter as much as the positive ones. A rule that fires on correct
content costs a repair attempt and a reviewer round on a problem that was never wrong.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import cells, hint, problem, scaffold, step
from oatutor_council.models import Severity
from oatutor_council.validation.rules import REGISTRY, describe_rules, run_rules
from oatutor_council.workbook.reader import read_workbook


def codes_for(path, rule_code: str) -> list[str]:
    return [f.code for f in run_rules(read_workbook(path), only={rule_code})]


def findings_for(path, rule_code: str):
    return run_rules(read_workbook(path), only={rule_code})


def test_every_rule_has_a_distinct_code_and_a_description():
    rules = describe_rules()
    assert len({r.code for r in rules}) == len(rules)
    assert all(r.description.strip() for r in rules)


def test_running_no_rules_is_possible():
    """`only=` with an unknown code must return nothing rather than everything --
    a filter that silently falls back to 'all' would make targeted reruns meaningless."""
    assert REGISTRY and "NOT_A_RULE" not in REGISTRY


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------


def test_duplicate_problem_name(make_workbook):
    path = make_workbook([problem("angles1"), step("angles1"), problem("angles1")])
    findings = findings_for(path, "DUPLICATE_PROBLEM_NAME")
    assert len(findings) == 1
    assert findings[0].detail["first_use_row"] == 2


def test_distinct_problem_names_are_fine(make_workbook):
    path = make_workbook([problem("angles1"), problem("angles2")])
    assert codes_for(path, "DUPLICATE_PROBLEM_NAME") == []


def test_block_with_no_step(make_workbook):
    path = make_workbook([problem("angles1"), hint("angles1", "h1", body="try this")])
    assert codes_for(path, "BLOCK_HAS_NO_STEP") == ["BLOCK_HAS_NO_STEP"]


def test_metadata_below_the_problem_row_is_reported(make_workbook):
    """Also the signature a shift leaves behind, which is why it is reported rather
    than tidied: the useful repair is often to the shift, not the stray cell."""
    path = make_workbook(
        [
            problem("angles1", license="CC-BY"),
            cells(problem_name="angles1", row_type="step", answer="1", license="CC-BY"),
        ]
    )
    findings = findings_for(path, "METADATA_ON_NON_PROBLEM_ROW")
    assert [f.row for f in findings] == [3]


# --------------------------------------------------------------------------------------
# Row types
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["string", "cos((theta))**2=1", "numerical"])
def test_invalid_answer_type(make_workbook, value):
    """The corpus contains a literal `string` and a whole trigonometric identity in this
    column. Both are outside the closed set of three."""
    path = make_workbook([problem("a1"), step("a1", answer="1", answer_type=value)])
    assert codes_for(path, "INVALID_ANSWER_TYPE") == ["INVALID_ANSWER_TYPE"]


@pytest.mark.parametrize("value", ["numeric", "algebra", "mc"])
def test_valid_answer_types_are_accepted(make_workbook, value):
    path = make_workbook(
        [problem("a1"), step("a1", answer="1", answer_type=value, mc_choices="1|2")]
    )
    assert codes_for(path, "INVALID_ANSWER_TYPE") == []


def test_step_without_an_answer(make_workbook):
    path = make_workbook([problem("a1"), cells(problem_name="a1", row_type="step")])
    assert codes_for(path, "STEP_MISSING_ANSWER") == ["STEP_MISSING_ANSWER"]


def test_hint_carrying_an_answer(make_workbook):
    """A real prior-tool finding: `... is "hint" but has answer`. The message names both
    readings, since a mislabelled scaffold is as likely as a stray answer."""
    path = make_workbook(
        [
            problem("a1"),
            step("a1"),
            cells(problem_name="a1", row_type="hint", body_text="x", answer="4"),
        ]
    )
    findings = findings_for(path, "HINT_HAS_ANSWER")
    assert len(findings) == 1
    assert "should be a scaffold" in findings[0].message


def test_hint_without_a_body(make_workbook):
    path = make_workbook([problem("a1"), step("a1"), hint("a1", "h1")])
    assert codes_for(path, "HINT_MISSING_BODY") == ["HINT_MISSING_BODY"]


def test_answer_without_a_type(make_workbook):
    path = make_workbook(
        [problem("a1"), cells(problem_name="a1", row_type="step", answer="1")]
    )
    assert codes_for(path, "ANSWER_WITHOUT_TYPE") == ["ANSWER_WITHOUT_TYPE"]


# --------------------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------------------


def test_unresolved_dependency(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "s1", dependency="s9")]
    )
    findings = findings_for(path, "DEPENDENCY_UNRESOLVED")
    assert findings[0].detail["reference"] == "s9"


def test_resolved_dependency_is_silent(make_workbook):
    path = make_workbook(
        [
            problem("a1"),
            step("a1"),
            scaffold("a1", "s1", answer="1"),
            scaffold("a1", "s2", answer="2", dependency="s1"),
        ]
    )
    assert codes_for(path, "DEPENDENCY_UNRESOLVED") == []


def test_self_dependency(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "s1", dependency="s1")]
    )
    assert codes_for(path, "DEPENDENCY_ON_SELF") == ["DEPENDENCY_ON_SELF"]


def test_duplicate_identifier(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "s1"), scaffold("a1", "s1")]
    )
    assert codes_for(path, "DUPLICATE_IDENTIFIER") == ["DUPLICATE_IDENTIFIER"]


def test_malformed_identifier(make_workbook):
    path = make_workbook([problem("a1"), step("a1"), scaffold("a1", "scaffold-one")])
    assert codes_for(path, "IDENTIFIER_MALFORMED") == ["IDENTIFIER_MALFORMED"]


def test_a_consistent_alternative_namespace_is_a_warning_not_an_error(make_workbook):
    """Six of eleven real workbooks use `h` throughout. Enforcing `s` literally would
    flag every scaffold in all six."""
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "h1"), scaffold("a1", "h2")]
    )
    findings = findings_for(path, "SCAFFOLD_NAMESPACE_DEVIATION")
    assert len(findings) == 2
    assert all(f.severity is Severity.WARNING for f in findings)
    assert all(f.detail["workbook_is_consistent"] for f in findings)


def test_a_mixed_namespace_workbook_keeps_the_error(make_workbook):
    """Two real workbooks mix `s` and `h`. Nothing there is a convention."""
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "s1"), scaffold("a1", "h2")]
    )
    findings = findings_for(path, "SCAFFOLD_NAMESPACE_DEVIATION")
    assert [f.severity for f in findings] == [Severity.ERROR]


def test_the_specified_namespace_produces_no_finding(make_workbook):
    path = make_workbook([problem("a1"), step("a1"), scaffold("a1", "s1")])
    assert codes_for(path, "SCAFFOLD_NAMESPACE_DEVIATION") == []


def test_numbering_gap(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "s1"), scaffold("a1", "s4")]
    )
    findings = findings_for(path, "DEPENDENCY_NUMBERING_GAP")
    assert findings[0].detail == {"previous": 1, "current": 4}


def test_contiguous_numbering_is_silent(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1"), scaffold("a1", "s1"), scaffold("a1", "s2")]
    )
    assert codes_for(path, "DEPENDENCY_NUMBERING_GAP") == []


# --------------------------------------------------------------------------------------
# Notation
# --------------------------------------------------------------------------------------


def test_date_coercion_suggests_the_fraction_and_ignores_the_year(make_workbook):
    """The year records when the file was edited -- the corpus carries two different
    ones -- so the repair is built from month and day alone."""
    path = make_workbook(
        [problem("a1"), step("a1", answer=datetime(2026, 1, 2), answer_type="numeric")]
    )
    findings = findings_for(path, "DATE_COERCION")
    assert findings[0].detail["suggested"] == "1/2"
    assert findings[0].severity is Severity.BLOCKING


def test_time_last_checked_is_exempt_from_date_coercion(make_workbook):
    """That column legitimately holds a datetime. Flagging it would put every real
    workbook into the repair loop for a cell that is correct."""
    row = [None] * 20
    row[0], row[1], row[19] = "a1", "step", datetime(2026, 7, 29, 10, 7)
    path = make_workbook([problem("a1"), row])
    assert codes_for(path, "DATE_COERCION") == []


def test_unicode_glyphs_in_an_ascii_workbook(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer="θ + π", answer_type="algebra")]
    )
    findings = findings_for(path, "NON_ASCII_MATH")
    assert set(findings[0].detail["glyphs"]) == {"π", "θ"}


def test_unicode_rule_does_not_apply_to_a_latex_workbook(make_workbook):
    """A LaTeX workbook renders its own symbols and has no business being told to spell
    out pi."""
    path = make_workbook(
        [
            problem("a1", body=r"$$\frac{\pi}{2}$$"),
            step("a1", answer=r"$$\theta°$$", answer_type="algebra"),
        ]
    )
    assert codes_for(path, "NON_ASCII_MATH") == []


def test_caret_exponent(make_workbook):
    path = make_workbook([problem("a1"), step("a1", answer="x^2", answer_type="algebra")])
    assert codes_for(path, "CARET_EXPONENT") == ["CARET_EXPONENT"]


def test_whitespace_padding(make_workbook):
    """Invisible in the spreadsheet and fatal to the exact match multiple-choice
    grading depends on."""
    path = make_workbook([problem("a1"), step("a1", answer=" 1/2 ")])
    findings = findings_for(path, "WHITESPACE_PADDING")
    assert findings[0].detail["stripped"] == "1/2"


# --------------------------------------------------------------------------------------
# Multiple choice
# --------------------------------------------------------------------------------------


def mc_step(**kwargs):
    return step("a1", answer_type="mc", **kwargs)


def test_answer_must_match_a_choice_exactly(make_workbook):
    path = make_workbook([problem("a1"), mc_step(answer="1/2", mc_choices="1/3|1/4")])
    findings = findings_for(path, "MC_ANSWER_NOT_IN_CHOICES")
    assert findings[0].detail["equivalent_choice_indexes"] == []


def test_an_equivalent_but_differently_written_choice_is_named_as_such(make_workbook):
    """The repair differs completely: one case is a missing choice, the other a
    formatting mismatch. Confusing them yields a wrong fix that still reads well."""
    path = make_workbook([problem("a1"), mc_step(answer="1/2", mc_choices="0.5|1/4")])
    findings = findings_for(path, "MC_ANSWER_NOT_IN_CHOICES")
    assert findings[0].detail["equivalent_choice_indexes"] == [0]
    assert "mathematically equal" in findings[0].message


def test_an_exact_match_is_silent(make_workbook):
    path = make_workbook([problem("a1"), mc_step(answer="1/2", mc_choices="1/2|1/4")])
    assert codes_for(path, "MC_ANSWER_NOT_IN_CHOICES") == []


def test_a_distractor_equal_to_the_answer(make_workbook):
    path = make_workbook([problem("a1"), mc_step(answer="1/2", mc_choices="1/2|0.5|1/4")])
    findings = findings_for(path, "MC_DISTRACTOR_EQUALS_ANSWER")
    assert findings[0].detail["choice_index"] == 1


def test_an_unparseable_distractor_is_not_reported(make_workbook):
    """`UNKNOWN` is not evidence of a defect. Reporting one would spend a repair attempt
    on a problem that may be perfectly correct."""
    path = make_workbook(
        [problem("a1"), mc_step(answer="1/2", mc_choices="1/2|none of these|1/4")]
    )
    assert codes_for(path, "MC_DISTRACTOR_EQUALS_ANSWER") == []


@pytest.mark.parametrize("choices", ["only-one", "a|b|c|d|e|f"])
def test_choice_count_outside_the_allowed_range(make_workbook, choices):
    path = make_workbook([problem("a1"), mc_step(answer="a", mc_choices=choices)])
    assert codes_for(path, "MC_CHOICE_COUNT") == ["MC_CHOICE_COUNT"]


def test_choices_on_a_non_mc_row(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer="1", answer_type="numeric", mc_choices="1|2")]
    )
    assert codes_for(path, "MC_CHOICES_ON_NON_MC_ROW") == ["MC_CHOICES_ON_NON_MC_ROW"]


def test_duplicate_and_empty_choices(make_workbook):
    path = make_workbook([problem("a1"), mc_step(answer="a", mc_choices="a|a||b")])
    assert codes_for(path, "MC_DUPLICATE_CHOICE") == ["MC_DUPLICATE_CHOICE"]
    assert findings_for(path, "MC_EMPTY_CHOICE")[0].detail["positions"] == [2]


def test_answer_first_is_an_observation_and_not_repairable(make_workbook):
    """The written rules require an exact match and never require shuffling. Reordering
    choices would be this system inventing a requirement nobody stated."""
    path = make_workbook([problem("a1"), mc_step(answer="1/2", mc_choices="1/2|1/4")])
    findings = findings_for(path, "MC_ANSWER_IS_FIRST_CHOICE")
    assert findings[0].severity is Severity.OBSERVATION
    assert not findings[0].repairable


# --------------------------------------------------------------------------------------
# LaTeX
# --------------------------------------------------------------------------------------


def test_unbalanced_latex_delimiters(make_workbook):
    path = make_workbook(
        [problem("a1", body=r"$$\frac{1}{2}"), step("a1", answer=r"$$x$$")]
    )
    assert codes_for(path, "LATEX_DELIMITER_UNBALANCED") == ["LATEX_DELIMITER_UNBALANCED"]


def test_the_pipe_corruption_is_found_although_the_cell_balances(make_workbook):
    """The whole cell has an even `$` count, so a cell-level balance check sees nothing.
    Only splitting on the pipe reveals it."""
    corrupted = (
        r"$$$$\alpha \;\middle$$|$$\; \beta \;\middle$$|$$\; \gamma$$$$"
    )
    path = make_workbook(
        [problem("a1", body="$$x$$"), step("a1", answer_type="mc", mc_choices=corrupted)]
    )
    # The whole cell balances; only the individual parts do not. Counting single `$`
    # characters finds nothing here, which is why the rule counts `$$` tokens.
    assert corrupted.count("$$") % 2 == 0, "fixture must balance at the cell level"
    assert all(part.count("$") % 2 == 0 for part in corrupted.split("|"))
    findings = findings_for(path, "MC_LATEX_PIPE_CORRUPTION")
    assert findings[0].detail["broken_choice_indexes"]
    assert findings[0].severity is Severity.BLOCKING


def test_double_escaped_backslash(make_workbook):
    path = make_workbook(
        [problem("a1", body=r"$$\\theta$$"), step("a1", answer=r"$$x$$")]
    )
    assert codes_for(path, "DOUBLE_ESCAPED_BACKSLASH") == ["DOUBLE_ESCAPED_BACKSLASH"]


def test_banned_latex_command(make_workbook):
    """A workbook cell is untrusted input rendered into a page."""
    path = make_workbook(
        [problem("a1", body=r"$$\input{/etc/passwd}$$"), step("a1", answer=r"$$x$$")]
    )
    findings = findings_for(path, "LATEX_BANNED_COMMAND")
    assert findings[0].detail["commands"] == ["\\input"]
    assert not findings[0].repairable


def test_latex_in_an_ascii_workbook(make_workbook):
    # LaTeX in the body of a workbook whose answers are plainly ASCII.
    path = make_workbook(
        [problem("a1", body=r"$$\frac{1}{2}$$"), step("a1", answer="0.5")]
    )
    assert codes_for(path, "LATEX_IN_ASCII_WORKBOOK") == ["LATEX_IN_ASCII_WORKBOOK"]


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------


def test_findings_are_returned_in_document_order(make_workbook):
    """The order rules import in is an implementation detail and must not be visible to
    a curator reading the report."""
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="x^2", answer_type="algebra"),
            hint("a1", "h1"),
            problem("a2"),
            step("a2", answer=" 3 "),
        ]
    )
    rows = [f.row for f in run_rules(read_workbook(path)) if f.row is not None]
    assert rows == sorted(rows)
