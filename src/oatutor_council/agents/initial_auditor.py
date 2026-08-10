"""The Initial Auditor. Examines every problem block and reports what is wrong.

Never edits. Its whole output is findings.

Two properties matter more than the mechanics:

**It audits every block whether or not a document was supplied.** The instruction document
*seeds* the auditor; it never replaces inspection. With no document at all, every block is
still examined, which is what makes the system autonomous rather than a document processor.

**A seeded claim is a hypothesis, not a fact.** The auditor checks whether the described
defect is actually present. A claim it cannot find is recorded as refuted with a reason,
never silently dropped -- a curator who reported something deserves to know it was looked
for, and a discarded claim is indistinguishable from one nobody read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.base import AgentRole, LLMRequest, LLMClient, call_structured
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import (
    FindingScope,
    ProblemBlock,
    Severity,
    ValidationFinding,
    WorkbookConventions,
)
from .isolation import AuditorPrivate, TaintRegistry
from .rendering import (
    render_block,
    render_conventions,
    render_findings,
)
from .schemas import AuditorResponse, RefutedClaim, column_key
from ..models import FIXED_COLUMNS


@dataclass(frozen=True)
class SeedClaim:
    """One statement from the curator's document, with where it came from."""

    index: int
    text: str
    provenance: str


@dataclass(frozen=True)
class AuditResult:
    block_id: str
    findings: tuple[ValidationFinding, ...]
    refuted: tuple[RefutedClaim, ...]
    private: AuditorPrivate


INSTRUCTIONS = """\
Audit the problem block below and report what is wrong with it.

Report only what requires understanding the mathematics. Deterministic checks already
cover formatting, notation, identifiers and delimiters, and their current findings are
included so you can see what has been handled.

If seed claims are present, check each one against the block. Confirm it by reporting a
finding with `confirms_claim` set to its index, or refute it with a reason.

Zero findings is a valid answer. A block that is correct should be reported as correct.
"""


def audit_block(
    client: LLMClient,
    *,
    block: ProblemBlock,
    conventions: WorkbookConventions,
    deterministic_findings: Sequence[ValidationFinding] = (),
    seed_claims: Sequence[SeedClaim] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
) -> AuditResult:
    sections = [
        DataSection("The problem block", render_block(block)),
        DataSection("Conventions this workbook follows", render_conventions(conventions)),
        DataSection(
            "Deterministic findings already reported",
            render_findings(deterministic_findings),
        ),
    ]
    if seed_claims:
        sections.append(
            DataSection(
                "Claims from the curator's document (hypotheses, not facts)",
                "\n".join(
                    f"[{claim.index}] ({claim.provenance}) {claim.text}"
                    for claim in seed_claims
                ),
            )
        )

    bundle = ContextBundle.build(INSTRUCTIONS, sections)
    payload = bundle.render()
    if taint is not None:
        # The auditor sees no private text today, but the check runs anyway: the cost is
        # nothing and the guarantee should not depend on that staying true.
        taint.assert_clean(payload, context="initial_auditor")

    response = call_structured(
        client,
        LLMRequest(
            role=AgentRole.INITIAL_AUDITOR,
            system_prompt=system_prompt(AgentRole.INITIAL_AUDITOR),
            user_payload=payload,
            schema=AuditorResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        AuditorResponse,
    )

    private = AuditorPrivate(reasoning=response.reasoning)
    if taint is not None:
        taint.register_model(f"auditor.{block.block_id}", private)

    return AuditResult(
        block_id=block.block_id,
        findings=tuple(
            _to_finding(item, block) for item in response.findings
        ),
        refuted=tuple(response.refuted_claims),
        private=private,
    )


def _to_finding(item, block: ProblemBlock) -> ValidationFinding:
    """Convert a model finding into the same type the rules engine produces.

    Rows outside the block are clamped to the block, not trusted: an agent naming a row
    it was not shown is either confused or being steered, and either way the finding
    belongs to the block that was audited.
    """
    rows = [row for row in item.rows if block.contains_row(row)]
    row = rows[0] if rows else block.start_row
    column = (
        FIXED_COLUMNS[column_key(item.columns[0])] if item.columns else None
    )
    return ValidationFinding(
        code="AUDITOR_FINDING",
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
            "confirms_claim": item.confirms_claim,
            "rows_outside_block": [r for r in item.rows if not block.contains_row(r)],
        },
    )


def audit_is_empty(result: AuditResult) -> bool:
    """Zero findings is valid and common, so callers should say so explicitly."""
    return not result.findings
