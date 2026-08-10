"""Reader tests.

The structures exercised here are the ones reconnaissance found in the real corpus, all
reproduced with invented mathematics. Each has a note saying which real failure mode it
stands for, because a test that only asserts "the parser works" tends to be deleted the
first time it becomes inconvenient.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import BLANK, DEFAULT_HEADERS, cells, hint, problem, scaffold, step
from oatutor_council.models import (
    ColumnKey,
    DependencyConvention,
    Notation,
    RowType,
    Severity,
    StructuralCode,
)
from oatutor_council.workbook.reader import (
    WorkbookReadError,
    read_workbook,
    render_cell,
)


def codes(parsed) -> list[str]:
    return [f.code for f in parsed.all_findings]


# --------------------------------------------------------------------------------------
# Cell rendering
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        (None, ""),
        ("theta", "theta"),
        (True, "TRUE"),
        (False, "FALSE"),
        # openpyxl reports whole-numbered cells as floats; rendering `1.0` would make a
        # perfectly good dependency fail every identifier rule.
        (3.0, "3"),
        (0.5, "0.5"),
        (datetime(2026, 1, 2), "2026-01-02 00:00:00"),
    ],
)
def test_render_cell(native, expected):
    assert render_cell(native) == expected


# --------------------------------------------------------------------------------------
# Header and column resolution
# --------------------------------------------------------------------------------------


def test_header_found_at_row_one(make_workbook):
    parsed = read_workbook(make_workbook([problem("angles1"), step("angles1")]))
    assert parsed.header_row == 1
    assert parsed.first_data_row == 2


def test_header_detected_below_leading_blank_rows(make_workbook):
    """One real workbook starts its data at row 3. A hardcoded row 1 reads a data row
    as the contract and silently mislabels every column."""
    path = make_workbook([BLANK, problem("unitcirc1"), step("unitcirc1")], header_row=2)
    parsed = read_workbook(path)
    assert parsed.header_row == 2
    assert parsed.first_data_row == 4
    assert len(parsed.blocks) == 1


def test_missing_header_row_is_an_error(make_workbook):
    path = make_workbook([problem("angles1")], headers=["Nope"] * len(DEFAULT_HEADERS))
    with pytest.raises(WorkbookReadError, match="no header row"):
        read_workbook(path)


def test_header_contract_mismatch_is_reported_not_fatal(make_workbook):
    headers = list(DEFAULT_HEADERS)
    headers[5] = "answer type"  # column F, `answerType`
    parsed = read_workbook(make_workbook([problem("angles1")], headers=headers))
    assert StructuralCode.HEADER_CONTRACT_MISMATCH in codes(parsed)
    assert len(parsed.blocks) == 1


def test_images_parenthetical_satisfies_the_contract(make_workbook):
    """Every real workbook labels column J `Images (space delimited)`. An equality check
    would flag all eleven."""
    parsed = read_workbook(make_workbook([problem("angles1")]))
    assert StructuralCode.HEADER_CONTRACT_MISMATCH not in codes(parsed)


@pytest.mark.parametrize("column", [18, 19, 20])
def test_trailing_columns_resolve_by_name_at_any_position(make_workbook, column):
    """`Validator Check` was found at column 18, 19 and 20 across the corpus. Reading it
    positionally would pull a neighbouring column's data on two of eleven workbooks."""
    headers: list[str | None] = list(DEFAULT_HEADERS)
    headers[16:] = [None] * 4
    headers[column - 1] = "Validator Check"
    parsed = read_workbook(make_workbook([problem("angles1")], headers=headers))
    assert parsed.column_map.index_of(ColumnKey.VALIDATOR_CHECK) == column


def test_duplicate_trailing_header_is_reported_and_first_wins(make_workbook):
    """Two real workbooks carry `Validator Check` twice."""
    headers = list(DEFAULT_HEADERS)
    headers[17] = "Validator Check"  # now at both column 18 and column 19
    parsed = read_workbook(make_workbook([problem("angles1")], headers=headers))
    assert StructuralCode.DUPLICATE_HEADER_LABEL in codes(parsed)
    assert parsed.column_map.index_of(ColumnKey.VALIDATOR_CHECK) == 18


def test_missing_trailing_column_resolves_to_nothing(make_workbook):
    """One real workbook has no `Time Last Checked`. Absent must mean absent, not
    'whatever is in column 20'."""
    headers = list(DEFAULT_HEADERS)
    headers[19] = None
    parsed = read_workbook(make_workbook([problem("angles1")], headers=headers))
    assert parsed.column_map.index_of(ColumnKey.TIME_LAST_CHECKED) is None
    assert StructuralCode.MISSING_NAMED_COLUMN in codes(parsed)


def test_a_row_with_content_only_past_the_contract_is_not_blank(make_workbook):
    """Real workbooks carry tooling columns past the documented contract -- `Debug
    Link`, `Problem ID`, `Lesson ID`, `Image Checksum`, out to column 25 -- and three of
    them put validator output on the row above the first problem row. Judging blankness
    from the mapped columns alone would let such a row be trimmed off a block or vanish
    from segmentation entirely."""
    headers = list(DEFAULT_HEADERS) + ["Debug Link", "Problem ID", "Lesson ID"]
    tooling_only = [None] * len(headers)
    tooling_only[21] = "L-0001"  # Lesson ID, well past the contract

    path = make_workbook(
        [tooling_only, problem("angles1"), step("angles1")], headers=headers
    )
    parsed = read_workbook(path)
    assert [r.row for r in parsed.orphan_rows] == [2]
    assert StructuralCode.ORPHAN_ROW_BEFORE_FIRST_PROBLEM in codes(parsed)


def test_trailing_column_is_found_past_the_documented_contract(make_workbook):
    """One real workbook duplicates `Validator Check` into column 20 and pushes `Time
    Last Checked` out to 21. Name resolution has to look past column T."""
    headers = list(DEFAULT_HEADERS) + ["Time Last Checked"]
    headers[19] = "Validator Check"  # column 20, displacing the usual occupant
    parsed = read_workbook(make_workbook([problem("angles1")], headers=headers))
    assert parsed.column_map.index_of(ColumnKey.TIME_LAST_CHECKED) == 21


def test_extra_sheets_are_reported(make_workbook):
    parsed = read_workbook(
        make_workbook([problem("angles1")], extra_sheets=("Notes",))
    )
    assert StructuralCode.MULTIPLE_SHEETS in codes(parsed)


# --------------------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------------------


def test_blocks_are_segmented_by_row_type(make_workbook):
    path = make_workbook(
        [
            problem("angles1"),
            step("angles1"),
            hint("angles1", "h1"),
            problem("angles2"),
            step("angles2"),
        ]
    )
    parsed = read_workbook(path)
    assert [b.problem_name for b in parsed.blocks] == ["angles1", "angles2"]
    assert (parsed.blocks[0].start_row, parsed.blocks[0].end_row) == (2, 4)
    assert (parsed.blocks[1].start_row, parsed.blocks[1].end_row) == (5, 6)


def test_a_blank_row_never_splits_a_block(make_workbook):
    """Blank rows separate blocks in practice, but a stray blank mid-block must not
    truncate one -- everything after it would be silently dropped."""
    path = make_workbook(
        [
            problem("angles1"),
            step("angles1"),
            BLANK,
            hint("angles1", "h1"),
            problem("angles2"),
        ]
    )
    parsed = read_workbook(path)
    assert len(parsed.blocks) == 2
    assert parsed.blocks[0].end_row == 5
    assert StructuralCode.INTERIOR_BLANK_ROW in codes(parsed)


def test_trailing_blank_rows_are_trimmed_from_a_block(make_workbook):
    path = make_workbook([problem("angles1"), step("angles1"), BLANK, BLANK])
    parsed = read_workbook(path)
    assert parsed.blocks[0].end_row == 3


def test_repeated_problem_name_does_not_merge_two_blocks(make_workbook):
    """`Row Type` is the boundary signal. Two consecutive problem rows sharing a name
    are two blocks, however unusual that looks."""
    parsed = read_workbook(make_workbook([problem("angles1"), problem("angles1")]))
    assert len(parsed.blocks) == 2


def test_rows_before_the_first_problem_row_become_orphans(make_workbook):
    """Real: one workbook holds content on the row above its first problem row. It
    cannot belong to the block that follows, and attaching it there would put a foreign
    row inside a block the Writer is allowed to edit."""
    path = make_workbook([step("sumprod1"), problem("sumprod1"), step("sumprod1")])
    parsed = read_workbook(path)
    assert [r.row for r in parsed.orphan_rows] == [2]
    assert StructuralCode.ORPHAN_ROW_BEFORE_FIRST_PROBLEM in codes(parsed)
    assert parsed.blocks[0].start_row == 3


def test_no_problem_rows_is_an_error(make_workbook):
    with pytest.raises(WorkbookReadError, match="no problem blocks"):
        read_workbook(make_workbook([step("angles1"), hint("angles1", "h1")]))


def test_unknown_row_type_is_reported(make_workbook):
    path = make_workbook(
        [problem("angles1"), cells(problem_name="angles1", row_type="sub-step")]
    )
    parsed = read_workbook(path)
    assert StructuralCode.UNKNOWN_ROW_TYPE in codes(parsed)


# --------------------------------------------------------------------------------------
# Signal disagreement
# --------------------------------------------------------------------------------------


def test_name_mismatch_inside_a_block_reports_the_disagreement(make_workbook):
    """Real: five rows named `sumprod3` sit inside the block whose problem row declares
    `sumprod4`. Either reading is a guess, so the reader reports the conflict instead of
    resolving it -- quietly preferring one is what hides structural damage."""
    path = make_workbook(
        [
            problem("sumprod4"),
            step("sumprod4"),
            step("sumprod3"),
            hint("sumprod3", "h1"),
        ]
    )
    parsed = read_workbook(path)
    found = codes(parsed)
    assert found.count(StructuralCode.PROBLEM_NAME_MISMATCH_IN_BLOCK) == 2
    assert StructuralCode.BLOCK_BOUNDARY_DISAGREEMENT in found

    disagreement = next(
        f
        for f in parsed.all_findings
        if f.code == StructuralCode.BLOCK_BOUNDARY_DISAGREEMENT
    )
    assert disagreement.severity is Severity.BLOCKING
    assert disagreement.detail["mismatched_rows"] == [4, 5]

    # One block, not two: the disagreement is recorded, never acted on.
    assert len(parsed.blocks) == 1


def test_a_consistent_block_reports_no_disagreement(make_workbook):
    path = make_workbook([problem("angles1"), step("angles1"), hint("angles1", "h1")])
    parsed = read_workbook(path)
    assert StructuralCode.BLOCK_BOUNDARY_DISAGREEMENT not in codes(parsed)
    assert StructuralCode.PROBLEM_NAME_MISMATCH_IN_BLOCK not in codes(parsed)


# --------------------------------------------------------------------------------------
# The two shift shapes
# --------------------------------------------------------------------------------------


def test_whole_row_right_shift_is_not_reported_as_a_missing_name(make_workbook):
    """Real: 32 rows where every value moved two columns right, so the name sits in
    `Title`. Calling this a missing Problem Name is worse than imprecise -- filling the
    name in would leave the corruption and add a duplicate."""
    shifted = [None] * len(DEFAULT_HEADERS)
    shifted[2] = "trig1"  # Problem Name landed in Title
    shifted[3] = "step"  # Row Type landed in Body Text
    shifted[5] = "Evaluate cos(0)"  # Body Text landed in answerType
    shifted[6] = "1"  # Answer landed in HintID

    parsed = read_workbook(make_workbook([problem("trig1"), shifted]))
    found = codes(parsed)
    assert StructuralCode.ROW_SHIFT_RIGHT in found
    assert StructuralCode.MISSING_PROBLEM_NAME not in found

    finding = next(
        f for f in parsed.all_findings if f.code == StructuralCode.ROW_SHIFT_RIGHT
    )
    assert finding.detail["shift"] == 2
    assert finding.severity is Severity.BLOCKING


def test_partial_left_shift_is_detected_by_an_identifier_in_answer_type(make_workbook):
    """Real: 12 rows where the G-I group moved one column left, putting a scaffold id in
    `answerType` and the dependency in `HintID`. `answerType` is a closed set, so an
    identifier there can only be displaced content."""
    path = make_workbook(
        [
            problem("othertrig1"),
            cells(
                problem_name="othertrig1",
                row_type="scaffold",
                answer_type="h1",
                hint_id="1",
            ),
        ]
    )
    parsed = read_workbook(path)
    finding = next(
        f for f in parsed.all_findings if f.code == StructuralCode.COLUMN_SHIFT
    )
    assert finding.severity is Severity.BLOCKING
    assert finding.column_key is ColumnKey.ANSWER_TYPE


def test_a_genuinely_missing_name_is_still_reported(make_workbook):
    """The shift detector must not swallow the ordinary case it was carved out of."""
    path = make_workbook(
        [problem("angles1"), cells(row_type="step", answer="1", answer_type="numeric")]
    )
    parsed = read_workbook(path)
    found = codes(parsed)
    assert StructuralCode.MISSING_PROBLEM_NAME in found
    assert StructuralCode.ROW_SHIFT_RIGHT not in found


# --------------------------------------------------------------------------------------
# Conventions
# --------------------------------------------------------------------------------------


def test_consistent_h_namespace_is_detected(make_workbook):
    """Six of eleven real workbooks use `h` for scaffold ids where the rules say `s`.
    That is a house style, and enforcing `s` literally would flag all of them."""
    path = make_workbook(
        [
            problem("angles1"),
            step("angles1"),
            scaffold("angles1", "h1"),
            scaffold("angles1", "h2"),
        ]
    )
    conventions = read_workbook(path).conventions
    assert conventions.dominant_scaffold_namespace == "h"
    assert conventions.scaffold_namespace_is_consistent


def test_mixed_namespaces_are_flagged_as_inconsistent(make_workbook):
    """Two real workbooks mix `s` and `h`. Those are genuinely inconsistent, and the
    convention detector must not launder them into a house style."""
    path = make_workbook(
        [
            problem("trig1"),
            step("trig1"),
            scaffold("trig1", "s1"),
            scaffold("trig1", "h2"),
        ]
    )
    conventions = read_workbook(path).conventions
    assert not conventions.scaffold_namespace_is_consistent
    assert set(conventions.scaffold_namespaces) == {"s", "h"}


def test_naming_stem_is_detected_case_sensitively(make_workbook):
    """Stems are sometimes capitalised (`Unitcirc`). Lowercasing them would report a
    convention the workbook does not follow."""
    path = make_workbook([problem("Unitcirc1"), problem("Unitcirc2")])
    assert read_workbook(path).conventions.naming_stems == ("Unitcirc",)


def test_single_step_block_leaves_the_dependency_convention_undecided(make_workbook):
    """A block with one step is evidence of nothing. Most real blocks are this shape, so
    a detector that guesses here invents a convention for the whole workbook."""
    path = make_workbook(
        [problem("angles1"), step("angles1"), scaffold("angles1", "h1")]
    )
    conventions = read_workbook(path).conventions
    assert conventions.dependency_convention is DependencyConvention.UNDECIDED


def test_reset_per_step_is_detected_from_two_populated_steps(make_workbook):
    path = make_workbook(
        [
            problem("angles1"),
            step("angles1"),
            scaffold("angles1", "h1"),
            scaffold("angles1", "h2"),
            step("angles1"),
            scaffold("angles1", "h1"),
            scaffold("angles1", "h2"),
        ]
    )
    conventions = read_workbook(path).conventions
    assert conventions.dependency_convention is DependencyConvention.RESET_PER_STEP


def test_continuous_numbering_is_detected(make_workbook):
    path = make_workbook(
        [
            problem("angles1"),
            step("angles1"),
            scaffold("angles1", "s1"),
            scaffold("angles1", "s2"),
            step("angles1"),
            scaffold("angles1", "s3"),
            scaffold("angles1", "s4"),
        ]
    )
    conventions = read_workbook(path).conventions
    assert conventions.dependency_convention is DependencyConvention.CONTINUOUS


def test_ascii_and_latex_workbooks_are_told_apart(make_workbook):
    ascii_path = make_workbook(
        [problem("trig1", body="Evaluate cos(theta)**2"), step("trig1", answer="1")]
    )
    assert read_workbook(ascii_path).conventions.notation is Notation.ASCII

    latex_path = make_workbook(
        [
            problem("trig1", body=r"$$\frac{\sqrt{2}}{2}$$"),
            step("trig1", answer=r"$$\theta$$"),
        ]
    )
    assert read_workbook(latex_path).conventions.notation is Notation.LATEX


# --------------------------------------------------------------------------------------
# Row access
# --------------------------------------------------------------------------------------


def test_raw_values_survive_for_the_rules_that_need_them(make_workbook):
    """Date coercion is only recognisable before the value is stringified: `1/2` became
    a datetime, and the repair depends on reading its month and day."""
    path = make_workbook(
        [problem("angles1"), step("angles1", answer=datetime(2026, 1, 2))]
    )
    parsed = read_workbook(path)
    row = parsed.blocks[0].rows[1]
    assert isinstance(row.raw[ColumnKey.ANSWER], datetime)
    assert row.get(ColumnKey.ANSWER) == "2026-01-02 00:00:00"


def test_block_helpers(make_workbook):
    path = make_workbook(
        [
            problem("angles1"),
            step("angles1"),
            hint("angles1", "h1"),
            scaffold("angles1", "h2"),
        ]
    )
    block = read_workbook(path).blocks[0]
    assert block.problem_row.row_type is RowType.PROBLEM
    assert [r.row for r in block.rows_of_type(RowType.HINT)] == [4]
    assert block.contains_row(3)
    assert not block.contains_row(9)
