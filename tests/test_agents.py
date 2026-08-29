"""Agent and isolation tests.

The isolation tests assert over **persisted prompts** -- the payload actually recorded on
the request -- not over which object was passed to which function. A guarantee that only
holds in the object graph is not a guarantee, and the deliberate-leak test exists to prove
the guard is live rather than dead code nobody has ever triggered.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from conftest import hint, problem, scaffold, step
from oatutor_council.agents.independent_reviewer import blocks_to_sweep, sweep_block
from oatutor_council.agents.initial_auditor import SeedClaim, audit_block
from oatutor_council.agents.isolation import (
    SHINGLE_SIZE,
    AuditorPrivate,
    ContextIsolationError,
    PrivateModel,
    PrivateText,
    TaintRegistry,
    WriterPrivate,
    assert_no_private_fields,
)
from oatutor_council.agents.known_issue_reviewer import build_context, review
from oatutor_council.agents.rendering import (
    ReviewerContext,
    render_block,
    render_block_diff,
    render_issue,
)
from oatutor_council.agents.schemas import (
    AuditorResponse,
    IndependentReviewResponse,
    ReviewerResponse,
    WriterResponse,
)
from oatutor_council.agents.writer import WriterProposedNothing, propose_patch
from oatutor_council.llm.base import AgentRole
from oatutor_council.llm.mock import ScriptedLLMClient
from oatutor_council.models import (
    ColumnKey,
    Issue,
    IssueCategory,
    IssueSource,
    ReviewDecision,
    Severity,
)
from oatutor_council.reporting.ledger import issue_from_finding
from oatutor_council.workbook.reader import read_workbook


@pytest.fixture
def parsed(make_workbook):
    return read_workbook(
        make_workbook(
            [
                problem("angles1", title="Convert 30 degrees", oer_src="s", license="CC"),
                step("angles1", answer="pi/6", answer_type="algebra"),
                hint("angles1", "h1", body="multiply by pi/180"),
                problem("angles2", title="Second", oer_src="s", license="CC"),
                step("angles2", answer="1/2", answer_type="numeric"),
            ]
        )
    )


@pytest.fixture
def block(parsed):
    return parsed.blocks[0]


class NestedPrivate(PrivateModel):
    """Module-level so its annotation resolves. See the test that uses it."""

    inner: str = ""


def make_issue(**kwargs) -> Issue:
    defaults = dict(
        issue_id="issue-1",
        job_id="job-1",
        block_id="block-0000",
        problem_name="angles1",
        source=IssueSource.INITIAL_AUDITOR,
        category=IssueCategory.MATHEMATICS,
        severity=Severity.ERROR,
        title="answer is wrong",
        description="the answer should be pi/6 but reads pi/3",
        cells=((3, 5),),
    )
    return Issue(**{**defaults, **kwargs})


# --------------------------------------------------------------------------------------
# PrivateText
# --------------------------------------------------------------------------------------


def test_private_text_does_not_leak_through_interpolation():
    """It deliberately does not subclass `str`: a subclass interpolates silently and the
    leak is invisible at the call site."""
    secret = PrivateText("because the identity requires the half-angle formula")
    assert "half-angle" not in f"{secret}"
    assert "half-angle" not in str(secret)
    assert "half-angle" not in repr(secret)
    assert not isinstance(secret, str)
    assert secret.reveal().startswith("because")


# --------------------------------------------------------------------------------------
# Structural check
# --------------------------------------------------------------------------------------


def test_the_reviewer_context_cannot_reference_private_reasoning():
    """Enforced at import time, so a field added later that reintroduces the leak fails
    to import rather than shipping."""
    assert assert_no_private_fields(ReviewerContext) is ReviewerContext


def test_a_context_with_a_private_field_is_refused():
    @dataclass(frozen=True)
    class Leaky:
        block: str
        rationale: WriterPrivate

    with pytest.raises(ContextIsolationError, match="WriterPrivate"):
        assert_no_private_fields(Leaky)


def test_a_private_type_nested_inside_a_container_is_still_caught():
    """A field typed `list[WriterPrivate]` leaks exactly as thoroughly as a bare one."""

    @dataclass(frozen=True)
    class Leaky:
        history: list[WriterPrivate]

    with pytest.raises(ContextIsolationError):
        assert_no_private_fields(Leaky)


def test_a_private_type_reached_through_another_model_is_caught():
    """The check walks the whole closure, not just the top level.

    Defined at module scope on purpose: under `from __future__ import annotations` a
    locally-defined type cannot be resolved back from its string annotation, and this
    test is about the resolved path that production code actually takes."""

    @dataclass(frozen=True)
    class Leaky:
        wrapper: NestedPrivate

    with pytest.raises(ContextIsolationError):
        assert_no_private_fields(Leaky)


# --------------------------------------------------------------------------------------
# Taint registry
# --------------------------------------------------------------------------------------


RATIONALE = (
    "The answer is wrong because converting thirty degrees to radians requires "
    "multiplying by pi over one hundred and eighty, which gives pi over six rather "
    "than the pi over three currently written in the answer column."
)


def test_an_exact_copy_of_private_text_is_caught():
    registry = TaintRegistry()
    registry.register("writer.issue-1", RATIONALE)
    with pytest.raises(ContextIsolationError, match="private text"):
        registry.assert_clean(f"Please review this. {RATIONALE}", context="reviewer")


def test_a_paraphrase_is_recorded_rather_than_fatal():
    """A shared span is *consistent with* a leak and does not establish one.

    This used to raise. It cannot: two agents reasoning correctly about the same equation
    produce the same sentence about it, and no threshold separates that from a paraphrase
    of a rationale. Treating it as proof killed two held-out jobs after 42 and 45 paid
    model calls and never caught a real leak, so the finding is surfaced for a human and
    the dispatch proceeds.
    """
    registry = TaintRegistry()
    registry.register("writer.issue-1", RATIONALE)
    paraphrase = (
        "Some preamble. converting thirty degrees to radians requires multiplying by "
        "pi over one hundred and eighty, which gives pi over six. Some trailing text."
    )

    registry.assert_clean(paraphrase, context="reviewer")

    assert [s.label for s in registry.suspicions] == ["writer.issue-1"]
    assert "not proof of a leak" in registry.suspicions[0].describe()


def test_two_agents_describing_one_defect_is_not_a_leak(parsed, block):
    """The exact false positive that killed a held-out run, through the real call path.

    The Initial Auditor privately noted that a body "says subtract 8 from both sides to
    obtain y=21". Its *public* finding says the same thing, because that is what the
    sentence describing that defect is. The finding travels to a reviewer by design, so
    the registry was matching the auditor against its own output and calling the agreement
    a breach -- after 42 paid model calls.
    """
    shared = (
        "the body says subtract 8 from both sides to obtain y equals 21 which is wrong "
        "because the equation requires adding 8 to both sides instead"
    )
    registry = TaintRegistry()
    registry.register("auditor.block-0000.reasoning", shared)

    issue = make_issue(description=shared)
    context = build_context(
        issue=issue,
        original_block=block,
        current_block=block,
        conventions=parsed.conventions,
    )
    assert shared in context.issue_summary  # the public finding really is in the payload

    client = ScriptedLLMClient(default=ReviewerResponse(decision="accept"))
    verdict = review(client, issue=issue, context=context, attempt_no=1, taint=registry)

    assert verdict.decision is ReviewDecision.ACCEPT
    assert client.call_count() == 1  # the job survived and the reviewer actually ran


def test_the_auditor_exemption_does_not_cover_the_writer(parsed, block):
    """The exemption is keyed to the agent that authored the public text, so a Writer
    rationale pasted into an issue description cannot declare itself public ground."""
    registry = TaintRegistry()
    registry.register("writer.issue-1.1", RATIONALE)

    issue = make_issue(description=RATIONALE)
    context = build_context(
        issue=issue,
        original_block=block,
        current_block=block,
        conventions=parsed.conventions,
    )

    client = ScriptedLLMClient(default=ReviewerResponse(decision="accept"))
    with pytest.raises(ContextIsolationError):
        review(client, issue=issue, context=context, attempt_no=1, taint=registry)
    assert client.call_count() == 0


def test_ordinary_shared_vocabulary_is_not_a_violation():
    """A problem name, a rule code and a repeated cell value all appear in both payloads
    legitimately. A check that fired on those would be unusable."""
    registry = TaintRegistry()
    registry.register("writer.issue-1", RATIONALE)
    registry.assert_clean(
        "Block angles1, row 3, column answer, rule MC_ANSWER_NOT_IN_CHOICES, value pi/6",
        context="reviewer",
    )


def test_short_private_text_falls_back_to_exact_containment():
    """Text shorter than a shingle produces none, so exact matching is the only check
    available -- and is sufficient, since there is little to paraphrase."""
    registry = TaintRegistry()
    registry.register("writer.issue-1", "use the half angle formula")
    registry.assert_clean("a completely unrelated payload", context="reviewer")
    with pytest.raises(ContextIsolationError):
        registry.assert_clean("I would use the half angle formula here", context="reviewer")


def test_an_empty_registry_permits_everything():
    TaintRegistry().assert_clean(RATIONALE, context="reviewer")


# -- public ground ---------------------------------------------------------------------
#
# The live failure these cover: a job whose first repair was correct was killed before its
# reviewer ran, because the Writer's derivation quoted the cells it was reasoning about and
# the reviewer was shown those same cells. Twelve tokens of shared mathematics is what a
# derivation and a block rendering *always* have in common.


#: A derivation that is almost entirely a quotation of the block it reasons about.
QUOTING_DERIVATION = (
    "The choices are 1/2, 3/4, 0.75, 2/3 and the answer column reads 3/4, so the answer "
    "3/4 matches the choice 3/4 exactly and the duplicate 0.75 is the cell to change."
)

#: What the reviewer is legitimately shown, containing the same mathematics.
PUBLIC_BLOCK = (
    "row 11 | mc | Which fraction equals three quarters? | answer: 3/4 | "
    "mcChoices: 1/2, 3/4, 0.75, 2/3 | the choices are 1/2, 3/4, 0.75, 2/3 and the answer "
    "column reads 3/4, so the answer 3/4 matches the choice 3/4 exactly"
)


def test_mathematics_quoted_from_the_workbook_is_not_a_leak():
    """The regression. Without public ground this raises and the job dies mid-repair."""
    registry = TaintRegistry()
    registry.register("writer.issue-1.1", QUOTING_DERIVATION)
    registry.assert_clean(
        f"Review this block. {PUBLIC_BLOCK}",
        context="known_issue_reviewer",
        public=(PUBLIC_BLOCK,),
    )


def test_public_ground_does_not_excuse_reasoning_that_is_not_in_it():
    """The other half: the exemption must not become a way to pass anything.

    The payload carries the block *and* the Writer's argument about it. The argument
    appears nowhere in the block, so subtracting public ground leaves it exposed — by
    exact containment when it is copied, and by shingle when it is paraphrased.
    """
    registry = TaintRegistry()
    registry.register("writer.issue-1.1", RATIONALE)

    with pytest.raises(ContextIsolationError, match="private text"):
        registry.assert_clean(
            f"Review this block. {PUBLIC_BLOCK} Note from the writer: {RATIONALE}",
            context="known_issue_reviewer",
            public=(PUBLIC_BLOCK,),
        )

    paraphrase = (
        "converting thirty degrees to radians requires multiplying by pi over one "
        "hundred and eighty, which gives pi over six"
    )
    registry.assert_clean(
        f"Review this block. {PUBLIC_BLOCK} {paraphrase}",
        context="known_issue_reviewer",
        public=(PUBLIC_BLOCK,),
    )
    # Recorded, not raised -- but public ground still did its job: the span the suspicion
    # names is the rationale's, not the block's mathematics.
    assert [s.label for s in registry.suspicions] == ["writer.issue-1.1"]


def test_a_short_private_string_that_is_itself_public_is_not_a_leak():
    """The exact-containment path needs the same exemption as the shingle path.

    `mcChoices: 1/2, 3/4, 0.75, 2/3` is a cell value. It reaching a reviewer is the
    reviewer being shown the workbook.
    """
    quoted_cell = "mcChoices: 1/2, 3/4, 0.75, 2/3"
    registry = TaintRegistry()
    registry.register("writer.issue-1.1", quoted_cell)
    registry.assert_clean(
        f"Review this block. {PUBLIC_BLOCK}",
        context="known_issue_reviewer",
        public=(PUBLIC_BLOCK,),
    )
    # Same string, no public ground offered: the old behaviour is intact.
    with pytest.raises(ContextIsolationError):
        registry.assert_clean(
            f"Review this block. {quoted_cell}", context="known_issue_reviewer"
        )


def test_public_ground_is_not_the_payload():
    """A caller that passed the outgoing payload as its own public ground would delete the
    check while leaving it apparently in place. Nothing in the package does that, and this
    records what it would cost: a leak that sails through.

    The assertion is deliberately the *broken* behaviour, so that if someone ever wires a
    call site up this way the reason it is wrong is written down next to it.
    """
    registry = TaintRegistry()
    registry.register("writer.issue-1.1", RATIONALE)
    leaking_payload = f"Review this block. {PUBLIC_BLOCK} {RATIONALE}"

    registry.assert_clean(
        leaking_payload, context="reviewer", public=(leaking_payload,)
    )  # passes, and must never be how a call site is written

    with pytest.raises(ContextIsolationError):
        registry.assert_clean(
            leaking_payload, context="reviewer", public=(PUBLIC_BLOCK,)
        )


# --------------------------------------------------------------------------------------
# Initial Auditor
# --------------------------------------------------------------------------------------


def test_the_auditor_audits_a_block_with_no_document_at_all(parsed, block):
    """The document seeds the auditor; it never replaces inspection. This is what makes
    the system autonomous rather than a document processor."""
    client = ScriptedLLMClient(
        default=AuditorResponse(
            reasoning="private",
            findings=[
                {
                    "cells": [{"row": 3, "column": "answer"}],
                    "problem": "the answer should be pi/6",
                    "severity": "error",
                    "category": "mathematics",
                }
            ],
        )
    )
    result = audit_block(client, block=block, conventions=parsed.conventions)
    assert len(result.findings) == 1
    assert result.findings[0].row == 3
    assert result.findings[0].column_key is ColumnKey.ANSWER


def test_an_auditor_finding_keeps_every_coordinated_target_cell(parsed, block):
    """The repair gate must receive the whole target set, not only the first column."""
    client = ScriptedLLMClient(
        default=AuditorResponse(
            findings=[
                {
                    "cells": [
                        {"row": 3, "column": "answer"},
                        {"row": 3, "column": "answer_type"},
                    ],
                    "problem": "both the result and its grading type are wrong",
                    "category": "row_type",
                }
            ]
        )
    )
    finding = audit_block(
        client, block=block, conventions=parsed.conventions
    ).findings[0]
    issue = issue_from_finding(
        finding, job_id="job-1", source=IssueSource.INITIAL_AUDITOR
    )

    assert issue.cells == ((3, 5), (3, 6))
    assert issue.is_structural


def test_a_multirow_finding_keeps_exact_pairs_not_a_cartesian_product(parsed, block):
    client = ScriptedLLMClient(
        default=AuditorResponse(
            findings=[
                {
                    "cells": [
                        {"row": 3, "column": "answer"},
                        {"row": 4, "column": "body_text"},
                    ],
                    "problem": "the answer and its explanatory hint disagree",
                }
            ]
        )
    )
    finding = audit_block(
        client, block=block, conventions=parsed.conventions
    ).findings[0]
    issue = issue_from_finding(
        finding, job_id="job-1", source=IssueSource.INITIAL_AUDITOR
    )

    assert issue.cells == ((3, 5), (4, 4))
    assert (3, 4) not in issue.cells
    assert (4, 5) not in issue.cells


def test_zero_findings_is_a_valid_answer(parsed, block):
    """Inventing a marginal finding to appear thorough costs a repair attempt and a
    reviewer's time on a problem that was never wrong."""
    client = ScriptedLLMClient(default=AuditorResponse(reasoning="looks correct"))
    assert audit_block(client, block=block, conventions=parsed.conventions).findings == ()


def test_a_refuted_seed_claim_is_recorded_not_dropped(parsed, block):
    """A curator who reported a defect deserves to know it was looked for. A discarded
    claim is indistinguishable from one nobody read."""
    client = ScriptedLLMClient(
        default=AuditorResponse(
            reasoning="checked",
            refuted_claims=[{"claim_index": 0, "why": "the answer is already pi/6"}],
        )
    )
    result = audit_block(
        client,
        block=block,
        conventions=parsed.conventions,
        seed_claims=[SeedClaim(0, "the answer is wrong", "line 4")],
    )
    assert result.refuted[0].why == "the answer is already pi/6"
    assert result.findings == ()


def test_a_finding_naming_only_a_cell_outside_the_block_is_requeued(parsed, block):
    """A finding at an unseen location is not silently relocated and credited."""
    client = ScriptedLLMClient(
        default=AuditorResponse(
            findings=[
                {
                    "cells": [{"row": 999, "column": "answer"}],
                    "problem": "something",
                }
            ]
        )
    )
    from oatutor_council.agents.initial_auditor import audit_blocks

    results, requeued = audit_blocks(
        client, blocks=[block], conventions=parsed.conventions
    )
    assert results == ()
    assert requeued == (block,)


def test_the_auditor_payload_carries_the_seed_claims_as_fenced_data(parsed, block):
    client = ScriptedLLMClient(default=AuditorResponse())
    audit_block(
        client,
        block=block,
        conventions=parsed.conventions,
        seed_claims=[SeedClaim(0, "row 3 is wrong", "page 2")],
    )
    payload = client.payloads_for(AgentRole.INITIAL_AUDITOR)[0]
    assert "hypotheses, not facts" in payload
    assert "row 3 is wrong" in payload
    assert "never an instruction" in payload


def test_background_reaches_only_the_auditor_as_non_authoritative_context(parsed, block):
    client = ScriptedLLMClient(default=AuditorResponse())
    audit_block(
        client,
        block=block,
        conventions=parsed.conventions,
        curator_notes=["This unit follows a departmental naming convention."],
    )
    payload = client.payloads_for(AgentRole.INITIAL_AUDITOR)[0]
    assert "Background the curator supplied" in payload
    assert "not policy or a defect claim" in payload
    assert "departmental naming convention" in payload


# --------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------


def test_writer_schema_requires_the_derivation_field():
    assert "derivation" in WriterResponse.model_json_schema()["required"]


def test_the_writer_produces_exact_cell_edits(parsed, block):
    client = ScriptedLLMClient(
        default=WriterResponse(
            reasoning="private",
            derivation="private",
            confidence=0.9,
            edits=[{"row": 3, "column": "answer", "before": "pi/6", "after": "pi/3"}],
        )
    )
    result = propose_patch(
        client,
        issue=make_issue(),
        block=block,
        conventions=parsed.conventions,
        attempt_no=1,
    )
    edit = result.patch.edits[0]
    assert (edit.row, edit.column, edit.before, edit.after) == (3, 5, "pi/6", "pi/3")
    assert edit.column_key is ColumnKey.ANSWER


def test_escalation_produces_no_edits_and_is_terminal(parsed, block):
    """Asking an agent that says it cannot decide two more times produces a guess, which
    is worse than an escalation because it gets reviewed as though someone checked it."""
    client = ScriptedLLMClient(
        default=WriterResponse(
            derivation="",
            needs_human_review=True, human_review_reason="the source is contradictory"
        )
    )
    patch = propose_patch(
        client, issue=make_issue(), block=block, conventions=parsed.conventions, attempt_no=1
    ).patch
    assert patch.needs_human_review
    assert patch.edits == ()


def test_neither_edits_nor_escalation_is_refused(parsed, block):
    """A patch with no edits and no escalation is not an answer to the question asked."""
    client = ScriptedLLMClient(default=WriterResponse(derivation=""))
    with pytest.raises(WriterProposedNothing):
        propose_patch(
            client, issue=make_issue(), block=block, conventions=parsed.conventions,
            attempt_no=1,
        )


def test_reviewer_feedback_reaches_the_writer(parsed, block):
    """Feedback is public by design: it is the only thing that travels back, so it has to
    be actionable."""
    client = ScriptedLLMClient(
        default=WriterResponse(
            derivation="a stated verification for the mathematical edit",
            edits=[{"row": 3, "column": "answer", "before": "pi/6", "after": "pi/3"}]
        )
    )
    propose_patch(
        client,
        issue=make_issue(),
        block=block,
        conventions=parsed.conventions,
        attempt_no=2,
        reviewer_feedback="the exponent in row 3 is still wrong",
    )
    assert "still wrong" in client.payloads_for(AgentRole.WRITER)[0]


def test_the_writer_rationale_is_registered_as_private(parsed, block):
    client = ScriptedLLMClient(
        default=WriterResponse(
            reasoning=RATIONALE,
            derivation="a stated verification for the mathematical edit",
            edits=[{"row": 3, "column": "answer", "before": "pi/6", "after": "pi/3"}],
        )
    )
    registry = TaintRegistry()
    propose_patch(
        client,
        issue=make_issue(),
        block=block,
        conventions=parsed.conventions,
        attempt_no=1,
        taint=registry,
    )
    assert any("writer.issue-1" in label for label in registry.entries)


# --------------------------------------------------------------------------------------
# Reviewers
# --------------------------------------------------------------------------------------


def test_the_reviewer_payload_never_contains_the_writers_rationale(parsed, block):
    """The central isolation assertion, made against the bytes that were sent."""
    writer_client = ScriptedLLMClient(
        default=WriterResponse(
            reasoning=RATIONALE,
            derivation="pi/180 times 30",
            edits=[{"row": 3, "column": "answer", "before": "pi/6", "after": "pi/3"}],
        )
    )
    registry = TaintRegistry()
    issue = make_issue()
    propose_patch(
        writer_client,
        issue=issue,
        block=block,
        conventions=parsed.conventions,
        attempt_no=1,
        taint=registry,
    )

    reviewer_client = ScriptedLLMClient(
        default=ReviewerResponse(decision="accept")
    )
    review(
        reviewer_client,
        issue=issue,
        context=build_context(
            issue=issue,
            original_block=block,
            current_block=block,
            conventions=parsed.conventions,
        ),
        attempt_no=1,
        taint=registry,
    )

    payload = reviewer_client.payloads_for(AgentRole.KNOWN_ISSUE_REVIEWER)[0]
    assert "pi/180 times 30" not in payload
    assert "half-angle" not in payload
    for phrase in ("requires multiplying by", "currently written in the answer column"):
        assert phrase not in payload


def test_a_deliberate_leak_fails_the_job_rather_than_warning(parsed, block):
    """Proves the guard is live rather than dead code nobody has triggered. A retry would
    send the same tainted payload again; a warning would let a leaked review count."""
    registry = TaintRegistry()
    registry.register("writer.issue-1.1", RATIONALE)

    issue = make_issue()
    context = build_context(
        issue=issue,
        original_block=block,
        current_block=block,
        conventions=parsed.conventions,
    )
    leaked = ReviewerContext(
        issue_summary=context.issue_summary + "\n\nWriter's reasoning: " + RATIONALE,
        original_block=context.original_block,
        current_block=context.current_block,
        block_diff=context.block_diff,
        conventions=context.conventions,
        deterministic_findings=context.deterministic_findings,
    )

    client = ScriptedLLMClient(default=ReviewerResponse(decision="accept"))
    with pytest.raises(ContextIsolationError):
        review(client, issue=issue, context=leaked, attempt_no=1, taint=registry)
    assert client.call_count() == 0  # nothing was dispatched


def test_the_reviewer_sees_the_whole_block_diff(parsed, make_workbook):
    """An edit can be correct in isolation and still break a sibling row. A reviewer
    shown only the changed cell cannot see that."""
    original = parsed.blocks[0]
    edited = read_workbook(
        make_workbook(
            [
                problem("angles1", title="Convert 30 degrees", oer_src="s", license="CC"),
                step("angles1", answer="pi/3", answer_type="algebra"),
                hint("angles1", "h1", body="multiply by pi/180"),
            ]
        )
    ).blocks[0]

    diff = render_block_diff(original, edited)
    assert "answer: 'pi/6' -> 'pi/3'" in diff


def test_a_revise_verdict_without_feedback_is_made_explicit(parsed, block):
    """A reviewer that rejects without saying why gives the Writer nothing to act on."""
    client = ScriptedLLMClient(default=ReviewerResponse(decision="revise", feedback=""))
    issue = make_issue()
    verdict = review(
        client,
        issue=issue,
        context=build_context(
            issue=issue,
            original_block=block,
            current_block=block,
            conventions=parsed.conventions,
        ),
        attempt_no=1,
    )
    assert verdict.decision is ReviewDecision.REVISE
    assert "without stating what is wrong" in verdict.feedback


# --------------------------------------------------------------------------------------
# Independent reviewer
# --------------------------------------------------------------------------------------


def test_the_sweep_reports_findings_on_an_unflagged_block(parsed, block):
    client = ScriptedLLMClient(
        default=IndependentReviewResponse(
            block_is_sound=False,
            findings=[
                {
                    "cells": [{"row": 3, "column": "answer"}],
                    "problem": "the answer is wrong",
                }
            ],
        )
    )
    result = sweep_block(client, block=block, conventions=parsed.conventions)
    assert not result.block_is_sound
    assert result.findings[0].code == "INDEPENDENT_FINDING"


def test_a_sound_block_produces_nothing(parsed, block):
    client = ScriptedLLMClient(default=IndependentReviewResponse(block_is_sound=True))
    result = sweep_block(client, block=block, conventions=parsed.conventions)
    assert result.block_is_sound and result.findings == ()


def test_findings_win_over_a_contradictory_soundness_claim(parsed, block):
    """The findings are the concrete claim; `block_is_sound` is a summary of them."""
    client = ScriptedLLMClient(
        default=IndependentReviewResponse(
            block_is_sound=True,
            findings=[
                {"cells": [{"row": 3, "column": "answer"}], "problem": "wrong"}
            ],
        )
    )
    assert not sweep_block(client, block=block, conventions=parsed.conventions).block_is_sound


def test_the_sweep_covers_blocks_whose_only_claim_was_refuted(parsed):
    """It also rechecks blocks with ledger entries; one finding is not exhaustive."""
    swept = blocks_to_sweep(parsed.blocks, frozenset({"block-0000"}))
    assert [b.block_id for b in swept] == ["block-0000", "block-0001"]


def test_the_sweep_prompt_differs_from_the_known_issue_prompt(parsed, block):
    """A different task deserves a different prompt: there is no claim to verify here."""
    from oatutor_council.llm.prompts import system_prompt

    assert system_prompt(AgentRole.INDEPENDENT_REVIEWER) != system_prompt(
        AgentRole.KNOWN_ISSUE_REVIEWER
    )
    assert "every current problem block" in system_prompt(
        AgentRole.INDEPENDENT_REVIEWER
    )


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def test_the_block_is_rendered_with_real_spreadsheet_rows(block):
    """A model given positions within the block will confidently name the wrong
    spreadsheet row in its patch."""
    rendered = render_block(block)
    assert rendered.splitlines()[1].startswith("2 | angles1")
    assert "3 | angles1" in rendered


def test_the_block_renders_every_agent_addressable_fixed_column(block):
    header = render_block(block).splitlines()[0]
    for column in (
        "images",
        "parent",
        "oer_src",
        "openstax_kc",
        "kc",
        "taxonomy",
        "license",
    ):
        assert column in header


def test_the_issue_rendering_carries_no_rationale(block):
    issue = make_issue()
    rendered = render_issue(issue)
    assert "confidence" not in rendered
    assert "derivation" not in rendered
    assert issue.description in rendered
