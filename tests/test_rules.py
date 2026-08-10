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
# Dependencies: the two conventions
# --------------------------------------------------------------------------------------


def reset_per_step_workbook(make_workbook, **overrides):
    """Two steps, each with its own `h1`/`h2` ladder. Correct under `reset_per_step`.

    Two populated steps are the minimum evidence the detector accepts, which is why the
    fixture has two: with one it would come back `undecided` and the convention-dependent
    rules would take their block-wide branch.
    """
    return make_workbook(
        [
            problem("a1", title="T", oer_src="s", license="CC"),
            step("a1", answer="1"),
            hint("a1", "h1", body="first"),
            hint("a1", "h2", body="second", dependency="h1"),
            step("a1", answer="2"),
            hint("a1", "h1", body="first again"),
            hint("a1", "h2", body="second again", dependency="h1"),
        ]
    )


def test_reset_per_step_numbering_is_not_a_duplicate(make_workbook):
    """The reported false positive, and the largest single source of noise the rules
    produced: a block-wide uniqueness check reports a duplicate for every step after the
    first in a workbook whose whole convention is to start again at `h1`."""
    path = reset_per_step_workbook(make_workbook)
    parsed = read_workbook(path)
    assert parsed.conventions.dependency_convention.value == "reset_per_step"
    assert codes_for(path, "DUPLICATE_IDENTIFIER") == []


def test_a_repeated_identifier_within_one_step_is_still_a_duplicate(make_workbook):
    """Scoping the check to the step is not the same as switching it off."""
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h1", body="y"),
            step("a1", answer="2"),
            hint("a1", "h1", body="z"),
        ]
    )
    assert codes_for(path, "DUPLICATE_IDENTIFIER") == ["DUPLICATE_IDENTIFIER"]


def continuous_workbook(make_workbook, tail):
    """A block whose numbering climbs across steps, plus whatever `tail` is under test.

    The leading block is what makes the detector answer `continuous`; the tail block
    carries the defect. They are separate because a block containing the defect usually
    stops voting for either convention, and a fixture that argued for continuous *and*
    demonstrated the defect in the same block would be doing two jobs badly.
    """
    return make_workbook(
        [
            problem("a1", title="T", oer_src="s", license="CC"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y", dependency="h1"),
            step("a1", answer="2"),
            hint("a1", "h3", body="z"),
            hint("a1", "h4", body="w", dependency="h3"),
            *tail,
        ]
    )


def test_a_repeated_identifier_is_a_duplicate_under_the_continuous_convention(
    make_workbook,
):
    """Where the numbering is supposed to keep climbing, `h5` recurring really is one."""
    path = continuous_workbook(
        make_workbook,
        [
            problem("a2"),
            step("a2", answer="1"),
            hint("a2", "h5", body="x"),
            step("a2", answer="2"),
            hint("a2", "h6", body="y"),
            hint("a2", "h5", body="repeat", dependency="h6"),
        ],
    )
    parsed = read_workbook(path)
    assert parsed.conventions.dependency_convention.value == "continuous"
    assert codes_for(path, "DUPLICATE_IDENTIFIER") == ["DUPLICATE_IDENTIFIER"]


def test_a_dependency_resolves_within_its_own_step(make_workbook):
    """Resolving block-wide would be laxer *and* wronger: under reset-per-step a
    dependency on `h1` written under step two resolves against step one's row and looks
    fine while pointing at a hint the student will never have seen there."""
    path = make_workbook(
        [
            problem("a1", title="T", oer_src="s", license="CC"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y", dependency="h1"),
            step("a1", answer="2"),
            hint("a1", "h1", body="z"),
            hint("a1", "h2", body="w", dependency="h1"),
            step("a1", answer="3"),
            scaffold("a1", "s1", answer="1", dependency="h2"),
        ]
    )
    findings = findings_for(path, "DEPENDENCY_UNRESOLVED")
    assert [f.row for f in findings] == [10]


def test_a_reset_per_step_reference_is_not_read_as_pointing_forwards(make_workbook):
    """A block-wide map keeps whichever `h1` came last, so every earlier reference looks
    like it points down the sheet. That detail alone produced 187 findings against real
    workbooks doing nothing wrong."""
    path = reset_per_step_workbook(make_workbook)
    assert codes_for(path, "DEPENDENCY_ON_LATER_ROW") == []


def test_a_dependency_on_a_row_below_is_reported(make_workbook):
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x", dependency="h2"),
            hint("a1", "h2", body="y"),
        ]
    )
    assert codes_for(path, "DEPENDENCY_ON_LATER_ROW") == ["DEPENDENCY_ON_LATER_ROW"]


# --------------------------------------------------------------------------------------
# Dependencies: the chain
# --------------------------------------------------------------------------------------


def test_the_first_hint_of_every_step_depends_on_nothing(make_workbook):
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x", dependency="h9"),
        ]
    )
    assert codes_for(path, "FIRST_HINT_HAS_DEPENDENCY") == ["FIRST_HINT_HAS_DEPENDENCY"]


def test_a_hint_depends_on_the_hint_before_it(make_workbook):
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y"),  # no dependency: releases both at once
        ]
    )
    findings = findings_for(path, "HINT_DEPENDENCY_NOT_PREVIOUS")
    assert [f.detail["expected"] for f in findings] == ["h1"]


def test_a_correct_hint_ladder_is_silent(make_workbook):
    path = reset_per_step_workbook(make_workbook)
    assert codes_for(path, "HINT_DEPENDENCY_NOT_PREVIOUS") == []
    assert codes_for(path, "FIRST_HINT_HAS_DEPENDENCY") == []


def test_a_scaffold_depends_on_the_hint_above_it(make_workbook):
    """Several scaffolds following one hint all name that same hint -- which is why
    they are not a chain, and why threading them into the hint sequence produced
    hundreds of findings on workbooks that were doing it right."""
    path = make_workbook(
        [
            problem("a1", title="T", oer_src="s", license="CC"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y", dependency="h1"),
            scaffold("a1", "s1", answer="1", dependency="h2"),
            scaffold("a1", "s2", answer="2", dependency="h2"),
            scaffold("a1", "s3", answer="3", dependency="h2"),
        ]
    )
    assert codes_for(path, "SCAFFOLD_DEPENDENCY_NOT_HINT") == []


def test_a_scaffold_pointing_at_the_wrong_hint_is_reported(make_workbook):
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y", dependency="h1"),
            scaffold("a1", "s1", answer="1", dependency="h1"),
        ]
    )
    findings = findings_for(path, "SCAFFOLD_DEPENDENCY_NOT_HINT")
    assert [(f.detail["expected"], f.detail["found"]) for f in findings] == [("h2", "h1")]


def test_a_scaffold_with_no_hint_above_it_depends_on_nothing(make_workbook):
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            scaffold("a1", "s1", answer="1", dependency="h1"),
        ]
    )
    assert codes_for(path, "SCAFFOLD_DEPENDENCY_NOT_HINT") == [
        "SCAFFOLD_DEPENDENCY_NOT_HINT"
    ]


# --------------------------------------------------------------------------------------
# Dependencies: shape of the cell
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["h1,h2", "h1;h2", "h1|h2", "h1 and h2", "h1&h2"])
def test_more_than_one_dependency_per_cell_is_refused(make_workbook, value):
    """The tutor reads the cell as a single identifier, so a list resolves to nothing
    and the prerequisite silently never fires."""
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y", dependency="h1"),
            hint("a1", "h3", body="z", dependency=value),
        ]
    )
    assert codes_for(path, "DEPENDENCY_MULTIPLE") == ["DEPENDENCY_MULTIPLE"]


def test_a_multi_dependency_cell_produces_exactly_one_finding(make_workbook):
    """The other rules stand aside so a comma is one clear defect rather than three
    overlapping ones competing to describe it."""
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="1"),
            hint("a1", "h1", body="x"),
            hint("a1", "h2", body="y", dependency="h1,h9"),
        ]
    )
    parsed = read_workbook(path)
    on_that_row = [f.code for f in run_rules(parsed) if f.row == 5 and "DEPEND" in f.code]
    assert on_that_row == ["DEPENDENCY_MULTIPLE"]


def test_a_step_row_carries_no_dependency(make_workbook):
    """Steps are ordered by position. A dependency there is inert at best, and is a
    common symptom of the column-shift corruption."""
    path = make_workbook(
        [problem("a1"), step("a1", answer="1", dependency="h1")]
    )
    assert codes_for(path, "STEP_HAS_DEPENDENCY") == ["STEP_HAS_DEPENDENCY"]


def test_a_dependency_into_another_step_is_reported_under_the_continuous_convention(
    make_workbook,
):
    """Only meaningful where identifiers are block-unique. Under reset-per-step the same
    text names this step's own `h1` and is not a cross-step reference at all."""
    path = continuous_workbook(
        make_workbook,
        [
            problem("a2"),
            step("a2", answer="1"),
            hint("a2", "h5", body="x"),
            step("a2", answer="2"),
            hint("a2", "h6", body="y"),
            # `h5` belongs to the previous step: a student here has not seen it.
            hint("a2", "h7", body="z", dependency="h5"),
        ],
    )
    parsed = read_workbook(path)
    assert parsed.conventions.dependency_convention.value == "continuous"
    assert codes_for(path, "DEPENDENCY_CROSSES_STEP") == ["DEPENDENCY_CROSSES_STEP"]


def test_the_same_reference_is_not_a_cross_step_under_reset_per_step(make_workbook):
    """There it names this step's own `h1`, and `DEPENDENCY_UNRESOLVED` already covers
    the case where no such row exists."""
    path = reset_per_step_workbook(make_workbook)
    assert codes_for(path, "DEPENDENCY_CROSSES_STEP") == []


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


def test_unicode_is_checked_in_a_latex_workbook_too(make_workbook):
    """LaTeX renders `\\theta`; it does not render a literal θ any better than the ASCII
    convention does. Skipping the check there left the one LaTeX workbook in the corpus
    unexamined for the defect it is most likely to have, since its content came from
    rendered output in the first place."""
    path = make_workbook(
        [
            problem("a1", body=r"$$\frac{\pi}{2}$$"),
            step("a1", answer=r"$$\theta°$$", answer_type="algebra"),
        ]
    )
    findings = findings_for(path, "NON_ASCII_MATH")
    assert [f.detail["glyphs"] for f in findings] == [["°"]]


def test_any_non_ascii_character_is_detected_not_just_a_known_glyph_list(make_workbook):
    """A list catches the characters someone thought of. The ones that reach a workbook
    are the ones nobody did -- a non-breaking space, a Unicode minus that looks exactly
    like a hyphen, a smart quote."""
    path = make_workbook(
        [
            problem("a1"),
            step("a1", answer="x−1", answer_type="algebra"),  # U+2212 MINUS SIGN
        ]
    )
    findings = findings_for(path, "NON_ASCII_MATH")
    assert len(findings) == 1
    assert "U+2212" in findings[0].message or "'-'" in findings[0].message


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


# --------------------------------------------------------------------------------------
# Row types: required and forbidden content
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row,missing",
    [
        (lambda: step("a1", title=""), "title"),
        (lambda: hint("a1", "h1", body="x", title=""), "title"),
        (lambda: scaffold("a1", "s1", answer="1", title=""), "title"),
        (lambda: scaffold("a1", "s1", answer="1", body_text=""), "body_text"),
    ],
)
def test_a_row_missing_content_its_type_requires(make_workbook, row, missing):
    """Every one of these is invisible in the spreadsheet and unmissable to a student,
    who is shown a hint with nothing in it or a step with no question."""
    path = make_workbook([problem("a1"), row()])
    findings = findings_for(path, "ROW_MISSING_REQUIRED_CONTENT")
    assert [f.detail["missing"] for f in findings] == [missing]


def test_a_well_formed_row_requires_nothing(make_workbook):
    path = make_workbook(
        [problem("a1", title="T"), step("a1"), scaffold("a1", "s1", answer="1")]
    )
    assert codes_for(path, "ROW_MISSING_REQUIRED_CONTENT") == []


@pytest.mark.parametrize(
    "rows,column",
    [
        ([problem("a1", title="T", hint_id="h1"), step("a1")], "hint_id"),
        ([problem("a1", title="T", dependency="h1"), step("a1")], "dependency"),
        ([problem("a1", title="T"), step("a1", hint_id="h1")], "hint_id"),
        (
            [
                problem("a1", title="T"),
                step("a1"),
                hint("a1", "h1", body="x", answer_type="numeric"),
            ],
            "answer_type",
        ),
    ],
)
def test_a_row_carrying_content_its_type_forbids(make_workbook, rows, column):
    """An identifier on a step and a dependency on a problem row are the shape the
    column-shift corruption takes, which is why they are reported rather than ignored."""
    path = make_workbook(rows)
    findings = findings_for(path, "ROW_HAS_FORBIDDEN_CONTENT")
    assert [f.column_key.value for f in findings] == [column]


def test_mc_choices_are_left_to_the_rule_that_owns_them(make_workbook):
    """`MC_CHOICES_ON_NON_MC_ROW` asks about answerType, which is the better question.
    Listing mcChoices here too would open two issues for one cell."""
    path = make_workbook(
        [problem("a1", title="T"), step("a1"), hint("a1", "h1", body="x", mc_choices="a|b")]
    )
    assert codes_for(path, "ROW_HAS_FORBIDDEN_CONTENT") == []
    assert codes_for(path, "MC_CHOICES_ON_NON_MC_ROW") == ["MC_CHOICES_ON_NON_MC_ROW"]


def test_a_row_with_no_problem_name(make_workbook):
    path = make_workbook(
        [problem("a1", title="T"), cells(row_type="step", answer="1", answer_type="numeric", title="T")]
    )
    findings = findings_for(path, "ROW_MISSING_PROBLEM_NAME")
    assert [f.detail["expected"] for f in findings] == ["a1"]


# --------------------------------------------------------------------------------------
# Spacing and expected ASCII forms
# --------------------------------------------------------------------------------------


def test_operator_spacing_in_a_graded_cell(make_workbook):
    path = make_workbook([problem("a1"), step("a1", answer="x + 1", answer_type="algebra")])
    findings = findings_for(path, "OPERATOR_SPACING")
    assert findings[0].detail["operator"] == "+"


def test_operator_spacing_is_not_reported_in_prose(make_workbook):
    """Title and Body Text are English, where a spaced minus sign is a dash."""
    path = make_workbook(
        [problem("a1", title="Convert 30 - 45 degrees", body="Use pi / 180 as a guide.")]
    )
    assert codes_for(path, "OPERATOR_SPACING") == []


@pytest.mark.parametrize("value", ["a\tb", "a\nb", "a  b"])
def test_irregular_whitespace(make_workbook, value):
    path = make_workbook([problem("a1"), step("a1", answer=value, answer_type="algebra")])
    assert codes_for(path, "IRREGULAR_WHITESPACE") == ["IRREGULAR_WHITESPACE"]


def test_ordinary_single_spaces_are_not_irregular(make_workbook):
    path = make_workbook([problem("a1", title="Convert the angle", body="One space only.")])
    assert codes_for(path, "IRREGULAR_WHITESPACE") == []


@pytest.mark.parametrize("value", ["sqrt 2", "sqrt2"])
def test_sqrt_without_parentheses(make_workbook, value):
    path = make_workbook([problem("a1"), step("a1", answer=value, answer_type="algebra")])
    assert codes_for(path, "SQRT_NOT_PARENTHESISED") == ["SQRT_NOT_PARENTHESISED"]


def test_a_parenthesised_root_is_silent(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer="(sqrt(2))/(2)", answer_type="algebra")]
    )
    assert codes_for(path, "SQRT_NOT_PARENTHESISED") == []


@pytest.mark.parametrize("value", ["cos**-1(x)", "sin^-1(x)", "asin(x)"])
def test_inverse_trig_spelling(make_workbook, value):
    path = make_workbook([problem("a1"), step("a1", answer=value, answer_type="algebra")])
    assert codes_for(path, "INVERSE_TRIG_FORM") == ["INVERSE_TRIG_FORM"]


def test_the_power_spelling_says_what_it_actually_means(make_workbook):
    """`sin**-1` is not merely the wrong spelling: read literally it is the reciprocal,
    which is a different function."""
    path = make_workbook(
        [problem("a1"), step("a1", answer="sin**-1(x)", answer_type="algebra")]
    )
    assert "reciprocal" in findings_for(path, "INVERSE_TRIG_FORM")[0].message


def test_arcsin_is_the_accepted_spelling(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer="arcsin(x)", answer_type="algebra")]
    )
    assert codes_for(path, "INVERSE_TRIG_FORM") == []


@pytest.mark.parametrize("value,expected", [("x=<1", "<="), ("x=>1", ">=")])
def test_reversed_inequality(make_workbook, value, expected):
    path = make_workbook([problem("a1"), step("a1", answer=value, answer_type="algebra")])
    assert findings_for(path, "INEQUALITY_FORM")[0].detail["expected"] == expected


# --------------------------------------------------------------------------------------
# LaTeX containers
# --------------------------------------------------------------------------------------


def test_an_empty_container(make_workbook):
    path = make_workbook(
        [problem("a1", body=r"The value is $$$$ exactly."), step("a1")]
    )
    assert codes_for(path, "LATEX_EMPTY_CONTAINER") == ["LATEX_EMPTY_CONTAINER"]


def test_padding_inside_the_delimiters(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer=r"$$ \frac{1}{2} $$", answer_type="algebra")]
    )
    assert codes_for(path, "LATEX_DELIMITER_PADDING") == ["LATEX_DELIMITER_PADDING"]


@pytest.mark.parametrize("command", [r"\quad", r"\,", r"\;", r"\!"])
def test_manual_spacing_commands_are_refused(make_workbook, command):
    path = make_workbook(
        [problem("a1"), step("a1", answer=f"$$x{command}y$$", answer_type="algebra")]
    )
    assert codes_for(path, "LATEX_SPACING_COMMAND") == ["LATEX_SPACING_COMMAND"]


def test_a_command_outside_a_container_is_printed_literally(make_workbook):
    """The cell still looks like LaTeX to a curator skimming the sheet, which is why
    this defect survives manual review."""
    path = make_workbook(
        [problem("a1", body=r"Take \frac{1}{2} of the angle $$\theta$$.")]
    )
    findings = findings_for(path, "LATEX_COMMAND_OUTSIDE_CONTAINER")
    assert findings[0].detail["commands"] == ["\\frac"]


def test_a_command_inside_a_container_is_fine(make_workbook):
    path = make_workbook([problem("a1", body=r"Take $$\frac{1}{2}$$ of it.")])
    assert codes_for(path, "LATEX_COMMAND_OUTSIDE_CONTAINER") == []


def test_one_expression_split_across_two_containers(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer=r"$$a$$ $$= b$$", answer_type="algebra")]
    )
    assert codes_for(path, "LATEX_SPLIT_EXPRESSION") == ["LATEX_SPLIT_EXPRESSION"]


def test_prose_inside_a_math_container(make_workbook):
    path = make_workbook(
        [problem("a1", body=r"$$the angle measured here$$")]
    )
    assert codes_for(path, "LATEX_PROSE_INSIDE_CONTAINER") == [
        "LATEX_PROSE_INSIDE_CONTAINER"
    ]


def test_short_symbolic_runs_are_not_prose(make_workbook):
    """`sin x` and `d theta` are mathematics that happens to be spelled with letters."""
    path = make_workbook([problem("a1", body=r"$$\sin x + d\theta$$")])
    assert codes_for(path, "LATEX_PROSE_INSIDE_CONTAINER") == []


def test_text_inside_math_is_the_correct_way_to_write_words(make_workbook):
    path = make_workbook([problem("a1", body=r"$$\text{the angle measured here}$$")])
    assert codes_for(path, "LATEX_PROSE_INSIDE_CONTAINER") == []


def test_delimiters_around_a_plain_value_are_unnecessary(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer="$$2$$", answer_type="numeric")]
    )
    findings = findings_for(path, "LATEX_UNNECESSARY_IN_ANSWER")
    assert findings[0].detail["stripped"] == "2"


def test_delimiters_around_real_latex_are_left_alone(make_workbook):
    path = make_workbook(
        [problem("a1"), step("a1", answer=r"$$\frac{1}{2}$$", answer_type="numeric")]
    )
    assert codes_for(path, "LATEX_UNNECESSARY_IN_ANSWER") == []


# --------------------------------------------------------------------------------------
# Appearance: the decision
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["WRAP_TEXT_ENABLED", "ROW_HEIGHT_NOT_STANDARD"])
def test_appearance_is_reported_everywhere_and_repaired_nowhere(code):
    """The decision, pinned. Normalising every row would produce hundreds of differences
    no `CellEdit` authorises, and the only way to let those through the diff gate is the
    blanket allowlist that would also conceal real damage."""
    rule = REGISTRY[code]
    assert rule.severity is Severity.OBSERVATION
    assert rule.repairable is False
