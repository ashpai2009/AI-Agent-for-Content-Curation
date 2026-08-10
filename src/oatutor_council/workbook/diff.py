"""Compare the source workbook against the output across every dimension.

Byte comparison is not available: openpyxl rewrites the archive, so two files with
identical content differ as bytes. The comparison is therefore structural, and the list
of compared dimensions is **explicit and exhaustive** rather than "the things that
seemed likely to change".

There is deliberately no `FORMATTING_NORMALIZATION` category. A general "openpyxl
probably did this" bucket would absorb genuine damage -- a lost merged range and a
harmless rounding both land in it -- so any difference not traceable to an accepted
`CellEdit` or to the edited-row rule fails the gate.

Measured, not assumed: a no-edit load-and-save of the real workbooks changes nothing at
all across cells, styles, geometry, merges, validations, hyperlinks or images. Exactly
two normalisations were observed, and each gets its own narrow exception rather than a
shared category:

* `shrink_to_fit` moves between `False` and `None`. In OOXML an absent attribute means
  false, so `None` is canonicalised to `False` for boolean style properties. A genuine
  `True -> None` is still reported.
* A cell holding an empty string is rewritten as a genuinely empty cell, changing its
  type from `inlineStr` to `n`. Both render as `""`, so the type comparison is skipped
  only when the cell is empty on **both** sides. A cell losing real content still fails.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, ConfigDict

from ..models import TEXT_FORCED_COLUMNS, ChangeRecord
from .reader import render_cell
from .styles import EDITED_ROW_HEIGHT, TEXT_NUMBER_FORMAT


class DiffDimension(StrEnum):
    """Every dimension compared. Adding a feature to the writer means adding it here."""

    SHEET_SET = "sheet_set"
    SHEET_ORDER = "sheet_order"
    SHEET_VISIBILITY = "sheet_visibility"
    CELL_VALUE = "cell_value"
    CELL_TYPE = "cell_type"
    NUMBER_FORMAT = "number_format"
    FONT = "font"
    FILL = "fill"
    BORDER = "border"
    ALIGNMENT = "alignment"
    ROW_HEIGHT = "row_height"
    ROW_HIDDEN = "row_hidden"
    COLUMN_WIDTH = "column_width"
    COLUMN_HIDDEN = "column_hidden"
    MERGED_RANGES = "merged_ranges"
    FREEZE_PANES = "freeze_panes"
    DATA_VALIDATION = "data_validation"
    HYPERLINK = "hyperlink"
    IMAGE = "image"


class WorkbookDifference(BaseModel):
    """One difference between source and output, located as precisely as it exists."""

    model_config = ConfigDict(frozen=True)

    dimension: DiffDimension
    sheet: str | None = None
    row: int | None = None
    column: int | None = None
    prop: str = ""
    before: str = ""
    after: str = ""

    @property
    def location(self) -> str:
        if self.row is not None and self.column is not None:
            return f"{self.sheet}!{get_column_letter(self.column)}{self.row}"
        if self.row is not None:
            return f"{self.sheet}!row {self.row}"
        if self.column is not None:
            return f"{self.sheet}!column {get_column_letter(self.column)}"
        return self.sheet or "workbook"

    def describe(self) -> str:
        name = f"{self.dimension}.{self.prop}" if self.prop else str(self.dimension)
        return f"{self.location}: {name} {self.before!r} -> {self.after!r}"


# --------------------------------------------------------------------------------------
# Property extraction
# --------------------------------------------------------------------------------------


def _flag(value: Any) -> bool:
    """Canonicalise a boolean style property.

    In OOXML an absent attribute means false, and openpyxl reports it as `None`. Only
    `None` and `False` are equated; `True` stays distinct, so a genuine loss of a set
    flag is still reported.
    """
    return bool(value)


def _colour(colour: Any) -> str:
    if colour is None:
        return ""
    return f"{colour.type}:{colour.rgb}:{colour.theme}:{colour.tint}"


def _font_properties(cell: Cell) -> dict[str, str]:
    font = cell.font
    return {
        "name": str(font.name),
        "size": str(font.sz),
        "bold": str(_flag(font.b)),
        "italic": str(_flag(font.i)),
        "underline": str(font.u or ""),
        "strike": str(_flag(font.strike)),
        "vert_align": str(font.vertAlign or ""),
        "color": _colour(font.color),
    }


def _fill_properties(cell: Cell) -> dict[str, str]:
    fill = cell.fill
    return {
        "fill_type": str(fill.fill_type or ""),
        "fg_color": _colour(getattr(fill, "fgColor", None)),
        "bg_color": _colour(getattr(fill, "bgColor", None)),
    }


def _border_properties(cell: Cell) -> dict[str, str]:
    border = cell.border
    properties: dict[str, str] = {}
    for side_name in ("left", "right", "top", "bottom", "diagonal"):
        side = getattr(border, side_name, None)
        properties[f"{side_name}.style"] = str(getattr(side, "style", None) or "")
        properties[f"{side_name}.color"] = _colour(getattr(side, "color", None))
    properties["diagonal_up"] = str(_flag(border.diagonalUp))
    properties["diagonal_down"] = str(_flag(border.diagonalDown))
    return properties


def _alignment_properties(cell: Cell) -> dict[str, str]:
    alignment = cell.alignment
    return {
        "horizontal": str(alignment.horizontal or ""),
        "vertical": str(alignment.vertical or ""),
        "wrap_text": str(_flag(alignment.wrap_text)),
        "shrink_to_fit": str(_flag(alignment.shrink_to_fit)),
        "indent": str(alignment.indent or 0),
        "text_rotation": str(alignment.text_rotation or 0),
    }


_CELL_STYLE_EXTRACTORS = (
    (DiffDimension.FONT, _font_properties),
    (DiffDimension.FILL, _fill_properties),
    (DiffDimension.BORDER, _border_properties),
    (DiffDimension.ALIGNMENT, _alignment_properties),
)


# --------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------


def _compare_cells(
    source: Worksheet, output: Worksheet, name: str
) -> list[WorkbookDifference]:
    differences: list[WorkbookDifference] = []
    rows = max(source.max_row or 0, output.max_row or 0)
    columns = max(source.max_column or 0, output.max_column or 0)

    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            a = source.cell(row=row, column=column)
            b = output.cell(row=row, column=column)

            before, after = render_cell(a.value), render_cell(b.value)
            if before != after:
                differences.append(
                    WorkbookDifference(
                        dimension=DiffDimension.CELL_VALUE,
                        sheet=name,
                        row=row,
                        column=column,
                        before=before,
                        after=after,
                    )
                )
            # Narrow, deliberate exception -- the only one in this module. openpyxl
            # writes a cell holding an empty string as a genuinely empty cell, moving
            # its type from `inlineStr` to `n`. Both render as "", so nothing is lost.
            # Scoped to cells that are empty on *both* sides, so a cell losing real
            # content is still reported.
            both_empty = not before and not after
            if a.data_type != b.data_type and not both_empty:
                differences.append(
                    WorkbookDifference(
                        dimension=DiffDimension.CELL_TYPE,
                        sheet=name,
                        row=row,
                        column=column,
                        before=a.data_type,
                        after=b.data_type,
                    )
                )
            before_link, after_link = _hyperlink_target(a), _hyperlink_target(b)
            if before_link != after_link:
                differences.append(
                    WorkbookDifference(
                        dimension=DiffDimension.HYPERLINK,
                        sheet=name,
                        row=row,
                        column=column,
                        before=before_link,
                        after=after_link,
                    )
                )
            if a.number_format != b.number_format:
                differences.append(
                    WorkbookDifference(
                        dimension=DiffDimension.NUMBER_FORMAT,
                        sheet=name,
                        row=row,
                        column=column,
                        before=a.number_format,
                        after=b.number_format,
                    )
                )
            for dimension, extract in _CELL_STYLE_EXTRACTORS:
                left, right = extract(a), extract(b)
                for prop, value in left.items():
                    if right[prop] != value:
                        differences.append(
                            WorkbookDifference(
                                dimension=dimension,
                                sheet=name,
                                row=row,
                                column=column,
                                prop=prop,
                                before=value,
                                after=right[prop],
                            )
                        )
    return differences


def _compare_geometry(
    source: Worksheet, output: Worksheet, name: str
) -> list[WorkbookDifference]:
    differences: list[WorkbookDifference] = []

    for row in sorted(set(source.row_dimensions) | set(output.row_dimensions)):
        a, b = source.row_dimensions[row], output.row_dimensions[row]
        if a.height != b.height:
            differences.append(
                WorkbookDifference(
                    dimension=DiffDimension.ROW_HEIGHT,
                    sheet=name,
                    row=row,
                    before=str(a.height),
                    after=str(b.height),
                )
            )
        if _flag(a.hidden) != _flag(b.hidden):
            differences.append(
                WorkbookDifference(
                    dimension=DiffDimension.ROW_HIDDEN,
                    sheet=name,
                    row=row,
                    before=str(_flag(a.hidden)),
                    after=str(_flag(b.hidden)),
                )
            )

    for letter in sorted(set(source.column_dimensions) | set(output.column_dimensions)):
        a, b = source.column_dimensions[letter], output.column_dimensions[letter]
        if a.width != b.width:
            differences.append(
                WorkbookDifference(
                    dimension=DiffDimension.COLUMN_WIDTH,
                    sheet=name,
                    prop=letter,
                    before=str(a.width),
                    after=str(b.width),
                )
            )
        if _flag(a.hidden) != _flag(b.hidden):
            differences.append(
                WorkbookDifference(
                    dimension=DiffDimension.COLUMN_HIDDEN,
                    sheet=name,
                    prop=letter,
                    before=str(_flag(a.hidden)),
                    after=str(_flag(b.hidden)),
                )
            )
    return differences


def _compare_features(
    source: Worksheet, output: Worksheet, name: str
) -> list[WorkbookDifference]:
    differences: list[WorkbookDifference] = []

    a_merged = sorted(str(r) for r in source.merged_cells.ranges)
    b_merged = sorted(str(r) for r in output.merged_cells.ranges)
    if a_merged != b_merged:
        differences.append(
            WorkbookDifference(
                dimension=DiffDimension.MERGED_RANGES,
                sheet=name,
                before=", ".join(a_merged),
                after=", ".join(b_merged),
            )
        )

    if source.freeze_panes != output.freeze_panes:
        differences.append(
            WorkbookDifference(
                dimension=DiffDimension.FREEZE_PANES,
                sheet=name,
                before=str(source.freeze_panes),
                after=str(output.freeze_panes),
            )
        )

    a_dv = sorted(_render_validations(source))
    b_dv = sorted(_render_validations(output))
    if a_dv != b_dv:
        differences.append(
            WorkbookDifference(
                dimension=DiffDimension.DATA_VALIDATION,
                sheet=name,
                before=" | ".join(a_dv),
                after=" | ".join(b_dv),
            )
        )

    a_images = sorted(_render_images(source))
    b_images = sorted(_render_images(output))
    if a_images != b_images:
        differences.append(
            WorkbookDifference(
                dimension=DiffDimension.IMAGE,
                sheet=name,
                before=" | ".join(a_images),
                after=" | ".join(b_images),
            )
        )
    return differences


def _render_validations(sheet: Worksheet) -> list[str]:
    return [
        f"{dv.type}:{dv.operator}:{dv.formula1}:{dv.formula2}:{dv.sqref}"
        for dv in sheet.data_validations.dataValidation
    ]


def _hyperlink_target(cell: Cell) -> str:
    """Read the link off the cell, not off `Worksheet._hyperlinks`.

    That list is the serialisation form and is only rebuilt on save, so a link removed
    from a cell in memory is still present in it. Reading the cell is what a curator
    would see, and it is what actually gets written.
    """
    link = cell.hyperlink
    if link is None:
        return ""
    return str(link.target or link.location or "")


def _render_images(sheet: Worksheet) -> list[str]:
    """Identify an image by its anchor and a digest of its bytes.

    `Image.ref` is a `BytesIO` once the workbook has been loaded, so comparing it
    compares object identity -- two byte-identical images would differ every time, and
    the diff would report damage on every job that touches a workbook with an image.
    """
    rendered = []
    for image in sheet._images:
        marker = getattr(getattr(image, "anchor", None), "_from", None)
        position = f"r{marker.row}c{marker.col}" if marker is not None else "?"
        rendered.append(f"{position}:{_image_digest(image)}")
    return rendered


def _image_digest(image: Any) -> str:
    try:
        data = image._data()
    except Exception:  # pragma: no cover - openpyxl shape varies by anchor type
        return "unreadable"
    return hashlib.sha256(data).hexdigest()[:16]


def compare_workbooks(source: Path, output: Path) -> tuple[WorkbookDifference, ...]:
    """Compare two workbook files across every dimension in `DiffDimension`."""
    a = load_workbook(source, data_only=False)
    b = load_workbook(output, data_only=False)
    try:
        return tuple(_compare(a, b))
    finally:
        a.close()
        b.close()


def _compare(a: Workbook, b: Workbook) -> list[WorkbookDifference]:
    differences: list[WorkbookDifference] = []

    missing = set(a.sheetnames) - set(b.sheetnames)
    added = set(b.sheetnames) - set(a.sheetnames)
    if missing or added:
        differences.append(
            WorkbookDifference(
                dimension=DiffDimension.SHEET_SET,
                before=", ".join(a.sheetnames),
                after=", ".join(b.sheetnames),
            )
        )
    elif a.sheetnames != b.sheetnames:
        differences.append(
            WorkbookDifference(
                dimension=DiffDimension.SHEET_ORDER,
                before=", ".join(a.sheetnames),
                after=", ".join(b.sheetnames),
            )
        )

    for name in a.sheetnames:
        if name not in b.sheetnames:
            continue
        source_sheet, output_sheet = a[name], b[name]
        if source_sheet.sheet_state != output_sheet.sheet_state:
            differences.append(
                WorkbookDifference(
                    dimension=DiffDimension.SHEET_VISIBILITY,
                    sheet=name,
                    before=source_sheet.sheet_state,
                    after=output_sheet.sheet_state,
                )
            )
        differences.extend(_compare_cells(source_sheet, output_sheet, name))
        differences.extend(_compare_geometry(source_sheet, output_sheet, name))
        differences.extend(_compare_features(source_sheet, output_sheet, name))

    return differences


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def net_changes(
    changes: Iterable[ChangeRecord],
) -> dict[tuple[int, int], ChangeRecord]:
    """Collapse each cell's edit history into its net effect.

    The change log is a *history* and the diff is a *net*, and conflating them fails a
    perfectly good job. A cell repaired once and then revised after review carries two
    records -- `'' -> '30'` and `'30' -> '31'` -- while the workbook shows a single
    difference of `'' -> '31'`. Matching differences against individual records finds
    neither, calls the change unexplained, and fails the integrity gate on a job that did
    exactly what it was supposed to.

    Every individual record still appears in the change log a curator reads: this is only
    how the two views are compared.
    """
    ordered: dict[tuple[int, int], list[ChangeRecord]] = {}
    for change in sorted(changes, key=lambda c: c.applied_at):
        ordered.setdefault((change.row, change.column), []).append(change)

    return {
        cell: history[0].model_copy(
            update={
                "after": history[-1].after,
                "column_key": history[-1].column_key,
            }
        )
        for cell, history in ordered.items()
    }


def reconcile(
    differences: Iterable[WorkbookDifference],
    changes: Iterable[ChangeRecord],
    *,
    sheet_name: str,
) -> tuple[WorkbookDifference, ...]:
    """Return the differences no accepted change accounts for.

    A difference is explained only by an exact match against the change ledger, or by
    the edited-row appearance rule on a row the ledger actually touched. Everything else
    survives, and a non-empty result fails the final gate.

    The asymmetry is deliberate. An unexplained difference is either a bug or damage,
    and there is no third possibility worth a category of its own.
    """
    ledger = net_changes(changes)
    edited_rows = {c.row for c in ledger.values()}
    unexplained: list[WorkbookDifference] = []

    for difference in differences:
        if difference.sheet != sheet_name:
            unexplained.append(difference)
            continue
        if not _is_explained(difference, ledger, edited_rows):
            unexplained.append(difference)
    return tuple(unexplained)


def _is_explained(
    difference: WorkbookDifference,
    ledger: dict[tuple[int, int], ChangeRecord],
    edited_rows: set[int],
) -> bool:
    dimension = difference.dimension

    if dimension is DiffDimension.CELL_VALUE:
        change = ledger.get((difference.row or 0, difference.column or 0))
        return (
            change is not None
            and change.before == difference.before
            and change.after == difference.after
        )

    if dimension is DiffDimension.CELL_TYPE:
        # Corrections are written as text, so a cell that used to hold a number or a
        # date legitimately becomes a string. Only a cell the ledger names may do so.
        return (difference.row or 0, difference.column or 0) in ledger

    if dimension is DiffDimension.NUMBER_FORMAT:
        change = ledger.get((difference.row or 0, difference.column or 0))
        return (
            change is not None
            and change.column_key in TEXT_FORCED_COLUMNS
            and difference.after == TEXT_NUMBER_FORMAT
        )

    if dimension is DiffDimension.ALIGNMENT:
        # The edited-row rule turns wrap off across the row, and nothing else.
        return (
            difference.prop == "wrap_text"
            and difference.after == "False"
            and difference.row in edited_rows
        )

    if dimension is DiffDimension.ROW_HEIGHT:
        return (
            difference.row in edited_rows
            and difference.after == str(EDITED_ROW_HEIGHT)
        )

    return False
