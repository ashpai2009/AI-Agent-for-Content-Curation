"""Structured response schemas for the four agents.

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


class AuditorFinding(BaseModel):
    rows: list[int] = Field(description="Spreadsheet rows the defect concerns")
    columns: list[ColumnName] = Field(default_factory=list)
    problem: str = Field(description="What is wrong, in one sentence")
    expected: str = Field(default="", description="What the content should be, if known")
    severity: Severity = Severity.ERROR
    category: IssueCategory = IssueCategory.MATHEMATICS
    #: Index into the seed claims supplied with the request, when this finding confirms
    #: one. `None` means the auditor found it independently.
    confirms_claim: int | None = None


class RefutedClaim(BaseModel):
    """A seeded claim the auditor checked and could not find.

    Recorded rather than dropped. A curator who reported a defect deserves to know it was
    looked for and was not there, and a silently discarded claim is indistinguishable
    from one nobody read.
    """

    claim_index: int
    why: str


class AuditorResponse(BaseModel):
    #: Private. Split off before anything else sees this response.
    reasoning: str = ""
    findings: list[AuditorFinding] = Field(default_factory=list)
    refuted_claims: list[RefutedClaim] = Field(default_factory=list)


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
    derivation: str = ""
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


class IndependentFinding(BaseModel):
    rows: list[int]
    columns: list[ColumnName] = Field(default_factory=list)
    problem: str
    expected: str = ""
    severity: Severity = Severity.ERROR
    category: IssueCategory = IssueCategory.MATHEMATICS


class IndependentReviewResponse(BaseModel):
    """The sweep over blocks nobody flagged. Public: these become issues."""

    findings: list[IndependentFinding] = Field(default_factory=list)
    block_is_sound: bool = True


def column_key(name: str) -> ColumnKey:
    return ColumnKey(name)
