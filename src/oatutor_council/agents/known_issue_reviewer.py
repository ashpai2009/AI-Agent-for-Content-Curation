"""The Known-Issue Reviewer. Decides whether a correction fixed the issue it was made for.

Fresh context, and structurally so: the payload is built entirely from a `ReviewerContext`,
a type whose field closure cannot reference the Writer's reasoning. The taint registry
checks the rendered bytes on top of that, because types catch the leak someone declared
and the registry catches the one someone pasted.

The reviewer sees the **whole block diff**, source to current, not just this issue's edits.
An edit can be correct in isolation and still break a sibling row -- a repaired answer that
no longer matches its choice list looks perfect on its own line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from ..llm.base import AgentRole, LLMClient, LLMRequest, call_structured
from ..llm.context import ContextBundle
from ..llm.prompts import system_prompt
from ..models import (
    Issue,
    ProblemBlock,
    ReviewDecision,
    ReviewerRole,
    ReviewVerdict,
    ValidationFinding,
)
from .isolation import TaintRegistry
from .rendering import (
    ReviewerContext,
    render_block,
    render_block_diff,
    render_candidate_edits,
    render_conventions,
    render_findings,
    render_issue,
)
from .schemas import ReviewerResponse

INSTRUCTIONS = """\
Decide whether the issue below is resolved by the current artifact shown.

If a candidate-edits section is present, the candidate has not been written: `accept`
authorises those exact edits, while `revise` or `human_review` leaves the workbook
unchanged. If that section is absent, this may be an unbiased check before any Writer
attempt. `accept` then means the current artifact is already correct and the claim should
be refuted (or was resolved by a visible sibling repair); `revise` means the defect is
genuinely present and your feedback must tell the Writer what to correct.

Verify the mathematics yourself. Check the whole block, not only the changed cells: an
edit can be right on its own line and still break a row that depends on it.

Return `accept`, `revise`, or `human_review`. For `revise`, name the cell and the content
that would resolve it — your feedback is the only thing the Writer receives.
"""

_DECISIONS = {
    "accept": ReviewDecision.ACCEPT,
    "revise": ReviewDecision.REVISE,
    "human_review": ReviewDecision.HUMAN_REVIEW,
}


def build_context(
    *,
    issue: Issue,
    original_block: ProblemBlock,
    current_block: ProblemBlock,
    conventions,
    deterministic_findings: Sequence[ValidationFinding] = (),
    curator_rules: Sequence[str] = (),
    candidate_edits=(),
) -> ReviewerContext:
    """Assemble exactly what a reviewer may see.

    Only the public patch artefact (cell locations and before/after values) may be passed.
    Its rationale, confidence and derivation remain structurally unavailable.
    """
    return ReviewerContext(
        issue_summary=render_issue(issue),
        original_block=render_block(original_block),
        current_block=render_block(current_block),
        block_diff=render_block_diff(original_block, current_block),
        conventions=render_conventions(conventions),
        deterministic_findings=render_findings(deterministic_findings),
        candidate_edits=render_candidate_edits(candidate_edits),
        curator_rules="\n".join(f"- {rule}" for rule in curator_rules),
    )


def review(
    client: LLMClient,
    *,
    issue: Issue,
    context: ReviewerContext,
    attempt_no: int,
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    #: The prompt version this job is pinned to. `None` means "whatever is newest", which
    #: is right for a call made outside a job and wrong for one made inside it.
    prompt_version: int | None = None,
    role: ReviewerRole = ReviewerRole.KNOWN_ISSUE_REVIEWER,
) -> ReviewVerdict:
    bundle = ContextBundle.build(INSTRUCTIONS, context.sections())
    payload = bundle.render()

    if taint is not None:
        # A backstop against an exact copy of private text, not the boundary itself --
        # that is `ReviewerContext`, which cannot name a private type. An exact copy still
        # fails the job; a merely-similar span is recorded for a human.
        #
        # Public ground is everything in this payload whose provenance is public: the
        # workbook, the rule engine, the curator's rules, the public patch artefact, and
        # the auditor's public finding. A Writer's derivation quotes the cells it reasons
        # about because that is what reasoning about a cell looks like, and the reviewer is
        # shown the same cells because that is what it is judging.
        taint.assert_clean(
            payload,
            context=role.value,
            public=(
                context.original_block,
                context.current_block,
                context.block_diff,
                context.conventions,
                context.deterministic_findings,
                context.candidate_edits,
                context.curator_rules,
            ),
            # The issue summary is the auditor's own *public* finding -- the channel by
            # which a defect is meant to reach a reviewer. So it is public ground for the
            # auditor's private notes, which describe the same defect in the same sentence,
            # and **not** for the Writer's rationale, which has no business being there.
            # Blanket-exempting it would let a rationale smuggled into a description
            # declare itself exempt, which is the leak this check exists for.
            public_for={"auditor.": (context.issue_summary,)},
        )

    agent_role = (
        AgentRole.KNOWN_ISSUE_REVIEWER
        if role is ReviewerRole.KNOWN_ISSUE_REVIEWER
        else AgentRole.INDEPENDENT_REVIEWER
    )
    response = call_structured(
        client,
        LLMRequest(
            role=agent_role,
            system_prompt=system_prompt(agent_role, prompt_version),
            user_payload=payload,
            schema=ReviewerResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
            issue_id=issue.issue_id,
        ),
        ReviewerResponse,
    )

    decision = _DECISIONS[response.decision]
    feedback = response.feedback
    if decision is not ReviewDecision.ACCEPT and not feedback.strip():
        # `ReviewVerdict` refuses to be constructed without a reason, and a reviewer that
        # rejects without saying why gives the Writer nothing to act on. Say so plainly
        # rather than failing validation with a schema error.
        feedback = (
            f"The reviewer returned {response.decision} without stating what is wrong."
        )

    return ReviewVerdict(
        verdict_id=uuid4().hex,
        issue_id=issue.issue_id,
        reviewer_role=role,
        attempt_no=attempt_no,
        decision=decision,
        feedback=feedback,
        rule_codes=tuple(response.rule_codes),
    )
