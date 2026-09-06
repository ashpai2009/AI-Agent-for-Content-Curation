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
_EXPLANATION_SUFFIX = re.compile(
    r"(?:,?\s+(?:because|since|using|where|from|which|given)\b|\s+\(because\b)",
    re.IGNORECASE,
)
_UNIT_SUFFIX = re.compile(
    r"\s*(?:degrees?|radians?|units?|square\s+units?|cubic\s+units?)\.?$",
    re.IGNORECASE,
)


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


def _answer_candidates(text: str) -> tuple[str, ...]:
    """Conservative mathematical readings of a verifier's short answer.

    Coverage prose is not a workbook cell. Models legitimately return a function label,
    a derivation, an approximation, or a unit beside the same value. Comparing only the
    complete strings made those forms look contradictory. These reductions remove only
    wrappers whose right-hand mathematical value remains explicit; ordinary prose stays
    unparseable and therefore can never become evidence of a contradiction.
    """
    cleaned = text.strip().strip("$").strip()
    concise = _EXPLANATION_SUFFIX.split(cleaned, maxsplit=1)[0].strip()
    fragments = [concise]
    for marker in ("≈", r"\approx"):
        expanded: list[str] = []
        for fragment in fragments:
            expanded.extend(part.strip() for part in fragment.split(marker) if part.strip())
        fragments = expanded or fragments

    candidates: list[str] = []
    for fragment in fragments:
        without_units = _UNIT_SUFFIX.sub("", fragment).strip()
        for candidate in (fragment, without_units):
            if candidate:
                candidates.append(candidate)
            if "=" in candidate:
                rhs = candidate.rsplit("=", 1)[1].strip()
                if rhs:
                    candidates.append(rhs)
            assigned = _assignment_value(candidate)
            if assigned != candidate:
                candidates.append(assigned)
    return tuple(dict.fromkeys(candidates))


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

        verdicts: list[MathVerdict] = []
        for computed_candidate in _answer_candidates(computed):
            for submitted_candidate in _answer_candidates(submitted):
                compact_computed = "".join(computed_candidate.casefold().split())
                compact_submitted = "".join(submitted_candidate.casefold().split())
                if compact_computed == compact_submitted:
                    verdicts = [MathVerdict.EQUIVALENT]
                    break
                verdicts.append(
                    answers_equivalent(computed_candidate, submitted_candidate)
                )
            if MathVerdict.EQUIVALENT in verdicts:
                break

        if MathVerdict.EQUIVALENT in verdicts:
            continue
        # UNKNOWN is not evidence of contradiction. Requiring every plausible reading
        # to be deterministically different makes this a high-precision warning rather
        # than a prose-similarity detector. Clear numeric disagreements such as 5 versus
        # 6 still fire; explanations and labels remain silent.
        if verdicts and all(verdict is MathVerdict.DIFFERENT for verdict in verdicts):
            contradictions.append(record.row)
    return tuple(contradictions)
