"""The Writer. One block and one or more issues in, one patch per issue out.

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
from typing import Mapping, Sequence
from uuid import uuid4

from pydantic import ValidationError

from ..llm.base import AgentRole, LLMClient, LLMRequest, MalformedResponse, call_structured
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import (
    FIXED_COLUMNS,
    CellEdit,
    Issue,
    Patch,
    PatchRejection,
    ProblemBlock,
    RejectionCode,
    ValidationFinding,
)
from .isolation import TaintRegistry, WriterPrivate
from .rendering import (
    render_block,
    render_conventions,
    render_findings,
    render_issue,
)
from .schemas import BlockWriterResponse, WriterResponse, column_key


@dataclass(frozen=True)
class WriterResult:
    patch: Patch | None
    private: WriterPrivate
    rejection: PatchRejection | None = None


INSTRUCTIONS = """\
Produce the exact cell edits that resolve the issue below.

Copy each `before` value character for character from the block. It is verified before
anything is written, and a mismatch rejects the whole patch and spends an attempt.

Edit only cells inside this block, and only cells the issue concerns.

Your patch must actually resolve the stated issue. It is simulated and the rule that
raised the issue is re-run against the result; a patch that leaves the defect in place is
rejected and spends an attempt, however sound the edit is on its own terms.

Do not make unrelated improvements. If a cell the issue did not name has to change for
this repair to work -- a choice list that must match a corrected answer, the cell a
shifted value came from -- edit it and say why in `related_edits_reason`. An edit outside
the issue's cells with no stated reason is rejected, and so is one that turns out not to
have been needed.

If the issue is about how a value is *written* rather than what it is -- notation, LaTeX,
formatting -- the corrected cell must still mean the same thing. That is checked
symbolically.

`derivation` is required in every response. If any edit targets `answer` or `mc_choices`,
it must be non-empty and must verify the proposed value concretely: show the calculation,
state which choice matches the answer exactly, or state why a notation-only rewrite is
mathematically equivalent. A mathematical edit with an empty derivation is rejected and
spends an attempt. For a response that edits no mathematical cell, use an empty string.

If you cannot determine the correct content confidently, set `needs_human_review` and
produce no edits; still include `derivation` as an empty string.
"""


class WriterProposedNothing(Exception):
    """The Writer returned neither edits nor an escalation.

    Treated as a malformed response rather than an empty patch: a patch with no edits and
    no escalation is not an answer to the question that was asked.
    """


class WriterBatchIncomplete(MalformedResponse):
    """A block response omitted, duplicated, or invented an issue identifier."""


BLOCK_INSTRUCTIONS = """\
Produce a coordinated repair proposal for every issue listed below. You are seeing the
issues together because their repairs share one problem block and may interact.

Return exactly one result for every supplied issue_id and no others. Each result remains
an independently reviewable patch: put every cell needed to resolve that issue in that
issue's proposal, do not split one necessary repair across two results, and never propose
two different after-values for the same cell.

For every proposal, follow the same rules as a single repair: copy `before` exactly,
edit only this block, make no unrelated improvement, explain any related cell, and put a
concrete verification in `derivation` whenever answer or mc_choices changes. If an issue
cannot be resolved confidently, escalate that issue only; do not guess and do not omit it.
"""


def propose_patches(
    client: LLMClient,
    *,
    issues: Sequence[Issue],
    block: ProblemBlock,
    conventions,
    attempt_numbers: Mapping[str, int],
    deterministic_findings: Sequence[ValidationFinding] = (),
    reviewer_feedback: Mapping[str, str] | None = None,
    curator_rules: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    prompt_version: int | None = None,
) -> tuple[WriterResult, ...]:
    """Ask once for every ready issue in a block, preserving per-issue patches."""
    if not issues:
        return ()
    feedback = reviewer_feedback or {}
    issue_text = []
    for issue in issues:
        rendered = f"issue_id: {issue.issue_id}\n{render_issue(issue)}"
        prior = feedback.get(issue.issue_id, "").strip()
        if prior:
            rendered += f"\nFeedback on its previous attempt:\n{prior}"
        issue_text.append(rendered)

    sections = [
        DataSection("Issues to resolve together", "\n\n".join(issue_text)),
        DataSection("The shared problem block", render_block(block)),
        DataSection("Conventions this workbook follows", render_conventions(conventions)),
        DataSection(
            "Deterministic findings for this block", render_findings(deterministic_findings)
        ),
    ]
    if curator_rules:
        sections.append(
            DataSection(
                "Curation rules the curator supplied",
                "\n".join(f"- {rule}" for rule in curator_rules),
            )
        )
    response = call_structured(
        client,
        LLMRequest(
            role=AgentRole.WRITER,
            system_prompt=system_prompt(AgentRole.WRITER, prompt_version),
            user_payload=ContextBundle.build(BLOCK_INSTRUCTIONS, sections).render(),
            schema=BlockWriterResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        BlockWriterResponse,
    )

    expected = {issue.issue_id for issue in issues}
    returned = [item.issue_id for item in response.results]
    if len(returned) != len(set(returned)) or set(returned) != expected:
        raise WriterBatchIncomplete(
            f"expected exactly {sorted(expected)}, received {sorted(returned)}"
        )
    by_id = {item.issue_id: item.proposal for item in response.results}
    results = []
    for issue in issues:
        attempt_no = attempt_numbers[issue.issue_id]
        proposal = by_id[issue.issue_id]
        private = WriterPrivate(
            reasoning=proposal.reasoning,
            derivation=proposal.derivation,
            confidence=proposal.confidence,
        )
        if taint is not None:
            taint.register_model(f"writer.{issue.issue_id}.{attempt_no}", private)
        try:
            patch = _to_patch(proposal, issue=issue, attempt_no=attempt_no)
        except (WriterProposedNothing, ValidationError) as error:
            # A coordinated call contains independently reviewable proposals. One bad
            # proposal must spend only its own attempt; letting it escape here crashes
            # the job and throws away valid sibling proposals from the same paid call.
            results.append(
                WriterResult(
                    patch=None,
                    private=private,
                    rejection=_invalid_patch_rejection(error),
                )
            )
        else:
            results.append(WriterResult(patch=patch, private=private))
    return tuple(results)


def propose_patch(
    client: LLMClient,
    *,
    issue: Issue,
    block: ProblemBlock,
    conventions,
    attempt_no: int,
    deterministic_findings: Sequence[ValidationFinding] = (),
    reviewer_feedback: str = "",
    curator_rules: Sequence[str] = (),
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    #: The prompt version this job is pinned to. `None` means "whatever is newest", which
    #: is right for a call made outside a job and wrong for one made inside it.
    prompt_version: int | None = None,
) -> WriterResult:
    sections = [
        DataSection("The issue to resolve", render_issue(issue)),
        DataSection("The problem block", render_block(block)),
        DataSection("Conventions this workbook follows", render_conventions(conventions)),
        DataSection(
            "Deterministic findings for this block", render_findings(deterministic_findings)
        ),
    ]
    if curator_rules:
        # Policy the curator supplied, applied rather than verified. Distinct from the
        # errata claims, which are hypotheses and go only to the auditor.
        sections.append(
            DataSection(
                "Curation rules the curator supplied",
                "\n".join(f"- {rule}" for rule in curator_rules),
            )
        )
    if reviewer_feedback:
        # Reviewer feedback and deterministic gate rejections are both public by design.
        # They are the only information that travels from a failed attempt back to the
        # Writer, and have to be actionable rather than making it repeat the same patch.
        sections.append(
            DataSection("Feedback on your previous attempt", reviewer_feedback)
        )

    bundle = ContextBundle.build(INSTRUCTIONS, sections)
    payload = bundle.render()

    response = call_structured(
        client,
        LLMRequest(
            role=AgentRole.WRITER,
            system_prompt=system_prompt(AgentRole.WRITER, prompt_version),
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

    try:
        patch = _to_patch(response, issue=issue, attempt_no=attempt_no)
    except (WriterProposedNothing, ValidationError) as error:
        return WriterResult(
            patch=None,
            private=private,
            rejection=_invalid_patch_rejection(error),
        )
    return WriterResult(patch=patch, private=private)


def _invalid_patch_rejection(
    error: WriterProposedNothing | ValidationError,
) -> PatchRejection:
    """Turn an unusable proposal into a normal, issue-local gate rejection."""
    message = str(error)
    if "changes nothing" in message or "returned no edits" in message:
        code = RejectionCode.NO_OP
    elif "same cell twice" in message:
        code = RejectionCode.DUPLICATE_CELL_EDIT
    else:
        code = RejectionCode.SCHEMA_INVALID
    return PatchRejection(code=code, message=message)


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
        if edit.before == edit.after:
            raise WriterProposedNothing(
                f"edit at row {edit.row} column {FIXED_COLUMNS[key]} changes nothing"
            )
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
        related_edits_reason=response.related_edits_reason,
        confidence=response.confidence,
    )
