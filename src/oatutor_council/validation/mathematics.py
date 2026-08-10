"""Mathematical checks over workbook expressions.

**A gate, not a proof.** Equivalence here can catch a distractor that is secretly the
same as the answer, or an answer that does not match any choice. It cannot tell you
whether a problem is *correct* -- that is the reviewer's job, and no amount of symbolic
manipulation substitutes for an agent actually solving the question.

Every result is three-valued. `UNKNOWN` is a real answer and is never collapsed into
`DIFFERENT`: an expression this module cannot parse is not evidence of a defect, and
reporting one as a mismatch manufactures issues the council then wastes attempts on.

Workbook cells are untrusted input, so parsing is defended twice. A character allowlist
rejects anything outside ordinary mathematical notation before SymPy sees it, and
`parse_expr` is given an explicit namespace containing only the functions and constants
the OATutor conventions use -- no builtins, no imports, no attribute access.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

import sympy
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)


class MathVerdict(StrEnum):
    EQUIVALENT = "equivalent"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


#: Characters an ordinary mathematical expression can contain. Notably absent:
#: underscore chains, brackets, braces, colons, semicolons and quotes -- everything an
#: expression could use to reach Python semantics.
_SAFE_CHARACTERS = re.compile(r"^[0-9A-Za-z+\-*/^().,=<>!\s]*$")

#: Belt and braces alongside the namespace restriction below.
_BANNED_SUBSTRINGS = ("__", "lambda", "import", "exec", "eval", "open", "globals")

#: Expressions longer than this are not mathematics, they are an attack or a mistake.
_MAX_EXPRESSION_LENGTH = 500

#: The only names an expression may reference. `parse_expr` is handed this as its entire
#: global namespace, so anything else raises rather than resolving.
_NAMESPACE: dict[str, Any] = {
    "sin": sympy.sin,
    "cos": sympy.cos,
    "tan": sympy.tan,
    "sec": sympy.sec,
    "csc": sympy.csc,
    "cot": sympy.cot,
    "asin": sympy.asin,
    "acos": sympy.acos,
    "atan": sympy.atan,
    "sqrt": sympy.sqrt,
    "log": sympy.log,
    "ln": sympy.log,
    "exp": sympy.exp,
    "Abs": sympy.Abs,
    "abs": sympy.Abs,
    "pi": sympy.pi,
    "E": sympy.E,
    "I": sympy.I,
    "oo": sympy.oo,
    # `standard_transformations` rewrites the token stream into constructor calls --
    # `1/2` becomes `Integer(1)/Integer(2)` and a bare name becomes `Symbol('x')` --
    # so these must resolve or every expression fails to parse. They are constructors,
    # not an escape route: the character allowlist has already excluded quotes and
    # underscores, so nothing can name anything outside this table.
    "Integer": sympy.Integer,
    "Float": sympy.Float,
    "Rational": sympy.Rational,
    "Symbol": sympy.Symbol,
    "factorial": sympy.factorial,
}

#: Symbols the OATutor conventions spell out rather than using a Unicode glyph.
for _name in ("x", "y", "z", "t", "k", "n", "a", "b", "c", "theta", "phi", "alpha", "beta"):
    _NAMESPACE[_name] = sympy.Symbol(_name)


class UnparseableExpression(Exception):
    """The text is not an expression this module is willing to interpret."""


# --------------------------------------------------------------------------------------
# LaTeX
# --------------------------------------------------------------------------------------

_LATEX_REPLACEMENTS = (
    (r"\left", ""),
    (r"\right", ""),
    (r"\cdot", "*"),
    (r"\times", "*"),
    (r"\div", "/"),
    (r"\pi", "pi"),
    (r"\theta", "theta"),
    (r"\phi", "phi"),
    (r"\alpha", "alpha"),
    (r"\beta", "beta"),
    (r"\infty", "oo"),
)

_LATEX_FUNCTIONS = ("sin", "cos", "tan", "sec", "csc", "cot", "log", "ln", "exp", "arcsin", "arccos", "arctan")

_FRACTION = re.compile(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_SUPERSCRIPT = re.compile(r"\^\s*\{([^{}]*)\}")


def strip_latex_delimiters(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
        return stripped[2:-2].strip()
    if stripped.startswith("$") and stripped.endswith("$") and len(stripped) > 2:
        return stripped[1:-1].strip()
    return stripped


def latex_to_ascii(text: str) -> str:
    """Translate the LaTeX subset the corpus uses into the ASCII convention.

    Deliberately not a general LaTeX parser. Anything outside the subset survives
    untranslated and is then rejected by the character allowlist, which yields `UNKNOWN`
    -- the honest outcome for an expression we do not understand.

    `\\pm` is refused rather than guessed at: it denotes two values, and silently
    choosing one would make an equivalence check quietly wrong.
    """
    result = strip_latex_delimiters(text)
    if r"\pm" in result or r"\mp" in result:
        raise UnparseableExpression("plus-or-minus denotes two values, not one")

    for _ in range(8):  # nested fractions; bounded so malformed input cannot spin
        replaced = _FRACTION.sub(r"((\1)/(\2))", result)
        replaced = _SQRT.sub(r"sqrt(\1)", replaced)
        if replaced == result:
            break
        result = replaced

    result = _SUPERSCRIPT.sub(r"**(\1)", result)
    for latex, ascii_form in _LATEX_REPLACEMENTS:
        result = result.replace(latex, ascii_form)
    for function in _LATEX_FUNCTIONS:
        result = result.replace("\\" + function, function)
    result = result.replace("^", "**").replace("{", "(").replace("}", ")")
    return result.strip()


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def looks_like_latex(text: str) -> bool:
    return "$" in text or "\\" in text


def parse_expression(text: str) -> sympy.Expr:
    """Parse one expression, refusing anything that is not plainly mathematics."""
    candidate = text.strip()
    if not candidate:
        raise UnparseableExpression("empty expression")
    if len(candidate) > _MAX_EXPRESSION_LENGTH:
        raise UnparseableExpression("expression is implausibly long")

    was_latex = looks_like_latex(candidate)
    if was_latex:
        candidate = latex_to_ascii(candidate)

    lowered = candidate.lower()
    if any(banned in lowered for banned in _BANNED_SUBSTRINGS):
        raise UnparseableExpression("expression contains a forbidden token")
    if not _SAFE_CHARACTERS.match(candidate):
        raise UnparseableExpression("expression contains characters outside the allowlist")

    # `^` means exponent in the workbook conventions, never xor.
    candidate = candidate.replace("^", "**")

    # Implicit multiplication is enabled for LaTeX only. LaTeX genuinely writes
    # `2\pi k`, but the ASCII convention requires explicit operators -- quietly reading
    # `2x` as `2*x` there would parse away the very notation defect the rules exist to
    # catch, and the expression would look fine while the cell was still wrong.
    transformations = standard_transformations
    if was_latex:
        transformations = transformations + (implicit_multiplication_application,)

    try:
        expression = parse_expr(
            candidate,
            local_dict={},
            global_dict=dict(_NAMESPACE),
            transformations=transformations,
            evaluate=True,
        )
    except Exception as error:  # SymPy raises a wide and undocumented set
        raise UnparseableExpression(str(error)) from error

    if not isinstance(expression, sympy.Basic):
        raise UnparseableExpression("expression did not evaluate to a SymPy object")
    return expression


def split_equation(text: str) -> tuple[str, str] | None:
    """Split `a = b` into its sides, ignoring `<=`, `>=`, `!=` and `==`."""
    stripped = text.strip()
    positions = [
        index
        for index, character in enumerate(stripped)
        if character == "="
        and stripped[index - 1 : index] not in ("<", ">", "!", "=")
        and stripped[index + 1 : index + 2] != "="
    ]
    if len(positions) != 1:
        return None
    at = positions[0]
    return stripped[:at], stripped[at + 1 :]


# --------------------------------------------------------------------------------------
# Equivalence
# --------------------------------------------------------------------------------------

#: Sample points for the numeric fallback. Chosen away from 0, 1 and multiples of pi so
#: that unrelated expressions do not coincide by accident, and irrational so that
#: polynomial identities do not hold spuriously.
_SAMPLE_POINTS = (0.37, 1.234, 2.718, -0.83, 3.9)


def equivalent(left: str, right: str) -> MathVerdict:
    """Decide whether two expressions denote the same thing.

    Symbolic simplification first; a numeric sample as the fallback, because SymPy
    returning a non-zero difference is not proof of inequality for anything it failed to
    simplify. Either step failing yields `UNKNOWN` rather than a guess.
    """
    try:
        a, b = parse_expression(left), parse_expression(right)
    except UnparseableExpression:
        return MathVerdict.UNKNOWN

    try:
        difference = sympy.simplify(a - b)
        if difference == 0:
            return MathVerdict.EQUIVALENT
    except Exception:
        return MathVerdict.UNKNOWN

    return _numeric_verdict(a, b)


def _numeric_verdict(a: sympy.Expr, b: sympy.Expr) -> MathVerdict:
    symbols = sorted(a.free_symbols | b.free_symbols, key=str)
    if len(symbols) > 3:
        return MathVerdict.UNKNOWN

    agreements = 0
    for offset, point in enumerate(_SAMPLE_POINTS):
        substitution = {
            symbol: point + index + offset for index, symbol in enumerate(symbols)
        }
        try:
            left = complex(a.subs(substitution).evalf())
            right = complex(b.subs(substitution).evalf())
        except Exception:
            return MathVerdict.UNKNOWN
        if any(
            value != value or abs(value) == float("inf") for value in (left, right)
        ):  # NaN or a pole: this sample proves nothing
            continue
        if abs(left - right) > 1e-9 * max(1.0, abs(left), abs(right)):
            return MathVerdict.DIFFERENT
        agreements += 1

    if agreements == 0:
        return MathVerdict.UNKNOWN
    return MathVerdict.EQUIVALENT


def equations_equivalent(left: str, right: str) -> MathVerdict:
    """Compare two statements that may each be an equation.

    `cos(t)**2 = 1 - sin(t)**2` and `sin(t)**2 + cos(t)**2 = 1` are the same identity,
    which comparing them as raw strings would miss.
    """
    left_sides, right_sides = split_equation(left), split_equation(right)
    if left_sides is None and right_sides is None:
        return equivalent(left, right)
    if left_sides is None or right_sides is None:
        return MathVerdict.DIFFERENT

    a_lhs, a_rhs = left_sides
    b_lhs, b_rhs = right_sides
    try:
        a = parse_expression(a_lhs) - parse_expression(a_rhs)
        b = parse_expression(b_lhs) - parse_expression(b_rhs)
    except UnparseableExpression:
        return MathVerdict.UNKNOWN

    direct = _compare_expressions(a, b)
    if direct is MathVerdict.EQUIVALENT:
        return direct
    # `a = b` and `b = a` are the same equation, as are `a - b = 0` and `b - a = 0`.
    return _compare_expressions(a, -b)


def _compare_expressions(a: sympy.Expr, b: sympy.Expr) -> MathVerdict:
    try:
        if sympy.simplify(a - b) == 0:
            return MathVerdict.EQUIVALENT
    except Exception:
        return MathVerdict.UNKNOWN
    return _numeric_verdict(a, b)


def is_numeric_literal(text: str) -> bool:
    try:
        expression = parse_expression(text)
    except UnparseableExpression:
        return False
    return not expression.free_symbols
