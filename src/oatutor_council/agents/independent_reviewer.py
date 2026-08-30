"""The Independent Reviewer. Sweeps every problem after known repairs.

A different system prompt and a fresh context, because this is a genuinely different task
from checking a correction. Here there is no claim to verify -- the reviewer works the
mathematics from scratch and decides whether the block is sound.

Silence is not evidence, and neither is one finding exhaustive. A block where the first
auditor found one defect may contain a second defect the issue reviewer is not authorised
to invent. Every current block is therefore reviewed from scratch after known repairs.

Re-reviewing corrections to its own findings reuses the Known-Issue Reviewer's flow with
this role's prompt: the task is identical once a claim exists, and duplicating it would
create two places for the reviewer contract to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.base import (
    AgentRole,
    LLMClient,
    LLMRequest,
    call_structured_recorded,
)
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import (
    FIXED_COLUMNS,
    FindingScope,
    ProblemBlock,
    ValidationFinding,
    WorkbookConventions,
)
from .batching import FindingAttributionError, attribute, cells_for_block, make_items
from .coverage import coverage_gaps
from .isolation import TaintRegistry
from .known_issue_reviewer import review as review_correction  # noqa: F401 - re-exported
from .rendering import render_block, render_conventions, render_findings
from .schemas import (
    BatchedIndependentReviewResponse,
    IndependentReviewResponse,
    RowCoverage,
    column_key,
)

INSTRUCTIONS = """\
Examine this current problem block from scratch, whether or not another agent previously
reported or repaired something in it.

Solve the problem as a student would and check that the stated answer is what you get.
Check that each step follows from the last, that hints point toward the answer, and that a
multiple-choice list has exactly one correct option.

Most blocks are correct. Report a finding only when you can state concretely what is wrong
and where; if the block is sound, say so.
"""


@dataclass(frozen=True)
class SweepResult:
    block_id: str
    findings: tuple[ValidationFinding, ...]
    block_is_sound: bool
    #: `block_is_sound` is an assertion; this is what backs it. A sweep that declares a
    #: block sound without accounting for each of its graded rows has asserted something
    #: about rows it did not say it looked at.
    coverage: tuple[RowCoverage, ...] = ()
    coverage_gaps: tuple[int, ...] = ()
    call_id: str = ""


def sweep_block(
    client: LLMClient,
    *,
    block: ProblemBlock,
    conventions: WorkbookConventions,
    deterministic_findings: Sequence[ValidationFinding] = (),
    curator_rules: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    #: The prompt version this job is pinned to. `None` means "whatever is newest", which
    #: is right for a call made outside a job and wrong for one made inside it.
    prompt_version: int | None = None,
) -> SweepResult:
    # Named rather than inlined because the taint check needs the same strings as public
    # ground: they come from the workbook and the rule engine, so a private entry that
    # merely quotes them is a quotation and not a leak.
    block_text = render_block(block)
    conventions_text = render_conventions(conventions)
    findings_text = render_findings(deterministic_findings)
    rules_text = "\n".join(f"- {rule}" for rule in curator_rules)

    sections = [
        DataSection("The problem block", block_text),
        DataSection("Conventions this workbook follows", conventions_text),
        DataSection("Deterministic findings for this block", findings_text),
    ]
    if curator_rules:
        sections.append(
            DataSection("Curation rules the curator supplied", rules_text)
        )
    bundle = ContextBundle.build(INSTRUCTIONS, sections)
    payload = bundle.render()
    if taint is not None:
        taint.assert_clean(
            payload,
            context="independent_reviewer",
            public=(block_text, conventions_text, findings_text, rules_text),
        )

    response, call_id = call_structured_recorded(
        client,
        LLMRequest(
            role=AgentRole.INDEPENDENT_REVIEWER,
            system_prompt=system_prompt(AgentRole.INDEPENDENT_REVIEWER, prompt_version),
            user_payload=payload,
            schema=IndependentReviewResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        IndependentReviewResponse,
    )

    findings = tuple(_to_finding(item, block) for item in response.findings)
    # A response claiming soundness while listing findings is contradictory; the findings
    # are the concrete claim, so they win.
    return SweepResult(
        block_id=block.block_id,
        findings=findings,
        block_is_sound=response.block_is_sound and not findings,
        coverage=tuple(response.coverage),
        coverage_gaps=coverage_gaps(block, response.coverage),
        call_id=call_id,
    )


def _to_finding(item, block: ProblemBlock) -> ValidationFinding:
    cells = [cell for cell in item.cells if block.contains_row(cell.row)]
    if len(cells) != len(item.cells):
        raise FindingAttributionError(
            "finding names a cell outside the block it reviewed"
        )
    cells = list({(cell.row, cell.column): cell for cell in cells}.values())
    row = cells[0].row
    column = FIXED_COLUMNS[column_key(cells[0].column)]
    target_rows = list(dict.fromkeys(cell.row for cell in cells))
    target_columns = list(dict.fromkeys(cell.column for cell in cells))
    return ValidationFinding(
        code="INDEPENDENT_FINDING",
        severity=item.severity,
        scope=FindingScope.CELL,
        message=item.problem,
        row=row,
        column=column,
        column_key=column_key(cells[0].column),
        block_id=block.block_id,
        problem_name=block.problem_name,
        detail={
            "expected": item.expected,
            "category": item.category.value,
            "rows": target_rows,
            "cells": [
                [cell.row, FIXED_COLUMNS[column_key(cell.column)]] for cell in cells
            ],
            "column_keys": target_columns,
            "cells_outside_block": [],
        },
    )


def blocks_to_sweep(
    all_blocks: Sequence[ProblemBlock], reviewed: frozenset[str] = frozenset()
):
    """Every block, including blocks with an earlier finding or accepted repair.

    `reviewed` is retained as an ignored compatibility parameter for callers and old
    integrations. A known-issue reviewer decides only whether one claim was resolved; it
    cannot certify that the rest of the block contains no independent defect.
    """
    del reviewed
    return tuple(all_blocks)


# --------------------------------------------------------------------------------------
# Batched sweeping
# --------------------------------------------------------------------------------------

BATCH_INSTRUCTIONS = """\
Review each current problem block below from scratch, whether or not another agent
previously reported or repaired something in it.

Each block is in its own section whose label carries a `batch_item` id. Return one result
per block, copying that id into `batch_item_id` exactly. A block you consider sound still
needs its own result, with `block_is_sound` true and an empty `findings` list -- omitting a
block does not mean it is sound, it means it was not reviewed, and it will be sent again.

Report a finding only under the block it belongs to, and name only exact target cells
whose rows are inside that block.

Solve each problem as a student would and check that the stated answer is what you get.
Check that each step follows from the last, that hints point toward the answer, and that a
multiple-choice list has exactly one correct option.

Most blocks are correct. Report a finding only when you can state concretely what is wrong
and where.
"""


def sweep_blocks(
    client: LLMClient,
    *,
    blocks: Sequence[ProblemBlock],
    conventions: WorkbookConventions,
    findings_for=None,
    curator_rules: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    prompt_version: int | None = None,
) -> tuple[tuple[SweepResult, ...], tuple[ProblemBlock, ...]]:
    """Sweep several blocks in one call. Returns `(results, blocks_to_requeue)`.

    **One block delegates to `sweep_block`**, so batch size 1 is the pre-batching code
    path rather than something that resembles it.
    """
    blocks = list(blocks)
    if not blocks:
        return (), ()
    if len(blocks) == 1:
        try:
            result = sweep_block(
                client,
                block=blocks[0],
                conventions=conventions,
                deterministic_findings=findings_for(blocks[0]) if findings_for else (),
                curator_rules=curator_rules,
                job_id=job_id,
                seed=seed,
                taint=taint,
                prompt_version=prompt_version,
            )
        except FindingAttributionError:
            return (), (blocks[0],)
        return (result,), ()

    items = make_items(blocks)
    sections = [
        DataSection("Conventions this workbook follows", render_conventions(conventions))
    ]
    for item in items:
        sections.append(
            DataSection(
                item.label,
                "\n".join(
                    [
                        f"problem name: {item.block.problem_name}",
                        render_block(item.block),
                        "",
                        "Deterministic findings for this block:",
                        render_findings(
                            findings_for(item.block) if findings_for else ()
                        ),
                    ]
                ),
            )
        )
    if curator_rules:
        sections.append(
            DataSection(
                "Curation rules the curator supplied",
                "\n".join(f"- {rule}" for rule in curator_rules),
            )
        )

    bundle = ContextBundle.build(BATCH_INSTRUCTIONS, sections)
    payload = bundle.render()
    if taint is not None:
        # Every section above is workbook content, a rendered convention, a deterministic
        # finding, or the curator's own rules -- all public by provenance, so all of them
        # are public ground. This is enumerated from the sections rather than written as
        # "the whole payload" on purpose: a section added later that carries agent prose
        # will not appear here, and so will still be checked.
        taint.assert_clean(
            payload,
            context="independent_reviewer",
            public=tuple(section.content for section in sections),
        )

    response, call_id = call_structured_recorded(
        client,
        LLMRequest(
            role=AgentRole.INDEPENDENT_REVIEWER,
            system_prompt=system_prompt(AgentRole.INDEPENDENT_REVIEWER, prompt_version),
            user_payload=payload,
            schema=BatchedIndependentReviewResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        BatchedIndependentReviewResponse,
    )

    attribution = attribute(items, response.results)
    results: list[SweepResult] = []
    requeue = [item.block for item in attribution.requeue]

    for item in items:
        result = attribution.resolved.get(item.item_id)
        if result is None:
            continue

        findings: list[ValidationFinding] = []
        relocated = False
        for finding in result.findings:
            cells = cells_for_block(finding.cells, item.block)
            if cells is None:
                relocated = True
                continue
            findings.append(
                _to_finding(finding.model_copy(update={"cells": cells}), item.block)
            )

        if relocated:
            requeue.append(item.block)
            continue

        results.append(
            SweepResult(
                block_id=item.block.block_id,
                findings=tuple(findings),
                # Same contradiction rule as the single-block path: concrete findings beat
                # a soundness claim made alongside them.
                block_is_sound=result.block_is_sound and not findings,
                coverage=tuple(result.coverage),
                coverage_gaps=coverage_gaps(item.block, result.coverage),
                call_id=call_id,
            )
        )

    return tuple(results), tuple(requeue)
