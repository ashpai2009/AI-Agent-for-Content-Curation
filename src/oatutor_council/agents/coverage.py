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

from typing import Iterable, Sequence

from ..models import ProblemBlock
from .schemas import RowCoverage


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


def self_contradicting(coverage: Iterable[RowCoverage]) -> tuple[int, ...]:
    """Rows whose own record does not hold together.

    A row that reports a computed answer differing from the submitted one while also
    reporting `answer_correct` has contradicted itself in a single record. That is not a
    defect in the workbook and it is not grounds to re-scan -- the model may simply have
    written the same value two ways -- but it is exactly the kind of thing a person
    auditing the audit needs pointed out, so it is surfaced rather than resolved here.
    """
    contradictions = []
    for record in coverage:
        computed = record.computed_answer.strip()
        submitted = record.submitted_answer.strip()
        if not computed or not submitted:
            continue
        if record.answer_correct and computed.casefold() != submitted.casefold():
            contradictions.append(record.row)
    return tuple(contradictions)
