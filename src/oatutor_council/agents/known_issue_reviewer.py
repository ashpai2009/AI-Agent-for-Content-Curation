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
from typing import Mapping, Sequence
from uuid import uuid4

from ..llm.base import AgentRole, LLMClient, LLMRequest, MalformedResponse, call_structured
from ..llm.context import ContextBundle, DataSection
from ..llm.prompts import system_prompt
from ..models import (
    Issue,
    IssueSource,
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
from .schemas import BlockReviewerResponse, ReviewerResponse

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

def origin_private_label(issue: Issue) -> str | None:
    """The one private record whose author also wrote this issue's public finding.

    An issue's summary is a *published* finding. The agent that published it also kept a
    private note about the same block, in the same call, about the same defect -- so those
    two texts share sentences by construction, and matching one against the other is an
    agent being compared with itself rather than a leak being detected.

    The link has to be exact. Keying on the prefix `"auditor."` exempted every block's
    auditor reasoning for every issue, including issues a different agent raised about a
    different block, which is a far wider hole than the one it was closing.

    `None` means no exemption at all, which is the right answer for every other source:
    the Independent Reviewer registers no private text (its findings are published
    directly), and instruction-document and final-validation findings are curator text and
    rule-engine output, neither of which is agent reasoning.
    """
    if issue.source is IssueSource.INITIAL_AUDITOR and issue.block_id:
        # Mirrors `initial_auditor`, which registers `auditor.{block_id}` and lets
        # `register_model` append the field name.
        return f"auditor.{issue.block_id}"
    return None


_DECISIONS = {
    "accept": ReviewDecision.ACCEPT,
    "revise": ReviewDecision.REVISE,
    "human_review": ReviewDecision.HUMAN_REVIEW,
}


BLOCK_INSTRUCTIONS = """\
Review the corrected problem block once as a whole. Several candidate repairs were
proposed together and the simulated block includes all of them.

Return exactly one result for every supplied issue_id and no others. For each issue,
decide `accept`, `revise`, or `human_review`. Verify the mathematics and instructions
yourself from the visible workbook content. Consider interactions across all changed
cells, but judge each issue's candidate on whether the resulting block resolves that
issue without creating another defect. For `revise`, name the cell and replacement the
Writer should produce next. You are not shown and must not infer the Writer's reasoning.
"""


def review_patches(
    client: LLMClient,
    *,
    issues: Sequence[Issue],
    original_block: ProblemBlock,
    simulated_block: ProblemBlock,
    conventions,
    candidate_edits: Mapping[str, Sequence],
    deterministic_findings: Sequence[ValidationFinding] = (),
    curator_rules: Sequence[str] = (),
    attempt_numbers: Mapping[str, int],
    job_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    prompt_version: int | None = None,
    role: ReviewerRole = ReviewerRole.KNOWN_ISSUE_REVIEWER,
) -> tuple[ReviewVerdict, ...]:
    """Judge every candidate in one changed-block call, retaining per-issue verdicts."""
    if not issues:
        return ()
    issue_sections = []
    for issue in issues:
        issue_sections.append(
            f"issue_id: {issue.issue_id}\n"
            + render_issue(issue)
            + "\nCandidate edits for this issue:\n"
            + render_candidate_edits(candidate_edits.get(issue.issue_id, ()))
        )
    public_sections = [
        DataSection("Issues and candidate edits", "\n\n".join(issue_sections)),
        DataSection("Original problem block", render_block(original_block)),
        DataSection("Simulated corrected problem block", render_block(simulated_block)),
        DataSection("Whole block diff", render_block_diff(original_block, simulated_block)),
        DataSection("Conventions", render_conventions(conventions)),
        DataSection("Deterministic findings", render_findings(deterministic_findings)),
    ]
    if curator_rules:
        public_sections.append(
            DataSection(
                "Curation rules the curator supplied",
                "\n".join(f"- {rule}" for rule in curator_rules),
            )
        )
    payload = ContextBundle.build(BLOCK_INSTRUCTIONS, public_sections).render()
    if taint is not None:
        public_for_lists: dict[str, list[str]] = {}
        for issue in issues:
            if label := origin_private_label(issue):
                public_for_lists.setdefault(label, []).append(render_issue(issue))
        public_for = {
            label: tuple(summaries) for label, summaries in public_for_lists.items()
        }
        taint.assert_clean(
            payload,
            context=role.value,
            public=tuple(section.content for section in public_sections[1:]),
            public_for=public_for or None,
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
            schema=BlockReviewerResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
        ),
        BlockReviewerResponse,
    )
    expected = {issue.issue_id for issue in issues}
    returned = [item.issue_id for item in response.results]
    if len(returned) != len(set(returned)) or set(returned) != expected:
        raise MalformedResponse(
            f"block reviewer expected exactly {sorted(expected)}, received {sorted(returned)}"
        )
    by_id = {item.issue_id: item for item in response.results}
    verdicts = []
    for issue in issues:
        item = by_id[issue.issue_id]
        decision = _DECISIONS[item.decision]
        feedback = item.feedback
        if decision is not ReviewDecision.ACCEPT and not feedback.strip():
            feedback = f"The reviewer returned {item.decision} without stating what is wrong."
        verdicts.append(
            ReviewVerdict(
                verdict_id=uuid4().hex,
                issue_id=issue.issue_id,
                reviewer_role=role,
                attempt_no=attempt_numbers[issue.issue_id],
                decision=decision,
                feedback=feedback,
                rule_codes=tuple(item.rule_codes),
            )
        )
    return tuple(verdicts)


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
            # The issue summary is public ground for exactly one private record: the note
            # kept by the agent that published this finding, about this block. Not for the
            # Writer's rationale, and not for another block's auditor note.
            public_for=(
                {origin: (context.issue_summary,)}
                if (origin := origin_private_label(issue))
                else None
            ),
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
