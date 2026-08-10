"""Mathematics gate tests.

The three-valued result is the point. `UNKNOWN` must never be reported as `DIFFERENT`:
an expression the gate cannot parse is not evidence of a defect, and a rule that treats
it as one manufactures issues the council then burns repair attempts on.
"""

from __future__ import annotations

import pytest

from oatutor_council.validation.mathematics import (
    MathVerdict,
    UnparseableExpression,
    equations_equivalent,
    equivalent,
    is_numeric_literal,
    latex_to_ascii,
    parse_expression,
    split_equation,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1/2", "0.5"),
        ("sqrt(2)/2", "1/sqrt(2)"),
        ("(a)/(b)", "a/b"),
        ("cos(theta)**2", "1 - sin(theta)**2"),
        ("2*x", "x + x"),
        ("theta**2", "theta*theta"),
        ("pi/3", "1.0471975511965976"),
        ("x^2", "x**2"),
    ],
)
def test_equivalent_expressions(left, right):
    assert equivalent(left, right) is MathVerdict.EQUIVALENT


@pytest.mark.parametrize(
    ("left", "right"),
    [("1/2", "1/3"), ("x", "y"), ("sin(x)", "cos(x)"), ("2*x", "3*x")],
)
def test_different_expressions(left, right):
    assert equivalent(left, right) is MathVerdict.DIFFERENT


def test_latex_translates_to_the_ascii_convention():
    assert equivalent(r"$$\frac{\sqrt{2}}{2}$$", "sqrt(2)/2") is MathVerdict.EQUIVALENT
    assert (
        equivalent(r"$$\frac{\pi}{3} + 2\pi k$$", "pi/3 + 2*pi*k")
        is MathVerdict.EQUIVALENT
    )
    assert equivalent(r"$$\cos^{2}(\theta)$$", "cos(theta)**2") is MathVerdict.EQUIVALENT


def test_implicit_multiplication_is_latex_only():
    """LaTeX genuinely writes `2\\pi k`. The ASCII convention requires explicit
    operators, so reading `2x` as `2*x` there would parse away the notation defect the
    rules exist to catch -- the expression would look fine while the cell was wrong."""
    assert equivalent("2x", "2*x") is MathVerdict.UNKNOWN
    assert equivalent(r"$$2\pi$$", "2*pi") is MathVerdict.EQUIVALENT


def test_plus_or_minus_is_refused_rather_than_guessed():
    """`\\pm` denotes two values. Silently choosing one would make the comparison
    quietly wrong in a way nothing downstream could detect."""
    assert equivalent(r"$$\pm \frac{1}{2}$$", "1/2") is MathVerdict.UNKNOWN
    with pytest.raises(UnparseableExpression, match="two values"):
        latex_to_ascii(r"\pm 1")


@pytest.mark.parametrize(
    "hostile",
    [
        "__import__('os').system('id')",
        "lambda: 1",
        "eval('1+1')",
        "open('/etc/passwd')",
        "globals()",
        "x" * 600,
    ],
)
def test_hostile_input_is_refused(hostile):
    """Workbook cells are untrusted input reaching a parser that evaluates. The
    character allowlist and the restricted namespace are both load-bearing."""
    with pytest.raises(UnparseableExpression):
        parse_expression(hostile)


def test_unparseable_input_is_unknown_not_different():
    assert equivalent("this is prose, not mathematics", "1") is MathVerdict.UNKNOWN
    assert equivalent("", "1") is MathVerdict.UNKNOWN


# --------------------------------------------------------------------------------------
# Equations
# --------------------------------------------------------------------------------------


def test_equations_are_compared_as_identities():
    """Comparing these as strings would miss that they are the same identity."""
    assert (
        equations_equivalent(
            "cos(theta)**2 = 1 - sin(theta)**2", "sin(theta)**2 + cos(theta)**2 = 1"
        )
        is MathVerdict.EQUIVALENT
    )


def test_a_reversed_equation_is_the_same_equation():
    assert equations_equivalent("x = 2", "2 = x") is MathVerdict.EQUIVALENT


def test_different_equations_are_different():
    assert equations_equivalent("x = 2", "x = 3") is MathVerdict.DIFFERENT


def test_an_equation_and_a_bare_expression_are_not_the_same_kind_of_thing():
    assert equations_equivalent("x = 2", "2") is MathVerdict.DIFFERENT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("x = 2", ("x ", " 2")),
        ("x <= 2", None),
        ("x >= 2", None),
        ("x == 2", None),
        ("a = b = c", None),
        ("2", None),
    ],
)
def test_split_equation(text, expected):
    assert split_equation(text) == expected


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def test_is_numeric_literal():
    assert is_numeric_literal("sqrt(2)/2")
    assert is_numeric_literal("1/3")
    assert not is_numeric_literal("x + 1")
    assert not is_numeric_literal("prose")
