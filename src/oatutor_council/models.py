"""Domain vocabulary shared by every layer.

Everything here is either a Pydantic model or an enum. There is no behaviour in this
module beyond validation, and deliberately no I/O: the reader, the rules engine, the
agents, the persistence layer and the API all speak these types, so a dependency in the
other direction would make the vocabulary answer to one consumer.

Two conventions worth knowing before reading further:

* Rows and columns are **1-based**, matching openpyxl and matching what a curator sees
  in Excel. A `row` in any model is the real spreadsheet row number.
* Cell payloads crossing an agent boundary are always **text**. A model can only emit
  and reason about text, so `before`/`after` on an edit are strings and the workbook
  layer owns the conversion to and from native Excel types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, NamedTuple, NewType

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------------------
# Column contract
# --------------------------------------------------------------------------------------


class ColumnKey(StrEnum):
    """Canonical name for each column in the workbook contract.

    Code refers to columns by these keys, never by letter or index, so that the two
    columns whose position is not stable across the corpus can be resolved differently
    from the sixteen that are.
    """

    PROBLEM_NAME = "problem_name"
    ROW_TYPE = "row_type"
    TITLE = "title"
    BODY_TEXT = "body_text"
    ANSWER = "answer"
    ANSWER_TYPE = "answer_type"
    HINT_ID = "hint_id"
    DEPENDENCY = "dependency"
    MC_CHOICES = "mc_choices"
    IMAGES = "images"
    PARENT = "parent"
    OER_SRC = "oer_src"
    OPENSTAX_KC = "openstax_kc"
    KC = "kc"
    TAXONOMY = "taxonomy"
    LICENSE = "license"
    VALIDATOR_CHECK = "validator_check"
    TIME_LAST_CHECKED = "time_last_checked"


#: Columns A-P. Reconnaissance confirmed these hold the same index in all eleven real
#: workbooks, so they are addressed positionally.
FIXED_COLUMNS: dict[ColumnKey, int] = {
    ColumnKey.PROBLEM_NAME: 1,
    ColumnKey.ROW_TYPE: 2,
    ColumnKey.TITLE: 3,
    ColumnKey.BODY_TEXT: 4,
    ColumnKey.ANSWER: 5,
    ColumnKey.ANSWER_TYPE: 6,
    ColumnKey.HINT_ID: 7,
    ColumnKey.DEPENDENCY: 8,
    ColumnKey.MC_CHOICES: 9,
    ColumnKey.IMAGES: 10,
    ColumnKey.PARENT: 11,
    ColumnKey.OER_SRC: 12,
    ColumnKey.OPENSTAX_KC: 13,
    ColumnKey.KC: 14,
    ColumnKey.TAXONOMY: 15,
    ColumnKey.LICENSE: 16,
}

#: Columns Q-T. These do *not* hold a stable index: `Validator Check` was found at
#: column 18, 19 and 20 across the corpus, sometimes duplicated, and one workbook has no
#: `Time Last Checked` at all. They are resolved by header label and may be absent.
NAMED_COLUMNS: dict[ColumnKey, str] = {
    ColumnKey.VALIDATOR_CHECK: "Validator Check",
    ColumnKey.TIME_LAST_CHECKED: "Time Last Checked",
}

#: The label expected in the header row for each column, used to verify the contract.
#: `Images` carries a parenthetical in every real workbook, so the check is a
#: normalised prefix match rather than equality (see `workbook.reader`).
HEADER_LABELS: dict[ColumnKey, str] = {
    ColumnKey.PROBLEM_NAME: "Problem Name",
    ColumnKey.ROW_TYPE: "Row Type",
    ColumnKey.TITLE: "Title",
    ColumnKey.BODY_TEXT: "Body Text",
    ColumnKey.ANSWER: "Answer",
    ColumnKey.ANSWER_TYPE: "answerType",
    ColumnKey.HINT_ID: "HintID",
    ColumnKey.DEPENDENCY: "Dependency",
    ColumnKey.MC_CHOICES: "mcChoices",
    ColumnKey.IMAGES: "Images",
    ColumnKey.PARENT: "Parent",
    ColumnKey.OER_SRC: "OER src",
    ColumnKey.OPENSTAX_KC: "openstax KC",
    ColumnKey.KC: "KC",
    ColumnKey.TAXONOMY: "Taxonomy",
    ColumnKey.LICENSE: "License",
    ColumnKey.VALIDATOR_CHECK: "Validator Check",
    ColumnKey.TIME_LAST_CHECKED: "Time Last Checked",
}

#: Columns that define what a problem block *is* rather than what it says. Editing one
#: is not forbidden -- the 7.3 column-shift corruption lives entirely in these columns
#: and would be unrepairable under a blanket refusal -- but it passes a stricter gate.
STRUCTURAL_COLUMNS: frozenset[ColumnKey] = frozenset(
    {
        ColumnKey.PROBLEM_NAME,
        ColumnKey.ROW_TYPE,
        ColumnKey.ANSWER_TYPE,
        ColumnKey.HINT_ID,
        ColumnKey.DEPENDENCY,
    }
)

#: Columns Excel will re-coerce if written as anything but text. A repaired `1/3` handed
#: back as a general-format cell becomes a date again -- the exact defect being fixed.
TEXT_FORCED_COLUMNS: frozenset[ColumnKey] = frozenset(
    {ColumnKey.ANSWER, ColumnKey.MC_CHOICES}
)

#: `Time Last Checked` legitimately holds a datetime, so date-coercion rules skip it.
DATETIME_EXEMPT_COLUMNS: frozenset[ColumnKey] = frozenset(
    {ColumnKey.TIME_LAST_CHECKED}
)

MC_CHOICE_DELIMITER = "|"
MIN_MC_CHOICES = 2
MAX_MC_CHOICES = 5


# --------------------------------------------------------------------------------------
# Workbook vocabulary
# --------------------------------------------------------------------------------------


class RowType(StrEnum):
    PROBLEM = "problem"
    STEP = "step"
    HINT = "hint"
    SCAFFOLD = "scaffold"


class AnswerType(StrEnum):
    NUMERIC = "numeric"
    ALGEBRA = "algebra"
    MC = "mc"


class Notation(StrEnum):
    """Which mathematical convention a workbook is written in."""

    ASCII = "ascii"
    LATEX = "latex"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class DependencyConvention(StrEnum):
    """How hint/scaffold dependency numbering restarts across steps.

    `UNDECIDED` is a real answer, not a failure: a block with one step offers no
    evidence either way, and guessing per block is how a detector invents a convention
    the workbook never had.
    """

    RESET_PER_STEP = "reset_per_step"
    CONTINUOUS = "continuous"
    UNDECIDED = "undecided"


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------


class Severity(StrEnum):
    BLOCKING = "blocking"
    ERROR = "error"
    WARNING = "warning"
    OBSERVATION = "observation"


class FindingScope(StrEnum):
    WORKBOOK = "workbook"
    BLOCK = "block"
    ROW = "row"
    CELL = "cell"


class StructuralCode(StrEnum):
    """Codes the reader may emit while parsing.

    These are separate from the rule codes in `validation.rules` because they describe a
    failure to interpret the file at all, and they are raised before any rule can
    meaningfully run. Where the two segmentation signals disagree the reader reports the
    disagreement instead of resolving it -- silently preferring either reading is what
    hides a column-shift corruption.
    """

    MISSING_HEADER_ROW = "MISSING_HEADER_ROW"
    HEADER_CONTRACT_MISMATCH = "HEADER_CONTRACT_MISMATCH"
    DUPLICATE_HEADER_LABEL = "DUPLICATE_HEADER_LABEL"
    MISSING_NAMED_COLUMN = "MISSING_NAMED_COLUMN"
    MULTIPLE_SHEETS = "MULTIPLE_SHEETS"
    NO_PROBLEM_ROWS = "NO_PROBLEM_ROWS"
    ORPHAN_ROW_BEFORE_FIRST_PROBLEM = "ORPHAN_ROW_BEFORE_FIRST_PROBLEM"
    PROBLEM_NAME_MISMATCH_IN_BLOCK = "PROBLEM_NAME_MISMATCH_IN_BLOCK"
    BLOCK_BOUNDARY_DISAGREEMENT = "BLOCK_BOUNDARY_DISAGREEMENT"
    MISSING_PROBLEM_NAME = "MISSING_PROBLEM_NAME"
    UNKNOWN_ROW_TYPE = "UNKNOWN_ROW_TYPE"
    ROW_SHIFT_RIGHT = "ROW_SHIFT_RIGHT"
    COLUMN_SHIFT = "COLUMN_SHIFT"
    INTERIOR_BLANK_ROW = "INTERIOR_BLANK_ROW"


class ValidationFinding(BaseModel):
    """One thing wrong with the workbook, located as precisely as it is known.

    A finding is an observation, never an instruction. Nothing in the pipeline edits a
    cell because a finding exists; findings become `Issue`s, issues get patches, and
    patches pass the gate.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    message: str
    scope: FindingScope = FindingScope.CELL
    block_id: str | None = None
    problem_name: str | None = None
    row: int | None = None
    column: int | None = None
    column_key: ColumnKey | None = None
    repairable: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _scope_is_located(self) -> ValidationFinding:
        if self.scope in (FindingScope.ROW, FindingScope.CELL) and self.row is None:
            raise ValueError(f"{self.code}: {self.scope} finding needs a row")
        if self.scope is FindingScope.CELL and self.column is None:
            raise ValueError(f"{self.code}: cell finding needs a column")
        return self


# --------------------------------------------------------------------------------------
# Parsed workbook
# --------------------------------------------------------------------------------------


class WorkbookRow(BaseModel):
    """One spreadsheet row, rendered as text plus the values Excel actually stored.

    `values` is what agents and rules read. `raw` retains the native Python objects
    openpyxl produced, because a datetime that used to be `1/2` is only recognisable as
    date coercion *before* it is stringified.
    """

    model_config = ConfigDict(frozen=True)

    row: int
    values: dict[ColumnKey, str] = Field(default_factory=dict)
    raw: dict[ColumnKey, Any] = Field(default_factory=dict, exclude=True)
    is_blank: bool = False
    #: Columns on this row with wrap enabled. Carried here so the appearance rules stay
    #: pure functions over the parsed model rather than reaching back into openpyxl.
    wrap_text_columns: tuple[int, ...] = ()

    def get(self, key: ColumnKey) -> str:
        return self.values.get(key, "")

    @property
    def row_type(self) -> RowType | None:
        try:
            return RowType(self.get(ColumnKey.ROW_TYPE).strip().lower())
        except ValueError:
            return None

    @property
    def answer_type(self) -> AnswerType | None:
        try:
            return AnswerType(self.get(ColumnKey.ANSWER_TYPE).strip().lower())
        except ValueError:
            return None


class StepScope(NamedTuple):
    """One step row and the hint/scaffold rows belonging to it.

    `step` is `None` for sub-rows appearing before the block's first step row -- a
    defect, but one the caller has to be able to see rather than one this type quietly
    normalises away.
    """

    step: WorkbookRow | None
    rows: tuple[WorkbookRow, ...]

    @property
    def identified(self) -> tuple[WorkbookRow, ...]:
        """Every sub-row carrying an identifier, in document order.

        Used for uniqueness and numbering, which apply to hints and scaffolds alike.
        The *dependency* rules deliberately do not use this: hints and scaffolds play
        different parts there, and `hints` below is the sequence that chains.
        """
        return tuple(
            row
            for row in self.rows
            if row.row_type in (RowType.HINT, RowType.SCAFFOLD)
            and row.get(ColumnKey.HINT_ID).strip()
        )

    @property
    def hints(self) -> tuple[WorkbookRow, ...]:
        """Hint rows carrying an identifier, in document order. The chain."""
        return tuple(
            row
            for row in self.rows
            if row.row_type is RowType.HINT and row.get(ColumnKey.HINT_ID).strip()
        )

    def hint_before(self, row: WorkbookRow) -> WorkbookRow | None:
        """The nearest hint above `row` in this step. What a scaffold depends on."""
        found = None
        for candidate in self.rows:
            if candidate.row >= row.row:
                break
            if candidate.row_type is RowType.HINT and candidate.get(
                ColumnKey.HINT_ID
            ).strip():
                found = candidate
        return found


class ProblemBlock(BaseModel):
    """A problem row and everything under it up to the next problem row.

    Segmentation is driven by `Row Type == "problem"`. `problem_name` is therefore the
    *declared* name taken from the problem row, and any disagreement with the names on
    interior rows is recorded in `findings` rather than reconciled.
    """

    model_config = ConfigDict(frozen=True)

    block_id: str
    index: int
    problem_name: str
    start_row: int
    end_row: int
    rows: tuple[WorkbookRow, ...]
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def problem_row(self) -> WorkbookRow:
        return self.rows[0]

    def rows_of_type(self, row_type: RowType) -> tuple[WorkbookRow, ...]:
        return tuple(r for r in self.rows if r.row_type is row_type)

    def contains_row(self, row: int) -> bool:
        return self.start_row <= row <= self.end_row

    def step_scopes(self) -> tuple[StepScope, ...]:
        """The block divided at its step rows.

        This is the unit the dependency rules are actually about, and not having it is
        why they were wrong. A block's hints and scaffolds belong to the step above them,
        and under the reset-per-step convention every step legitimately restarts at `h1`
        -- so a block-wide uniqueness check reports a duplicate for every step after the
        first. That single missing distinction accounts for 312 false findings across the
        real corpus.

        Rows before the first step row form a leading scope with `step=None`. They are
        usually nothing, but a hint sitting above every step is a real defect and
        silently dropping it here would hide it.
        """
        scopes: list[StepScope] = []
        step: WorkbookRow | None = None
        current: list[WorkbookRow] = []

        for row in self.rows:
            if row.row_type is RowType.PROBLEM:
                continue
            if row.row_type is RowType.STEP:
                if step is not None or current:
                    scopes.append(StepScope(step=step, rows=tuple(current)))
                step, current = row, []
                continue
            if not row.is_blank:
                current.append(row)

        if step is not None or current:
            scopes.append(StepScope(step=step, rows=tuple(current)))
        return tuple(scopes)


class WorkbookConventions(BaseModel):
    """Per-workbook habits that are detected, not assumed.

    Six of the eleven real workbooks use the `h` namespace for scaffold identifiers
    where the written rules specify `s`. That is a house style, not damage, so a
    workbook-wide consistent alternative downgrades the finding to a warning while a
    workbook that mixes both keeps the error.
    """

    model_config = ConfigDict(frozen=True)

    naming_stems: tuple[str, ...] = ()
    scaffold_namespaces: tuple[str, ...] = ()
    dominant_scaffold_namespace: str | None = None
    scaffold_namespace_is_consistent: bool = True
    dependency_convention: DependencyConvention = DependencyConvention.UNDECIDED
    notation: Notation = Notation.UNKNOWN


class ColumnMap(BaseModel):
    """Resolved position of every contract column in one specific workbook.

    Fixed columns come from `FIXED_COLUMNS`; the trailing pair is looked up by header
    label and may be missing, so `index_of` returns `None` rather than guessing.
    """

    model_config = ConfigDict(frozen=True)

    header_row: int
    positions: dict[ColumnKey, int]
    headers: tuple[str | None, ...]

    def index_of(self, key: ColumnKey) -> int | None:
        return self.positions.get(key)

    def require(self, key: ColumnKey) -> int:
        index = self.positions.get(key)
        if index is None:
            raise KeyError(f"column {key} is not present in this workbook")
        return index

    def key_at(self, index: int) -> ColumnKey | None:
        for key, position in self.positions.items():
            if position == index:
                return key
        return None


class ParsedWorkbook(BaseModel):
    """Everything the deterministic layer knows about a workbook after one read."""

    model_config = ConfigDict(frozen=True)

    sheet_name: str
    header_row: int
    first_data_row: int
    max_row: int
    column_map: ColumnMap
    blocks: tuple[ProblemBlock, ...]
    orphan_rows: tuple[WorkbookRow, ...] = ()
    row_heights: dict[int, float] = Field(default_factory=dict)
    conventions: WorkbookConventions = WorkbookConventions()
    findings: tuple[ValidationFinding, ...] = ()

    def block_by_id(self, block_id: str) -> ProblemBlock | None:
        return next((b for b in self.blocks if b.block_id == block_id), None)

    def block_containing(self, row: int) -> ProblemBlock | None:
        return next((b for b in self.blocks if b.contains_row(row)), None)

    @property
    def all_findings(self) -> tuple[ValidationFinding, ...]:
        return self.findings + tuple(f for b in self.blocks for f in b.findings)


# --------------------------------------------------------------------------------------
# Edits and patches
# --------------------------------------------------------------------------------------


class CellEdit(BaseModel):
    """A single cell rewrite, stated as exact before and after text.

    `before` is not advisory. The gate re-reads the cell and refuses the patch on any
    mismatch, which is what makes a stale patch -- one written against a block that a
    sibling edit has since changed -- fail loudly instead of overwriting a newer value.
    """

    model_config = ConfigDict(frozen=True)

    row: int = Field(gt=0)
    column: int = Field(gt=0)
    column_key: ColumnKey | None = None
    before: str
    after: str

    @model_validator(mode="after")
    def _not_a_no_op(self) -> CellEdit:
        if self.before == self.after:
            raise ValueError(
                f"edit at row {self.row} column {self.column} changes nothing"
            )
        return self

    @property
    def is_structural(self) -> bool:
        return self.column_key in STRUCTURAL_COLUMNS


class Patch(BaseModel):
    """One Writer response: the edits, plus reasoning that reviewers never see.

    `reason`, `derivation` and `confidence` are Writer reasoning. Decision 1 of the
    design keeps them out of every reviewer prompt -- a reviewer told *why* an edit was
    made reviews the argument rather than the artifact -- so they are persisted for the
    human change log and excluded from the reviewer context type.
    """

    model_config = ConfigDict(frozen=True)

    patch_id: str
    issue_id: str
    attempt_no: int = Field(gt=0)
    edits: tuple[CellEdit, ...]
    reason: str = ""
    derivation: str = ""
    #: Why cells the issue did not name are part of the same repair. Public, unlike the
    #: three fields above: the gate reads it, and an unexplained edit outside the issue's
    #: own cells is refused. A repair that genuinely needs a sibling cell can say so; an
    #: unrelated improvement has nothing to write here.
    related_edits_reason: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_human_review: bool = False
    human_review_reason: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> Patch:
        if self.needs_human_review:
            if self.edits:
                raise ValueError("a patch escalating to human review must not edit")
            if not self.human_review_reason.strip():
                raise ValueError("escalation to human review needs a stated reason")
            return self
        if not self.edits:
            raise ValueError("a patch must either edit something or escalate")
        seen = {(e.row, e.column) for e in self.edits}
        if len(seen) != len(self.edits):
            raise ValueError("patch edits the same cell twice")
        return self

    @property
    def touches_structural_columns(self) -> bool:
        return any(e.is_structural for e in self.edits)


class RejectionCode(StrEnum):
    """Why the deterministic gate refused a patch.

    `STALE_BEFORE` and the infrastructure codes are the only ones that do not consume a
    repair attempt: every other rejection followed a Writer call that was spent.
    """

    BEFORE_MISMATCH = "BEFORE_MISMATCH"
    STALE_BEFORE = "STALE_BEFORE"
    CELL_NOT_FOUND = "CELL_NOT_FOUND"
    OUT_OF_BLOCK_SCOPE = "OUT_OF_BLOCK_SCOPE"
    UNRELATED_CELL = "UNRELATED_CELL"
    RULE_VIOLATION = "RULE_VIOLATION"
    NO_OP = "NO_OP"
    #: The patch is well-formed, breaks nothing, and leaves the defect exactly where it
    #: was. Without this the repair loop can close an issue by editing something else.
    ISSUE_NOT_RESOLVED = "ISSUE_NOT_RESOLVED"
    #: The row/column pair and the named column disagree, so the patch describes one cell
    #: and would write another.
    COLUMN_KEY_MISMATCH = "COLUMN_KEY_MISMATCH"
    #: A repair to how a value is *written* changed what the value *is*.
    MATH_NOT_EQUIVALENT = "MATH_NOT_EQUIVALENT"
    #: A mathematics finding proposed only an equivalent restatement. Correct content is
    #: not a defect merely because a simpler representation exists.
    MATHEMATICALLY_EQUIVALENT_REWRITE = "MATHEMATICALLY_EQUIVALENT_REWRITE"
    #: The candidate changes an answer away from a form the question explicitly requests.
    REQUESTED_FORM_VIOLATION = "REQUESTED_FORM_VIOLATION"
    MISSING_MATH_VERIFICATION = "MISSING_MATH_VERIFICATION"
    DUPLICATE_CELL_EDIT = "DUPLICATE_CELL_EDIT"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    TARGET_IS_SOURCE = "TARGET_IS_SOURCE"
    STRUCTURAL_COLUMN_UNAUTHORIZED = "STRUCTURAL_COLUMN_UNAUTHORIZED"
    #: A model proposed changing the workbook's structural contract without any
    #: deterministic structural finding supporting that row.
    STRUCTURAL_EVIDENCE_MISSING = "STRUCTURAL_EVIDENCE_MISSING"
    STRUCTURAL_BLOCK_INVARIANT_BROKEN = "STRUCTURAL_BLOCK_INVARIANT_BROKEN"
    STRUCTURAL_CONTENT_NOT_CONSERVED = "STRUCTURAL_CONTENT_NOT_CONSERVED"
    ROW_STRUCTURE_CHANGE_PROHIBITED = "ROW_STRUCTURE_CHANGE_PROHIBITED"


class PatchRejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: RejectionCode
    message: str
    row: int | None = None
    column: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def consumes_attempt(self) -> bool:
        return self.code is not RejectionCode.STALE_BEFORE


class ChangeRecord(BaseModel):
    """A cell edit that actually landed in the working copy.

    The change log is reconciled against the source-to-output diff at finalisation: a
    difference with no `ChangeRecord` behind it fails the gate, which is how an edit
    nobody authorised is caught.
    """

    model_config = ConfigDict(frozen=True)

    change_id: str
    issue_id: str | None
    patch_id: str | None
    block_id: str | None
    row: int
    column: int
    column_key: ColumnKey | None = None
    before: str
    after: str
    applied_at: datetime


# --------------------------------------------------------------------------------------
# Issues, reviews, attempts
# --------------------------------------------------------------------------------------


class IssueSource(StrEnum):
    INSTRUCTION_DOCUMENT = "instruction_document"
    INITIAL_AUDITOR = "initial_auditor"
    INDEPENDENT_REVIEWER = "independent_reviewer"
    FINAL_VALIDATION = "final_validation"


class ReviewerRole(StrEnum):
    KNOWN_ISSUE_REVIEWER = "known_issue_reviewer"
    INDEPENDENT_REVIEWER = "independent_reviewer"


class IssueCategory(StrEnum):
    STRUCTURE = "structure"
    ROW_TYPE = "row_type"
    DEPENDENCY = "dependency"
    NOTATION = "notation"
    FORMATTING = "formatting"
    MATHEMATICS = "mathematics"
    MULTIPLE_CHOICE = "multiple_choice"
    LATEX = "latex"
    METADATA = "metadata"
    APPEARANCE = "appearance"


class IssueState(StrEnum):
    OPEN = "open"
    AWAITING_PATCH = "awaiting_patch"
    PATCH_PROPOSED = "patch_proposed"
    #: The reviewer accepted the simulated patch. No workbook byte has been written yet;
    #: application is a separate, crash-safe step.
    PATCH_APPROVED = "patch_approved"
    APPLYING = "applying"
    PATCH_APPLIED = "patch_applied"
    AWAITING_REVIEW = "awaiting_review"
    PATCH_REJECTED = "patch_rejected"
    REVISION_REQUESTED = "revision_requested"
    ACCEPTED = "accepted"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


#: An issue in one of these states is finished. Success additionally requires that none
#: of them is NEEDS_HUMAN_REVIEW -- see `JobResult`.
TERMINAL_ISSUE_STATES: frozenset[IssueState] = frozenset(
    {
        IssueState.ACCEPTED,
        IssueState.REFUTED,
        IssueState.SUPERSEDED,
        IssueState.NEEDS_HUMAN_REVIEW,
    }
)

#: The only terminal states compatible with reporting success. A refuted claim is a
#: correct outcome: the auditor checked and the defect was not there.
SUCCESSFUL_ISSUE_STATES: frozenset[IssueState] = frozenset(
    {IssueState.ACCEPTED, IssueState.REFUTED, IssueState.SUPERSEDED}
)


class Issue(BaseModel):
    """One defect claim, tracked from discovery to a terminal state.

    `fingerprint` is what stops the validation loop from cycling: a finding that
    fingerprints onto an already-terminal issue does not open a new one, it escalates
    the job to human attention.
    """

    issue_id: str
    job_id: str
    block_id: str | None = None
    problem_name: str | None = None
    source: IssueSource
    category: IssueCategory
    severity: Severity
    title: str
    description: str
    expected: str = ""
    observed: str = ""
    rule_codes: tuple[str, ...] = ()
    cells: tuple[tuple[int, int], ...] = ()
    is_structural: bool = False
    state: IssueState = IssueState.OPEN
    reviewer_role: ReviewerRole = ReviewerRole.KNOWN_ISSUE_REVIEWER
    attempts_used: int = 0
    interrupted_retries_used: int = 0
    fingerprint: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ISSUE_STATES


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    HUMAN_REVIEW = "human_review"


class ReviewVerdict(BaseModel):
    """A reviewer's judgment on the current state of a block.

    Reviewers cannot edit. `feedback` is the only thing that reaches the Writer on a
    revision, so it has to be actionable rather than a restatement of the issue.
    """

    model_config = ConfigDict(frozen=True)

    verdict_id: str
    issue_id: str
    reviewer_role: ReviewerRole
    #: Zero is reserved for a claim pre-check made before any Writer attempt. Positive
    #: numbers identify the Writer attempt whose candidate the reviewer judged.
    attempt_no: int = Field(ge=0)
    decision: ReviewDecision
    feedback: str = ""
    rule_codes: tuple[str, ...] = ()
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _actionable(self) -> ReviewVerdict:
        if self.decision is not ReviewDecision.ACCEPT and not self.feedback.strip():
            raise ValueError(f"a {self.decision} verdict must say what is wrong")
        return self


class AttemptOutcome(StrEnum):
    PATCH_ACCEPTED = "patch_accepted"
    PATCH_REJECTED = "patch_rejected"
    REVISION_REQUESTED = "revision_requested"
    ESCALATED = "escalated"
    INTERRUPTED = "interrupted"


class RepairAttempt(BaseModel):
    """One Writer call and everything that followed from it.

    The attempt row is committed *before* the call, not after. Incrementing afterwards
    means a process that dies mid-call leaves no durable evidence it happened, and a
    crash loop then burns unbounded spend against a counter that never moves.
    """

    attempt_id: str
    issue_id: str
    attempt_no: int = Field(gt=0)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    outcome: AttemptOutcome | None = None
    patch_id: str | None = None
    rejection: PatchRejection | None = None
    verdict_id: str | None = None


class IssueLedger(BaseModel):
    """A derived view over the issues of one job.

    Never stored. A persisted aggregate is somewhere for the database and reality to
    disagree after a crash, and this one is cheap to recompute.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    issues: tuple[Issue, ...]

    def by_state(self, state: IssueState) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.state is state)

    def by_source(self, source: IssueSource) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.source is source)

    @property
    def open_issues(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if not i.is_terminal)

    @property
    def all_resolved(self) -> bool:
        return all(i.state in SUCCESSFUL_ISSUE_STATES for i in self.issues)

    @property
    def blocks_with_ledger_entry(self) -> frozenset[str]:
        """Blocks the Known-Issue Reviewer owns.

        A block whose only entry was refuted is deliberately excluded, so it falls to
        the Independent Reviewer's sweep. Otherwise a bogus claim would buy a problem
        permanent immunity from any review at all.
        """
        return frozenset(
            i.block_id
            for i in self.issues
            if i.block_id is not None and i.state is not IssueState.REFUTED
        )


# --------------------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------------------


class JobState(StrEnum):
    """Pipeline position, and nothing else.

    There is deliberately no RUNNING state and no `*_INTERRUPTED` variants: liveness
    lives in the lease columns. A crashed job simply sits in the state it reached with
    an expired lease, which makes resume a pure function of this value.
    """

    CREATED = "created"
    INGESTING = "ingesting"
    AUDITING = "auditing"
    REPAIRING_KNOWN = "repairing_known"
    INDEPENDENT_REVIEW = "independent_review"
    FINAL_VALIDATION = "final_validation"
    REPAIRING_VALIDATION = "repairing_validation"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    NEEDS_HUMAN_ATTENTION = "needs_human_attention"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.NEEDS_HUMAN_ATTENTION,
        JobState.FAILED,
        JobState.CANCELLED,
    }
)


class FailureReason(StrEnum):
    CORRUPTION = "corruption"
    ISOLATION_VIOLATION = "isolation_violation"
    CONFIG = "config"
    PROVIDER = "provider"
    #: Ran past its wall-clock ceiling. Resumable: the work is intact and the next run
    #: starts from where this one stopped, which is the right answer for a job that was
    #: merely slow.
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_INPUT = "invalid_input"
    INTERNAL = "internal"


#: Failures that must never be resumed: the job's premises are broken, so retrying it
#: would repeat the damage rather than recover from it.
NON_RESUMABLE_FAILURES: frozenset[FailureReason] = frozenset(
    {
        FailureReason.CORRUPTION,
        FailureReason.ISOLATION_VIOLATION,
        FailureReason.CONFIG,
    }
)


class ClaimOutcome(StrEnum):
    """What became of one statement from the curator's document.

    `REFUTED` and `UNRESOLVED` are deliberately different answers. Refuted means a block
    was asked about the claim and reported that the defect is not there; unresolved means
    nothing ever reached a conclusion about it. Collapsing them would tell a curator
    their report was checked and dismissed when in fact it was never read.
    """

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNRESOLVED = "unresolved"


class ArtifactKind(StrEnum):
    SOURCE_WORKBOOK = "source_workbook"
    INSTRUCTION_DOCUMENT = "instruction_document"
    WORKING_WORKBOOK = "working_workbook"
    CORRECTED_WORKBOOK = "corrected_workbook"
    ISSUE_LEDGER = "issue_ledger"
    CHANGE_LOG = "change_log"
    REVIEW_HISTORY = "review_history"
    VALIDATION_REPORT = "validation_report"


SourcePath = NewType("SourcePath", str)
"""The immutable source workbook.

A distinct type so that no write-capable function can accept one by accident. It is one
of four independent defences -- alongside `chmod 0444`, a target-is-not-source assertion,
and hash re-verification at resume and finalisation -- because a single mistake here
destroys a curator's original file.
"""


class CurationJob(BaseModel):
    """The durable record of one curation run."""

    job_id: str
    state: JobState = JobState.CREATED
    failure_reason: FailureReason | None = None
    source_filename: str = ""
    source_sha256: str = ""
    instruction_filename: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_epoch: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    validation_rounds_used: int = 0
    steps_used: int = 0
    llm_calls_used: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES


class JobResult(BaseModel):
    """The final answer handed to the curator.

    Derived, never stored. `succeeded` is a computed conjunction rather than a flag
    somebody sets, so there is no code path that can report success without every gate
    having passed.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    state: JobState
    failure_reason: FailureReason | None = None
    ledger: IssueLedger
    changes: tuple[ChangeRecord, ...] = ()
    findings: tuple[ValidationFinding, ...] = ()
    artifacts: dict[ArtifactKind, str] = Field(default_factory=dict)
    unresolved_summary: str = ""

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.SUCCEEDED

    @property
    def needs_a_person(self) -> tuple[Issue, ...]:
        return self.ledger.by_state(IssueState.NEEDS_HUMAN_REVIEW)
