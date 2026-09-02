"""The Final Semantic Verifier. Re-solves corrected blocks after their last edit.

Every other scan in this pipeline examined a workbook that has since been edited. The
Initial Auditor read the file as submitted; the Independent Reviewer swept it mid-repair.
By the time the last patch is applied, **nothing in the record is a statement about the
file the curator is handed** -- and the repairs themselves are the edits most worth
re-checking, because each one was made by a model and accepted by a model.

So this phase runs after all repairs and before deterministic validation, and it is not a
re-run of an earlier marker. A block's verification is *invalidated by any accepted repair
to it* and redone, which is the only way "this workbook was checked after its last change"
can be true rather than merely plausible.

It is shown the block, conventions, curator rules, and the literal public net diff from
the uploaded block. It receives no issue description, deterministic finding, model
reasoning, or verdict. The diff focuses it on the content the pipeline changed without
letting an earlier agent's explanation become its evidence.

It never edits. Its findings are model claims like any other, and they go through the same
claim-blind corroboration and adjudication before a Writer is allowed near a cell. A last
agent whose word alone could rewrite the file would be the least reviewed edit in the run.
"""

from __future__ import annotations

from typing import Sequence

from ..llm.base import (
    AgentRole,
    LLMClient,
    LLMRequest,
    call_structured_recorded,
)
from ..llm.context import ContextBundle
from ..llm.prompts import system_prompt
from ..models import (
    ChangeRecord,
    FindingScope,
    ProblemBlock,
    Severity,
    ValidationFinding,
    WorkbookConventions,
)
from .batching import FindingAttributionError
from .coverage import coverage_gaps
from .isolation import TaintRegistry
from .rendering import (
    FinalVerificationContext,
    render_block,
    render_candidate_edits,
    render_conventions,
)
from .schemas import FinalVerificationResponse, RowCoverage, column_key
from ..models import FIXED_COLUMNS
from dataclasses import dataclass

INSTRUCTIONS = """\
This changed problem block is about to be handed back to a curator. Check it after its
last accepted edit.

Solve every graded row yourself, from the question as posed. You are given the block, the
conventions the workbook follows, the curation rules, and a literal source-to-current diff.
The diff says what bytes changed, not why. Treat it as a checklist, not an answer key:
derive the affected mathematics yourself and check that coordinated fields still agree.

Return a coverage record for **every** graded row — every row whose Row Type is `step` or
`scaffold` — whether or not you find anything wrong. A block short of its graded rows is
verified again, so an omission settles nothing and costs a call.

Report a finding only for a defect that is still present now. Name every exact cell that
must change. Most blocks at this stage are correct: equivalent expressions are not defects,
and do not ask for simplification, rewording, decimal conversion or choice reordering when
the content is already right.
"""


@dataclass(frozen=True)
class VerificationResult:
    block_id: str
    findings: tuple[ValidationFinding, ...]
    block_is_sound: bool
    coverage: tuple[RowCoverage, ...] = ()
    coverage_gaps: tuple[int, ...] = ()
    call_id: str = ""


def verify_block(
    client: LLMClient,
    *,
    block: ProblemBlock,
    conventions: WorkbookConventions,
    changes: Sequence[ChangeRecord] = (),
    curator_rules: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    prompt_version: int | None = None,
) -> VerificationResult:
    context = FinalVerificationContext(
        block=render_block(block),
        conventions=render_conventions(conventions),
        changed_content=render_candidate_edits(changes),
        curator_rules="\n".join(f"- {rule}" for rule in curator_rules),
    )
    bundle = ContextBundle.build(INSTRUCTIONS, context.sections())
    payload = bundle.render()

    if taint is not None:
        # Every section is public by provenance -- the workbook, its derived conventions,
        # the curator's own rules. Nothing an agent wrote reaches this payload, which is
        # what makes the check here narrow enough to be meaningful.
        taint.assert_clean(
            payload,
            context=AgentRole.FINAL_VERIFIER.value,
            public=(
                context.block,
                context.changed_content,
                context.conventions,
                context.curator_rules,
            ),
        )

    response, call_id = call_structured_recorded(
        client,
        LLMRequest(
            role=AgentRole.FINAL_VERIFIER,
            system_prompt=system_prompt(AgentRole.FINAL_VERIFIER, prompt_version),
            user_payload=payload,
            schema=FinalVerificationResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        FinalVerificationResponse,
    )

    # **Any target outside the block rejects the whole result**, rather than the in-block
    # part of it being kept. Pruning looks conservative and is not: a finding naming an
    # answer here and its choice list two blocks away describes one repair, and keeping
    # half of it authorises an edit the verifier never proposed -- an incomplete repair
    # that then passes review because the reviewer is shown the half that survived. It is
    # also evidence the agent was not reading this block, which is not a thing to salvage
    # a partial answer from. Same rule the batch reader applies, and the same exception.
    stray = [
        (item, cell)
        for item in response.findings
        for cell in item.cells
        if not block.contains_row(cell.row)
    ]
    if stray:
        named = ", ".join(
            f"row {cell.row}" for _, cell in stray[:3]
        ) + ("…" if len(stray) > 3 else "")
        raise FindingAttributionError(
            f"final verification of {block.block_id} named {named}, outside rows "
            f"{block.start_row}-{block.end_row}"
        )

    findings = tuple(_to_finding(item, block) for item in response.findings)
    return VerificationResult(
        block_id=block.block_id,
        findings=findings,
        # A concrete finding beats a soundness claim made alongside it, the same rule the
        # independent sweep applies: an agent that reports a defect and calls the block
        # sound has contradicted itself, and the finding is the specific half.
        block_is_sound=response.block_is_sound and not findings,
        coverage=tuple(response.coverage),
        coverage_gaps=coverage_gaps(block, response.coverage),
        call_id=call_id,
    )


def _to_finding(item, block: ProblemBlock) -> ValidationFinding:
    """One published finding. Every cell is already known to be inside this block.

    The caller rejects the entire response if any target was outside, so there is nothing
    to filter here -- and deliberately no filtering, because a silent filter is how a
    partial repair gets authorised.
    """
    cells = list(item.cells)
    primary = cells[0]
    column = FIXED_COLUMNS[column_key(primary.column)]
    return ValidationFinding(
        code="FINAL_VERIFICATION_FINDING",
        severity=item.severity or Severity.ERROR,
        scope=FindingScope.CELL,
        message=item.problem,
        row=primary.row,
        column=column,
        column_key=column_key(primary.column),
        block_id=block.block_id,
        problem_name=block.problem_name,
        detail={
            "cells": [
                [cell.row, FIXED_COLUMNS[column_key(cell.column)]] for cell in cells
            ],
            "column_keys": list(dict.fromkeys(cell.column for cell in cells)),
            "category": item.category.value,
            "expected": item.expected,
            "repairable": True,
        },
    )
