"""The golden collection, run end to end through the deterministic core.

Each case in `golden.py` carries the verdict a curator would give the workbook. These
tests check that the engine reaches that verdict — not that some rule fires somewhere, but
that the *whole* answer on a *whole* file is the expected one.

The clean cases are the load-bearing half. A rule that flags correct mathematics is worse
than a missing rule, because it buries every true finding beside it in a report nobody
finishes reading.
"""

from __future__ import annotations

import pytest

from conftest import write_workbook
from golden import GOLDEN_CASES, GoldenCase, cases_tagged
from oatutor_council.models import ColumnKey, Severity
from oatutor_council.validation.rules import run_rules
from oatutor_council.workbook.reader import read_workbook

BAD = {Severity.ERROR, Severity.BLOCKING}


def _findings(case: GoldenCase, tmp_path):
    path = write_workbook(tmp_path / f"{case.name}.xlsx", list(case.rows))
    parsed = read_workbook(path)
    return parsed, tuple(run_rules(parsed)) + parsed.all_findings


def _describe(findings) -> str:
    return "\n".join(
        f"  [{f.severity.value}] {f.code} row {f.row}: {f.message}" for f in findings
    ) or "  (none)"


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.name)
def test_the_engine_reaches_the_expected_verdict(case: GoldenCase, tmp_path):
    _, findings = _findings(case, tmp_path)
    codes = {f.code for f in findings}

    missing = case.expect - codes
    assert not missing, (
        f"{case.verdict}\n\nexpected but not found: {sorted(missing)}\n"
        f"what the engine said:\n{_describe(findings)}"
    )

    wrongly_found = case.forbid & codes
    assert not wrongly_found, (
        f"{case.verdict}\n\nfound but must not be: {sorted(wrongly_found)}\n"
        f"what the engine said:\n{_describe(findings)}"
    )


@pytest.mark.parametrize("case", cases_tagged("clean"), ids=lambda c: c.name)
def test_correct_material_produces_no_errors(case: GoldenCase, tmp_path):
    """The half that catches over-eager rules.

    An observation is fine here -- "the correct answer is listed first" is worth saying and
    is not a defect. An `ERROR` on correct mathematics is a rule that has to change, and
    the fraction-parenthesisation rule was declined on exactly this evidence."""
    if not case.must_be_clean:
        pytest.skip("this case asserts specific codes rather than silence")

    _, findings = _findings(case, tmp_path)
    serious = [f for f in findings if f.severity in BAD]
    assert not serious, f"{case.verdict}\n\nbut the engine reported:\n{_describe(serious)}"


@pytest.mark.parametrize("case", cases_tagged("structural"), ids=lambda c: c.name)
def test_a_structural_corruption_is_never_read_as_something_tidier(
    case: GoldenCase, tmp_path
):
    """The `7.3` shape. Silently picking the convenient reading is what hides it, so the
    requirement is only that the file does not come back clean -- which reading the engine
    reports is a matter for the rules, but reporting nothing is not an option."""
    _, findings = _findings(case, tmp_path)
    assert [f for f in findings if f.severity in BAD], (
        f"{case.verdict}\n\nbut the engine reported nothing serious:\n{_describe(findings)}"
    )


@pytest.mark.parametrize("case", cases_tagged("injection"), ids=lambda c: c.name)
def test_an_instruction_in_a_cell_changes_nothing(case: GoldenCase, tmp_path):
    """A cell is data. The deterministic pass has no notion of being persuaded, and this
    is the test that says so out loud: the planted defect is still reported, and the
    payload is carried as content rather than acted on."""
    parsed, findings = _findings(case, tmp_path)
    codes = {f.code for f in findings}
    assert case.expect <= codes, (
        f"{case.verdict}\n\nthe engine said:\n{_describe(findings)}"
    )

    # And the text survived intact into the parsed block -- neither obeyed nor silently
    # rewritten, because a repair proposed against a sanitised cell would fail its
    # `before` check against the real one.
    titles = " ".join(
        row.get(ColumnKey.TITLE) for block in parsed.blocks for row in block.rows
    )
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in titles


def test_every_golden_case_states_what_a_curator_would_say():
    """The verdict is the specification and the codes are how the system expresses it.
    A case with no sentence is a case whose expectations nobody can check against
    intent -- it would only ever assert that the engine still does what it does."""
    for case in GOLDEN_CASES:
        assert len(case.verdict.split()) >= 8, case.name
        assert case.expect or case.forbid or case.must_be_clean, case.name
