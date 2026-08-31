"""What an audit actually looked at, as opposed to what it reported.

A findings list is evidence of detection and no evidence at all of coverage. An audit that
examined three of nine graded rows and an audit that examined all nine and found them
clean return the same empty list, and the pipeline had no way to tell them apart. On the
2026-08-29 held-out workbooks, **eight of eleven misses were rows nothing ever reported
on** -- and every one of them was indistinguishable, from the outside, from a row that had
been checked and was fine.

So a scan response now has a denominator. Every graded row in the block comes back with a
`RowCoverage`, and a block whose coverage is short is *not* finished: it goes back on the
queue and is scanned again. That is the whole mechanism, and its value is entirely in what
it refuses to accept.

**Silence is the failure mode this closes, and it is worth being precise about the limit.**
Coverage says a row was claimed to have been examined. It does not say the examination was
competent, and a model that fabricates a coverage row learns nothing and proves nothing.
What it removes is the case where nobody looked and nothing in the record said so -- which
is the case the misses were actually in.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from ..models import ProblemBlock
from ..validation.mathematics import MathVerdict, answers_equivalent
from .schemas import RowCoverage


_SIMPLE_ASSIGNMENT = re.compile(r"^[A-Za-z][A-Za-z0-9]*=(.+)$")


def _assignment_value(text: str) -> str:
    """Return the value from a simple ``x=...`` answer, otherwise the input.

    A verifier may solve to ``7`` while the workbook records ``x=sqrt(49)``. Comparing
    those whole strings is unparseable, even though their values are equivalent. Only a
    single identifier on the left is stripped; a calculation such as ``2+3=5`` and a
    function definition such as ``f(x)=...`` keep their full meaning.
    """
    compact = "".join(text.split())
    match = _SIMPLE_ASSIGNMENT.fullmatch(compact)
    return match.group(1) if match else text


def coverage_gaps(
    block: ProblemBlock, coverage: Sequence[RowCoverage]
) -> tuple[int, ...]:
    """Graded rows this response did not account for, exactly once each.

    Both directions are gaps, and for the same reason. A **missing** row was not examined.
    A **duplicated** row means two records claim the same row, so at least one describes
    something else and neither can be trusted to say which -- the same argument the batch
    reader makes about a duplicated `batch_item_id`.

    Rows the response invented outside the block are ignored rather than counted as gaps:
    they are noise in this answer, not evidence about a graded row, and treating them as
    gaps would let a stray entry force an unbounded re-scan of a block that was covered.
    """
    graded = block.graded_rows
    if not graded:
        return ()
    seen: dict[int, int] = {}
    for record in coverage:
        seen[record.row] = seen.get(record.row, 0) + 1
    return tuple(row for row in graded if seen.get(row, 0) != 1)


def self_contradicting(
    coverage: Iterable[RowCoverage], *, graded_rows: Iterable[int] | None = None
) -> tuple[int, ...]:
    """Rows whose own record does not hold together.

    A row that reports a computed answer differing from the submitted one while also
    reporting `answer_correct` has contradicted itself in a single record. That is not a
    defect in the workbook and it is not grounds to re-scan -- the model may simply have
    written the same value two ways -- but it is exactly the kind of thing a person
    auditing the audit needs pointed out, so it is surfaced rather than resolved here.
    """
    allowed = set(graded_rows) if graded_rows is not None else None
    contradictions = []
    for record in coverage:
        # A response is contracted to contain coverage only for graded rows. Preserve a
        # stray record in the audit trail, but do not turn commentary about a hint or
        # problem row into a contradiction about an answer that row does not carry.
        if allowed is not None and record.row not in allowed:
            continue
        computed = record.computed_answer.strip()
        submitted = record.submitted_answer.strip()
        if not computed or not submitted:
            continue
        if not record.answer_correct:
            continue

        # Models often put a derivation around the short value. Check a complete
        # calculation chain's final RHS *before* stripping prose: the live verifier wrote
        # ``P(red then blue) = (5/9)*(4/8) = 20/72 = 5/18``. Cutting at the first `` (``
        # turns that into ``P(red then blue) =`` and manufactures a contradiction.
        compact_full = "".join(computed.casefold().split())
        compact_submitted = "".join(submitted.casefold().split())
        if compact_full == compact_submitted:
            continue
        if "=" in compact_full:
            final_rhs = compact_full.rsplit("=", 1)[1]
            if final_rhs == compact_submitted:
                continue
            if answers_equivalent(
                _assignment_value(final_rhs), _assignment_value(submitted)
            ) is MathVerdict.EQUIVALENT:
                continue

        # A shorter common form is ``83 (because 7+19*4=83)``. Strip only a
        # parenthetical introduced after a space (never a function call such as
        # ``sqrt(3)``), then compare again.
        concise = computed
        for marker in (" (", " is ", " since ", " because "):
            concise = concise.split(marker, 1)[0]
        concise = concise.strip()
        compact_computed = "".join(concise.casefold().split())
        compact_submitted = "".join(submitted.casefold().split())
        if compact_computed == compact_submitted:
            continue
        if (
            "=" in compact_computed
            and compact_computed.rsplit("=", 1)[1] == compact_submitted
        ):
            continue

        verdict = answers_equivalent(
            _assignment_value(concise), _assignment_value(submitted)
        )
        # UNKNOWN is not evidence of contradiction. This is a safety gate, so it may only
        # accuse a record when deterministic mathematics says the two values differ.
        if verdict is MathVerdict.DIFFERENT:
            contradictions.append(record.row)
    return tuple(contradictions)
