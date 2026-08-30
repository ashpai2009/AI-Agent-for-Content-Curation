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

from ..llm.base import (
    AgentRole,
    LLMClient,
    LLMRequest,
    call_structured_recorded,
)
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import (
    FindingScope,
    ProblemBlock,
    Severity,
    ValidationFinding,
    WorkbookConventions,
)
from .batching import FindingAttributionError, attribute, cells_for_block, make_items
from .coverage import coverage_gaps
from .isolation import AuditorPrivate, TaintRegistry
from .rendering import (
    render_block,
    render_conventions,
    render_findings,
)
from .schemas import (
    AuditorResponse,
    BatchedAuditorResponse,
    RefutedClaim,
    RowCoverage,
    column_key,
)
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
    #: One record per graded row, and the graded rows this response did not account for.
    #: `coverage_gaps` is what decides whether the block is finished: an empty findings
    #: list means "nothing wrong on the rows I examined", and only coverage says which
    #: rows those were.
    coverage: tuple[RowCoverage, ...] = ()
    coverage_gaps: tuple[int, ...] = ()
    #: The `llm_calls` row this result was parsed from, so a persisted coverage record can
    #: name the invocation it came from instead of being an unsourced assertion.
    call_id: str = ""


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

Background notes, where present, are context only. They are neither policy nor evidence
that a defect exists; never create a finding solely because a background note says so.

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
    curator_notes: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    #: The prompt version this job is pinned to. `None` means "whatever is newest", which
    #: is right for a call made outside a job and wrong for one made inside it.
    prompt_version: int | None = None,
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
    if curator_notes:
        sections.append(
            DataSection(
                "Background the curator supplied (context, not policy or a defect claim)",
                "\n".join(f"- {note}" for note in curator_notes),
            )
        )

    bundle = ContextBundle.build(INSTRUCTIONS, sections)
    payload = bundle.render()
    if taint is not None:
        # The auditor sees no private text today, but the check runs anyway: the cost is
        # nothing and the guarantee should not depend on that staying true.
        #
        # Every section here is the workbook, a deterministic finding, or the curator's
        # own document -- public by provenance. Enumerated from the sections so a section
        # added later carrying agent prose is still checked rather than silently exempt.
        taint.assert_clean(
            payload,
            context="initial_auditor",
            public=tuple(section.content for section in sections),
        )

    response, call_id = call_structured_recorded(
        client,
        LLMRequest(
            role=AgentRole.INITIAL_AUDITOR,
            system_prompt=system_prompt(AgentRole.INITIAL_AUDITOR, prompt_version),
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
    shown = frozenset(claim.index for claim in applicable)
    return AuditResult(
        block_id=block.block_id,
        findings=tuple(
            _to_finding(item, block, shown=shown) for item in response.findings
        ),
        refuted=tuple(
            claim for claim in response.refuted_claims if claim.claim_index in shown
        ),
        private=private,
        coverage=tuple(response.coverage),
        coverage_gaps=coverage_gaps(block, response.coverage),
        call_id=call_id,
    )


def _to_finding(
    item, block: ProblemBlock, *, shown: frozenset[int] | None = None
) -> ValidationFinding:
    """Convert a model finding into the same type the rules engine produces.

    A finding containing any cell outside the block is rejected as a unit. Keeping only
    its in-block cells would silently turn one coordinated repair into a different,
    incomplete repair. The caller requeues that block for a fresh audit rather than
    relocating or pruning what the model said.

    `shown` filters `confirms_claim` the same way refutations have always been filtered.
    Without it a block can confirm a claim it was never given, and since one confirmation
    anywhere settles a claim, a wrong index reaches the curator's report as "found". Only
    the *association* is stripped, never the finding: the claim link being unverified says
    nothing about whether the mathematics is wrong, and discarding the finding would lose a
    real defect because the model attached a bad citation to it.
    """
    cells = [cell for cell in item.cells if block.contains_row(cell.row)]
    if len(cells) != len(item.cells):
        raise FindingAttributionError(
            "finding names a cell outside the block it audited"
        )
    cells = list({(cell.row, cell.column): cell for cell in cells}.values())
    row = cells[0].row
    confirms = item.confirms_claim
    if confirms is not None and shown is not None and confirms not in shown:
        confirms = None
    column = FIXED_COLUMNS[column_key(cells[0].column)]
    target_cells = [
        [cell.row, FIXED_COLUMNS[column_key(cell.column)]] for cell in cells
    ]
    target_rows = list(dict.fromkeys(cell.row for cell in cells))
    target_columns = list(dict.fromkeys(cell.column for cell in cells))
    return ValidationFinding(
        code="AUDITOR_FINDING",
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
            # `row`/`column` retain the primary location for reporting and stable
            # fingerprints. `cells` is the complete repair authority. A semantic defect
            # can require coordinated edits (for example Answer and answerType on the
            # same row), and throwing every target after the first away is what made the
            # gate reject the only complete repair as an unrelated structural edit.
            "cells": target_cells,
            "column_keys": target_columns,
            "confirms_claim": confirms,
            "cells_outside_block": [],
        },
    )


def audit_is_empty(result: AuditResult) -> bool:
    """Zero findings is valid and common, so callers should say so explicitly."""
    return not result.findings


# --------------------------------------------------------------------------------------
# Batched auditing
# --------------------------------------------------------------------------------------

BATCH_INSTRUCTIONS = """\
Audit each problem block below and report what is wrong with it.

Each block is in its own section whose label carries a `batch_item` id. Return one result
per block, copying that id into `batch_item_id` exactly. A block with nothing wrong still
needs its own result, with an empty `findings` list -- omitting a block does not mean it is
correct, it means it was not examined, and it will be sent again.

Report a finding only under the block it belongs to, and name only exact target cells
whose rows are inside that block.

Report only what requires understanding the mathematics. Deterministic checks already
cover formatting, notation, identifiers and delimiters, and their current findings are
included so you can see what has been handled.

If seed claims are present for a block, check each one against that block. Confirm it by
reporting a finding with `confirms_claim` set to its index, or refute it with a reason. A
claim is a hypothesis about the block it was shown with.

Curation rules, where present, are policy rather than hypotheses. Apply them; do not
confirm or refute them.

Background notes, where present, are context only. They are neither policy nor evidence
that a defect exists; never create a finding solely because a background note says so.
"""


def audit_blocks(
    client: LLMClient,
    *,
    blocks: Sequence[ProblemBlock],
    conventions,
    findings_for=None,
    seed_claims: Sequence[SeedClaim] = (),
    curator_rules: Sequence[str] = (),
    curator_notes: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    prompt_version: int | None = None,
) -> tuple[tuple[AuditResult, ...], tuple[ProblemBlock, ...]]:
    """Audit several blocks in one call. Returns `(results, blocks_to_requeue)`.

    **One block delegates to `audit_block`.** Not a wrapper the other way around: at
    `SCAN_BATCH_SIZE=1` the prompt, the schema and the payload must be the ones that were
    there before batching existed, or "the default changes nothing" is a claim about
    similarity rather than identity.
    """
    blocks = list(blocks)
    if not blocks:
        return (), ()
    if len(blocks) == 1:
        try:
            result = audit_block(
                client,
                block=blocks[0],
                conventions=conventions,
                deterministic_findings=findings_for(blocks[0]) if findings_for else (),
                seed_claims=seed_claims,
                curator_rules=curator_rules,
                curator_notes=curator_notes,
                job_id=job_id,
                seed=seed,
                taint=taint,
                prompt_version=prompt_version,
            )
        except FindingAttributionError:
            return (), (blocks[0],)
        return (result,), ()

    items = make_items(
        blocks,
        claims_for=lambda block: [
            claim.index for claim in seed_claims if claim.applies_to(block)
        ],
    )

    sections = [
        DataSection("Conventions this workbook follows", render_conventions(conventions))
    ]
    for item in items:
        # The problem name lives *inside* the neutralised body. The label carries generated
        # data only -- see `BatchItem.label`.
        body = [
            f"problem name: {item.block.problem_name}",
            render_block(item.block),
            "",
            "Deterministic findings already reported:",
            render_findings(findings_for(item.block) if findings_for else ()),
        ]
        applicable = [claim for claim in seed_claims if claim.index in item.claims_shown]
        if applicable:
            body += [
                "",
                "Claims from the curator's document (hypotheses about this block):",
                *(
                    f"[{claim.index}] ({claim.provenance}) {claim.text}"
                    for claim in applicable
                ),
            ]
        sections.append(DataSection(item.label, "\n".join(body)))

    if curator_rules:
        sections.append(
            DataSection(
                "Curation rules the curator supplied (policy, not hypotheses)",
                "\n".join(f"- {rule}" for rule in curator_rules),
            )
        )
    if curator_notes:
        sections.append(
            DataSection(
                "Background the curator supplied (context, not policy or a defect claim)",
                "\n".join(f"- {note}" for note in curator_notes),
            )
        )

    bundle = ContextBundle.build(BATCH_INSTRUCTIONS, sections)
    payload = bundle.render()
    if taint is not None:
        # As in the single-block path: workbook, deterministic findings and the curator's
        # document, enumerated rather than blanket-exempted.
        taint.assert_clean(
            payload,
            context="initial_auditor",
            public=tuple(section.content for section in sections),
        )

    response, call_id = call_structured_recorded(
        client,
        LLMRequest(
            role=AgentRole.INITIAL_AUDITOR,
            system_prompt=system_prompt(AgentRole.INITIAL_AUDITOR, prompt_version),
            user_payload=payload,
            schema=BatchedAuditorResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        BatchedAuditorResponse,
    )

    attribution = attribute(items, response.results)
    results: list[AuditResult] = []
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
                # Every row it named is in some other block. Discard it and send this
                # block back: relocating it would invent a defect nobody reported.
                relocated = True
                continue
            findings.append(
                _to_finding(
                    finding.model_copy(update={"cells": cells}),
                    item.block,
                    shown=item.claims_shown,
                )
            )

        if relocated:
            requeue.append(item.block)
            continue

        private = AuditorPrivate(reasoning=result.reasoning)
        if taint is not None:
            taint.register_model(f"auditor.{item.block.block_id}", private)
        results.append(
            AuditResult(
                block_id=item.block.block_id,
                findings=tuple(findings),
                refuted=tuple(
                    claim
                    for claim in result.refuted_claims
                    if claim.claim_index in item.claims_shown
                ),
                private=private,
                coverage=tuple(result.coverage),
                coverage_gaps=coverage_gaps(item.block, result.coverage),
                call_id=call_id,
            )
        )

    return tuple(results), tuple(requeue)
