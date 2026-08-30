"""The Adjudicator. Settles a disagreement between two independent audits.

This is the fifth agent, and it was added because the pipeline had no way to say
"unsettled". A model-only finding was checked by a claim-blind audit in the opposite
role, and anything short of exact agreement -- the same cells, the same category -- was
recorded as `REFUTED`. On two held-out workbooks that discarded three defects the first
audit had correctly found. **"The second agent did not independently rediscover it" and
"the second agent examined it and says the content is right" are different findings**, and
only the second one justifies closing an issue.

That is a lower bar than it sounds, and it is worth being exact about: requiring stated
reasoning establishes that this agent explained itself, **not that its mathematics is
true**. An adjudicator can reason badly and refute a real defect. What the requirement
removes is the specific failure that was measured -- refutation by silence, where nobody
examined the claim at all -- and nothing stronger than that.

So the blind check now classifies rather than decides:

* exact agreement is corroboration and goes straight to the Writer, unchanged;
* a *related* finding -- overlapping cells, or the same row under a compatible category --
  is agreement about the defect and disagreement about its extent, which is a question
  with an answer;
* silence is silence.

The last two arrive here. This agent is the only one in the council that is shown another
agent's conclusion, and that is deliberate: choosing between two readings of one block is
not a thing a blind observer can do. What keeps it from being a rubber stamp is that it
must state the check it ran, and that `undecided` costs it nothing -- an adjudicator with
only two answers available would learn to pick the confident-sounding one.

Nothing here can reach private reasoning. Both claims it sees are published findings, and
`AdjudicationContext` cannot name a private type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.base import AgentRole, LLMClient, LLMRequest, call_structured
from ..llm.context import ContextBundle
from ..llm.prompts import system_prompt
from ..models import FIXED_COLUMNS, IssueCategory
from .isolation import TaintRegistry
from .rendering import AdjudicationContext
from .schemas import AdjudicatorResponse, column_key

INSTRUCTIONS = """\
Two independent audits of the same problem block disagree, and you decide between them.

The first audit says the cells below are defective. The second audit examined the same
block from scratch, without being told what the first one claimed, and reported what you
see in the second section — which may be a related finding, or nothing about those cells
at all.

Work the mathematics yourself before you answer. Solve the question as posed, check the
answer that is actually recorded, check that its form and answerType match what the
question asks for, and check that every choice matches exactly where a choice list
applies. Your `evidence` must be the check you ran, not a restatement of either claim.

Then return one verdict:

- `defect_confirmed` — the content is wrong. List in `cells` **every** cell that must
  change for the repair to be complete, including cells neither audit named: an answer
  and its answerType, or an answer and the choice it must match, are one repair. These
  cells replace the disputed claim's, so anything you leave out will not be corrected.
- `content_correct` — the content is right and the first audit was mistaken. Only use
  this when you can show it. Say what the correct value is and why the recorded value
  already is it.
- `undecided` — you cannot establish either. This is a real answer. A person will look at
  it, which is a better outcome than a confident guess in either direction.
"""

#: A defect in one of these columns cannot be repaired under a non-structural issue, so
#: the adjudicator's canonical category has to carry that decision with it.
_STRUCTURAL_CATEGORIES = frozenset(
    {IssueCategory.STRUCTURE, IssueCategory.ROW_TYPE, IssueCategory.DEPENDENCY}
)


@dataclass(frozen=True)
class Adjudication:
    """The public outcome, already normalised for the council to act on."""

    verdict: str
    evidence: str
    cells: tuple[tuple[int, int], ...]
    category: IssueCategory
    expected: str

    @property
    def is_structural(self) -> bool:
        return self.category in _STRUCTURAL_CATEGORIES


def adjudicate(
    client: LLMClient,
    *,
    context: AdjudicationContext,
    block_rows: Sequence[int],
    job_id: str = "",
    issue_id: str = "",
    seed: int | None = None,
    taint: TaintRegistry | None = None,
    public_for: dict[str, Sequence[str]] | None = None,
    prompt_version: int | None = None,
) -> Adjudication:
    """Ask for a settlement and return it already reduced to what the council needs.

    Column names resolve through `FIXED_COLUMNS`, the same way the auditor's findings do;
    the response schema admits only A-P names, so the lookup cannot miss. `block_rows`
    bounds the answer to the block under dispute: an adjudicator naming a row outside it
    has not adjudicated this disagreement, and relocating the cell would invent a third
    claim nobody made.
    """
    bundle = ContextBundle.build(INSTRUCTIONS, context.sections())
    payload = bundle.render()

    if taint is not None:
        # Every section here is public by provenance: the workbook block, the rule
        # engine's findings, the curator's rules, and two *published* findings. The
        # per-record exemptions the caller supplies cover the one legitimate overlap --
        # an audit's own private note about the same block, in the same call, about the
        # same defect as the finding it published.
        taint.assert_clean(
            payload,
            context=AgentRole.ADJUDICATOR.value,
            public=(
                context.block,
                context.conventions,
                context.deterministic_findings,
                context.curator_rules,
            ),
            public_for=dict(public_for or {}),
        )

    response = call_structured(
        client,
        LLMRequest(
            role=AgentRole.ADJUDICATOR,
            system_prompt=system_prompt(AgentRole.ADJUDICATOR, prompt_version),
            user_payload=payload,
            schema=AdjudicatorResponse.model_json_schema(),
            seed=seed,
            job_id=job_id,
            issue_id=issue_id,
        ),
        AdjudicatorResponse,
    )

    permitted = frozenset(block_rows)
    cells: list[tuple[int, int]] = []
    outside = False
    for cell in response.cells:
        index = FIXED_COLUMNS[column_key(cell.column)]
        if cell.row not in permitted:
            outside = True
            continue
        pair = (cell.row, index)
        if pair not in cells:
            cells.append(pair)

    verdict = response.verdict
    evidence = response.evidence.strip()
    if not evidence:
        # A verdict is only worth as much as the check behind it, and `content_correct`
        # without one is exactly the silence-as-refutation this agent exists to remove.
        verdict = "undecided"
        evidence = "The adjudicator returned a verdict without stating the check it ran."
    elif verdict == "defect_confirmed" and (outside or not cells):
        # A confirmation with no target authorises nothing. A confirmation that reaches
        # outside the disputed block is an answer to a different question, and the whole
        # cell list goes with it rather than being pruned down to the part that fits --
        # the same rule the auditors' findings follow, for the same reason: a target
        # outside the block is evidence the agent was not reading this block. Neither case
        # is a refutation, so neither may close the issue.
        verdict = "undecided"
        evidence = (
            f"{evidence}\n\nThe confirmation "
            + (
                "named a cell outside the disputed block"
                if outside
                else "named no repairable cell"
            )
            + ", so it could not authorise a correction."
        )

    return Adjudication(
        verdict=verdict,
        evidence=evidence,
        cells=() if verdict != "defect_confirmed" else tuple(cells),
        category=response.category,
        expected=response.expected.strip(),
    )
