"""Tests for the database-independent before/after workbook comparison."""

from pathlib import Path

import pytest
from openpyxl import Workbook

from scripts.compare_to_reference import grade, summary


def _workbook(path: Path, values: dict[str, object], *, title: str = "problems") -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for coordinate, value in values.items():
        sheet[coordinate] = value
    workbook.save(path)
    workbook.close()
    return path


def test_reference_comparison_distinguishes_every_edit_outcome(tmp_path):
    source = _workbook(
        tmp_path / "source.xlsx",
        {"A1": "matched-old", "A2": "missed-old", "A3": "different-old", "A4": "stable"},
    )
    expected = _workbook(
        tmp_path / "expected.xlsx",
        {"A1": "matched-new", "A2": "missed-new", "A3": "different-new", "A4": "stable"},
    )
    actual = _workbook(
        tmp_path / "actual.xlsx",
        {"A1": "matched-new", "A2": "missed-old", "A3": "alternative", "A4": "surprise"},
    )

    grades = grade(source, expected, actual)

    assert [(item.coordinate, item.outcome) for item in grades] == [
        ("A1", "matched_reference"),
        ("A2", "missed"),
        ("A3", "different_correction"),
        ("A4", "unexpected_edit"),
    ]
    assert summary(grades) == {
        "expected_changed_cells": 3,
        "actual_changed_cells": 3,
        "matched_reference": 1,
        "missed": 1,
        "different_correction": 1,
        "unexpected_edit": 1,
        "target_recall": 1 / 3,
        "edit_precision": 1 / 3,
    }


def test_reference_comparison_rejects_different_active_sheets(tmp_path):
    source = _workbook(tmp_path / "source.xlsx", {"A1": 1}, title="source")
    expected = _workbook(tmp_path / "expected.xlsx", {"A1": 1}, title="source")
    actual = _workbook(tmp_path / "actual.xlsx", {"A1": 1}, title="other")

    with pytest.raises(ValueError, match="active sheets differ"):
        grade(source, expected, actual)


def test_unchanged_identical_workbooks_score_as_complete(tmp_path):
    source = _workbook(tmp_path / "source.xlsx", {"A1": "same"})
    expected = _workbook(tmp_path / "expected.xlsx", {"A1": "same"})
    actual = _workbook(tmp_path / "actual.xlsx", {"A1": "same"})

    grades = grade(source, expected, actual)

    assert grades == ()
    assert summary(grades)["target_recall"] == 1.0
    assert summary(grades)["edit_precision"] == 1.0
