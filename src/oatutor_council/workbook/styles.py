"""The appearance contract.

Exactly two appearance changes are authorised anywhere in this system, both confined to
rows an accepted edit actually touched: `wrap_text` is turned off, and the row height is
set to 15. Everything else -- column widths, fonts, colours, borders, alignment beyond
wrap, number formats on untouched cells -- is preserved, and the final gate fails on any
difference it cannot trace to an accepted edit or to these two rules.

The rules apply to the whole edited row rather than the edited cell alone. That is the
stated contract, and it is the reason `EDITED_ROW_DIMENSIONS` exists as a named constant:
the reconciler has to recognise the change it is being asked to forgive, and a rule
nobody can enumerate is indistinguishable from damage.
"""

from __future__ import annotations

from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment
from openpyxl.worksheet.worksheet import Worksheet

from ..models import TEXT_FORCED_COLUMNS, ColumnKey

#: Height applied to any row carrying an accepted edit.
EDITED_ROW_HEIGHT = 15.0

#: Excel's text number format. Answer and choice cells are written with it because a
#: repaired `1/3` in a general-format cell is silently re-coerced to a date on the next
#: open -- which is the very defect being repaired.
TEXT_NUMBER_FORMAT = "@"

#: The properties the edited-row rule is allowed to change, named so that the diff
#: reconciler can recognise them and forgive nothing else.
EDITED_ROW_DIMENSIONS = ("alignment.wrap_text", "row_height")


def clear_wrap_text(cell: Cell) -> None:
    """Turn wrap off while preserving every other alignment property.

    `Alignment` is immutable in openpyxl, so this copies the existing object rather than
    assigning a fresh one -- a fresh `Alignment(wrap_text=False)` would silently discard
    the cell's horizontal, vertical, indent and rotation settings.
    """
    current = cell.alignment
    if not current.wrap_text:
        return
    cell.alignment = Alignment(
        horizontal=current.horizontal,
        vertical=current.vertical,
        text_rotation=current.text_rotation,
        wrap_text=False,
        shrink_to_fit=current.shrink_to_fit,
        indent=current.indent,
        justifyLastLine=current.justifyLastLine,
        readingOrder=current.readingOrder,
        relativeIndent=current.relativeIndent,
    )


def apply_edited_row_appearance(sheet: Worksheet, row: int, width: int) -> None:
    """Apply the edited-row rule to one row."""
    for column in range(1, width + 1):
        clear_wrap_text(sheet.cell(row=row, column=column))
    sheet.row_dimensions[row].height = EDITED_ROW_HEIGHT


def write_cell_text(cell: Cell, text: str, column_key: ColumnKey | None) -> None:
    """Write a correction as text, defeating Excel's re-coercion.

    Two coercions are defended against. Answer and choice cells get the text number
    format, so a fraction stays a fraction instead of becoming a date. And any value
    beginning with `=` would be stored as a *formula* by openpyxl's type inference: a
    corrected answer such as `=1/2` would become a live formula whose displayed value is
    `0.5`, so the data type is forced back to string.
    """
    cell.value = text
    if column_key in TEXT_FORCED_COLUMNS:
        cell.number_format = TEXT_NUMBER_FORMAT
    if text.startswith("=") and cell.data_type == "f":
        cell.data_type = "s"
