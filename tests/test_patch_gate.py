"""Patch gate and structural-edit gate tests.

The structural cases carry the most weight. A blanket refusal to touch structural columns
would pass every "is it safe" test and still be wrong, because it would make the real
column-shift corruption permanently unrepairable. These tests pin the narrower rule: those
edits are allowed, under conditions that are all checked.
"""

from __future__ import annotations

import pytest

from conftest import cells, hint, problem, scaffold, step
from oatutor_council.models import (
    DependencyConvention,
    CellEdit,
    ColumnKey,
    Issue,
    IssueCategory,
    IssueSource,
    Patch,
    RejectionCode,
    Severity,
)
from oatutor_council.validation.patch_gate import (
    rejection_consumes_attempt,
    simulate_block,
    validate_patch,
)
from oatutor_council.workbook.reader import read_workbook


@pytest.fixture
def parsed(make_workbook):
    return read_workbook(
        make_workbook(
            [
                problem("angles1", title="Convert", oer_src="s", license="CC"),
                step("angles1", answer="pi/6", answer_type="algebra"),
                scaffold("angles1", "s1", answer="30", answer_type="numeric"),
                scaffold("angles1", "s2", answer="pi", answer_type="numeric",
                         dependency="s1"),
            ]
        )
    )


@pytest.fixture
def block(parsed):
    return parsed.blocks[0]


def make_issue(**kwargs) -> Issue:
    defaults = dict(
        issue_id="issue-1",
        job_id="job-1",
        block_id="block-0000",
        source=IssueSource.INITIAL_AUDITOR,
        category=IssueCategory.MATHEMATICS,
        severity=Severity.ERROR,
        title="t",
        description="d",
    )
    return Issue(**{**defaults, **kwargs})


def make_patch(*edits: CellEdit, **kwargs) -> Patch:
    defaults = dict(
        patch_id="p1",
        issue_id="issue-1",
        attempt_no=1,
        edits=tuple(edits),
        derivation="30 degrees times pi/180 is pi/6",
    )
    return Patch(**{**defaults, **kwargs})


def edit(row: int, key: ColumnKey, before: str, after: str) -> CellEdit:
    from oatutor_council.models import FIXED_COLUMNS

    return CellEdit(
        row=row, column=FIXED_COLUMNS[key], column_key=key, before=before, after=after
    )


def check(patch, issue, block, parsed):
    return validate_patch(patch, issue=issue, block=block, parsed=parsed)


# --------------------------------------------------------------------------------------
# Ordinary content edits
# --------------------------------------------------------------------------------------


def test_a_well_formed_content_edit_is_accepted(parsed, block):
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "pi/6", "pi/3")),
        make_issue(),
        block,
        parsed,
    )
    assert result.accepted, result.rejection


def test_a_candidate_with_the_before_value_from_a_different_cell_is_rejected(parsed, block):
    """Pre-write review must never approve a simulation that cannot later be applied.

    The live adversarial run copied the problem Body Text as the `before` value for its
    Title edit. The old apply-before-review lifecycle caught that in the file writer; the
    simulated-review lifecycle needs the same exact check before review.
    """
    result = check(
        make_patch(
            edit(
                2,
                ColumnKey.TITLE,
                "this came from the body-text cell",
                "Corrected title",
            ),
            derivation="",
        ),
        make_issue(cells=((2, 3),)),
        block,
        parsed,
    )

    assert result.rejection.code is RejectionCode.BEFORE_MISMATCH
    assert result.rejection.detail == {"actual": "Convert"}


def test_an_escalation_carries_no_edits_and_is_not_a_rejection(parsed, block):
    patch = Patch(
        patch_id="p1",
        issue_id="issue-1",
        attempt_no=1,
        edits=(),
        needs_human_review=True,
        human_review_reason="the source is contradictory",
    )
    assert check(patch, make_issue(), block, parsed).accepted


def test_an_edit_outside_the_block_is_rejected(parsed, block):
    result = check(
        make_patch(edit(99, ColumnKey.ANSWER, "x", "y")), make_issue(), block, parsed
    )
    assert result.rejection.code is RejectionCode.OUT_OF_BLOCK_SCOPE


def test_two_edits_to_one_cell_cannot_even_be_constructed():
    """`Patch` refuses this at construction, so the gate never sees one. Its own
    duplicate check remains as defence for patches assembled elsewhere."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="same cell twice"):
        make_patch(
            edit(3, ColumnKey.ANSWER, "pi/6", "pi/3"),
            edit(3, ColumnKey.ANSWER, "pi/6", "pi/4"),
        )


def test_a_metadata_edit_under_a_mathematics_issue_is_out_of_scope(parsed, block):
    """An improvement nobody asked for is an unreviewed change."""
    result = check(
        make_patch(edit(2, ColumnKey.LICENSE, "CC", "CC-BY-4.0")),
        make_issue(category=IssueCategory.MATHEMATICS),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.UNRELATED_CELL


def test_the_same_metadata_edit_is_allowed_under_a_metadata_issue(parsed, block):
    result = check(
        make_patch(edit(2, ColumnKey.LICENSE, "CC", "CC-BY-4.0")),
        make_issue(category=IssueCategory.METADATA),
        block,
        parsed,
    )
    assert result.accepted, result.rejection


def test_a_mathematical_edit_without_a_derivation_is_rejected(parsed, block):
    """A correction nobody can check is not a correction. The derivation is visible to
    this gate and deliberately not to reviewers."""
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "pi/6", "pi/3"), derivation="  "),
        make_issue(),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.MISSING_MATH_VERIFICATION


def test_a_non_mathematical_edit_needs_no_derivation(parsed, block):
    result = check(
        make_patch(edit(2, ColumnKey.TITLE, "Convert", "Convert to radians"),
                   derivation=""),
        make_issue(),
        block,
        parsed,
    )
    assert result.accepted, result.rejection


def test_a_mathematics_issue_cannot_rewrite_a_provably_equivalent_answer(parsed, block):
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "pi/6", "2*pi/12")),
        make_issue(category=IssueCategory.MATHEMATICS),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.MATHEMATICALLY_EQUIVALENT_REWRITE


def test_an_exact_fraction_request_cannot_be_repaired_by_decimalizing_the_answer(
    make_workbook,
):
    parsed = read_workbook(
        make_workbook(
            [
                problem("mc1", title="Select an exact fraction"),
                step(
                    "mc1",
                    title="Choose the exact probability.",
                    answer="1/2",
                    answer_type="mc",
                    mc_choices="0.5|1/3|1/4",
                ),
            ]
        )
    )
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "1/2", "0.5")),
        make_issue(
            category=IssueCategory.MULTIPLE_CHOICE,
            rule_codes=("MC_ANSWER_NOT_IN_CHOICES",),
            cells=((3, FIXED_COLUMNS[ColumnKey.ANSWER]),),
        ),
        parsed.blocks[0],
        parsed,
    )
    assert result.rejection.code is RejectionCode.REQUESTED_FORM_VIOLATION


def test_exact_form_language_on_a_sibling_step_does_not_govern_this_answer(
    make_workbook,
):
    parsed = read_workbook(
        make_workbook(
            [
                problem("mixed1", title="Complete both parts"),
                step(
                    "mixed1",
                    title="Give an exact fraction.",
                    answer="1/2",
                    answer_type="algebra",
                ),
                step(
                    "mixed1",
                    title="Give a decimal approximation.",
                    answer="0.25",
                    answer_type="numeric",
                ),
            ]
        )
    )

    result = check(
        make_patch(edit(4, ColumnKey.ANSWER, "0.25", "0.5")),
        make_issue(category=IssueCategory.MATHEMATICS),
        parsed.blocks[0],
        parsed,
    )

    assert result.accepted, result.rejection


def test_a_model_cannot_renumber_a_valid_identifier_merely_to_close_a_gap(parsed, block):
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(
            edit(5, ColumnKey.HINT_ID, "s2", "s3"),
            derivation="",
        ),
        make_issue(
            category=IssueCategory.DEPENDENCY,
            is_structural=True,
            rule_codes=("INDEPENDENT_FINDING",),
            cells=((5, FIXED_COLUMNS[ColumnKey.HINT_ID]),),
        ),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_EVIDENCE_MISSING


# --------------------------------------------------------------------------------------
# Regression detection
# --------------------------------------------------------------------------------------


def test_a_patch_that_breaks_something_else_is_rejected(parsed, block):
    """Cheaper to catch here than in review, and it is the case where a fix looks
    perfectly correct on its own line."""
    result = check(
        make_patch(edit(4, ColumnKey.ANSWER, "30", "")),
        make_issue(),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.RULE_VIOLATION
    assert "SCAFFOLD_MISSING_ANSWER" in result.rejection.detail["codes"]


def test_pre_existing_findings_do_not_block_a_patch(make_workbook):
    """A block usually has several findings and only one is being repaired. Requiring a
    clean block would make every repair impossible."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("a1"),  # no title, no metadata: several warnings already
                step("a1", answer="x^2", answer_type="algebra"),
            ]
        )
    )
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "x^2", "x^3")),
        make_issue(),
        parsed.blocks[0],
        parsed,
    )
    assert result.accepted, result.rejection


# --------------------------------------------------------------------------------------
# Does the patch fix what it was asked to fix?
# --------------------------------------------------------------------------------------


@pytest.fixture
def unanswered(make_workbook):
    """A block whose scaffold has no answer, and an issue that says exactly that."""
    return read_workbook(
        make_workbook(
            [
                problem("angles1", title="Convert", oer_src="s", license="CC"),
                step("angles1", answer="pi/6", answer_type="algebra"),
                scaffold("angles1", "s1", answer="", answer_type="numeric"),
            ]
        )
    )


def missing_answer_issue(**kwargs) -> Issue:
    from oatutor_council.models import FIXED_COLUMNS

    defaults = dict(
        category=IssueCategory.ROW_TYPE,
        rule_codes=("SCAFFOLD_MISSING_ANSWER",),
        cells=((4, FIXED_COLUMNS[ColumnKey.ANSWER]),),
    )
    return make_issue(**{**defaults, **kwargs})


def test_a_patch_resolving_the_named_defect_is_accepted(unanswered):
    result = check(
        make_patch(edit(4, ColumnKey.ANSWER, "", "30")),
        missing_answer_issue(),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.accepted, result.rejection


def test_an_unrelated_title_edit_under_an_answer_issue_is_refused(unanswered):
    """The reported case. Editing the Title of the row whose Answer is missing is a
    perfectly valid edit that has nothing to do with the issue, and nothing about it
    breaks a rule -- so only a scope check catches it."""
    result = check(
        make_patch(edit(4, ColumnKey.TITLE, "Work through this part", "First part")),
        missing_answer_issue(),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.rejection.code is RejectionCode.UNRELATED_CELL


def test_an_unrelated_edit_with_an_explanation_still_has_to_fix_the_issue(unanswered):
    """A stated reason is necessary, not sufficient. The Writer can always produce a
    sentence; it cannot produce a defect that is no longer there."""
    result = check(
        make_patch(
            edit(4, ColumnKey.TITLE, "Work through this part", "First part"),
            related_edits_reason="the title clarifies what the scaffold is asking",
        ),
        missing_answer_issue(),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.rejection.code is RejectionCode.ISSUE_NOT_RESOLVED
    assert "SCAFFOLD_MISSING_ANSWER" in result.rejection.detail["codes"]


def test_a_patch_that_edits_the_right_cell_but_does_not_fix_it_is_refused(unanswered):
    """The edit is in scope, well-formed, breaks nothing, and leaves the scaffold with
    no answer -- because whitespace is not an answer."""
    result = check(
        make_patch(edit(4, ColumnKey.ANSWER, "", " ")),
        missing_answer_issue(),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.rejection.code is RejectionCode.ISSUE_NOT_RESOLVED


def test_a_sibling_cell_the_repair_needs_is_allowed_when_explained(make_workbook):
    """An answer that matches no choice is repaired in `mcChoices`, which is not the
    cell the issue named. Scope must permit that or the defect is unfixable."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("mc1", title="Choose", oer_src="s", license="CC"),
                step("mc1", answer="1/2", answer_type="mc", mc_choices="0.5|1/3|1/4"),
            ]
        )
    )
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(
            edit(3, ColumnKey.MC_CHOICES, "0.5|1/3|1/4", "1/2|1/3|1/4"),
            related_edits_reason="the answer is correct; the choice list must contain "
            "it character for character",
        ),
        make_issue(
            category=IssueCategory.MULTIPLE_CHOICE,
            rule_codes=("MC_ANSWER_NOT_IN_CHOICES",),
            cells=((3, FIXED_COLUMNS[ColumnKey.ANSWER]),),
        ),
        parsed.blocks[0],
        parsed,
    )
    assert result.accepted, result.rejection


def test_an_edit_the_repair_did_not_need_is_refused_even_with_an_explanation(unanswered):
    """Necessity, decided by consequence rather than by intent: drop the edit, and if the
    issue is still resolved and nothing new is broken, the repair never needed it."""
    result = check(
        make_patch(
            edit(4, ColumnKey.ANSWER, "", "30"),
            edit(4, ColumnKey.TITLE, "Work through this part", "First part"),
            related_edits_reason="the title makes the scaffold clearer",
        ),
        missing_answer_issue(),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.rejection.code is RejectionCode.UNRELATED_CELL
    assert result.rejection.row == 4


def test_an_issue_with_no_rule_code_is_left_to_its_reviewer(unanswered):
    """An agent-discovered defect carries no registered rule, so there is nothing to
    re-run. The gate must not read "cannot be checked" as "not fixed" -- that would make
    every semantic issue permanently unrepairable."""
    result = check(
        make_patch(edit(4, ColumnKey.TITLE, "Work through this part", "First part")),
        make_issue(rule_codes=("AUDITOR_FINDING",)),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.accepted, result.rejection


def test_the_named_column_must_agree_with_the_column_index(unanswered):
    """The writer addresses the cell by index and the scope checks read the key. If they
    disagree the patch is checked against one cell and written to another."""
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(
            CellEdit(
                row=4,
                column=FIXED_COLUMNS[ColumnKey.TITLE],
                column_key=ColumnKey.ANSWER,
                before="",
                after="30",
            )
        ),
        missing_answer_issue(),
        unanswered.blocks[0],
        unanswered,
    )
    assert result.rejection.code is RejectionCode.COLUMN_KEY_MISMATCH


# --------------------------------------------------------------------------------------
# Presentation repairs must not change the mathematics
# --------------------------------------------------------------------------------------


@pytest.fixture
def caret(make_workbook):
    return read_workbook(
        make_workbook(
            [
                problem("a1", title="Simplify", oer_src="s", license="CC"),
                step("a1", answer="x^2", answer_type="algebra"),
            ]
        )
    )


def notation_issue(**kwargs) -> Issue:
    from oatutor_council.models import FIXED_COLUMNS

    defaults = dict(
        category=IssueCategory.NOTATION,
        rule_codes=("CARET_EXPONENT",),
        cells=((3, FIXED_COLUMNS[ColumnKey.ANSWER]),),
    )
    return make_issue(**{**defaults, **kwargs})


def test_a_notation_repair_that_preserves_the_value_is_accepted(caret):
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "x^2", "x**2")),
        notation_issue(),
        caret.blocks[0],
        caret,
    )
    assert result.accepted, result.rejection


def test_a_notation_repair_that_changes_the_value_is_refused(caret):
    """`x**3` is correctly-written ASCII and satisfies the rule that raised the issue.
    It is also a different answer, and every other check in the gate would pass it."""
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "x^2", "x**3")),
        notation_issue(),
        caret.blocks[0],
        caret,
    )
    assert result.rejection.code is RejectionCode.MATH_NOT_EQUIVALENT


def test_a_mathematics_issue_is_allowed_to_change_the_value(caret):
    """The equivalence check applies to presentation repairs only. Under a mathematics
    issue, changing the value is the entire point."""
    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "x^2", "x**3")),
        notation_issue(category=IssueCategory.MATHEMATICS),
        caret.blocks[0],
        caret,
    )
    assert result.accepted, result.rejection


def test_a_mathematics_issue_cannot_replace_a_solved_equation_with_its_value(
    make_workbook,
):
    """Preservation is semantic, not string-shaped.

    The live false positive changed ``x=sqrt(4)``/algebra to ``2``/numeric.  One is an
    equation and one is a scalar, so generic equation comparison calls them different;
    as graded answers they represent the same solved value and the change is merely a
    normalization of already-correct content.
    """
    parsed = read_workbook(
        make_workbook(
            [
                problem("a1", title="Solve for x", oer_src="s", license="CC"),
                step("a1", answer="x=sqrt(4)", answer_type="algebra"),
            ]
        )
    )
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(
            edit(3, ColumnKey.ANSWER, "x=sqrt(4)", "2"),
            edit(3, ColumnKey.ANSWER_TYPE, "algebra", "numeric"),
        ),
        make_issue(
            category=IssueCategory.MATHEMATICS,
            rule_codes=("AUDITOR_FINDING",),
            cells=(
                (3, FIXED_COLUMNS[ColumnKey.ANSWER]),
                (3, FIXED_COLUMNS[ColumnKey.ANSWER_TYPE]),
            ),
            is_structural=True,
        ),
        parsed.blocks[0],
        parsed,
    )
    assert result.rejection.code is RejectionCode.MATHEMATICALLY_EQUIVALENT_REWRITE


def test_a_genuinely_wrong_solved_value_can_still_be_corrected(make_workbook):
    parsed = read_workbook(
        make_workbook(
            [
                problem("a1", title="Solve for x", oer_src="s", license="CC"),
                step("a1", answer="x=3", answer_type="algebra"),
            ]
        )
    )
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(
            edit(3, ColumnKey.ANSWER, "x=3", "2"),
            edit(3, ColumnKey.ANSWER_TYPE, "algebra", "numeric"),
        ),
        make_issue(
            category=IssueCategory.MATHEMATICS,
            rule_codes=("AUDITOR_FINDING",),
            cells=(
                (3, FIXED_COLUMNS[ColumnKey.ANSWER]),
                (3, FIXED_COLUMNS[ColumnKey.ANSWER_TYPE]),
            ),
            is_structural=True,
        ),
        parsed.blocks[0],
        parsed,
    )
    assert result.accepted, result.rejection


def test_mathematics_sympy_cannot_decide_is_left_to_the_reviewer(make_workbook):
    """SymPy is a gate, not a proof. An expression it cannot parse yields UNKNOWN, and
    UNKNOWN must pass through to a reviewer -- refusing it would reject most correct
    LaTeX repairs, which is worse than the risk it guards against."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("a1", title="State", oer_src="s", license="CC"),
                step("a1", answer="the domain of f", answer_type="algebra"),
            ]
        )
    )
    from oatutor_council.models import FIXED_COLUMNS

    result = check(
        make_patch(edit(3, ColumnKey.ANSWER, "the domain of f", "all real numbers")),
        make_issue(
            category=IssueCategory.NOTATION,
            rule_codes=(),
            cells=((3, FIXED_COLUMNS[ColumnKey.ANSWER]),),
        ),
        parsed.blocks[0],
        parsed,
    )
    assert result.accepted, result.rejection


# --------------------------------------------------------------------------------------
# Structural edits
# --------------------------------------------------------------------------------------


def test_a_structural_edit_under_a_non_structural_issue_is_refused(parsed, block):
    """Condition 1. The issue must explicitly identify a structural defect."""
    result = check(
        make_patch(edit(4, ColumnKey.HINT_ID, "s1", "s3")),
        make_issue(is_structural=False),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_COLUMN_UNAUTHORIZED
    assert "hint_id" in result.rejection.message


def test_a_pure_identifier_rename_without_a_defect_is_refused(parsed, block):
    """Structural authority alone does not make a gap-closing rename a repair."""
    result = check(
        make_patch(
            edit(4, ColumnKey.HINT_ID, "s1", "s3"),
            edit(5, ColumnKey.DEPENDENCY, "s1", "s3"),
        ),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_EVIDENCE_MISSING


def test_a_column_shift_repair_that_moves_content_is_accepted(make_workbook):
    """The shape found in the real corpus: an identifier sat in `answerType` and the
    dependency in `HintID`. The repair moves both and vacates the source."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("t1", title="T", oer_src="s", license="CC"),
                step("t1", answer="1", answer_type="numeric"),
                cells(
                    problem_name="t1",
                    row_type="scaffold",
                    answer="2",
                    answer_type="h1",  # displaced identifier
                    hint_id="1",       # displaced dependency
                ),
            ]
        )
    )
    block = parsed.blocks[0]
    result = check(
        make_patch(
            edit(4, ColumnKey.ANSWER_TYPE, "h1", "numeric"),
            edit(4, ColumnKey.HINT_ID, "1", "h1"),
        ),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.accepted, result.rejection


def test_a_move_that_drops_the_vacated_value_is_refused(make_workbook):
    """Condition 6. The patch writes a destination but lets a different vacated value
    disappear -- content deleted in the middle of a move, which looks fine cell by cell."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("t1", title="T", oer_src="s", license="CC"),
                step("t1", answer="1", answer_type="numeric"),
                cells(problem_name="t1", row_type="scaffold", answer="2",
                      answer_type="h1", hint_id="1", dependency="s1"),
                scaffold("t1", "s1", answer="3", answer_type="numeric"),
            ]
        )
    )
    result = check(
        make_patch(
            edit(4, ColumnKey.ANSWER_TYPE, "h1", "numeric"),  # writes a destination
            edit(4, ColumnKey.DEPENDENCY, "s1", ""),          # drops content instead of moving it
        ),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        parsed.blocks[0],
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_CONTENT_NOT_CONSERVED
    assert "not drop it" in result.rejection.message


def test_a_deletion_that_breaks_the_block_is_judged_on_its_consequences(make_workbook):
    """Clearing a cell is a deletion, not a move, so conservation does not apply. Whether
    it is acceptable is decided by what the resulting block looks like -- a more precise
    answer than counting values."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("t1", title="T", oer_src="s", license="CC"),
                step("t1", answer="1", answer_type="numeric"),
                cells(problem_name="t1", row_type="scaffold", answer="2",
                      answer_type="h1", hint_id="1"),
            ]
        )
    )
    result = check(
        make_patch(edit(4, ColumnKey.ANSWER_TYPE, "h1", "")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        parsed.blocks[0],
        parsed,
    )
    assert result.rejection.code is RejectionCode.RULE_VIOLATION
    assert "ANSWER_WITHOUT_TYPE" in result.rejection.detail["codes"]


def test_removing_a_dangling_dependency_is_a_legitimate_deletion(make_workbook):
    """Requiring a destination for every cleared value would make a reference to an
    identifier the block does not contain permanently unfixable -- the repair is to
    delete it, and there is nowhere for it to go.

    The row has to be the *first* under its step for deletion to be the whole repair:
    anywhere else the chain expects a dependency, so clearing one dangling reference
    just trades it for a broken chain, and the correct repair is a replacement.
    """
    parsed = read_workbook(
        make_workbook(
            [
                problem("angles1", title="Convert", oer_src="s", license="CC"),
                step("angles1", answer="pi/6", answer_type="algebra"),
                scaffold("angles1", "s1", answer="30", answer_type="numeric",
                         dependency="s9"),
            ]
        )
    )
    result = check(
        make_patch(edit(4, ColumnKey.DEPENDENCY, "s9", "")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        parsed.blocks[0],
        parsed,
    )
    assert result.accepted, result.rejection


def test_clearing_a_dependency_the_chain_needs_is_refused(make_workbook):
    """The mirror case, and the reason the deletion above needs its own fixture. `h2`
    follows `h1` under one step, so an empty Dependency releases both hints at once --
    which is exactly what the chain exists to prevent."""
    parsed = read_workbook(
        make_workbook(
            [
                problem("angles1", title="Convert", oer_src="s", license="CC"),
                step("angles1", answer="pi/6", answer_type="algebra"),
                hint("angles1", "h1", body="Start from the definition."),
                hint("angles1", "h2", body="Now substitute.", dependency="h1"),
            ]
        )
    )
    result = check(
        make_patch(edit(5, ColumnKey.DEPENDENCY, "h1", "")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        parsed.blocks[0],
        parsed,
    )
    assert result.rejection.code is RejectionCode.RULE_VIOLATION
    assert "HINT_DEPENDENCY_NOT_PREVIOUS" in result.rejection.detail["codes"]


def test_a_shift_repair_that_duplicates_content_is_refused(parsed, block):
    """The other half of condition 6: filling the destination without emptying the
    source leaves the same value in two places."""
    result = check(
        make_patch(edit(5, ColumnKey.HINT_ID, "s2", "s1")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    # Caught by uniqueness rather than by counting values: `s1` would exist on two rows.
    # The uniqueness question is asked by the rule engine, which knows the workbook's
    # identifier convention -- so the refusal arrives as a regression rather than as a
    # block invariant. What matters is that it arrives and names the right defect.
    assert result.rejection.code is RejectionCode.RULE_VIOLATION
    assert "DUPLICATE_IDENTIFIER" in result.rejection.detail["codes"]


def test_a_patch_that_would_split_the_block_is_refused(parsed, block):
    """Condition 8 in effect: block boundaries may not change. Turning a step into a
    second problem row would do exactly that."""
    result = check(
        make_patch(edit(3, ColumnKey.ROW_TYPE, "step", "problem")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_BLOCK_INVARIANT_BROKEN
    assert "second problem row" in result.rejection.message


def test_a_patch_that_empties_the_problem_name_is_refused(parsed, block):
    result = check(
        make_patch(edit(2, ColumnKey.PROBLEM_NAME, "angles1", " ")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_BLOCK_INVARIANT_BROKEN
    assert "without a Problem Name" in result.rejection.message


def test_a_patch_leaving_a_dangling_dependency_is_refused(parsed, block):
    """The complete resulting block is simulated and must satisfy the dependency
    invariant before anything is written — asked of the rule engine, which resolves a
    dependency within its own step rather than across the block."""
    result = check(
        make_patch(edit(4, ColumnKey.HINT_ID, "s1", "s9")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.RULE_VIOLATION
    assert "DEPENDENCY_UNRESOLVED" in result.rejection.detail["codes"]


def test_a_patch_creating_duplicate_identifiers_is_refused(parsed, block):
    result = check(
        make_patch(
            edit(4, ColumnKey.HINT_ID, "s1", "s2"),
            edit(5, ColumnKey.DEPENDENCY, "s1", "s2"),
        ),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.RULE_VIOLATION
    assert "DUPLICATE_IDENTIFIER" in result.rejection.detail["codes"]


# -- reset-per-step numbering ----------------------------------------------------------
#
# The live regression these exist for. A workbook that restarts identifiers at every step
# is *correct* under `RESET_PER_STEP`, and the gate was reading each step's `h1` as a
# duplicate of the last one's. The condition was pre-existing, so it was not something a
# patch could clear: every repair inside a multi-step problem was refused three times and
# the issue walked to `NEEDS_HUMAN_REVIEW` with its budget spent. Eight of nine misses on
# a real workbook came from this one check.


@pytest.fixture
def multi_step_parsed(make_workbook):
    """Two steps, each with its own `h1`/`h2`, each depending within its own step."""
    return read_workbook(
        make_workbook(
            [
                problem("angles2", title="Convert", oer_src="s", license="CC"),
                step("angles2", answer="pi/6", answer_type="algebra"),
                hint("angles2", "h1", body="Start from degrees."),
                hint("angles2", "h2", body="Multiply by pi/180.", dependency="h1"),
                step("angles2", answer="pi/3", answer_type="algebra"),
                hint("angles2", "h1", body="Same conversion again."),
                hint("angles2", "h2", body="Now for sixty.", dependency="h1"),
            ]
        )
    )


def test_reused_identifiers_across_steps_do_not_block_an_unrelated_repair(
    multi_step_parsed,
):
    """The regression, stated as the curator would: a correct repair must not be refused
    because a *different* step reuses `h1` exactly as the convention says it should."""
    block = multi_step_parsed.blocks[0]
    assert [row.get(ColumnKey.HINT_ID) for row in block.rows].count("h1") == 2
    # Stated rather than assumed: if the reader ever stopped calling this reset-per-step,
    # the test would still pass on the delta alone and quietly stop testing the scope.
    assert (
        multi_step_parsed.conventions.dependency_convention
        is DependencyConvention.RESET_PER_STEP
    )

    result = check(
        make_patch(edit(4, ColumnKey.BODY_TEXT, "Start from degrees.", "Begin in degrees.")),
        make_issue(),
        block,
        multi_step_parsed,
    )

    assert result.rejection is None


def test_a_duplicate_within_one_step_is_still_refused(multi_step_parsed):
    """The exemption is per step, not per block. Two `h2` rows under the *same* step is
    the defect the check exists for, and it must still fire."""
    block = multi_step_parsed.blocks[0]
    result = check(
        make_patch(edit(4, ColumnKey.HINT_ID, "h1", "h2")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        multi_step_parsed,
    )

    assert result.rejection is not None
    assert "DUPLICATE_IDENTIFIER" in result.rejection.detail["codes"]


def test_a_pre_existing_defect_does_not_make_every_patch_unapplyable(parsed, block):
    """The second half of the same bug, independent of scoping.

    The identifier checks ran *absolutely* over the patched block rather than as a delta,
    so a block that arrived with a dangling dependency could never be repaired at all —
    the gate refused every patch for a defect the patch had not introduced. A workbook
    full of pre-existing defects is the only kind anyone uploads.
    """
    broken = simulate_block(block, (edit(5, ColumnKey.DEPENDENCY, "s1", "s404"),))
    patched_parsed = parsed.model_copy(update={"blocks": (broken,)})

    result = check(
        make_patch(edit(4, ColumnKey.ANSWER, "30", "30 degrees")),
        make_issue(),
        broken,
        patched_parsed,
    )

    assert result.rejection is None


# --------------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------------


def test_simulation_does_not_touch_the_original_block(parsed, block):
    patched = simulate_block(block, [edit(3, ColumnKey.ANSWER, "pi/6", "pi/3")])
    assert patched.rows[1].get(ColumnKey.ANSWER) == "pi/3"
    assert block.rows[1].get(ColumnKey.ANSWER) == "pi/6"


# --------------------------------------------------------------------------------------
# Attempt accounting
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        RejectionCode.RULE_VIOLATION,
        RejectionCode.OUT_OF_BLOCK_SCOPE,
        RejectionCode.STRUCTURAL_COLUMN_UNAUTHORIZED,
        RejectionCode.BEFORE_MISMATCH,
    ],
)
def test_a_rejected_patch_costs_an_attempt(code):
    """The Writer call was made and the loop-forming resource consumed."""
    from oatutor_council.models import PatchRejection

    assert rejection_consumes_attempt(PatchRejection(code=code, message="x"))


def test_a_stale_before_does_not_cost_an_attempt():
    """The patch was written against a block that changed underneath it. That is the
    system's scheduling, not the Writer's mistake."""
    from oatutor_council.models import PatchRejection

    assert not rejection_consumes_attempt(
        PatchRejection(code=RejectionCode.STALE_BEFORE, message="x")
    )
