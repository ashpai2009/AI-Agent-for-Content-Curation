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
        make_patch(edit(3, ColumnKey.ANSWER, "x^2", "x**2")),
        make_issue(),
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


def test_a_structural_edit_under_a_structural_issue_is_permitted(parsed, block):
    """The whole point of not refusing these outright: a blanket ban would make the real
    column-shift corruption permanently unrepairable."""
    result = check(
        make_patch(
            edit(4, ColumnKey.HINT_ID, "s1", "s3"),
            edit(5, ColumnKey.DEPENDENCY, "s1", "s3"),
        ),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.accepted, result.rejection


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


def test_removing_a_dangling_dependency_is_a_legitimate_deletion(parsed, block):
    """Requiring a destination for every cleared value would make a reference to an
    identifier the block does not contain permanently unfixable -- the repair is to
    delete it, and there is nowhere for it to go."""
    result = check(
        make_patch(edit(5, ColumnKey.DEPENDENCY, "s1", "")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.accepted, result.rejection


def test_a_shift_repair_that_duplicates_content_is_refused(parsed, block):
    """The other half of condition 6: filling the destination without emptying the
    source leaves the same value in two places."""
    result = check(
        make_patch(edit(5, ColumnKey.HINT_ID, "s2", "s1")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    # Caught precisely by the uniqueness invariant rather than by counting values:
    # `s1` would then exist on two rows.
    assert result.rejection.code is RejectionCode.STRUCTURAL_BLOCK_INVARIANT_BROKEN
    assert "duplicate identifiers" in result.rejection.message


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
    """Condition 5: the complete resulting block is simulated and must satisfy the
    dependency invariant before anything is written."""
    result = check(
        make_patch(edit(4, ColumnKey.HINT_ID, "s1", "s9")),
        make_issue(is_structural=True, category=IssueCategory.STRUCTURE),
        block,
        parsed,
    )
    assert result.rejection.code is RejectionCode.STRUCTURAL_BLOCK_INVARIANT_BROKEN
    assert "would depend on" in result.rejection.message


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
    assert result.rejection.code is RejectionCode.STRUCTURAL_BLOCK_INVARIANT_BROKEN
    assert "duplicate identifiers" in result.rejection.message


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
