"""Read a workbook into `ParsedWorkbook`. Never writes.

The reader's job is to be honest about a file it does not fully understand. Real
workbooks in this corpus contain shifted rows, duplicated headers, names that disagree
with structure, and content sitting above the first problem row. For every one of those
the reader records a `ValidationFinding` and carries on, because the alternative --
picking whichever interpretation parses cleanly -- is exactly how a column-shift
corruption becomes invisible.

Two segmentation rules are load-bearing:

* A block starts at a row whose `Row Type` is `problem` and runs to the row before the
  next such row. `Problem Name` is a *validation* signal, never the segmentation signal.
* A blank row never ends a block. Blank rows do separate blocks in practice, but a stray
  blank in the middle of one must not truncate it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from ..models import (
    FIXED_COLUMNS,
    HEADER_LABELS,
    NAMED_COLUMNS,
    ColumnKey,
    ColumnMap,
    DependencyConvention,
    FindingScope,
    Notation,
    ParsedWorkbook,
    ProblemBlock,
    RowType,
    Severity,
    StructuralCode,
    ValidationFinding,
    WorkbookConventions,
    WorkbookRow,
)

#: How far down to look for the header row. Every real workbook has it at row 1, but
#: `7.5` proves leading blank rows exist, so the scan is bounded rather than assumed.
MAX_HEADER_SCAN_ROWS = 20

#: Trailing identifier digits, e.g. `angles12` -> stem `angles`.
_STEM_PATTERN = re.compile(r"^(.*?)(\d+)$")

#: A scaffold/hint identifier: a namespace letter followed by a number.
_IDENTIFIER_PATTERN = re.compile(r"^([A-Za-z]+)(\d+)$")

_LATEX_MARKERS = ("$$", "\\frac", "\\sqrt", "\\theta", "\\pi", "\\left", "\\right")

#: The columns that are pure mathematics, and therefore the only reliable evidence of
#: which notation a workbook is written in. Titles and body text are prose in *both*
#: conventions: including them drags the one genuinely LaTeX workbook in the corpus down
#: to a 0.55 ratio and misclassifies it, while these two columns alone separate it
#: cleanly at 0.83 against 0.00 everywhere else.
_NOTATION_COLUMNS = (ColumnKey.ANSWER, ColumnKey.MC_CHOICES)

#: Share of those cells that must be LaTeX before the whole workbook counts as LaTeX.
_LATEX_MAJORITY = 0.6


class WorkbookReadError(Exception):
    """The file could not be interpreted as an OATutor workbook at all.

    Raised only when there is nothing to report findings *about* -- no header row, no
    problem rows. Anything the reader can describe becomes a finding instead, so that a
    damaged workbook still reaches the council rather than being rejected at the door.
    """


# --------------------------------------------------------------------------------------
# Cell rendering
# --------------------------------------------------------------------------------------


def render_cell(value: Any) -> str:
    """Render a native Excel value as the text agents and rules see.

    Floats that are whole numbers render without the trailing `.0`: openpyxl reports an
    integer-valued cell as `float`, and a dependency of `1.0` would otherwise fail every
    identifier rule for a reason the workbook author never caused.

    Datetimes are rendered in ISO form rather than being repaired here. Recognising that
    a datetime used to be the fraction `1/2` is a *rule's* judgment, and it needs the
    native value, which is why `WorkbookRow.raw` keeps it.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


def _normalise_header(label: Any) -> str:
    return re.sub(r"\s+", " ", render_cell(label)).strip().casefold()


# --------------------------------------------------------------------------------------
# Header and column resolution
# --------------------------------------------------------------------------------------


def find_header_row(sheet: Worksheet, max_scan: int = MAX_HEADER_SCAN_ROWS) -> int:
    """Locate the header row by looking for `Problem Name` in column A.

    Detected, not assumed: `7.5` has a blank row where other workbooks have data, and a
    hardcoded row 1 would silently read a data row as the contract.
    """
    target = _normalise_header(HEADER_LABELS[ColumnKey.PROBLEM_NAME])
    limit = min(max_scan, sheet.max_row or 0)
    for row in range(1, limit + 1):
        if _normalise_header(sheet.cell(row=row, column=1).value) == target:
            return row
    raise WorkbookReadError(
        f"no header row found in the first {limit} rows: "
        f"column A never contains {HEADER_LABELS[ColumnKey.PROBLEM_NAME]!r}"
    )


def _read_header_labels(sheet: Worksheet, header_row: int) -> tuple[str | None, ...]:
    width = max(sheet.max_column or 0, max(FIXED_COLUMNS.values()))
    labels: list[str | None] = []
    for column in range(1, width + 1):
        text = render_cell(sheet.cell(row=header_row, column=column).value).strip()
        labels.append(text or None)
    return tuple(labels)


def resolve_columns(
    sheet: Worksheet, header_row: int, findings: list[ValidationFinding]
) -> ColumnMap:
    """Build the column map for this specific workbook.

    Columns A-P are positional -- reconnaissance confirmed they never move. The trailing
    pair is resolved by header label because they genuinely do move: `Validator Check`
    was found at column 18, 19 and 20 across the corpus, duplicated in two workbooks,
    and one workbook has no `Time Last Checked` column at all. A duplicate resolves to
    the first occurrence and reports the collision; a missing column resolves to nothing
    and every rule that needs it is skipped rather than reading a neighbour's data.
    """
    labels = _read_header_labels(sheet, header_row)
    positions: dict[ColumnKey, int] = dict(FIXED_COLUMNS)

    for key, expected_index in FIXED_COLUMNS.items():
        actual = labels[expected_index - 1] if expected_index <= len(labels) else None
        expected_label = _normalise_header(HEADER_LABELS[key])
        # `Images (space delimited)` carries a parenthetical in every real workbook, so
        # the contract check is a prefix match on the normalised label.
        if not _normalise_header(actual).startswith(expected_label):
            findings.append(
                ValidationFinding(
                    code=StructuralCode.HEADER_CONTRACT_MISMATCH,
                    severity=Severity.ERROR,
                    scope=FindingScope.CELL,
                    row=header_row,
                    column=expected_index,
                    column_key=key,
                    repairable=False,
                    message=(
                        f"column {expected_index} header is {actual!r}, "
                        f"expected {HEADER_LABELS[key]!r}"
                    ),
                    detail={"found": actual, "expected": HEADER_LABELS[key]},
                )
            )

    for key, label in NAMED_COLUMNS.items():
        wanted = _normalise_header(label)
        matches = [
            index
            for index, found in enumerate(labels, start=1)
            if index > max(FIXED_COLUMNS.values()) and _normalise_header(found) == wanted
        ]
        if not matches:
            findings.append(
                ValidationFinding(
                    code=StructuralCode.MISSING_NAMED_COLUMN,
                    severity=Severity.WARNING,
                    scope=FindingScope.WORKBOOK,
                    repairable=False,
                    message=f"no {label!r} column in the header row",
                    detail={"column_key": key.value},
                )
            )
            continue
        if len(matches) > 1:
            findings.append(
                ValidationFinding(
                    code=StructuralCode.DUPLICATE_HEADER_LABEL,
                    severity=Severity.WARNING,
                    scope=FindingScope.WORKBOOK,
                    repairable=False,
                    message=(
                        f"{label!r} appears in columns {matches}; "
                        f"reading column {matches[0]}"
                    ),
                    detail={"column_key": key.value, "columns": matches},
                )
            )
        positions[key] = matches[0]

    return ColumnMap(header_row=header_row, positions=positions, headers=labels)


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


def _read_row(sheet: Worksheet, row: int, column_map: ColumnMap, width: int) -> WorkbookRow:
    """Read one row, mapping the contract columns and scanning the full width.

    Real workbooks carry tooling columns past the documented contract -- `Debug Link`,
    `Problem ID`, `Lesson ID`, `Image Checksum`, out to column 25. Those are not curated
    and so are not mapped, but blankness is judged across **every** column: a row whose
    only content sits in an unmapped column is not blank, and treating it as blank would
    let it be trimmed off the end of a block or skipped during segmentation.
    """
    values: dict[ColumnKey, str] = {}
    raw: dict[ColumnKey, Any] = {}
    for key, column in column_map.positions.items():
        native = sheet.cell(row=row, column=column).value
        raw[key] = native
        values[key] = render_cell(native)
    is_blank = True
    wrapped: list[int] = []
    for column in range(1, width + 1):
        cell = sheet.cell(row=row, column=column)
        if render_cell(cell.value).strip():
            is_blank = False
        if cell.alignment.wrap_text:
            wrapped.append(column)
    return WorkbookRow(
        row=row,
        values=values,
        raw=raw,
        is_blank=is_blank,
        wrap_text_columns=tuple(wrapped),
    )


# --------------------------------------------------------------------------------------
# Shift detection
# --------------------------------------------------------------------------------------


def _detect_row_shift_right(row: WorkbookRow, expected_name: str) -> int | None:
    """Detect a whole-row rightward shift, as found in 32 rows of `7.2`.

    Every value moved two columns right, so `Problem Name` is empty while the block's
    name sits in `Title`. Checked before `MISSING_PROBLEM_NAME` because reporting these
    as a missing name is not merely imprecise -- a repair that fills in the name would
    leave the real corruption in place and add a duplicate.

    Returns the shift distance, or `None` if this is not a shift.
    """
    if row.get(ColumnKey.PROBLEM_NAME).strip() or not expected_name:
        return None
    ordered = [key for key, _ in sorted(FIXED_COLUMNS.items(), key=lambda kv: kv[1])]
    for offset, key in enumerate(ordered):
        if offset == 0:
            continue
        if row.get(key).strip() == expected_name:
            return offset
    return None


def _detect_column_shift_left(row: WorkbookRow) -> bool:
    """Detect the partial left shift found in 12 rows of `7.3`.

    There, the `G`-`I` group moved one column left, so a scaffold identifier such as
    `h1` lands in `answerType` and the dependency lands in `HintID`. The signature is an
    identifier-shaped value in the `answerType` column, which is a closed set that can
    never legitimately hold one.
    """
    answer_type = row.get(ColumnKey.ANSWER_TYPE).strip()
    return bool(answer_type) and bool(_IDENTIFIER_PATTERN.match(answer_type))


# --------------------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------------------


def _is_problem_row(row: WorkbookRow) -> bool:
    return row.get(ColumnKey.ROW_TYPE).strip().casefold() == RowType.PROBLEM.value


def segment_blocks(
    rows: list[WorkbookRow],
    findings: list[ValidationFinding],
) -> tuple[tuple[ProblemBlock, ...], tuple[WorkbookRow, ...]]:
    """Split rows into blocks at every `Row Type == "problem"` row.

    Trailing blank rows are trimmed off the end of each block so a block's `end_row` is
    its last row of content, but interior blanks stay inside the block and are reported
    -- they are a formatting oddity, not a boundary.

    Rows appearing before the first problem row cannot belong to any block. They are
    returned separately as orphans with an `ORPHAN_ROW_BEFORE_FIRST_PROBLEM` finding
    rather than being attached to the block that happens to follow them.
    """
    starts = [i for i, row in enumerate(rows) if _is_problem_row(row)]
    if not starts:
        raise WorkbookReadError(
            'no rows with Row Type == "problem"; the workbook has no problem blocks'
        )

    orphans = tuple(row for row in rows[: starts[0]] if not row.is_blank)
    for row in orphans:
        findings.append(
            ValidationFinding(
                code=StructuralCode.ORPHAN_ROW_BEFORE_FIRST_PROBLEM,
                severity=Severity.ERROR,
                scope=FindingScope.ROW,
                row=row.row,
                message=(
                    "row carries content but precedes the first problem row, "
                    "so it belongs to no block"
                ),
                detail={"problem_name": row.get(ColumnKey.PROBLEM_NAME)},
            )
        )

    blocks: list[ProblemBlock] = []
    boundaries = starts + [len(rows)]
    for index, (begin, end) in enumerate(zip(boundaries, boundaries[1:])):
        span = rows[begin:end]
        while span and span[-1].is_blank:
            span.pop()
        blocks.append(_build_block(index, span))
    return tuple(blocks), orphans


def _build_block(index: int, span: list[WorkbookRow]) -> ProblemBlock:
    problem_row = span[0]
    declared_name = problem_row.get(ColumnKey.PROBLEM_NAME).strip()
    block_findings: list[ValidationFinding] = []

    if not declared_name:
        block_findings.append(
            ValidationFinding(
                code=StructuralCode.MISSING_PROBLEM_NAME,
                severity=Severity.ERROR,
                scope=FindingScope.CELL,
                row=problem_row.row,
                column=FIXED_COLUMNS[ColumnKey.PROBLEM_NAME],
                column_key=ColumnKey.PROBLEM_NAME,
                message="problem row has no Problem Name",
            )
        )

    block_id = f"block-{index:04d}"
    mismatched: list[int] = []

    for row in span[1:]:
        if row.is_blank:
            block_findings.append(
                ValidationFinding(
                    code=StructuralCode.INTERIOR_BLANK_ROW,
                    severity=Severity.OBSERVATION,
                    scope=FindingScope.ROW,
                    row=row.row,
                    block_id=block_id,
                    problem_name=declared_name or None,
                    message="blank row inside a problem block",
                )
            )
            continue

        shift = _detect_row_shift_right(row, declared_name)
        if shift is not None:
            block_findings.append(
                ValidationFinding(
                    code=StructuralCode.ROW_SHIFT_RIGHT,
                    severity=Severity.BLOCKING,
                    scope=FindingScope.ROW,
                    row=row.row,
                    block_id=block_id,
                    problem_name=declared_name or None,
                    message=(
                        f"every value on this row sits {shift} column(s) right of "
                        f"where it belongs; the Problem Name is in column {shift + 1}"
                    ),
                    detail={"shift": shift},
                )
            )
            continue

        if _detect_column_shift_left(row):
            block_findings.append(
                ValidationFinding(
                    code=StructuralCode.COLUMN_SHIFT,
                    severity=Severity.BLOCKING,
                    scope=FindingScope.CELL,
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.ANSWER_TYPE],
                    column_key=ColumnKey.ANSWER_TYPE,
                    block_id=block_id,
                    problem_name=declared_name or None,
                    message=(
                        "answerType holds an identifier, which it can never legitimately "
                        "contain; the columns from here rightward appear shifted left"
                    ),
                    detail={"answer_type": row.get(ColumnKey.ANSWER_TYPE)},
                )
            )

        name = row.get(ColumnKey.PROBLEM_NAME).strip()
        if not name:
            block_findings.append(
                ValidationFinding(
                    code=StructuralCode.MISSING_PROBLEM_NAME,
                    severity=Severity.WARNING,
                    scope=FindingScope.CELL,
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.PROBLEM_NAME],
                    column_key=ColumnKey.PROBLEM_NAME,
                    block_id=block_id,
                    problem_name=declared_name or None,
                    message="row inside a block has no Problem Name",
                )
            )
        elif declared_name and name != declared_name:
            mismatched.append(row.row)
            block_findings.append(
                ValidationFinding(
                    code=StructuralCode.PROBLEM_NAME_MISMATCH_IN_BLOCK,
                    severity=Severity.ERROR,
                    scope=FindingScope.CELL,
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.PROBLEM_NAME],
                    column_key=ColumnKey.PROBLEM_NAME,
                    block_id=block_id,
                    problem_name=declared_name,
                    message=(
                        f"row is named {name!r} but sits in the block whose problem "
                        f"row declares {declared_name!r}"
                    ),
                    detail={"found": name, "declared": declared_name},
                )
            )

        row_type_text = row.get(ColumnKey.ROW_TYPE).strip()
        if row_type_text and row.row_type is None:
            block_findings.append(
                ValidationFinding(
                    code=StructuralCode.UNKNOWN_ROW_TYPE,
                    severity=Severity.ERROR,
                    scope=FindingScope.CELL,
                    row=row.row,
                    column=FIXED_COLUMNS[ColumnKey.ROW_TYPE],
                    column_key=ColumnKey.ROW_TYPE,
                    block_id=block_id,
                    problem_name=declared_name or None,
                    message=f"Row Type {row_type_text!r} is not one of {_row_type_list()}",
                    detail={"found": row_type_text},
                )
            )

    if mismatched:
        # The two segmentation signals disagree. Both readings are guesses -- the names
        # may be wrong, or a problem row may be missing -- so the disagreement itself is
        # reported and left for the council to judge.
        block_findings.append(
            ValidationFinding(
                code=StructuralCode.BLOCK_BOUNDARY_DISAGREEMENT,
                severity=Severity.BLOCKING,
                scope=FindingScope.BLOCK,
                block_id=block_id,
                problem_name=declared_name or None,
                message=(
                    f"block segmented by Row Type spans rows {span[0].row}-{span[-1].row}, "
                    f"but {len(mismatched)} row(s) carry a different Problem Name; "
                    "segmentation by name would produce different blocks"
                ),
                detail={"declared": declared_name, "mismatched_rows": mismatched},
            )
        )

    # Block findings stay on the block. `ParsedWorkbook.all_findings` unions the two
    # lists, so promoting anything here would report it twice.
    return ProblemBlock(
        block_id=block_id,
        index=index,
        problem_name=declared_name,
        start_row=span[0].row,
        end_row=span[-1].row,
        rows=tuple(span),
        findings=tuple(block_findings),
    )


def _row_type_list() -> str:
    return ", ".join(sorted(t.value for t in RowType))


# --------------------------------------------------------------------------------------
# Convention detection
# --------------------------------------------------------------------------------------


def detect_conventions(blocks: Iterable[ProblemBlock]) -> WorkbookConventions:
    """Infer the habits of this particular workbook.

    Nothing here is enforcement. The rules engine decides what to do with a detected
    convention; the reader only reports what the file consistently does, so that a
    house style can be told apart from damage.
    """
    stems: dict[str, int] = {}
    namespaces: dict[str, int] = {}
    latex_cells = 0
    text_cells = 0
    reset_evidence = 0
    continuous_evidence = 0

    for block in blocks:
        match = _STEM_PATTERN.match(block.problem_name)
        if match and match.group(1):
            stems[match.group(1)] = stems.get(match.group(1), 0) + 1

        step_dependencies: list[list[int]] = []
        current: list[int] | None = None

        for row in block.rows:
            # Only the columns that carry mathematics or rendered prose count. Including
            # names, row types and metadata would dilute the ratio so far that a workbook
            # written entirely in LaTeX still looks mostly ASCII.
            for key in _NOTATION_COLUMNS:
                text = row.get(key)
                if not text.strip():
                    continue
                text_cells += 1
                if any(marker in text for marker in _LATEX_MARKERS):
                    latex_cells += 1

            identifier = row.get(ColumnKey.HINT_ID).strip()
            id_match = _IDENTIFIER_PATTERN.match(identifier)
            if row.row_type is RowType.SCAFFOLD and id_match:
                namespace = id_match.group(1).casefold()
                namespaces[namespace] = namespaces.get(namespace, 0) + 1

            if row.row_type is RowType.STEP:
                current = []
                step_dependencies.append(current)
            elif current is not None and id_match:
                current.append(int(id_match.group(2)))

        # A block with fewer than two populated steps is no evidence either way, and
        # counting it as one is how a detector invents a convention.
        populated = [numbers for numbers in step_dependencies if numbers]
        if len(populated) >= 2:
            if all(numbers and min(numbers) == 1 for numbers in populated):
                reset_evidence += 1
            elif _is_strictly_increasing_across(populated):
                continuous_evidence += 1

    dominant = max(namespaces, key=lambda ns: namespaces[ns]) if namespaces else None
    if reset_evidence > continuous_evidence:
        convention = DependencyConvention.RESET_PER_STEP
    elif continuous_evidence > reset_evidence:
        convention = DependencyConvention.CONTINUOUS
    else:
        convention = DependencyConvention.UNDECIDED

    # A workbook is LaTeX when it is overwhelmingly LaTeX, ASCII when it contains none at
    # all, and MIXED otherwise. MIXED is a *defect state*, not a third valid convention:
    # both real conventions are internally consistent, so a workbook sitting between them
    # has cells written in the wrong one, which is what the notation rules then report.
    if latex_cells == 0:
        notation = Notation.ASCII if text_cells else Notation.UNKNOWN
    elif latex_cells >= _LATEX_MAJORITY * text_cells:
        notation = Notation.LATEX
    else:
        notation = Notation.MIXED

    return WorkbookConventions(
        naming_stems=tuple(sorted(stems, key=lambda s: (-stems[s], s))),
        scaffold_namespaces=tuple(sorted(namespaces)),
        dominant_scaffold_namespace=dominant,
        scaffold_namespace_is_consistent=len(namespaces) <= 1,
        dependency_convention=convention,
        notation=notation,
    )


def _is_strictly_increasing_across(groups: list[list[int]]) -> bool:
    highest = 0
    for numbers in groups:
        if min(numbers) <= highest:
            return False
        highest = max(numbers)
    return True


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def read_workbook(path: str | Path) -> ParsedWorkbook:
    """Parse a workbook read-only.

    `data_only=False` keeps formulas as formulas: a cached value would silently replace
    a curator's formula on the way back out.
    """
    workbook = load_workbook(filename=str(path), data_only=False, read_only=False)
    try:
        sheet = workbook.active
        findings: list[ValidationFinding] = []

        if len(workbook.sheetnames) > 1:
            findings.append(
                ValidationFinding(
                    code=StructuralCode.MULTIPLE_SHEETS,
                    severity=Severity.WARNING,
                    scope=FindingScope.WORKBOOK,
                    repairable=False,
                    message=(
                        f"workbook has {len(workbook.sheetnames)} sheets; "
                        f"only {sheet.title!r} is curated"
                    ),
                    detail={"sheets": list(workbook.sheetnames)},
                )
            )

        header_row = find_header_row(sheet)
        column_map = resolve_columns(sheet, header_row, findings)

        width = max(sheet.max_column or 0, len(column_map.headers))
        rows = [
            _read_row(sheet, row, column_map, width)
            for row in range(header_row + 1, (sheet.max_row or header_row) + 1)
        ]
        blocks, orphans = segment_blocks(rows, findings)

        populated = [row for row in rows if not row.is_blank]
        first_data_row = populated[0].row if populated else header_row + 1

        return ParsedWorkbook(
            sheet_name=sheet.title,
            header_row=header_row,
            first_data_row=first_data_row,
            max_row=sheet.max_row or header_row,
            column_map=column_map,
            blocks=blocks,
            orphan_rows=orphans,
            row_heights={
                index: dimension.height
                for index, dimension in sheet.row_dimensions.items()
                if dimension.height is not None
            },
            conventions=detect_conventions(blocks),
            findings=tuple(findings),
        )
    finally:
        workbook.close()
