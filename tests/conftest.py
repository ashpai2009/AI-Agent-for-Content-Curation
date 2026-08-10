"""Synthetic workbook construction for the test suite.

Every fixture is generated into `tmp_path` with invented mathematics. No content from
the real curator corpus is ever copied into a committed test -- the tests reproduce the
*structures* reconnaissance found (`docs/recon-findings.md`), not the material.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest
from openpyxl import Workbook

from oatutor_council.models import FIXED_COLUMNS, ColumnKey

#: The header row as the real workbooks write it: sixteen contract columns, two blank
#: spacer columns, then the two trailing columns whose position varies in practice.
DEFAULT_HEADERS: list[str | None] = [
    "Problem Name",
    "Row Type",
    "Title",
    "Body Text",
    "Answer",
    "answerType",
    "HintID",
    "Dependency",
    "mcChoices",
    "Images (space delimited)",
    "Parent",
    "OER src",
    "openstax KC",
    "KC",
    "Taxonomy",
    "License",
    None,
    None,
    "Validator Check",
    "Time Last Checked",
]

#: Marks a deliberately blank row. `None` alone is ambiguous with "no value here", and
#: blank-row placement is the thing several segmentation tests turn on.
BLANK = object()


def cells(**values: Any) -> list[Any]:
    """Build one row from column keys, padded to the full contract width.

    Keyword names are `ColumnKey` values, e.g. `cells(problem_name="trig1",
    row_type="problem")`. Anything unset is left empty, which is what real rows do.
    """
    row: list[Any] = [None] * len(DEFAULT_HEADERS)
    for name, value in values.items():
        key = ColumnKey(name)
        index = FIXED_COLUMNS.get(key)
        if index is None:
            raise KeyError(f"{name} is not a fixed column; place it positionally")
        row[index - 1] = value
    return row


def problem(name: str, *, title: str = "", body: str = "", **extra: Any) -> list[Any]:
    return cells(
        problem_name=name, row_type="problem", title=title, body_text=body, **extra
    )


def step(
    name: str, *, answer: str = "1", answer_type: str = "numeric", **extra: Any
) -> list[Any]:
    return cells(
        problem_name=name,
        row_type="step",
        answer=answer,
        answer_type=answer_type,
        **extra,
    )


def scaffold(name: str, identifier: str, *, dependency: str = "", **extra: Any):
    return cells(
        problem_name=name,
        row_type="scaffold",
        hint_id=identifier,
        dependency=dependency,
        **extra,
    )


def hint(name: str, identifier: str, *, body: str = "", **extra: Any) -> list[Any]:
    return cells(
        problem_name=name, row_type="hint", hint_id=identifier, body_text=body, **extra
    )


def write_workbook(
    path: Path,
    rows: Sequence[Any],
    *,
    header_row: int = 1,
    headers: Sequence[str | None] | None = None,
    sheet_title: str = "Sheet1",
    extra_sheets: Sequence[str] = (),
) -> Path:
    """Write a synthetic workbook.

    `rows` are written consecutively from `header_row + 1`; a `BLANK` entry leaves a row
    empty. `header_row` above 1 reproduces the leading-blank-row layout one real
    workbook has, which is why the reader detects the header rather than assuming it.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title

    for column, label in enumerate(headers or DEFAULT_HEADERS, start=1):
        if label is not None:
            sheet.cell(row=header_row, column=column, value=label)

    for offset, row in enumerate(rows):
        target = header_row + 1 + offset
        if row is BLANK:
            continue
        for column, value in enumerate(row, start=1):
            if value is not None:
                sheet.cell(row=target, column=column, value=value)

    for title in extra_sheets:
        workbook.create_sheet(title=title)

    workbook.save(path)
    return path


@pytest.fixture
def make_workbook(tmp_path: Path):
    """Return a factory writing a uniquely-named workbook into `tmp_path`."""
    counter = {"n": 0}

    def factory(rows: Sequence[Any], **kwargs: Any) -> Path:
        counter["n"] += 1
        path = tmp_path / f"workbook-{counter['n']}.xlsx"
        return write_workbook(path, rows, **kwargs)

    return factory
