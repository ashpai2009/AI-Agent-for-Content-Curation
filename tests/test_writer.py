"""Working-copy and edit-application tests.

The subject here is not "does the edit land" but "can the curator's original ever be
touched, and can a partial write ever reach disk". Both answers must be no.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from openpyxl import load_workbook

from conftest import problem, step
from oatutor_council.models import (
    CellEdit,
    ColumnKey,
    RejectionCode,
    SourcePath,
)
from oatutor_council.workbook.diff import compare_workbooks
from oatutor_council.workbook.reader import read_workbook
from oatutor_council.workbook.styles import EDITED_ROW_HEIGHT
from oatutor_council.workbook.writer import (
    EditRejected,
    WorkbookWriteError,
    WorkingCopy,
    apply_edits,
    atomic_save,
    create_working_copy,
    sha256_of,
    sweep_orphan_temp_files,
)


@pytest.fixture
def workbook_source(make_workbook) -> Path:
    return make_workbook(
        [
            problem("angles1", title="Convert to radians"),
            step("angles1", answer="1/2", answer_type="numeric"),
            step("angles1", answer="3", answer_type="numeric"),
        ]
    )


@pytest.fixture
def copy(workbook_source, tmp_path) -> WorkingCopy:
    return create_working_copy(SourcePath(str(workbook_source)), tmp_path / "job")


# --------------------------------------------------------------------------------------
# The working copy
# --------------------------------------------------------------------------------------


def test_creating_a_working_copy_makes_the_source_read_only(workbook_source, copy):
    mode = stat.S_IMODE(workbook_source.stat().st_mode)
    assert not mode & stat.S_IWUSR
    assert os.access(copy.path, os.W_OK)


def test_the_working_copy_is_byte_identical_to_the_source(workbook_source, copy):
    """The copy is made with `copy2`, not through openpyxl. Routing it through a load
    and save would normalise the file before the diff ever compared it, quietly
    laundering whatever that changed."""
    assert sha256_of(copy.path) == sha256_of(workbook_source) == copy.source_sha256


def test_writing_to_the_source_itself_is_refused(workbook_source, tmp_path):
    forged = WorkingCopy(
        source=SourcePath(str(workbook_source)),
        source_sha256=sha256_of(workbook_source),
        path=workbook_source,
        tmp_dir=tmp_path,
    )
    with pytest.raises(EditRejected) as caught:
        forged.assert_not_source()
    assert caught.value.rejection.code is RejectionCode.TARGET_IS_SOURCE


def test_a_hard_link_to_the_source_is_refused(workbook_source, tmp_path):
    """A path comparison alone passes this and destroys the original. The inode check is
    the reason it does not."""
    link = tmp_path / "looks-different.xlsx"
    os.link(workbook_source, link)
    forged = WorkingCopy(
        source=SourcePath(str(workbook_source)),
        source_sha256=sha256_of(workbook_source),
        path=link,
        tmp_dir=tmp_path,
    )
    with pytest.raises(EditRejected) as caught:
        forged.assert_not_source()
    assert caught.value.rejection.code is RejectionCode.TARGET_IS_SOURCE


def test_a_source_that_changed_on_disk_stops_the_write(workbook_source, copy):
    workbook_source.chmod(0o644)
    workbook_source.write_bytes(workbook_source.read_bytes() + b"tampered")
    with pytest.raises(WorkbookWriteError, match="changed on disk"):
        apply_edits(copy, [CellEdit(row=3, column=5, before="1/2", after="1/3")])


# --------------------------------------------------------------------------------------
# Applying edits
# --------------------------------------------------------------------------------------


def test_an_edit_is_applied_and_recorded(copy, workbook_source):
    before_hash = sha256_of(workbook_source)
    records = apply_edits(
        copy,
        [CellEdit(row=3, column=5, column_key=ColumnKey.ANSWER, before="1/2", after="1/3")],
        issue_id="issue-1",
        patch_id="patch-1",
        block_id="block-0000",
    )
    assert [(r.row, r.column, r.before, r.after) for r in records] == [
        (3, 5, "1/2", "1/3")
    ]
    assert read_workbook(copy.path).blocks[0].rows[1].get(ColumnKey.ANSWER) == "1/3"
    assert sha256_of(workbook_source) == before_hash


def test_a_stale_before_is_rejected_and_nothing_is_written(copy):
    """`before` is not decoration. A patch written against a block that a sibling edit
    has since changed must fail loudly rather than overwrite the newer value."""
    unchanged = sha256_of(copy.path)
    with pytest.raises(EditRejected) as caught:
        apply_edits(copy, [CellEdit(row=3, column=5, before="7/8", after="1/3")])
    assert caught.value.rejection.code is RejectionCode.BEFORE_MISMATCH
    assert caught.value.rejection.detail["actual"] == "1/2"
    assert sha256_of(copy.path) == unchanged


def test_a_batch_with_one_stale_edit_applies_none_of_it(copy):
    """A patch is one unit. A half-applied structural repair is a corruption no reviewer
    was ever shown, so verification runs over the whole batch before anything is
    written."""
    unchanged = sha256_of(copy.path)
    with pytest.raises(EditRejected):
        apply_edits(
            copy,
            [
                CellEdit(row=3, column=5, before="1/2", after="1/3"),
                CellEdit(row=4, column=5, before="wrong", after="4"),
            ],
        )
    assert sha256_of(copy.path) == unchanged
    assert read_workbook(copy.path).blocks[0].rows[1].get(ColumnKey.ANSWER) == "1/2"


def test_two_edits_to_the_same_cell_are_rejected(copy):
    with pytest.raises(EditRejected) as caught:
        apply_edits(
            copy,
            [
                CellEdit(row=3, column=5, before="1/2", after="1/3"),
                CellEdit(row=3, column=5, before="1/2", after="1/4"),
            ],
        )
    assert caught.value.rejection.code is RejectionCode.DUPLICATE_CELL_EDIT


def test_no_edits_is_a_no_op(copy):
    unchanged = sha256_of(copy.path)
    assert apply_edits(copy, []) == ()
    assert sha256_of(copy.path) == unchanged


# --------------------------------------------------------------------------------------
# Excel re-coercion
# --------------------------------------------------------------------------------------


def test_an_answer_is_written_as_text_so_excel_cannot_re_coerce_it(copy):
    """The defect being repaired is Excel turning `1/2` into a date. Writing the repair
    into a general-format cell hands the same defect straight back."""
    apply_edits(
        copy,
        [CellEdit(row=3, column=5, column_key=ColumnKey.ANSWER, before="1/2", after="1/3")],
    )
    workbook = load_workbook(copy.path)
    cell = workbook.active.cell(row=3, column=5)
    assert cell.number_format == "@"
    assert cell.data_type == "s"
    workbook.close()


def test_a_correction_beginning_with_equals_does_not_become_a_formula(copy):
    """openpyxl infers a formula from a leading `=`. A corrected answer of `=1/2` would
    silently become a live formula displaying `0.5`."""
    apply_edits(
        copy,
        [
            CellEdit(
                row=3, column=5, column_key=ColumnKey.ANSWER, before="1/2", after="=1/2"
            )
        ],
    )
    workbook = load_workbook(copy.path)
    cell = workbook.active.cell(row=3, column=5)
    assert cell.data_type == "s"
    assert cell.value == "=1/2"
    workbook.close()


# --------------------------------------------------------------------------------------
# Appearance
# --------------------------------------------------------------------------------------


def test_the_edited_row_gets_the_appearance_rule_and_other_rows_do_not(copy):
    apply_edits(copy, [CellEdit(row=3, column=5, before="1/2", after="1/3")])
    workbook = load_workbook(copy.path)
    sheet = workbook.active
    assert sheet.row_dimensions[3].height == EDITED_ROW_HEIGHT
    assert sheet.row_dimensions[4].height is None
    assert sheet.cell(row=3, column=3).alignment.wrap_text in (None, False)
    workbook.close()


def test_the_only_differences_from_source_are_the_edit_and_its_row(copy, workbook_source):
    """End to end: apply one edit, then diff against the untouched source. Anything
    beyond the edit itself and the authorised row appearance is damage."""
    apply_edits(
        copy,
        [CellEdit(row=3, column=5, column_key=ColumnKey.ANSWER, before="1/2", after="1/3")],
    )
    differences = compare_workbooks(workbook_source, copy.path)
    assert {d.row for d in differences} == {3}
    assert {str(d.dimension) for d in differences} <= {
        "cell_value",
        "cell_type",
        "number_format",
        "row_height",
    }


# --------------------------------------------------------------------------------------
# Atomic save
# --------------------------------------------------------------------------------------


def test_a_successful_save_leaves_no_temp_files(copy):
    apply_edits(copy, [CellEdit(row=3, column=5, before="1/2", after="1/3")])
    assert list(copy.tmp_dir.glob("*.tmp.xlsx")) == []


def test_a_cross_filesystem_temp_directory_is_refused(copy, monkeypatch):
    """`os.replace` across filesystems degrades to a non-atomic copy, so a crash mid-move
    would leave a truncated workbook. The guard has to fire before the save, not after."""
    from oatutor_council.workbook import writer as writer_module

    monkeypatch.setattr(
        writer_module,
        "device_of",
        lambda path: 1 if path == copy.tmp_dir else 2,
    )
    workbook = load_workbook(copy.path)
    with pytest.raises(WorkbookWriteError, match="different filesystem"):
        atomic_save(workbook, copy.path, copy.tmp_dir)
    workbook.close()


def test_a_failed_save_removes_its_temp_file_and_leaves_the_target(copy, monkeypatch):
    unchanged = sha256_of(copy.path)
    workbook = load_workbook(copy.path)
    monkeypatch.setattr(
        type(workbook), "save", lambda self, path: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        atomic_save(workbook, copy.path, copy.tmp_dir)
    assert list(copy.tmp_dir.glob("*.tmp.xlsx")) == []
    assert sha256_of(copy.path) == unchanged
    workbook.close()


def test_orphan_temp_files_are_swept(copy):
    """A temp file is never the authority for anything -- `os.replace` either happened or
    it did not -- so a leftover is unambiguously garbage."""
    (copy.tmp_dir / "working.abc.tmp.xlsx").write_bytes(b"junk")
    assert sweep_orphan_temp_files(copy.tmp_dir) == ("working.abc.tmp.xlsx",)
    assert list(copy.tmp_dir.glob("*.tmp.xlsx")) == []
