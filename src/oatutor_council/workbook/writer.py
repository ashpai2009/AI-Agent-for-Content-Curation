"""Apply cell edits to a job-local working copy. The source is never opened for writing.

Source immutability is defended four ways, and this module owns three of them:

1. `SourcePath` is a distinct type. No function here accepts one as a write target, so a
   mixed-up argument is a type error rather than a destroyed original.
2. The source is `chmod 0444` the moment the working copy is made.
3. Every write asserts the target is not the source, by resolved path *and* by inode --
   a symlink or hard link would defeat a path comparison alone.

The fourth is hash re-verification at every resume and at finalisation, which lives with
the orchestrator because it needs the durable record of what the hash was.

Writes are atomic. The workbook is saved into a temp file inside the job directory,
fsynced, reopened as a proof that it parses, and only then moved into place with
`os.replace`. The temp file must be on the same filesystem as the target: across
filesystems `os.replace` degrades to a copy, which is exactly the non-atomic write the
design is trying to avoid, so that is asserted rather than assumed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook

from ..models import (
    FIXED_COLUMNS,
    CellEdit,
    ChangeRecord,
    ColumnKey,
    PatchRejection,
    RejectionCode,
    SourcePath,
)
from .reader import render_cell
from .styles import apply_edited_row_appearance, write_cell_text

READ_ONLY_MODE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH  # 0o444

#: Marks an in-flight save. The `.xlsx` tail is required because openpyxl decides
#: whether it will open a file from its extension, and the atomic write reopens the
#: temp file to prove it parses before it replaces anything.
TEMP_SUFFIX = ".tmp.xlsx"


class WorkbookWriteError(Exception):
    """The working copy could not be written safely."""


class EditRejected(Exception):
    """An edit failed the deterministic checks and was not applied.

    Carries the `PatchRejection` so the caller can decide whether the attempt is
    consumed. Nothing is written when this is raised: the edits are all verified before
    any of them is applied, and the save happens once at the end.
    """

    def __init__(self, rejection: PatchRejection) -> None:
        super().__init__(rejection.message)
        self.rejection = rejection


def device_of(path: Path) -> int:
    """Filesystem device id. A seam so the cross-filesystem guard is testable without
    needing a second real filesystem mounted on the machine running the tests."""
    return path.stat().st_dev


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class WorkingCopy:
    """A job-local copy of the workbook, and the only thing anything is allowed to edit."""

    source: SourcePath
    source_sha256: str
    path: Path
    tmp_dir: Path

    def assert_not_source(self) -> None:
        """Prove the write target is not the curator's file.

        Compared by resolved path and by `(device, inode)`. A hard link or symlink to
        the source has a different path and the same inode, and writing through one
        would destroy the original just as thoroughly.
        """
        source = Path(self.source)
        if self.path.resolve() == source.resolve():
            raise EditRejected(
                PatchRejection(
                    code=RejectionCode.TARGET_IS_SOURCE,
                    message="refusing to write: the target is the source workbook",
                )
            )
        if self.path.exists() and source.exists():
            target_stat, source_stat = self.path.stat(), source.stat()
            if (target_stat.st_dev, target_stat.st_ino) == (
                source_stat.st_dev,
                source_stat.st_ino,
            ):
                raise EditRejected(
                    PatchRejection(
                        code=RejectionCode.TARGET_IS_SOURCE,
                        message=(
                            "refusing to write: the target is a link to the source "
                            "workbook (same device and inode)"
                        ),
                    )
                )

    def verify_source_unchanged(self) -> None:
        actual = sha256_of(Path(self.source))
        if actual != self.source_sha256:
            raise WorkbookWriteError(
                f"source workbook changed on disk: expected {self.source_sha256}, "
                f"found {actual}"
            )


def create_working_copy(
    source: SourcePath, job_dir: Path, *, filename: str = "working.xlsx"
) -> WorkingCopy:
    """Copy the source into the job directory and make the original read-only.

    `copy2` preserves the bytes exactly -- the copy is not routed through openpyxl,
    because loading and saving would normalise the file before the diff ever has a
    chance to compare it against the original.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise WorkbookWriteError(f"source workbook does not exist: {source_path.name}")

    work_dir = job_dir / "work"
    tmp_dir = work_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / filename

    digest = sha256_of(source_path)
    shutil.copy2(source_path, target)
    os.chmod(source_path, READ_ONLY_MODE)
    # The copy must stay writable even though its parent is now read-only.
    os.chmod(target, target.stat().st_mode | stat.S_IWUSR)

    copy = WorkingCopy(
        source=source, source_sha256=digest, path=target, tmp_dir=tmp_dir
    )
    copy.assert_not_source()
    return copy


def atomic_save(workbook: Workbook, target: Path, tmp_dir: Path) -> None:
    """Save the workbook so the target is either wholly old or wholly new.

    The reopen step is not paranoia about openpyxl: it is the only check that the bytes
    on disk parse *before* they replace a file the job depends on. A truncated save
    caught here costs a retry; caught later it costs the working copy.
    """
    target_dir = target.parent
    if device_of(tmp_dir) != device_of(target_dir):
        raise WorkbookWriteError(
            "temp directory is on a different filesystem from the workbook; "
            "os.replace would silently degrade to a non-atomic copy"
        )

    # The `.xlsx` suffix is kept: openpyxl validates the format by extension, and the
    # reopen check below is the whole point of writing to a temp file first.
    tmp_path = tmp_dir / f"{target.stem}.{uuid4().hex}{TEMP_SUFFIX}"
    try:
        workbook.save(tmp_path)
        with open(tmp_path, "rb") as handle:
            os.fsync(handle.fileno())

        # Prove it parses before it becomes the working copy.
        check = load_workbook(tmp_path, data_only=False)
        check.close()

        os.replace(tmp_path, target)
        directory = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _column_key_for(edit: CellEdit) -> ColumnKey | None:
    if edit.column_key is not None:
        return edit.column_key
    return next((k for k, i in FIXED_COLUMNS.items() if i == edit.column), None)


def apply_edits(
    copy: WorkingCopy,
    edits: Iterable[CellEdit],
    *,
    issue_id: str | None = None,
    patch_id: str | None = None,
    block_id: str | None = None,
) -> tuple[ChangeRecord, ...]:
    """Verify every edit, then apply them all, then save once.

    Verification runs over the whole batch first. A patch is one unit: if its third edit
    is stale, the first two must not be on disk, because a half-applied structural repair
    is a corruption that no reviewer was ever shown.

    `before` is checked against what the cell actually holds. This is what makes a stale
    patch -- one written against a block a sibling edit has since changed -- fail loudly
    instead of overwriting a newer value with an older author's assumptions.
    """
    copy.assert_not_source()
    copy.verify_source_unchanged()

    batch = list(edits)
    if not batch:
        return ()

    seen: set[tuple[int, int]] = set()
    for edit in batch:
        if (edit.row, edit.column) in seen:
            raise EditRejected(
                PatchRejection(
                    code=RejectionCode.DUPLICATE_CELL_EDIT,
                    message="two edits target the same cell",
                    row=edit.row,
                    column=edit.column,
                )
            )
        seen.add((edit.row, edit.column))

    workbook = load_workbook(copy.path, data_only=False)
    try:
        sheet = workbook.active
        width = max(sheet.max_column or 0, max(FIXED_COLUMNS.values()))

        for edit in batch:
            actual = render_cell(sheet.cell(row=edit.row, column=edit.column).value)
            if actual != edit.before:
                raise EditRejected(
                    PatchRejection(
                        code=RejectionCode.BEFORE_MISMATCH,
                        message=(
                            f"cell holds {actual!r}, but the patch was written against "
                            f"{edit.before!r}"
                        ),
                        row=edit.row,
                        column=edit.column,
                        detail={"actual": actual, "expected": edit.before},
                    )
                )

        applied_at = datetime.now(timezone.utc)
        records: list[ChangeRecord] = []
        for edit in batch:
            key = _column_key_for(edit)
            write_cell_text(sheet.cell(row=edit.row, column=edit.column), edit.after, key)
            records.append(
                ChangeRecord(
                    change_id=uuid4().hex,
                    issue_id=issue_id,
                    patch_id=patch_id,
                    block_id=block_id,
                    row=edit.row,
                    column=edit.column,
                    column_key=key,
                    before=edit.before,
                    after=edit.after,
                    applied_at=applied_at,
                )
            )

        for row in sorted({edit.row for edit in batch}):
            apply_edited_row_appearance(sheet, row, width)

        atomic_save(workbook, copy.path, copy.tmp_dir)
        return tuple(records)
    finally:
        workbook.close()


def sweep_orphan_temp_files(tmp_dir: Path) -> tuple[str, ...]:
    """Delete temp files left behind by a crash.

    A temp file is never the authority for anything -- `os.replace` either happened or
    it did not -- so a leftover is unambiguously garbage and removing it can lose no
    committed work.
    """
    removed = []
    for path in sorted(tmp_dir.glob(f"*{TEMP_SUFFIX}")):
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return tuple(removed)
