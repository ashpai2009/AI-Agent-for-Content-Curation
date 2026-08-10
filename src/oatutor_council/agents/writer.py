"""The Writer. One issue and one block in, one patch out.

The Writer is the only agent that proposes changes, and its output is deliberately narrow:
exact cell edits with exact `before` values. Everything about whether those edits are
*allowed* is decided afterwards by the deterministic gate, not here.

Its rationale is split off immediately into `WriterPrivate` and registered with the taint
registry. From that point the patch travels onward and the argument for it does not.

`needs_human_review` is terminal on first occurrence. An agent that says it cannot decide
is giving a useful answer, and asking it twice more produces a guess -- which is worse
than an escalation, because a guessed correction gets reviewed as though someone checked
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from ..llm.base import AgentRole, LLMClient, LLMRequest, call_structured
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import FIXED_COLUMNS, CellEdit, Issue, Patch, ProblemBlock, ValidationFinding
from .isolation import TaintRegistry, WriterPrivate
from .rendering import (
    render_block,
    render_conventions,
    render_findings,
    render_issue,
)
from .schemas import WriterResponse, column_key


@dataclass(frozen=True)
class WriterResult:
    patch: Patch
    private: WriterPrivate


INSTRUCTIONS = """\
Produce the exact cell edits that resolve the issue below.

Copy each `before` value character for character from the block. It is verified before
anything is written, and a mismatch rejects the whole patch and spends an attempt.

Edit only cells inside this block, and only cells the issue concerns.

If you cannot determine the correct content confidently, set `needs_human_review` and
produce no edits.
"""


class WriterProposedNothing(Exception):
    """The Writer returned neither edits nor an escalation.

    Treated as a malformed response rather than an empty patch: a patch with no edits and
    no escalation is not an answer to the question that was asked.
    """


def propose_patch(
    client: LLMClient,
    *,
    issue: Issue,
    block: ProblemBlock,
    conventions,
    attempt_no: int,
    deterministic_findings: Sequence[ValidationFinding] = (),
    reviewer_feedback: str = "",
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
) -> WriterResult:
    sections = [
        DataSection("The issue to resolve", render_issue(issue)),
        DataSection("The problem block", render_block(block)),
        DataSection("Conventions this workbook follows", render_conventions(conventions)),
        DataSection(
            "Deterministic findings for this block", render_findings(deterministic_findings)
        ),
    ]
    if reviewer_feedback:
        # The reviewer's feedback is public by design -- it is the only thing that
        # travels back to the Writer, and it has to be actionable.
        sections.append(
            DataSection("Reviewer feedback on your previous attempt", reviewer_feedback)
        )

    bundle = ContextBundle.build(INSTRUCTIONS, sections)
    payload = bundle.render()

    response = call_structured(
        client,
        LLMRequest(
            role=AgentRole.WRITER,
            system_prompt=system_prompt(AgentRole.WRITER),
            user_payload=payload,
            schema=WriterResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
            issue_id=issue.issue_id,
        ),
        WriterResponse,
    )

    private = WriterPrivate(
        reasoning=response.reasoning,
        derivation=response.derivation,
        confidence=response.confidence,
    )
    if taint is not None:
        taint.register_model(f"writer.{issue.issue_id}.{attempt_no}", private)

    patch = _to_patch(response, issue=issue, attempt_no=attempt_no)
    return WriterResult(patch=patch, private=private)


def _to_patch(response: WriterResponse, *, issue: Issue, attempt_no: int) -> Patch:
    if response.needs_human_review:
        return Patch(
            patch_id=uuid4().hex,
            issue_id=issue.issue_id,
            attempt_no=attempt_no,
            edits=(),
            needs_human_review=True,
            human_review_reason=response.human_review_reason
            or "the Writer could not determine the correct content confidently",
        )

    if not response.edits:
        raise WriterProposedNothing(
            "the Writer returned no edits and did not escalate; a patch must either "
            "change something or say it cannot"
        )

    edits = []
    for edit in response.edits:
        key = column_key(edit.column)
        edits.append(
            CellEdit(
                row=edit.row,
                column=FIXED_COLUMNS[key],
                column_key=key,
                before=edit.before,
                after=edit.after,
            )
        )

    return Patch(
        patch_id=uuid4().hex,
        issue_id=issue.issue_id,
        attempt_no=attempt_no,
        edits=tuple(edits),
        # The rationale lives on the patch for the human change log. Reviewer prompts are
        # built from `ReviewerContext`, which structurally cannot reference it.
        reason=response.reasoning,
        derivation=response.derivation,
        confidence=response.confidence,
    )
