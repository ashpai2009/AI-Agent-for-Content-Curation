"""The held-out scorer accepts invariants, not one synthetic author's favorite text."""

from __future__ import annotations

from openpyxl import load_workbook

from conftest import problem, step
from scripts.evaluate_controlled_run import evaluate_item, keyed_items


def test_multiple_valid_mc_repairs_are_scored_by_the_grading_contract(make_workbook):
    path = make_workbook(
        [
            problem("p1", title="Choose the exact probability", oer_src="s", license="CC"),
            step(
                "p1",
                answer="1/2",
                answer_type="mc",
                mc_choices="1/2|1/3|1/4|3/4",
            ),
        ]
    )
    workbook = load_workbook(path, data_only=False)
    try:
        passed, _, _ = evaluate_item(
            {"cell": "I3", "predicate": "mc_exact_answer_once"}, workbook.active
        )
    finally:
        workbook.close()
    assert passed


def test_an_equivalent_graded_answer_is_not_forced_to_one_string(make_workbook):
    path = make_workbook(
        [
            problem("p1", title="Solve", oer_src="s", license="CC"),
            step("p1", answer="2", answer_type="numeric"),
        ]
    )
    workbook = load_workbook(path, data_only=False)
    try:
        passed, _, _ = evaluate_item(
            {"cell": "E3", "mathEquivalentTo": "x=sqrt(4)"}, workbook.active
        )
    finally:
        workbook.close()
    assert passed


def test_a_key_without_a_machine_check_stays_manual():
    automated, manual = keyed_items(
        {
            "defectGroups": [
                {
                    "problemName": "p1",
                    "kind": "instructional_quality",
                    "corrections": [
                        {"cell": "D4", "manual": "Hint should teach without giving away the answer"}
                    ],
                }
            ]
        }
    )
    assert automated == []
    assert len(manual) == 1
