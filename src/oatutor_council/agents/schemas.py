"""Structured response schemas for the five agents.

Every model call is schema-constrained, and each schema is validated against here rather
than parsed out of prose. The split between public artefact and private reasoning happens
at this boundary: a response model carries both, and the agent module immediately
separates them so that only the artefact travels onward.

Cells are addressed by **column name**, not index. A model that has to count to the ninth
column will eventually miscount, and `mcChoices` is a more reviewable thing to see in a
patch than `9`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..models import ColumnKey, IssueCategory, Severity

#: The columns an agent may name. Presented to the model as an enum in the JSON schema,
#: so an invalid column is a schema violation rather than something to validate later.
ColumnName = Literal[
    "problem_name",
    "row_type",
    "title",
    "body_text",
    "answer",
    "answer_type",
    "hint_id",
    "dependency",
    "mc_choices",
    "images",
    "parent",
    "oer_src",
    "openstax_kc",
    "kc",
    "taxonomy",
    "license",
]


# --------------------------------------------------------------------------------------
# Initial Auditor
# --------------------------------------------------------------------------------------


class FindingCell(BaseModel):
    """One exact spreadsheet cell the repair must change.

    Rows and columns used to be separate lists, which made a two-row, two-column finding
    authorize their four-cell Cartesian product. Pairing them in the response contract
    keeps the model's intended scope exact all the way to the patch gate.
    """

    row: int = Field(gt=0, description="The real 1-based spreadsheet row")
    column: ColumnName


class AuditorFinding(BaseModel):
    #: The repair is authorised against these exact pairs, so naming the cell where the
    #: defect was *noticed* rather than the cell that must *change* produces a correction
    #: the gate refuses and a defect that survives the job.
    cells: list[FindingCell] = Field(
        min_length=1,
        description=(
            "Every exact row-and-column cell that must change to complete this one "
            "repair. Name repair targets, not cells where a symptom was noticed"
        ),
    )
    problem: str = Field(description="What is wrong, in one sentence")
    expected: str = Field(default="", description="What the content should be, if known")
    severity: Severity = Severity.ERROR
    #: Structural columns are repairable only under a finding that classifies itself as
    #: structural, so this field decides whether the defect can be fixed at all.
    category: IssueCategory = Field(
        default=IssueCategory.MATHEMATICS,
        description=(
            "Use structure, row_type or dependency for a defect in Problem Name, Row "
            "Type, answerType, HintID/Scaffold ID or Dependency -- those corrections are "
            "refused under a mathematics finding"
        ),
    )
    #: Index into the seed claims supplied with the request, when this finding confirms
    #: one. `None` means the auditor found it independently.
    confirms_claim: int | None = None


class RowCoverage(BaseModel):
    """Proof of work for one graded row: what was checked, and what it came to.

    **The point is the denominator.** An audit's `findings` list says what it found; it
    says nothing at all about what it looked at, so a model that examined three of nine
    graded rows and a model that examined all nine and found them clean return the same
    empty list. Eight of eleven misses on the held-out workbooks were rows nothing ever
    reported on, and there was no way to tell those from rows that were checked and were
    fine.

    So every graded row must come back with one of these. The booleans are not a
    checklist for the model to tick: they are the specific questions the misses were
    hiding behind -- an extraneous root (`solution_count_checked`), a domain restriction
    (`domain_checked`), an exact form silently decimalised (`requested_form_correct`), an
    answer that is valid mathematics for a different question (`computed_answer` beside
    `submitted_answer`, where a reader can see they diverge).

    Writing `computed_answer` down is what makes the rest inspectable. A row whose
    computed and submitted answers differ while `answer_correct` is true is a self-
    contradicting record, and a human reading the audit trail can see it.
    """

    row: int = Field(gt=0, description="The real 1-based spreadsheet row")
    computed_answer: str = Field(
        description=(
            "The answer you derived yourself, before looking at what is recorded. Use a "
            "short description when the answer is not a value"
        )
    )
    submitted_answer: str = Field(description="What the Answer cell actually contains")
    answer_correct: bool = Field(
        description="Does the recorded answer answer the question that was asked"
    )
    answer_type_correct: bool = Field(
        description="Does answerType match what the recorded answer actually is"
    )
    requested_form_correct: bool = Field(
        default=True,
        description=(
            "Exact versus decimal, simplified, units -- as the question requires. True "
            "when the question requires nothing in particular"
        ),
    )
    domain_checked: bool = Field(
        default=False, description="You checked domain restrictions and excluded values"
    )
    solution_count_checked: bool = Field(
        default=False,
        description="You checked how many solutions exist and whether any are extraneous",
    )
    units_checked: bool = Field(
        default=False, description="You checked units, or confirmed none are involved"
    )
    choices_checked: bool = Field(
        default=False,
        description=(
            "You checked that exactly one choice matches the answer exactly, or "
            "confirmed this row has no choice list"
        ),
    )
    finding_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Zero-based positions in this response's findings list that concern this "
            "row. Empty means you checked the row and it is correct"
        ),
    )


class RefutedClaim(BaseModel):
    """A seeded claim the auditor checked and could not find.

    Recorded rather than dropped. A curator who reported a defect deserves to know it was
    looked for and was not there, and a silently discarded claim is indistinguishable
    from one nobody read.
    """

    claim_index: int
    why: str


class AuditorResponse(BaseModel):
    """The single-block audit response.

    `SCAN_BATCH_SIZE=1` uses this schema and the single-block payload directly rather than
    wrapping one item in the batch schema. That keeps batching disabled at its default.
    """

    #: Private. Split off before anything else sees this response.
    reasoning: str = ""
    findings: list[AuditorFinding] = Field(default_factory=list)
    refuted_claims: list[RefutedClaim] = Field(default_factory=list)
    #: One entry per graded row. A block whose coverage is short is re-audited rather
    #: than accepted, because an unexamined row is not a clean row.
    coverage: list[RowCoverage] = Field(default_factory=list)


class AuditorBlockResult(BaseModel):
    """One block's verdict inside a batched audit.

    `batch_item_id` is the whole safety mechanism. Attribution by row containment alone
    cannot tell a block the model *omitted* from a block it examined and found clean --
    both produce nothing mentioning that block -- and marking the omitted one done is a
    workbook reported as reviewed when nothing looked at it.
    """

    batch_item_id: str = Field(
        description="Copy the batch_item id from the block's section label exactly"
    )
    reasoning: str = ""
    findings: list[AuditorFinding] = Field(default_factory=list)
    refuted_claims: list[RefutedClaim] = Field(default_factory=list)
    coverage: list[RowCoverage] = Field(default_factory=list)


class BatchedAuditorResponse(BaseModel):
    """Used only when a batch holds more than one block.

    Every dispatched block must appear exactly once. A block with nothing wrong still
    needs its own entry, carrying an empty `findings` list -- silence is not an answer.
    """

    results: list[AuditorBlockResult] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------


class WriterEdit(BaseModel):
    row: int
    column: ColumnName
    before: str = Field(
        description="The cell's current contents, character for character"
    )
    after: str


class WriterResponse(BaseModel):
    #: Private. Reviewers never see these three.
    reasoning: str = ""
    derivation: str = Field(
        description=(
            "Required verification for the proposed edits. When editing answer or "
            "mc_choices, state the calculation, exact-choice check, or symbolic "
            "equivalence that proves the new value is correct. Use an empty string only "
            "when no mathematical cell is edited."
        )
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    edits: list[WriterEdit] = Field(default_factory=list)
    #: Public, and read by the gate. Required whenever the patch edits a cell the issue
    #: did not name.
    related_edits_reason: str = Field(
        default="",
        description=(
            "If any edit is to a cell the issue did not name, why that cell is part of "
            "the same repair. Leave empty when every edit is to a named cell."
        ),
    )
    needs_human_review: bool = False
    human_review_reason: str = ""


# --------------------------------------------------------------------------------------
# Reviewers
# --------------------------------------------------------------------------------------


class ReviewerResponse(BaseModel):
    """A reviewer's output is entirely public.

    There is no private field here by design. A reviewer produces a decision and the
    feedback that justifies it; if any of that had to be withheld from the Writer, the
    feedback could not be acted on.
    """

    decision: Literal["accept", "revise", "human_review"]
    feedback: str = ""
    rule_codes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Adjudicator
# --------------------------------------------------------------------------------------


class AdjudicatorResponse(BaseModel):
    """The settlement of a disagreement between two independent audits.

    Entirely public, like a reviewer's response: an adjudication that had to withhold its
    reasoning could not be acted on by the Writer, and its whole value is the evidence.

    `undecided` is a first-class answer and not a failure. The reason this agent exists is
    that the pipeline previously had no way to say "two audits disagreed and neither was
    shown wrong", so it said `refuted` instead and discarded real defects. Restoring that
    by pressuring this response into a binary would reintroduce the same loss one layer
    further in.
    """

    verdict: Literal["defect_confirmed", "content_correct", "undecided"]
    evidence: str = Field(
        default="",
        description=(
            "The check you actually performed -- the arithmetic, the substitution, the "
            "exact-match comparison. Required for every verdict, and most of all for "
            "content_correct: dismiss a claim only by showing the recorded value is "
            "already right, never by not finding anything wrong with it"
        ),
    )
    cells: list[FindingCell] = Field(
        default_factory=list,
        description=(
            "For defect_confirmed: every exact cell that must change to complete the "
            "repair, including cells neither audit named. This replaces the disputed "
            "claim's targets, so an incomplete list leaves the defect half-repaired"
        ),
    )
    category: IssueCategory = Field(
        default=IssueCategory.MATHEMATICS,
        description=(
            "The canonical classification of the confirmed defect. Use structure, "
            "row_type or dependency whenever a named cell is in Problem Name, Row Type, "
            "answerType, HintID/Scaffold ID or Dependency"
        ),
    )
    expected: str = Field(
        default="", description="What the content should be, when you can state it"
    )


class IndependentFinding(BaseModel):
    cells: list[FindingCell] = Field(
        min_length=1,
        description=(
            "Every exact row-and-column cell that must change to complete this one "
            "repair; list all coordinated targets, not just where the symptom appears"
        ),
    )
    problem: str
    expected: str = ""
    severity: Severity = Severity.ERROR
    category: IssueCategory = IssueCategory.MATHEMATICS


class IndependentReviewResponse(BaseModel):
    """The fresh sweep over every current block. Public: these become issues.

    Like `AuditorResponse`, this is used directly when batch size is one.
    """

    findings: list[IndependentFinding] = Field(default_factory=list)
    block_is_sound: bool = True
    #: One entry per graded row, for the same reason the auditor carries them: this
    #: response's `block_is_sound` is an assertion, and coverage is what backs it.
    coverage: list[RowCoverage] = Field(default_factory=list)


class IndependentBlockResult(BaseModel):
    """One block's sweep verdict inside a batch."""

    batch_item_id: str = Field(
        description="Copy the batch_item id from the block's section label exactly"
    )
    findings: list[IndependentFinding] = Field(default_factory=list)
    block_is_sound: bool = True
    coverage: list[RowCoverage] = Field(default_factory=list)


class BatchedIndependentReviewResponse(BaseModel):
    """Used only when a batch holds more than one block.

    A block absent from `results` is **not** sound; it is unswept, and it goes back on the
    queue. Treating an omission as a pass is how a sweep reports coverage it never had.
    """

    results: list[IndependentBlockResult] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Final Semantic Verifier
# --------------------------------------------------------------------------------------


class FinalVerificationResponse(BaseModel):
    """The last word on the corrected workbook, from an agent shown nothing else.

    Structurally close to `IndependentReviewResponse` and deliberately a separate type.
    The two answer different questions -- one sweeps a workbook mid-repair, the other
    certifies the file about to be handed over -- and sharing a model would mean a field
    added for one silently changed the contract of the other.

    There is no private field and no reasoning field. This agent publishes everything it
    concludes, because a certification whose grounds are withheld cannot be checked.
    """

    findings: list[IndependentFinding] = Field(default_factory=list)
    #: Mandatory, and the reason this phase can mean anything. `findings` says what is
    #: wrong; only coverage says which rows were solved to find out.
    coverage: list[RowCoverage] = Field(default_factory=list)
    block_is_sound: bool = True


def column_key(name: str) -> ColumnKey:
    return ColumnKey(name)
