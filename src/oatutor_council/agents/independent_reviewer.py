"""The Independent Reviewer. Sweeps the problems nobody flagged.

A different system prompt and a fresh context, because this is a genuinely different task
from checking a correction. Here there is no claim to verify -- the reviewer works the
mathematics from scratch and decides whether the block is sound.

Silence is not evidence. A block reaching this stage was either never reported or had its
only claim refuted, and neither says anything about whether it is correct. Treating "not
mentioned" as "fine" would mean a bogus claim, once refuted, bought a problem permanent
immunity from review.

Re-reviewing corrections to its own findings reuses the Known-Issue Reviewer's flow with
this role's prompt: the task is identical once a claim exists, and duplicating it would
create two places for the reviewer contract to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.base import AgentRole, LLMClient, LLMRequest, call_structured
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import (
    FIXED_COLUMNS,
    FindingScope,
    ProblemBlock,
    ValidationFinding,
    WorkbookConventions,
)
from .batching import attribute, make_items, rows_for_block
from .isolation import TaintRegistry
from .known_issue_reviewer import review as review_correction  # noqa: F401 - re-exported
from .rendering import render_block, render_conventions, render_findings
from .schemas import (
    BatchedIndependentReviewResponse,
    IndependentReviewResponse,
    column_key,
)

INSTRUCTIONS = """\
This problem block was not reported as defective. Examine it yourself.

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
    sections = [
        DataSection("The problem block", render_block(block)),
        DataSection(
            "Conventions this workbook follows", render_conventions(conventions)
        ),
        DataSection(
            "Deterministic findings for this block",
            render_findings(deterministic_findings),
        ),
    ]
    if curator_rules:
        sections.append(
            DataSection(
                "Curation rules the curator supplied",
                "\n".join(f"- {rule}" for rule in curator_rules),
            )
        )
    bundle = ContextBundle.build(INSTRUCTIONS, sections)
    payload = bundle.render()
    if taint is not None:
        taint.assert_clean(payload, context="independent_reviewer")

    response = call_structured(
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
    )


def _to_finding(item, block: ProblemBlock) -> ValidationFinding:
    rows = [row for row in item.rows if block.contains_row(row)]
    row = rows[0] if rows else block.start_row
    column = FIXED_COLUMNS[column_key(item.columns[0])] if item.columns else None
    return ValidationFinding(
        code="INDEPENDENT_FINDING",
        severity=item.severity,
        scope=FindingScope.CELL if column else FindingScope.ROW,
        message=item.problem,
        row=row,
        column=column,
        column_key=column_key(item.columns[0]) if item.columns else None,
        block_id=block.block_id,
        problem_name=block.problem_name,
        detail={
            "expected": item.expected,
            "category": item.category.value,
            "rows": rows or [block.start_row],
        },
    )


def blocks_to_sweep(all_blocks: Sequence[ProblemBlock], reviewed: frozenset[str]):
    """Every block without a surviving ledger entry.

    `reviewed` deliberately excludes blocks whose only issue was refuted -- see
    `IssueLedger.blocks_with_ledger_entry`. A refuted claim leaves the block untouched, so
    it belongs in this sweep.
    """
    return tuple(block for block in all_blocks if block.block_id not in reviewed)


# --------------------------------------------------------------------------------------
# Batched sweeping
# --------------------------------------------------------------------------------------

BATCH_INSTRUCTIONS = """\
Review each problem block below as a fresh reader who has been told nothing about it.

Each block is in its own section whose label carries a `batch_item` id. Return one result
per block, copying that id into `batch_item_id` exactly. A block you consider sound still
needs its own result, with `block_is_sound` true and an empty `findings` list -- omitting a
block does not mean it is sound, it means it was not reviewed, and it will be sent again.

Report a finding only under the block it belongs to, and only with rows from that block.

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
        taint.assert_clean(payload, context="independent_reviewer")

    response = call_structured(
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
            rows = rows_for_block(finding.rows, item.block)
            if rows is None:
                relocated = True
                continue
            findings.append(
                _to_finding(finding.model_copy(update={"rows": rows}), item.block)
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
            )
        )

    return tuple(results), tuple(requeue)
