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
    render_conventions,
    render_findings,
    render_issue,
)
from .schemas import ReviewerResponse

INSTRUCTIONS = """\
Decide whether the issue below has been resolved by the change made to this block.

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
) -> ReviewerContext:
    """Assemble exactly what a reviewer may see.

    Note what is not a parameter: the patch, its rationale, its confidence, and the
    Writer's derivation. There is nowhere to put them.
    """
    return ReviewerContext(
        issue_summary=render_issue(issue),
        original_block=render_block(original_block),
        current_block=render_block(current_block),
        block_diff=render_block_diff(original_block, current_block),
        conventions=render_conventions(conventions),
        deterministic_findings=render_findings(deterministic_findings),
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
    role: ReviewerRole = ReviewerRole.KNOWN_ISSUE_REVIEWER,
) -> ReviewVerdict:
    bundle = ContextBundle.build(INSTRUCTIONS, context.sections())
    payload = bundle.render()

    if taint is not None:
        # The load-bearing check. A violation raises and the job fails; it is never
        # downgraded to a warning, because a leaked review still counts as a review.
        taint.assert_clean(payload, context=role.value)

    agent_role = (
        AgentRole.KNOWN_ISSUE_REVIEWER
        if role is ReviewerRole.KNOWN_ISSUE_REVIEWER
        else AgentRole.INDEPENDENT_REVIEWER
    )
    response = call_structured(
        client,
        LLMRequest(
            role=agent_role,
            system_prompt=system_prompt(agent_role),
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
