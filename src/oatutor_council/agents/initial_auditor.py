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
    """One *hypothesis* from the curator's document, with where it came from.

    A claim is something a block can confirm or refute. Governing rules from the same
    document are deliberately not claims and never arrive here -- see
    `CurationCouncil.curator_rules`, which routes them to the Writer and reviewers as
    policy instead. A rule sent to thirty blocks as a hypothesis is refuted by the
    twenty-nine it was never about.
    """

    index: int
    text: str
    provenance: str
    #: Problem names and rows the claim points at, if it named any. Empty means it named
    #: none and there is no honest way to narrow it.
    problem_names: frozenset[str] = frozenset()
    rows: frozenset[int] = frozenset()

    def applies_to(self, block) -> bool:
        """Whether this claim could be about this block.

        A claim naming nothing applies everywhere: the curator did not say where to look,
        so refusing to look anywhere would be worse than looking everywhere. A claim that
        *did* name a problem or a row is checked only against blocks matching it, which
        is what stops twenty-nine unrelated blocks from refuting a valid report.
        """
        if not self.problem_names and not self.rows:
            return True
        if block.problem_name.casefold() in self.problem_names:
            return True
        return any(block.contains_row(row) for row in self.rows)


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
finding with `confirms_claim` set to its index, or refute it with a reason. A claim is a
hypothesis about this block: refute it only if you checked and the defect is not here,
never merely because the claim seems to be about something else.

Curation rules, where present, are policy rather than hypotheses. Apply them; do not
confirm or refute them.

Zero findings is a valid answer. A block that is correct should be reported as correct.
"""


def audit_block(
    client: LLMClient,
    *,
    block: ProblemBlock,
    conventions: WorkbookConventions,
    deterministic_findings: Sequence[ValidationFinding] = (),
    seed_claims: Sequence[SeedClaim] = (),
    curator_rules: Sequence[str] = (),
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
    # Only claims that could be about *this* block. Sending all of them and asking the
    # model to sort it out is how a claim about problem 3 gets refuted by problem 17.
    applicable = [claim for claim in seed_claims if claim.applies_to(block)]
    if applicable:
        sections.append(
            DataSection(
                "Claims from the curator's document (hypotheses, not facts)",
                "\n".join(
                    f"[{claim.index}] ({claim.provenance}) {claim.text}"
                    for claim in applicable
                ),
            )
        )
    if curator_rules:
        sections.append(
            DataSection(
                "Curation rules the curator supplied (policy, not hypotheses)",
                "\n".join(f"- {rule}" for rule in curator_rules),
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

    # A refutation only counts for a claim this block was actually shown. A model that
    # refutes something it was never given is answering a question nobody asked, and
    # recording it would let an unrelated block dismiss a valid report.
    shown = {claim.index for claim in applicable}
    return AuditResult(
        block_id=block.block_id,
        findings=tuple(
            _to_finding(item, block) for item in response.findings
        ),
        refuted=tuple(
            claim for claim in response.refuted_claims if claim.claim_index in shown
        ),
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
