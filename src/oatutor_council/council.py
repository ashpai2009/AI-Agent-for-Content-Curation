"""The council loop: five stages, driven one step at a time.

*(Deviation from the sketched layout, for the same reason `validation/rules/` is a
package: `orchestrator.py` owns crash-safety and recovery, and putting the phase driving
there too would make one module answer two unrelated questions.)*

**The `step()` contract, which everything else rests on:** at most one model call, at most
one workbook mutation, exactly one committed unit of progress, and never a transaction
held open across a model call. That makes crash-safety a per-step argument rather than a
whole-pipeline one, and it lets a test kill the worker at step N deterministically.

**Phases are queue predicates, not loops.** Each job state defines a query; the
orchestrator drains it; the phase advances when it returns nothing. The cursor is always
derived from durable rows, never held in memory, so a phase is idempotent and resumes in
the right place without anyone recording where it got to.

**Three independent brakes stop this terminating badly.** The per-issue attempt cap, the
validation-round budget on the single cyclic edge, and global step and call fuses. Plus
fingerprint dedup with terminal absorption: a validator finding matching an issue that is
already terminal does not open a new issue with a fresh budget -- it escalates the job to
human attention, which is the honest outcome after three failed repairs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from openpyxl import load_workbook

from .agents import (
    adjudicator,
    independent_reviewer,
    initial_auditor,
    known_issue_reviewer,
    writer,
)
from .agents.batching import FindingAttributionError
from .agents.rendering import (
    render_block,
    render_claim,
    render_claims,
    render_conventions,
    render_findings,
)
from .agents.isolation import ContextIsolationError, TaintRegistry
from .config import Settings
from .ingestion.instruction_documents import SegmentPurpose, referenced_locations
from .llm.audit import RecordingClient
from .llm.base import (
    AgentRole,
    LLMClient,
    MalformedResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderRefused,
    RetryingClient,
    sanitize_provider_message,
)
from .llm.prompts import current_prompt_versions, resolve_prompt
from .models import (
    FIXED_COLUMNS,
    STRUCTURAL_COLUMNS,
    ArtifactKind,
    AttemptOutcome,
    CellEdit,
    ClaimOutcome,
    ColumnKey,
    CurationJob,
    FailureReason,
    Issue,
    IssueCategory,
    IssueSource,
    IssueState,
    JobState,
    ParsedWorkbook,
    Patch,
    ProblemBlock,
    RejectionCode,
    RepairAttempt,
    ReviewDecision,
    ReviewVerdict,
    ReviewerRole,
    SourcePath,
    ValidationFinding,
)
from .orchestrator import JobCorrupted, apply_patch, failure_for, recover_job
from .persistence import (
    ConcurrencyError,
    Database,
    assert_lease_held,
    blocks_done,
    count_events,
    describe_artifacts,
    get_job,
    increment_counters,
    insert_attempt,
    insert_issue,
    insert_patch,
    insert_verdict,
    list_changes,
    list_artifacts,
    list_claim_results,
    list_issues,
    load_instruction_segments,
    load_ledger,
    load_job_settings,
    load_private_blobs,
    load_prompt_pins,
    load_prompt_versions,
    pin_job_settings,
    pin_prompt_versions,
    save_private_blob,
    mark_block_done,
    next_attempt_number,
    record_artifact,
    record_claim_result,
    record_event,
    record_findings,
    rediscovery_counts,
    save_issue,
    settle_attempt,
    token_usage,
    transition_job,
)
from .reporting.ledger import (
    actionable,
    dedupe,
    fingerprint,
    issue_from_finding,
    unresolved,
)
from .reporting.reports import build_reports, render_markdown
from .state_machine import AttemptsExhausted, IssueMachine, advance_issue
from .validation.final_gate import run_final_gate
from .validation.patch_gate import (
    rejection_consumes_attempt,
    simulate_block,
    target_findings,
    validate_patch,
)
from .validation.rules import run_rules
from .workbook.diff import net_changes
from .workbook.reader import read_workbook, render_cell
from .workbook.writer import (
    EditRejected,
    WorkingCopy,
    create_working_copy,
    sha256_of,
)

#: States an issue can be picked up from for another Writer call.
_NEEDS_WRITER = frozenset(
    {
        IssueState.OPEN,
        IssueState.AWAITING_PATCH,
        IssueState.REVISION_REQUESTED,
        IssueState.PATCH_REJECTED,
    }
)
_NEEDS_APPLY = frozenset({IssueState.PATCH_APPROVED, IssueState.APPLYING})
_NEEDS_REVIEW = frozenset(
    {IssueState.PATCH_PROPOSED, IssueState.PATCH_APPLIED, IssueState.AWAITING_REVIEW}
)
LIVE_ISSUE_STATES = _NEEDS_WRITER | _NEEDS_APPLY | _NEEDS_REVIEW

#: Findings that explain several downstream symptoms in the same block. Repairing the
#: displaced/mis-typed row first lets the ordinary rule recheck supersede the answer,
#: dependency and choice errors it caused. The adversarial run did the reverse, spending
#: eleven Writer calls on symptoms before eventually applying the one root repair.
_ROOT_FINDING_CODES = frozenset(
    {
        "ROW_SHIFT_RIGHT",
        "COLUMN_SHIFT",
        "BLOCK_BOUNDARY_DISAGREEMENT",
        "PROBLEM_NAME_MISMATCH_IN_BLOCK",
        "MISSING_PROBLEM_NAME",
        "ROW_HAS_FORBIDDEN_CONTENT",
        "PROBLEM_ROW_HAS_GRADED_CONTENT",
    }
)

#: The durable event kind counted against `provider_failure_budget`.
PROVIDER_FAILURE_EVENT = "provider_failure"

#: How an adjudication maps onto the verdict vocabulary. `undecided` is `UNRESOLVED` and
#: not `HUMAN_REVIEW`: nobody failed at anything, and no repair was attempted -- two
#: audits read one block differently and the question is still open.
_ADJUDICATIONS = {
    "defect_confirmed": ReviewDecision.REVISE,
    "content_correct": ReviewDecision.ACCEPT,
    "undecided": ReviewDecision.UNRESOLVED,
}

#: Categories that authorise an edit to a structural column.
_STRUCTURAL_CATEGORIES = frozenset(
    {IssueCategory.STRUCTURE, IssueCategory.ROW_TYPE, IssueCategory.DEPENDENCY}
)

_COLUMN_BY_INDEX = {index: key for key, index in FIXED_COLUMNS.items()}


def _targets_are_structural(
    cells: Sequence[tuple[int, int]], category: IssueCategory
) -> bool:
    """Whether an issue over these cells may edit a structural column.

    Mirrors `ledger._is_structural`, on already-resolved coordinates rather than a
    finding. The column test comes first and the declared category second, for the reason
    the auditor prompt was rewritten: a defect *in* `answerType` is structural whatever
    the agent chose to call it, and an agent that classifies its own finding as structural
    is telling the gate something it cannot derive from the coordinates alone.
    """
    if category in _STRUCTURAL_CATEGORIES:
        return True
    return any(
        _COLUMN_BY_INDEX.get(column) in STRUCTURAL_COLUMNS for _, column in cells
    )


def _blind_reviewer_role(issue: Issue) -> ReviewerRole:
    """Which role's verdict a claim-blind audit of this issue is recorded under.

    The opposite audit role, so an agent never certifies its own unsupported claim:
    Initial-Auditor findings are checked by the Independent Reviewer and vice versa. It is
    also the key that separates the blind verdict from the adjudicator's, both of which
    live at attempt zero.
    """
    return (
        ReviewerRole.KNOWN_ISSUE_REVIEWER
        if issue.source is IssueSource.INDEPENDENT_REVIEWER
        else ReviewerRole.INDEPENDENT_REVIEWER
    )


def _finding_targets(finding: ValidationFinding) -> frozenset[tuple[int, int]]:
    """The exact cells a model finding says must change."""
    detailed = finding.detail.get("cells") or ()
    if detailed:
        return frozenset((int(row), int(column)) for row, column in detailed)
    if finding.row is not None and finding.column is not None:
        return frozenset({(finding.row, finding.column)})
    return frozenset()


class Corroboration(StrEnum):
    """How a claim-blind audit's findings stand in relation to the claim under test."""

    #: The same repair targets under the same category. Independent agreement, and the
    #: only outcome that reaches the Writer without anyone looking at both claims.
    EXACT = "exact"
    #: Both audits point at the same row of the same block and describe it differently --
    #: different columns, a wider or narrower target set, a different category. That is
    #: agreement that something is wrong and disagreement about what, which is a question
    #: with an answer rather than a reason to drop the claim.
    RELATED = "related"
    #: The second audit reported nothing touching these rows.
    SILENT = "silent"


def _classify_corroboration(
    issue: Issue, findings: Sequence[ValidationFinding]
) -> tuple[Corroboration, tuple[ValidationFinding, ...]]:
    """Say how a from-scratch audit relates to a model-only claim. Never decide.

    Prose similarity is deliberately irrelevant: asking another model whether an
    accusation *sounds like* the first accusation is another form of anchoring. So the
    comparison is over exact repair targets, and it now has three answers instead of two.

    **The two-answer version was wrong in a way that cost real defects.** Anything short
    of an exact match was recorded as a refutation, and on two held-out workbooks that
    discarded three defects the first audit had correctly found -- among them an Answer
    and its `answerType` on one row, which the second audit reported as one finding over
    both cells where the first had named only the Answer. Two audits agreeing that row 16
    is broken is not evidence that row 16 is fine.

    Row overlap, not cell overlap, is the `RELATED` test, and deliberately so. The most
    expensive detection failure on the first real workbook was a finding that cited the
    cell where a mismatch was *visible* (`Answer`) instead of the cell that had to change
    (`answerType`) -- one row, two columns, no cell in common. A category filter on top
    would rebuild exactly the brittleness being removed, since disagreeing about the
    category *is* one of the things two audits disagree about.
    """
    wanted = frozenset(issue.cells)
    wanted_rows = {row for row, _ in wanted}
    exact: list[ValidationFinding] = []
    related: list[ValidationFinding] = []

    for finding in findings:
        targets = _finding_targets(finding)
        if not targets:
            continue
        try:
            category: IssueCategory | None = IssueCategory(
                str(finding.detail.get("category", ""))
            )
        except ValueError:
            category = None
        if wanted and category is issue.category and targets == wanted:
            exact.append(finding)
        elif wanted_rows & {row for row, _ in targets}:
            related.append(finding)

    if exact:
        return Corroboration.EXACT, tuple(exact)
    if related:
        return Corroboration.RELATED, tuple(related)
    return Corroboration.SILENT, ()

#: The round number the final gate's findings are stored under. Deliberately above any
#: repair round: it is what the workbook looked like when it was handed over, which is the
#: only answer to "is this file finished" that is still true afterwards.
FINAL_GATE_ROUND = 1_000

#: Bumped when the CLI adapter's invocation changes in a way that could alter results --
#: a flag added or removed, the envelope read differently.
#:
#: Pinned per job and **compared on every resume**. A pinned version nothing checks is a
#: note in a drawer: the job would carry on under a different adapter from the one its
#: first half ran under, and the record would say otherwise. Unlike the batch size or the
#: model, this one cannot be re-applied -- the running code is the running code -- so the
#: only safe answer to a mismatch is to stop.
#:
#: There is deliberately no output-limit formula version beside it. One was pinned and
#: recorded for a while with no output limit anywhere in the adapter to govern; metadata
#: describing a mechanism that does not exist is worse than no metadata, because it is
#: read as evidence that the mechanism does. The CLI at 2.1.219 offers no output-token
#: ceiling (`--max-thinking-tokens` and `--task-budget` are different things), so the
#: honest record is silence.
#: 2 (2026-08-16): `--max-turns` went from a hardcoded 1 to `COUNCIL_CLAUDE_MAX_TURNS`,
#: defaulting to 2, after 1 was measured to abort correct calls before their structured
#: output arrived. That changes what every call carries, which is exactly what this
#: constant exists to record -- a job that started under 1 must not finish under 2.
CLI_ADAPTER_VERSION = 2

#: Version of orchestration and user-payload construction that changes what agents see or
#: when workbook bytes are committed. Prompt files are pinned separately; this covers the
#: Python-side instructions, context sections, review coverage and patch lifecycle.
#: 4 (2026-08-29): a claim-blind audit that does not reproduce a claim no longer refutes
#: it. The outcome is classified rather than decided, an adjudicator settles what is left,
#: and an unsettled claim ends `UNCONFIRMED` instead of `REFUTED`. That is a fifth agent,
#: a second call on some issues, and a different terminal state -- a job cannot run half
#: under each reading and still be described as one run.
PIPELINE_CONTRACT_VERSION = 4


class BudgetExhausted(Exception):
    """A global fuse blew. Always terminal, never retried."""


class JobSettingsMigrationRequired(Exception):
    """This job was pinned to an adapter version the running code is not.

    Deployments happen mid-job, and the pinned settings exist precisely so a resumed job
    keeps the behaviour it started under. Batch size and model can simply be re-applied.
    An adapter version cannot: the code that would honour it has been replaced. Carrying
    on regardless would mean a job whose first half ran under one set of flags and whose
    second half ran under another, with its own record insisting they were the same.
    """


class JobDeadlineExceeded(Exception):
    """The job ran past its wall-clock ceiling.

    Distinct from the step and call fuses, which bound *work*. This bounds *time*, and it
    is the only brake that catches a job whose worker is alive, within budget, and simply
    not getting anywhere -- a provider degraded to one call a minute, say.
    """


def _adapter_mismatch(pinned: dict[str, object]) -> str | None:
    """The message to fail with, or `None` when the pin matches the running code.

    A job pinned before this key existed is not a mismatch -- there is nothing to
    disagree with, and treating absence as disagreement would fail every job that was
    in flight across this very deployment.
    """
    recorded = pinned.get("cli_adapter_version")
    if recorded is not None and int(recorded) != CLI_ADAPTER_VERSION:
        return (
            f"this job was pinned to CLI adapter version {int(recorded)} and this process "
            f"is running version {CLI_ADAPTER_VERSION}. The adapter decides what flags each "
            "model call carries, so continuing would finish the job under different "
            "instructions from the ones it started under. Resume it on a process running "
            f"adapter {int(recorded)}, or submit the workbook again."
        )

    pipeline = pinned.get("pipeline_contract_version")
    if pipeline is not None and int(pipeline) != PIPELINE_CONTRACT_VERSION:
        return (
            f"this job was pinned to pipeline contract version {int(pipeline)} and this "
            f"process is running version {PIPELINE_CONTRACT_VERSION}. The contract decides "
            "which blocks and context each agent sees and when an approved patch is written. "
            "Resume it on the original version, or submit the workbook again."
        )
    return None


def _prompt_mismatch(pins: dict[str, tuple[int, str]]) -> str | None:
    """Refuse a same-version prompt whose composed bytes changed on disk.

    The database always stored the hash but the runner previously loaded only the version,
    so editing a shared prompt fragment changed in-flight jobs while their records still
    claimed they were pinned. The hash is now an enforced premise, not decorative audit
    metadata.
    """
    for role_name, (version, expected_sha) in pins.items():
        try:
            resolved = resolve_prompt(AgentRole(role_name), version)
        except Exception as error:
            return (
                f"the pinned {role_name} prompt v{version} cannot be loaded: {error}. "
                "Restore the pinned prompt files or submit the workbook again."
            )
        if resolved.sha256 != expected_sha:
            return (
                f"the pinned {role_name} prompt v{version} has changed on disk "
                f"({expected_sha[:12]} expected, {resolved.sha256[:12]} found). Prompt "
                "content must receive a new version; restore the original bytes or "
                "submit the workbook again."
            )
    return None


@dataclass(frozen=True)
class StepOutcome:
    did_work: bool
    description: str
    state: JobState


@dataclass
class Rediscovery:
    """What a validation round did with its findings.

    Kept as four separate counts rather than one total because they mean different
    things: `reopened` is the council getting another go at a repair that did not hold,
    while `absorbed` is a defect that has now defeated the council and is on its way to a
    person. Collapsing them would make the report unable to tell a curator which happened.
    """

    opened: int = 0
    reopened: int = 0
    absorbed: int = 0
    still_open: int = 0

    @property
    def needs_another_round(self) -> bool:
        return bool(self.opened or self.reopened)


class CurationCouncil:
    """Drives one job. Stateless between steps except for what is on disk."""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        client: LLMClient,
        job_id: str,
        copy: WorkingCopy,
        worker_id: str = "worker",
        run_epoch: int | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.job_id = job_id
        self.copy = copy
        self.worker_id = worker_id
        # **Pinned at construction, and never re-read.** This is the whole fencing
        # mechanism: every write carries the epoch this worker started with, so once
        # another worker steals the lease and bumps the epoch, this one's writes match
        # zero rows and it stops. Reading `self.job.run_epoch` at each write instead --
        # which is what this used to do -- always agrees with itself and fences nothing.
        self.run_epoch = get_job(db, job_id).run_epoch if run_epoch is None else run_epoch
        self.started_at = datetime.now(timezone.utc)
        self.machine = IssueMachine(
            max_attempts=settings.max_repair_attempts,
            interrupted_retry_budget=settings.interrupted_retry_budget,
        )
        # The prompt versions this job is pinned to. Pinned on the first step that runs
        # and read from the database thereafter, so deploying `writer.v2.md` mid-job does
        # not mean attempt one was made under one set of instructions and attempt two
        # under another with nothing in the record to say so.
        self.prompt_versions = load_prompt_versions(db, job_id)
        self._prompt_migration = _prompt_mismatch(load_prompt_pins(db, job_id))

        # A resumed job must run under the settings it *started* with, from its very first
        # step -- not from the step where pinning happens to re-run. Loaded here so a
        # second worker picks up the same batch size and effort the first one used.
        self.settings = self._settings_from_pins()

        # One registry per job. Two jobs share no reasoning, and a global one would make
        # a different job's rationale a false positive here.
        #
        # **Rebuilt from `private_blobs`, not started empty.** A registry that lives only
        # in the worker is a guarantee that ends at the first crash: the Writer's
        # rationale from before it is still on the patch, and a resumed job with an empty
        # registry would pass every check it was asked to make while handing that
        # rationale to a reviewer.
        self.taint = TaintRegistry.rebuilt(
            (blob["label"] or f"{blob['role']}.{blob['blob_id'][:8]}", blob["text"])
            for blob in load_private_blobs(db, job_id)
        )

        # Wrapped last, so every agent gets the audit trail without any of them knowing
        # about it. An audit trail each new agent has to remember to write to is one with
        # invisible holes: a missing row looks exactly like a call never made.
        #
        # The order of the two wrappers is the whole fix to a real hole: retry **outside**
        # recording, recording outside the provider. With the retry inside the provider,
        # one logical call could start four `claude` processes while producing one audit
        # row and one budget charge -- so the trail understated the spend and the budget
        # bounded a quarter of what it named. Every physical invocation now passes through
        # the recorder, and `_charge_call` commits its budget before it starts.
        self.recorder = RecordingClient(
            client,
            db,
            job_id,
            prompt_versions=self.prompt_versions,
            behaviour=load_job_settings(db, job_id),
            before_call=self._charge_call,
        )
        self.client = RetryingClient(
            self.recorder,
            attempts=self.settings.provider_max_attempts,
            backoff_ceiling=self.settings.provider_backoff_ceiling_seconds,
        )
        self._source_parse: ParsedWorkbook | None = None
        # Recovery belongs to the first step of every worker instance, not only to the
        # INGESTING phase. A replacement worker normally resumes the durable phase it
        # inherited; forcing recovery to live in one earlier phase meant it never ran for
        # the mid-repair crashes it exists to repair.
        self._recovery_checked = False

    # -- helpers ----------------------------------------------------------------------

    @property
    def job(self) -> CurationJob:
        return get_job(self.db, self.job_id)

    @property
    def seed_claims(self) -> tuple[initial_auditor.SeedClaim, ...]:
        """The curator's claims, read from the database every time they are needed.

        **Never held in worker memory.** Instructions that live only in the process that
        accepted the upload are instructions a resumed job does not have -- and the
        failure is silent, because a job auditing against zero claims looks exactly like
        a job whose claims were all refuted. A curator who reported a defect would be
        told it was checked and was not there.
        """
        claims = []
        for row in load_instruction_segments(self.db, self.job_id):
            if row.get("purpose") != SegmentPurpose.ERRATA:
                continue
            names, rows = referenced_locations(row["text"])
            claims.append(
                initial_auditor.SeedClaim(
                    index=row["segment_index"],
                    text=row["text"],
                    provenance=row["provenance"],
                    problem_names=names,
                    rows=rows,
                )
            )
        return tuple(claims)

    @property
    def curator_rules(self) -> tuple[str, ...]:
        """Governing instructions from the curator's document.

        Policy, not hypotheses: these reach the Initial Auditor, Writer and both reviewers,
        who have to *apply* them, and never the claim machinery, which would ask thirty blocks to
        confirm or refute a statement that is true of all of them.

        Every accepted rule passage is returned. Instruction ingestion already enforces
        the document-wide size bound; imposing a smaller hidden cap here caused the API
        to accept a guide and silently omit most of it from every model call.
        """
        return tuple(
            row["text"].strip()
            for row in load_instruction_segments(self.db, self.job_id)
            if row.get("purpose") == SegmentPurpose.RULES and row["text"].strip()
        )

    @property
    def curator_notes(self) -> tuple[str, ...]:
        """Background shown only to the Initial Auditor.

        Notes are evidence that can help interpret a block, but they are neither defect
        claims to confirm/refute nor policy that authorises a change. The auditor may use
        them while independently deciding whether a concrete finding exists; downstream
        agents receive the resulting issue, not the background prose.
        """
        return tuple(
            row["text"].strip()
            for row in load_instruction_segments(self.db, self.job_id)
            if row.get("purpose") == SegmentPurpose.NOTES and row["text"].strip()
        )

    def source_workbook(self) -> ParsedWorkbook:
        """The workbook as submitted. Parsed once: the source cannot change."""
        if self._source_parse is None:
            self._source_parse = read_workbook(Path(self.copy.source))
        return self._source_parse

    def current_workbook(self) -> ParsedWorkbook:
        """The working copy as it stands now. Re-read every time, deliberately.

        Caching this would mean a step reasoning about a block an earlier step has since
        changed, which is precisely the stale-patch failure the `before` check exists to
        catch. Re-reading is cheap next to a model call.
        """
        return read_workbook(self.copy.path)

    def _spend(self, *, steps: int = 1) -> None:
        job = self.job
        if job.steps_used + steps > self.settings.step_budget:
            raise BudgetExhausted(
                f"job exceeded its step budget of {self.settings.step_budget}"
            )
        increment_counters(
            self.db, self.job_id, run_epoch=self.run_epoch, steps=steps
        )

    def _charge_call(self) -> None:
        """One unit of the model-call budget, committed before the process starts.

        Hung on the recorder rather than called from each phase, for the reason the
        recorder itself is a wrapper: a budget each caller has to remember to charge is a
        budget with holes, and the holes are invisible. It also means the thing counted is
        the thing that costs -- a physical `claude` invocation -- rather than a phase's
        intention to make one. A retried outage and a schema retry each cost a real call,
        and both used to be free.
        """
        if self.job.llm_calls_used + 1 > self.settings.llm_call_budget:
            raise BudgetExhausted(
                f"job exceeded its model-call budget of {self.settings.llm_call_budget}"
            )
        increment_counters(
            self.db, self.job_id, run_epoch=self.run_epoch, llm_calls=1
        )

    def _advance(self, target: JobState, reason: FailureReason | None = None) -> None:
        transition_job(
            self.db, self.job_id, target, run_epoch=self.run_epoch, failure_reason=reason
        )

    def _note_rediscovery(self, kind: str, issue: Issue, finding: ValidationFinding) -> None:
        """Write down that a closed issue's defect came back.

        A count the report can show, rather than a state change a reader has to infer.
        "Repaired, rediscovered, repaired again" and "repaired, rediscovered, gave up" end
        in visibly different places, but nothing in the final row says the rediscovery
        happened at all -- and that is the part a curator most needs to know about.
        """
        record_event(
            self.db,
            self.job_id,
            kind,
            f"{issue.issue_id} ({finding.code} at row {finding.row}) in "
            f"{issue.problem_name} after {issue.attempts_used} attempt(s)",
        )

    def _record_gate_findings(self, gate) -> None:
        """Store the final gate's verdict where the API can read it.

        Written under `FINAL_GATE_ROUND` rather than a repair round, so `latest_findings`
        returns *this* -- the state of the workbook as it was handed over -- rather than
        whatever the last repair round happened to see before its repairs were applied.
        """
        for kind, findings in (
            ("content", gate.content_findings),
            ("integrity", gate.integrity_findings),
        ):
            deduped = dedupe(tuple(findings))
            record_findings(
                self.db,
                self.job_id,
                FINAL_GATE_ROUND,
                deduped,
                [fingerprint(f) for f in deduped],
                kind=kind,
            )

    def _take_batch(
        self, parsed: ParsedWorkbook, pending: Sequence[ProblemBlock]
    ) -> list[ProblemBlock]:
        """The next blocks to scan in one call, bounded by count *and* by size.

        Two limits because they bound different things. `scan_batch_size` bounds how many
        verdicts one response has to carry; `scan_batch_max_characters` bounds how much
        context one call has to hold, which a count cannot -- a five-row block and a
        hundred-row block are not the same allowance.

        **At least one block always goes**, even one that exceeds the size cap by itself.
        Otherwise an oversized first block is never dispatched, never marked done, and the
        phase requeues it forever: a livelock created by a safety limit, which is worse
        than the limit not existing.
        """
        limit = max(1, self.settings.scan_batch_size)
        budget = self.settings.scan_batch_max_characters

        batch: list[ProblemBlock] = []
        # These sections occur once per request, not once per block, but they still count
        # toward the request-size promise. Counting zero here let a long policy or
        # background document bypass the cap while every per-block calculation looked
        # correct. Notes are present only in the initial audit; including them during the
        # independent sweep is a conservative bound, not transmitted data.
        used = sum(len(rule) for rule in self.curator_rules)
        used += sum(len(note) for note in self.curator_notes)
        for block in pending[:limit]:
            cost = self._batch_cost(parsed, block)
            if batch and used + cost > budget:
                break
            batch.append(block)
            used += cost
        return batch

    def _batch_cost(self, parsed: ParsedWorkbook, block: ProblemBlock) -> int:
        """A block's full contribution to a batched payload.

        The rendered block *plus* its applicable claims and its deterministic findings --
        not `render_block` alone, or a block with forty findings slips under a cap it
        dominates.
        """
        from .agents.rendering import render_block, render_findings

        cost = len(render_block(block))
        cost += len(render_findings(self._findings_for_block(parsed, block)))
        cost += sum(
            len(claim.text) for claim in self.seed_claims if claim.applies_to(block)
        )
        # Labels, headers and fencing per section. Small, fixed, and counted so the cap
        # means what it says.
        return cost + 200

    def _pin_prompts(self) -> None:
        """Fix this job's prompt versions *and* its behaviour settings, once.

        `INSERT OR IGNORE` for both, so a resumed job keeps what it started with rather
        than adopting whatever is on disk or in the environment now. Recording them per
        call would be auditing; pinning them is what stops a worker restarted after an
        `.env` edit from giving one job half its blocks at batch 1 and the other half at
        batch 10 -- a run the report would then describe as a single coherent thing.
        """
        pin_prompt_versions(self.db, self.job_id, current_prompt_versions())
        self.prompt_versions = load_prompt_versions(self.db, self.job_id)
        self._prompt_migration = _prompt_mismatch(
            load_prompt_pins(self.db, self.job_id)
        )
        self.client.prompt_versions = self.prompt_versions

        pin_job_settings(self.db, self.job_id, self._behaviour_settings())
        self.settings = self._settings_from_pins()
        self.client.behaviour = load_job_settings(self.db, self.job_id)

    def _behaviour_settings(self) -> dict[str, object]:
        """What must not change under a running job.

        Two kinds of entry, and they are enforced differently. The first four are
        *re-applied* on resume, which is enforcement enough: whatever the environment says
        now, the job runs at the batch size and model it started with. `cli_adapter_version`
        cannot be re-applied -- it names the code, and the code is whatever was deployed --
        so it is **compared** instead, and a mismatch stops the job.
        """
        return {
            "scan_batch_size": self.settings.scan_batch_size,
            "scan_batch_max_characters": self.settings.scan_batch_max_characters,
            "role_effort": {
                role.value: self.settings.effort_for(role.value) for role in AgentRole
            },
            "model": self.settings.claude_model,
            "cli_adapter_version": CLI_ADAPTER_VERSION,
            "pipeline_contract_version": PIPELINE_CONTRACT_VERSION,
        }

    def _settings_from_pins(self) -> Settings:
        """Re-read the settings this job is pinned to, overriding the live ones.

        The version check is recorded rather than raised, because this runs in the
        constructor and a worker that cannot build a council cannot mark the job failed --
        it would raise a traceback into the pool and leave the job sitting at a state
        nobody moves. `step()` asks, and fails the job properly.
        """
        pinned = load_job_settings(self.db, self.job_id)
        self._settings_migration = _adapter_mismatch(pinned)
        if not pinned:
            return self.settings
        return replace(
            self.settings,
            scan_batch_size=int(
                pinned.get("scan_batch_size", self.settings.scan_batch_size)
            ),
            scan_batch_max_characters=int(
                pinned.get(
                    "scan_batch_max_characters", self.settings.scan_batch_max_characters
                )
            ),
            role_effort=pinned.get("role_effort") or self.settings.role_effort,
            claude_model=str(pinned.get("model", self.settings.claude_model)),
        )

    def _check_pinned_adapter(self) -> None:
        """Refuse to continue under changed invocation, context, or prompt bytes."""
        mismatch = self._settings_migration or self._prompt_migration
        if mismatch:
            raise JobSettingsMigrationRequired(mismatch)

    def _prompt_version(self, role: AgentRole) -> int | None:
        return self.prompt_versions.get(role.value)

    def _keep_private(self, role: AgentRole, label: str, model, issue_id=None) -> None:
        """Register reasoning with the taint registry *and* write it down.

        Both, always, and in that order. The registry is what stops the text reaching a
        reviewer in this process; the row is what stops it reaching one in the next.
        """
        self.taint.register_model(label, model)
        for field, value in model.model_dump().items():
            if isinstance(value, str) and value.strip():
                save_private_blob(
                    self.db,
                    self.job_id,
                    role=role.value,
                    label=f"{label}.{field}",
                    text=value,
                    issue_id=issue_id,
                )

    def _check_deadline(self) -> None:
        """Bound this *run*, not the job's whole existence.

        Anchoring to `created_at` was the first reading and it is a trap: a job that timed
        out would fail again the instant anyone resumed it, since the age that tripped the
        ceiling only grows. Anchoring to when this worker started bounds the thing that
        actually needs bounding -- a worker holding a lease and getting nowhere -- and
        gives a resumed job a fresh window, which is right, because somebody decided to
        resume it.
        """
        limit = self.settings.run_deadline_seconds
        if limit <= 0:
            return
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        if elapsed > limit:
            raise JobDeadlineExceeded(
                f"this run has been going for {int(elapsed)}s, past its "
                f"{int(limit)}s ceiling"
            )

    # -- the loop ---------------------------------------------------------------------

    def run(self, *, max_steps: int | None = None, should_stop=None) -> CurationJob:
        """Drain steps until the job reaches a terminal state.

        `should_stop` is the cooperative half of shutdown. A pool that cancels futures can
        only cancel the ones that have not started; a worker already inside a step has to
        be asked, and asking it between steps is exactly right because the step contract
        makes every boundary a consistent point.

        Losing the lease is not a failure of the job -- somebody else owns it now, and the
        correct behaviour is to stop touching it and say so, not to mark it failed and
        overwrite the new owner's work.
        """
        taken = 0
        while not self.job.is_terminal:
            if max_steps is not None and taken >= max_steps:
                break
            if should_stop is not None and should_stop():
                record_event(self.db, self.job_id, "worker_stopped", "shutdown requested")
                break
            try:
                self.step()
            except ConcurrencyError as error:
                record_event(self.db, self.job_id, "lease_lost", str(error))
                break
            taken += 1
        return self.job

    def step(self) -> StepOutcome:
        """One unit of progress. See the module docstring for the contract."""
        job = self.job
        try:
            self._check_pinned_adapter()
            self._check_deadline()
            if not self._recovery_checked:
                recovery = recover_job(
                    self.db,
                    self.job,
                    self.copy,
                    self.machine,
                    list_issues(self.db, self.job_id),
                )
                self._recovery_checked = True
                if recovery.did_anything:
                    return StepOutcome(
                        True, f"recovered interrupted work: {recovery.describe()}", job.state
                    )
            return self._dispatch(job)
        except JobSettingsMigrationRequired as error:
            # `CONFIG`, and therefore non-resumable: resuming runs the same code against
            # the same pin and stops in the same place. A person decides what to do --
            # roll the deployment back, or start the workbook again under the new adapter.
            record_event(self.db, self.job_id, "settings_migration_required", str(error))
            self._advance(JobState.FAILED, FailureReason.CONFIG)
            return StepOutcome(False, str(error), self.job.state)
        except JobDeadlineExceeded as error:
            record_event(self.db, self.job_id, "deadline_exceeded", str(error))
            self._advance(JobState.FAILED, FailureReason.TIMEOUT)
            return StepOutcome(False, str(error), self.job.state)
        except BudgetExhausted as error:
            record_event(self.db, self.job_id, "budget_exhausted", str(error))
            self._advance(JobState.FAILED, FailureReason.BUDGET_EXHAUSTED)
            return StepOutcome(False, str(error), self.job.state)
        except ProviderConfigurationError as error:
            # Caught **before** the general provider clause, because it is a subclass of
            # it and the two need opposite handling. Handled here rather than in `_write`
            # so it covers all four agents: the auditor and the reviewers make the same
            # call against the same settings, and a key that is wrong for one is wrong for
            # every one of them. Retrying is a loop that spends the whole job budget to
            # arrive at the same message, so the job fails as CONFIG -- non-resumable
            # until somebody changes the settings.
            record_event(self.db, self.job_id, "provider_misconfigured", str(error))
            self._advance(JobState.FAILED, FailureReason.CONFIG)
            return StepOutcome(False, str(error), self.job.state)
        except (ProviderError, MalformedResponse) as error:
            # Every agent's calls land here, not just the Writer's. The auditor and both
            # reviewers call the same provider with the same settings, and an outage that
            # interrupts one interrupts all four -- so the accounting has to be in one
            # place or three quarters of the failures go unbudgeted. `_write` handles the
            # cases it can do something better with -- a refusal escalates the issue, an
            # outage refunds the attempt -- and only what it re-raises reaches here.
            return self._provider_failed(error)
        except ContextIsolationError as error:
            # Never a warning and never a retry: the same tainted payload would be sent
            # again, and a leaked review would still count as a review.
            record_event(self.db, self.job_id, "isolation_violation", str(error))
            self._advance(JobState.FAILED, FailureReason.ISOLATION_VIOLATION)
            return StepOutcome(False, str(error), self.job.state)
        except JobCorrupted as error:
            record_event(self.db, self.job_id, "corruption", str(error))
            self._advance(JobState.FAILED, failure_for(error))
            return StepOutcome(False, str(error), self.job.state)
        finally:
            # Drained whatever happened, including on the paths that fail the job: a
            # suspicion raised by the call that preceded a failure is exactly the one
            # somebody investigating will want, and it is the one an early return would
            # discard.
            self._flush_suspicions()

    def _flush_suspicions(self) -> None:
        """Write non-fatal isolation overlaps to `job_events`.

        Without this the guarantee was a claim and not a fact: `TaintRegistry.suspicions`
        is an in-memory list, so "recorded for a human to read" lasted exactly as long as
        the worker process and nothing outside the tests ever read it.

        **The event carries the label, the role and the length of the overlap, and not one
        word of the text.** What is being described is a span shared between private
        reasoning and an outgoing payload; writing the span itself into the audit trail
        would put the suspected leak in a second, more durable place -- and `job_events`
        is rendered into reports.
        """
        drained, suppressed = self.taint.drain_suspicions()
        if not drained and not suppressed:
            return
        for suspicion in drained:
            record_event(
                self.db,
                self.job_id,
                "isolation_suspicion",
                f"{suspicion.context} shares a {suspicion.shared_tokens}-token span with "
                f"private record {suspicion.label!r}. Not proof of a leak: two agents "
                "describing one defect produce one sentence. Worth a look.",
            )
        if suppressed:
            record_event(
                self.db,
                self.job_id,
                "isolation_suspicion",
                f"{suppressed} further overlap(s) matched an already-recorded "
                "(record, role) pair or passed the per-job cap.",
            )

    def _provider_failed(self, error: Exception) -> StepOutcome:
        """Record one lost model call, and fail the job once too many are lost.

        The step is *not* retried here. Every phase is a queue predicate over durable
        rows, so the work this step was going to do is still queued and the next step
        picks it up -- which means a transient outage costs a step and nothing else, and
        no retry logic has to be written twice.

        The budget is what stops that from being an infinite loop. It is counted from
        durable events rather than an instance attribute, because the failure mode being
        bounded here -- a provider that is down -- routinely takes the worker down with
        it, and a counter that resets on every crash bounds nothing.
        """
        message = sanitize_provider_message(str(error))
        record_event(self.db, self.job_id, PROVIDER_FAILURE_EVENT, message)
        spent = count_events(self.db, self.job_id, PROVIDER_FAILURE_EVENT)

        # A failure that says it will not succeed is taken at its word. Spending the whole
        # budget on it means twelve calls, each retried four times, to reach a conclusion
        # the first one already stated -- which is what the live pilot did against an
        # exhausted quota: four and a half minutes and forty-eight requests to learn that
        # the account had run out twenty calls ago. The budget is for failures that might
        # not repeat.
        if isinstance(error, ProviderError) and not error.retryable:
            self._advance(JobState.FAILED, FailureReason.PROVIDER)
            return StepOutcome(False, f"the provider refused to continue: {message}", self.job.state)

        if spent >= self.settings.provider_failure_budget:
            self._advance(JobState.FAILED, FailureReason.PROVIDER)
            return StepOutcome(
                False,
                f"the provider failed {spent} times: {message}",
                self.job.state,
            )
        return StepOutcome(
            True, f"provider failure {spent}, retrying next step: {message}", self.job.state
        )

    def _dispatch(self, job: CurationJob) -> StepOutcome:
        if job.state is JobState.CREATED:
            self._spend()
            self._advance(JobState.INGESTING)
            return StepOutcome(True, "job accepted", JobState.INGESTING)

        if job.state is JobState.INGESTING:
            return self._ingest()

        if job.state is JobState.AUDITING:
            return self._audit()

        if job.state in (JobState.REPAIRING_KNOWN, JobState.REPAIRING_VALIDATION):
            return self._repair(job.state)

        if job.state is JobState.INDEPENDENT_REVIEW:
            return self._independent_review()

        if job.state is JobState.FINAL_VALIDATION:
            return self._validate()

        if job.state is JobState.FINALIZING:
            return self._finalize()

        return StepOutcome(False, f"nothing to do in {job.state}", job.state)

    # -- phases -----------------------------------------------------------------------

    def _ingest(self) -> StepOutcome:
        """Recover anything a crash left behind, then open the deterministic issues."""
        self._spend()
        self._pin_prompts()
        recover_job(
            self.db, self.job, self.copy, self.machine, list_issues(self.db, self.job_id)
        )

        self._verify_instructions()

        parsed = self.current_workbook()
        findings = dedupe(tuple(run_rules(parsed)) + parsed.all_findings)
        opened = self._open_issues(findings, source=IssueSource.INITIAL_AUDITOR)

        record_findings(
            self.db, self.job_id, 0, findings, [fingerprint(f) for f in findings]
        )
        self._advance(JobState.AUDITING)
        return StepOutcome(
            True, f"deterministic pass opened {opened} issue(s)", JobState.AUDITING
        )

    def _audit(self) -> StepOutcome:
        """One block per step, so the phase is drainable and interruptible."""
        parsed = self.current_workbook()
        done = blocks_done(self.db, self.job_id, "audited")
        pending = [b for b in parsed.blocks if b.block_id not in done]

        if not pending:
            self._spend()
            self._advance(JobState.REPAIRING_KNOWN)
            return StepOutcome(True, "audit complete", JobState.REPAIRING_KNOWN)

        batch = self._take_batch(parsed, pending)
        self._spend()
        results, requeued = initial_auditor.audit_blocks(
            self.client,
            blocks=batch,
            conventions=parsed.conventions,
            findings_for=lambda block: self._findings_for_block(parsed, block),
            seed_claims=self.seed_claims,
            curator_rules=self.curator_rules,
            curator_notes=self.curator_notes,
            job_id=self.job_id,
            taint=self.taint,
            prompt_version=self._prompt_version(AgentRole.INITIAL_AUDITOR),
        )

        by_id = {block.block_id: block for block in batch}
        opened = 0
        for result in results:
            block = by_id[result.block_id]
            # **One block persisted completely, then marked done, then the next.** A crash
            # between two blocks of a batch must leave the finished ones finished and the
            # rest queued -- so the mark is the last write for each block, never a single
            # sweep at the end of the batch.
            self._keep_private(
                AgentRole.INITIAL_AUDITOR, f"auditor.{block.block_id}", result.private
            )
            opened += self._open_issues(
                result.findings, source=IssueSource.INITIAL_AUDITOR
            )
            self._record_claim_verdicts(block.block_id, result)
            mark_block_done(self.db, self.job_id, block.block_id, "audited")

        if requeued:
            record_event(
                self.db, self.job_id, "blocks_requeued",
                f"{len(requeued)} block(s) came back unattributable from an audit batch",
            )
        return StepOutcome(
            True,
            f"audited {len(results)} block(s): {opened} issue(s) opened"
            + (f", {len(requeued)} requeued" if requeued else ""),
            JobState.AUDITING,
        )

    def _independent_review(self) -> StepOutcome:
        """Sweep every current block from scratch, then repair what it finds."""
        parsed = self.current_workbook()
        done = blocks_done(self.db, self.job_id, "swept")

        pending = [
            block
            for block in independent_reviewer.blocks_to_sweep(parsed.blocks)
            if block.block_id not in done
        ]

        if pending:
            batch = self._take_batch(parsed, pending)
            self._spend()
            results, requeued = independent_reviewer.sweep_blocks(
                self.client,
                blocks=batch,
                conventions=parsed.conventions,
                findings_for=lambda block: self._findings_for_block(parsed, block),
                curator_rules=self.curator_rules,
                job_id=self.job_id,
                taint=self.taint,
                prompt_version=self._prompt_version(AgentRole.INDEPENDENT_REVIEWER),
            )
            opened = 0
            for result in results:
                opened += self._open_issues(
                    result.findings,
                    source=IssueSource.INDEPENDENT_REVIEWER,
                    reviewer_role=ReviewerRole.INDEPENDENT_REVIEWER,
                )
                mark_block_done(self.db, self.job_id, result.block_id, "swept")
            if requeued:
                record_event(
                    self.db, self.job_id, "blocks_requeued",
                    f"{len(requeued)} block(s) came back unattributable from a sweep batch",
                )
            return StepOutcome(
                True,
                f"swept {len(results)} block(s): {opened} issue(s)"
                + (f", {len(requeued)} requeued" if requeued else ""),
                JobState.INDEPENDENT_REVIEW,
            )

        issue = self._next_live_issue(ReviewerRole.INDEPENDENT_REVIEWER)
        if issue is not None:
            return self._advance_issue(issue, JobState.INDEPENDENT_REVIEW)

        self._spend()
        self._advance(JobState.FINAL_VALIDATION)
        return StepOutcome(True, "independent review complete", JobState.FINAL_VALIDATION)

    def _repair(self, state: JobState) -> StepOutcome:
        role = (
            ReviewerRole.KNOWN_ISSUE_REVIEWER
            if state is JobState.REPAIRING_KNOWN
            else None
        )
        issue = self._next_live_issue(role)
        if issue is not None:
            return self._advance_issue(issue, state)

        self._spend()
        target = (
            JobState.INDEPENDENT_REVIEW
            if state is JobState.REPAIRING_KNOWN
            else JobState.FINAL_VALIDATION
        )
        self._advance(target)
        return StepOutcome(True, f"{state.value} drained", target)

    def _validate(self) -> StepOutcome:
        """Run the deterministic gate and decide whether another repair round is warranted."""
        self._spend()
        job = self.job
        round_no = job.validation_rounds_used + 1

        gate = run_final_gate(
            source=self.copy.source,
            source_sha256=self.copy.source_sha256,
            output=self.copy.path,
            changes=list_changes(self.db, self.job_id),
        )
        if not gate.passed:
            # Integrity, not content. Nothing a reviewer can decide makes an output that
            # cannot be accounted for acceptable.
            record_event(self.db, self.job_id, "integrity_failed", gate.summary())
            self._advance(JobState.FINALIZING)
            return StepOutcome(True, gate.summary(), JobState.FINALIZING)

        findings = dedupe(gate.content_findings)
        record_findings(
            self.db, self.job_id, round_no, findings, [fingerprint(f) for f in findings]
        )
        increment_counters(
            self.db, self.job_id, run_epoch=job.run_epoch, validation_rounds=1
        )

        found = self._open_validation_issues(findings)
        if found.absorbed:
            # A defect the council already failed to repair three times. Opening it again
            # would hand it a fresh budget and loop; escalating is the honest outcome.
            record_event(
                self.db, self.job_id, "finding_absorbed",
                f"{found.absorbed} finding(s) match issues whose attempts are spent; "
                "each is escalated to human review",
            )
        if found.reopened:
            record_event(
                self.db, self.job_id, "issue_reopened",
                f"{found.reopened} accepted repair(s) did not hold and were reopened",
            )

        if found.needs_another_round and round_no < self.settings.max_validation_rounds:
            self._advance(JobState.REPAIRING_VALIDATION)
            return StepOutcome(
                True,
                f"validation round {round_no}: {found.opened} new issue(s), "
                f"{found.reopened} reopened",
                JobState.REPAIRING_VALIDATION,
            )

        self._advance(JobState.FINALIZING)
        return StepOutcome(
            True, f"validation round {round_no} complete", JobState.FINALIZING
        )

    def _finalize(self) -> StepOutcome:
        """Write the outputs, **then** evaluate the gates.

        This ordering is the mechanical form of "never falsely report success". A job that
        needs a person still hands over the corrected workbook and a report saying plainly
        what is unresolved, because the artefacts exist before anything decides whether to
        call the job successful.
        """
        self._spend()
        job = self.job
        changes = list_changes(self.db, self.job_id)

        gate = run_final_gate(
            source=self.copy.source,
            source_sha256=self.copy.source_sha256,
            output=self.copy.path,
            changes=changes,
        )

        # A terminal issue ledger is history, while this decision is about the artifact
        # being handed back. Another accepted repair can resolve a deterministic issue
        # after that issue exhausted its own attempts. Re-derive those escalations against
        # the final workbook so “needs a person” never points at a defect that is gone.
        self._reconcile_stale_escalations(self.current_workbook())
        ledger = load_ledger(self.db, self.job_id)

        outputs = self.copy.path.parent.parent / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        corrected = outputs / "corrected.xlsx"
        corrected.write_bytes(self.copy.path.read_bytes())
        record_artifact(
            self.db,
            self.job_id,
            ArtifactKind.CORRECTED_WORKBOOK,
            str(corrected),
            data_root=self.settings.data_root,
            # Hashed so a curator can tell the file they downloaded is the file this
            # report is about. An artefact with no hash is a claim about a file nobody
            # can check they are holding.
            sha256=sha256_of(corrected),
        )

        # **Persisted, not just rendered.** These used to exist only inside the markdown
        # report, so `GET /report` answered with zero remaining findings for a job whose
        # own report listed them -- the API quietly reassuring a curator that the file the
        # report says needs work is fine.
        self._record_gate_findings(gate)

        # Three conditions, and all three are load-bearing. Integrity says the output is
        # an accounted-for descendant of the source. The ledger says every claim reached
        # a good end. **The remaining findings say the workbook is actually fixed** --
        # without which a job can close every issue it opened and still hand back a
        # defective workbook marked succeeded, because the defect it never opened an
        # issue for, or opened one and closed it wrongly, is invisible to the other two.
        remaining = unresolved(gate.content_findings)
        succeeded = gate.passed and ledger.all_resolved and not remaining
        final_state = (
            JobState.SUCCEEDED if succeeded else JobState.NEEDS_HUMAN_ATTENTION
        )
        if remaining and gate.passed and ledger.all_resolved:
            record_event(
                self.db, self.job_id, "unresolved_findings",
                f"every issue is resolved but {len(remaining)} finding(s) remain, "
                f"starting with {remaining[0].code} at row {remaining[0].row}",
            )

        reports = build_reports(
            job_id=self.job_id,
            state=final_state,
            ledger=ledger,
            changes=changes,
            verdicts=_verdicts(self.db, self.job_id),
            attempts=_attempts(self.db, self.job_id),
            findings=gate.content_findings,
            integrity_findings=gate.integrity_findings,
            claims=load_instruction_segments(self.db, self.job_id),
            claim_results=list_claim_results(self.db, self.job_id),
            usage=token_usage(self.db, self.job_id),
            artifacts=describe_artifacts(self.db, self.job_id),
            rediscoveries=rediscovery_counts(self.db, self.job_id),
        )
        report_path = outputs / "report.md"
        report_path.write_text(render_markdown(reports), encoding="utf-8")
        record_artifact(
            self.db,
            self.job_id,
            ArtifactKind.VALIDATION_REPORT,
            str(report_path),
            data_root=self.settings.data_root,
            sha256=sha256_of(report_path),
        )

        # `SUCCEEDED` is set here and nowhere else, guarded by every gate.
        self._advance(final_state)
        return StepOutcome(True, reports.validation_report["unresolved_summary"], final_state)

    def _reconcile_stale_escalations(self, parsed: ParsedWorkbook) -> None:
        changes = list_changes(self.db, self.job_id)
        for issue in list_issues(self.db, self.job_id):
            if issue.state is not IssueState.NEEDS_HUMAN_REVIEW or not issue.rule_codes:
                continue
            block = parsed.block_by_id(issue.block_id) if issue.block_id else None
            if block is None:
                continue
            remaining = target_findings(issue, parsed, block)
            if remaining is None or remaining:
                continue
            # Compatibility with jobs that began before rejected-patch rollback existed:
            # a rule may be quiet only because this very issue's rejected bytes are still
            # present. Superseding that issue would launder the unaccepted patch. A real
            # sibling resolution has no surviving net edit owned by the stale issue.
            own_net = net_changes(
                change for change in changes if change.issue_id == issue.issue_id
            )
            if any(change.before != change.after for change in own_net.values()):
                continue
            save_issue(self.db, advance_issue(issue, IssueState.SUPERSEDED))
            record_event(
                self.db,
                self.job_id,
                "stale_escalation_reconciled",
                f"{issue.issue_id}: final workbook no longer has "
                f"{', '.join(issue.rule_codes)} at {issue.problem_name}",
            )

    # -- issue-level steps -------------------------------------------------------------

    def _advance_issue(self, issue: Issue, state: JobState) -> StepOutcome:
        if issue.state in _NEEDS_WRITER:
            return self._write(issue, state)
        if issue.state in _NEEDS_APPLY:
            return self._apply(issue, state)
        return self._review(issue, state)

    def _write(self, issue: Issue, state: JobState) -> StepOutcome:
        if not self.machine.can_attempt(issue):
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, f"{issue.issue_id} exhausted its attempts", state)

        parsed = self.current_workbook()
        block = parsed.block_by_id(issue.block_id) if issue.block_id else None
        if block is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, f"{issue.issue_id} has no block to repair", state)

        # The defect may already be gone -- a repair to a sibling issue in this block can
        # resolve this one on the way past. That is what `SUPERSEDED` is for, and asking
        # the Writer to repair something that is no longer there costs a model call and
        # gets back either an escalation or an invented change. Only decidable for an
        # issue carrying a rule code; a semantic one still goes to the Writer.
        #
        # **Only before this issue's own first attempt.** Once the council has edited for
        # it, the rule not firing is this issue's own work, and a reviewer who then asked
        # for a revision has said the value is wrong even though the rule is satisfied.
        # Superseding there would let a mechanically-clean but incorrect repair close the
        # issue by silencing the reviewer -- the exact failure this whole section exists
        # to prevent.
        remaining = target_findings(issue, parsed, block)
        if issue.attempts_used == 0 and remaining is not None and not remaining:
            save_issue(self.db, advance_issue(issue, IssueState.SUPERSEDED))
            record_event(
                self.db, self.job_id, "issue_superseded",
                f"{issue.issue_id}: {', '.join(issue.rule_codes)} no longer fires in "
                f"{issue.problem_name}",
            )
            return StepOutcome(True, f"{issue.issue_id} no longer applies", state)

        # Exact cleanup belongs to deterministic code, not a probabilistic Writer. The
        # live pilot spent repeated Claude calls on one trailing space and copied the
        # wrong `before` value each time. These two repairs are safe only when their
        # preconditions prove no content is being inferred or discarded.
        mechanical = self._mechanical_patch(issue, parsed, block)
        if issue.state is IssueState.OPEN and issue.attempts_used == 0 and mechanical:
            outcome = self._apply_mechanical_patch(issue, mechanical, state)
            if outcome is not None:
                return outcome

        # A semantic issue cannot be re-derived mechanically, but it may already have
        # been resolved by an earlier accepted repair in the same block. Sending it
        # straight to the Writer is what produced the pilot's false "needs a person"
        # entry for a date-coercion defect whose cell already contained the repaired
        # fraction. Ask the issue's assigned reviewer to judge the current artifact first.
        # A revision still reaches the Writer on the next step, carrying the reviewer's
        # feedback; an acceptance closes this issue as resolved by another repair.
        if (
            issue.state is IssueState.OPEN
            and issue.attempts_used == 0
            and remaining is None
        ):
            return self._review_unpatched_issue(issue, state)

        # Reserved and committed BEFORE the call. Incrementing afterwards would let a
        # crash loop burn unbounded spend against a counter that never moves.
        try:
            issue = self.machine.reserve_attempt(issue)
        except AttemptsExhausted:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, f"{issue.issue_id} exhausted its attempts", state)

        # Moved to AWAITING_PATCH before the call, not after it succeeds. That is what
        # the issue actually is at this moment, and it means a rejection has a legal
        # transition to reach -- an issue still marked OPEN cannot become PATCH_REJECTED.
        if issue.state is not IssueState.AWAITING_PATCH:
            issue = advance_issue(issue, IssueState.AWAITING_PATCH)
        save_issue(self.db, issue)
        attempt = RepairAttempt(
            attempt_id=uuid4().hex,
            issue_id=issue.issue_id,
            attempt_no=next_attempt_number(self.db, issue.issue_id),
        )
        insert_attempt(self.db, attempt)
        self._spend()

        try:
            result = writer.propose_patch(
                self.client,
                issue=issue,
                block=block,
                conventions=parsed.conventions,
                attempt_no=attempt.attempt_no,
                deterministic_findings=self._findings_for_block(parsed, block),
                reviewer_feedback=_latest_feedback(self.db, issue.issue_id),
                curator_rules=self.curator_rules,
                job_id=self.job_id,
                taint=self.taint,
                prompt_version=self._prompt_version(AgentRole.WRITER),
            )
        except ProviderConfigurationError:
            # Not an interrupted attempt. Refunding it would hand the budget back so the
            # same misconfiguration could spend it again; `step` fails the job instead.
            raise
        except ProviderRefused as error:
            # The model declined to answer for this block's content. Not an outage and not
            # a refundable interruption: the same cells will trip the same filter on the
            # next call, so refunding the attempt would buy three identical refusals. It
            # is also not a reason to fail the whole job -- one block a filter dislikes
            # says nothing about the other twenty-nine. The issue goes to a person and the
            # council carries on, which is what escalation is for.
            settle_attempt(
                self.db,
                attempt.model_copy(
                    update={
                        "outcome": AttemptOutcome.ESCALATED,
                        "finished_at": datetime.now(timezone.utc),
                    }
                ),
            )
            save_issue(self.db, self.machine.exhausted(issue))
            record_event(
                self.db,
                self.job_id,
                "provider_refused",
                f"{issue.issue_id}: {sanitize_provider_message(str(error))}",
            )
            return StepOutcome(
                True, f"{issue.issue_id} escalated: the model declined to answer", state
            )
        except (ProviderError, MalformedResponse, writer.WriterProposedNothing) as error:
            # Infrastructure or an unusable response. The attempt is refunded within the
            # bounded budget, because neither is the Writer failing at the task.
            settle_attempt(
                self.db,
                attempt.model_copy(
                    update={
                        "outcome": AttemptOutcome.INTERRUPTED,
                        "finished_at": datetime.now(timezone.utc),
                    }
                ),
            )
            save_issue(self.db, self.machine.refund_interrupted(issue))
            if isinstance(error, (ProviderError, MalformedResponse)):
                # Refunded *and* counted against the job. The refund budget alone bounds
                # this per issue, but a provider that is down would then walk every issue
                # in the workbook to `NEEDS_HUMAN_REVIEW` one refund at a time and hand
                # the curator a report saying their content needs a person. It does not;
                # the provider was unavailable, and the job should say so.
                return self._provider_failed(error)
            return StepOutcome(True, f"writer call failed: {error}", state)

        # Written down before anything is decided about the patch. A rationale persisted
        # only for accepted patches would leave the rejected ones -- the interesting ones,
        # when someone asks why the council did what it did -- with no record at all, and
        # would leave the taint registry blind to reasoning a resumed job never saw.
        self._keep_private(
            AgentRole.WRITER,
            f"writer.{issue.issue_id}.{attempt.attempt_no}",
            result.private,
            issue_id=issue.issue_id,
        )

        patch = result.patch
        if patch.needs_human_review:
            settle_attempt(
                self.db,
                attempt.model_copy(
                    update={
                        "outcome": AttemptOutcome.ESCALATED,
                        "finished_at": datetime.now(timezone.utc),
                    }
                ),
            )
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(
                True, f"{issue.issue_id} escalated: {patch.human_review_reason}", state
            )

        decision = validate_patch(patch, issue=issue, block=block, parsed=parsed)
        if not decision.accepted:
            settle_attempt(
                self.db,
                attempt.model_copy(
                    update={
                        "outcome": AttemptOutcome.PATCH_REJECTED,
                        "rejection": decision.rejection,
                        "finished_at": datetime.now(timezone.utc),
                    }
                ),
            )
            if not rejection_consumes_attempt(decision.rejection):
                issue = self.machine.refund_interrupted(issue)
            next_state = (
                IssueState.PATCH_REJECTED
                if self.machine.can_attempt(issue)
                else IssueState.NEEDS_HUMAN_REVIEW
            )
            save_issue(self.db, advance_issue(issue, next_state))
            return StepOutcome(
                True,
                f"patch rejected: {decision.rejection.code.value}",
                state,
            )

        insert_patch(self.db, patch)
        settle_attempt(self.db, attempt.model_copy(update={"patch_id": patch.patch_id}))
        save_issue(self.db, advance_issue(issue, IssueState.PATCH_PROPOSED))
        return StepOutcome(True, f"patch proposed for {issue.issue_id}", state)

    def _mechanical_patch(
        self, issue: Issue, parsed: ParsedWorkbook, block: ProblemBlock
    ) -> Patch | None:
        if len(issue.cells) != 1 or len(issue.rule_codes) != 1:
            return None
        row_number, column_number = issue.cells[0]
        row = next((candidate for candidate in block.rows if candidate.row == row_number), None)
        key = parsed.column_map.key_at(column_number)
        if row is None or key is None:
            return None
        before = row.get(key)
        code = issue.rule_codes[0]

        if code == "WHITESPACE_PADDING":
            after = before.strip()
            if not before or after == before:
                return None
            reason = "remove leading or trailing whitespace exactly"
        elif code == "METADATA_ON_NON_PROBLEM_ROW":
            # Clearing a misplaced value is lossless only when the problem row already
            # carries that metadata field. Otherwise it may be displaced source content
            # and the Writer/reviewer must decide where it belongs.
            if not before or not block.problem_row.get(key).strip():
                return None
            after = ""
            reason = "remove duplicate metadata from a non-problem row"
        elif (
            code == "ANSWER_TYPE_MISMATCH"
            and key is ColumnKey.ANSWER_TYPE
            and before.strip().lower() == "numeric"
        ):
            after = "algebra"
            reason = "label an explicit variable equation as algebra"
        else:
            return None

        return Patch(
            patch_id=f"deterministic-{uuid4().hex}",
            issue_id=issue.issue_id,
            attempt_no=1,
            edits=(
                CellEdit(
                    row=row_number,
                    column=column_number,
                    column_key=key,
                    before=before,
                    after=after,
                ),
            ),
            reason=reason,
            derivation=(
                "removing boundary whitespace preserves the mathematical value exactly"
                if code == "WHITESPACE_PADDING"
                else ""
            ),
        )

    def _apply_mechanical_patch(
        self, issue: Issue, patch: Patch, state: JobState
    ) -> StepOutcome | None:
        parsed = self.current_workbook()
        block = parsed.block_by_id(issue.block_id) if issue.block_id else None
        if block is None:
            return None
        decision = validate_patch(patch, issue=issue, block=block, parsed=parsed)
        if not decision.accepted:
            # Fall back to the ordinary Writer path on the next step. Exact automation
            # never weakens the same gate a model-authored patch must pass.
            record_event(
                self.db,
                self.job_id,
                "deterministic_repair_declined",
                f"{issue.issue_id}: {decision.rejection.code.value}",
            )
            return None

        self._spend()
        insert_patch(self.db, patch)
        progress = advance_issue(issue, IssueState.AWAITING_PATCH)
        progress = advance_issue(progress, IssueState.PATCH_PROPOSED)
        progress = advance_issue(progress, IssueState.AWAITING_REVIEW)
        progress = advance_issue(progress, IssueState.PATCH_APPROVED)
        progress = advance_issue(progress, IssueState.APPLYING)
        save_issue(self.db, progress)
        assert_lease_held(
            self.db, self.job_id, self.worker_id, run_epoch=self.run_epoch
        )
        apply_patch(
            self.db,
            self.job.model_copy(update={"run_epoch": self.run_epoch}),
            self.copy,
            patch,
            block_id=issue.block_id,
        )
        progress = advance_issue(progress, IssueState.PATCH_APPLIED)
        progress = advance_issue(progress, IssueState.ACCEPTED)
        save_issue(self.db, progress)
        record_event(
            self.db,
            self.job_id,
            "deterministic_repair",
            f"{issue.issue_id}: {patch.reason}; no model call required",
        )
        return StepOutcome(True, f"{issue.issue_id} repaired deterministically", state)

    def _apply(self, issue: Issue, state: JobState) -> StepOutcome:
        patch = _latest_patch(self.db, issue.issue_id)
        if patch is None:
            save_issue(self.db, advance_issue(issue, IssueState.PATCH_REJECTED))
            return StepOutcome(True, "no patch to apply", state)

        self._spend()
        if issue.state is not IssueState.APPLYING:
            issue = advance_issue(issue, IssueState.APPLYING)
            save_issue(self.db, issue)

        # The last thing before a byte is written. Database writes are fenced by the epoch
        # and a fenced worker simply updates nothing; `os.replace` consults no table, so
        # without this check a worker whose lease was stolen mid-step could still rewrite
        # the workbook the new owner is reading.
        assert_lease_held(
            self.db, self.job_id, self.worker_id, run_epoch=self.run_epoch
        )

        position = self._patch_position(patch)
        if position == "before":
            try:
                apply_patch(
                    self.db,
                    self.job.model_copy(update={"run_epoch": self.run_epoch}),
                    self.copy,
                    patch,
                    block_id=issue.block_id,
                )
            except EditRejected as error:
                code = error.rejection.code
                # A `before` mismatch here means the block changed underneath the patch --
                # the system's scheduling, not the Writer's mistake -- so no attempt is spent.
                if code is RejectionCode.BEFORE_MISMATCH:
                    issue = self.machine.refund_interrupted(issue)
                save_issue(self.db, advance_issue(issue, IssueState.PATCH_REJECTED))
                return StepOutcome(True, f"apply rejected: {code.value}", state)
        else:
            # Recovery after the atomic file write but before the issue-state commit.
            # Re-applying would fail its own `before` check and spend a valid attempt.
            record_event(
                self.db,
                self.job_id,
                "approved_patch_recovered",
                f"{patch.patch_id} was already present; application rolled forward",
            )

        issue = advance_issue(issue, IssueState.PATCH_APPLIED)
        save_issue(self.db, advance_issue(issue, IssueState.ACCEPTED))
        return StepOutcome(True, f"patch applied for {issue.issue_id}", state)

    def _review(self, issue: Issue, state: JobState) -> StepOutcome:
        legacy_applied = issue.state is IssueState.PATCH_APPLIED
        if issue.state in (IssueState.PATCH_PROPOSED, IssueState.PATCH_APPLIED):
            save_issue(self.db, advance_issue(issue, IssueState.AWAITING_REVIEW))
            issue = advance_issue(issue, IssueState.AWAITING_REVIEW)

        # A verdict is persisted before a rejected patch is rolled back. If the worker
        # dies in that narrow window, reuse the durable verdict rather than paying for a
        # second physical model call to make the same decision.
        patch = _latest_patch(self.db, issue.issue_id)
        review_attempt_no = patch.attempt_no if patch is not None else (issue.attempts_used or 1)
        position = self._patch_position(patch) if patch is not None else "before"
        verdict = _verdict_for_attempt(self.db, issue.issue_id, review_attempt_no)
        if verdict is None:
            verdict = self._ask_reviewer(
                issue, attempt_no=review_attempt_no, candidate_patch=patch
            )
        if verdict is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, "block vanished; escalating", state)
        _settle_reviewed_attempt(self.db, issue.issue_id, verdict)

        if verdict.decision is ReviewDecision.ACCEPT:
            next_state = (
                IssueState.ACCEPTED
                if legacy_applied or position == "after"
                else IssueState.PATCH_APPROVED
            )
            save_issue(self.db, advance_issue(issue, next_state))
            message = "accepted" if next_state is IssueState.ACCEPTED else "approved"
            return StepOutcome(True, f"{issue.issue_id} {message}", state)

        # New jobs review a simulation, so a rejection has no workbook mutation to undo.
        # `after` is possible only for an in-flight job created under the former
        # apply-before-review lifecycle; retain its crash-safe rollback path.
        if patch is not None and position == "after":
            self._rollback_rejected_patch(issue, patch, verdict.verdict_id)
        elif patch is not None:
            record_event(
                self.db,
                self.job_id,
                "candidate_patch_rejected",
                f"{patch.patch_id} was not applied after verdict {verdict.verdict_id}",
            )

        if verdict.decision is ReviewDecision.HUMAN_REVIEW:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, f"{issue.issue_id} sent to a person", state)

        next_state = (
            IssueState.REVISION_REQUESTED
            if self.machine.can_attempt(issue)
            else IssueState.NEEDS_HUMAN_REVIEW
        )
        save_issue(self.db, advance_issue(issue, next_state))
        return StepOutcome(True, f"{issue.issue_id} revision requested", state)

    def _patch_position(self, patch: Patch) -> str:
        """Whether every target cell contains the proposal's `before` or `after` value.

        New patches are reviewed while still at `before`. `after` exists for recovery and
        for jobs created under the former apply-before-review lifecycle. A mixture cannot
        be produced by the atomic workbook writer and is therefore corruption.
        """
        workbook = load_workbook(self.copy.path, data_only=False, read_only=True)
        try:
            sheet = workbook.active
            actual = [
                render_cell(sheet.cell(row=edit.row, column=edit.column).value)
                for edit in patch.edits
            ]
        finally:
            workbook.close()

        if all(value == edit.before for value, edit in zip(actual, patch.edits)):
            return "before"
        if all(value == edit.after for value, edit in zip(actual, patch.edits)):
            return "after"
        raise JobCorrupted(
            f"working copy is neither before nor after patch {patch.patch_id}",
            job_id=self.job_id,
        )

    def _rollback_rejected_patch(
        self, issue: Issue, patch: Patch, verdict_id: str
    ) -> None:
        """Remove a non-accepted patch, idempotently and with a durable apply intent."""
        workbook = load_workbook(self.copy.path, data_only=False, read_only=True)
        try:
            sheet = workbook.active
            actual = [
                render_cell(sheet.cell(row=edit.row, column=edit.column).value)
                for edit in patch.edits
            ]
        finally:
            workbook.close()

        all_patched = all(value == edit.after for value, edit in zip(actual, patch.edits))
        all_original = all(value == edit.before for value, edit in zip(actual, patch.edits))
        if all_original:
            return
        if not all_patched:
            raise JobCorrupted(
                f"working copy is neither applied nor rolled back for rejected patch "
                f"{patch.patch_id}",
                job_id=self.job_id,
            )

        assert_lease_held(
            self.db, self.job_id, self.worker_id, run_epoch=self.run_epoch
        )
        inverse = Patch(
            patch_id=f"rollback-{verdict_id}",
            issue_id=issue.issue_id,
            attempt_no=patch.attempt_no,
            edits=tuple(
                CellEdit(
                    row=edit.row,
                    column=edit.column,
                    column_key=edit.column_key,
                    before=edit.after,
                    after=edit.before,
                )
                for edit in patch.edits
            ),
            reason="automatic rollback: reviewer did not accept the proposed patch",
        )
        apply_patch(
            self.db,
            self.job.model_copy(update={"run_epoch": self.run_epoch}),
            self.copy,
            inverse,
            block_id=issue.block_id,
        )
        record_event(
            self.db,
            self.job_id,
            "patch_rolled_back",
            f"{patch.patch_id} was removed after verdict {verdict_id}",
        )

    def _review_unpatched_issue(self, issue: Issue, state: JobState) -> StepOutcome:
        """Establish whether a model-only claim is real before permitting any edit.

        Two checks, in two steps, because `step()` makes at most one model call.

        **First**, a claim-blind audit by the opposite audit role. The second agent
        receives the current block and the rules only; the accusation appears nowhere in
        its system prompt, payload, or response schema, so its findings are its own.
        Attempt zero is a durable namespace for both checks and cannot be mistaken for a
        verdict on Writer attempt one after a crash.

        **Second**, when that audit neither reproduced the claim nor contradicted it, an
        adjudicator that is shown both readings and decides between them.

        The second check is the correction. This method used to read "the blind audit did
        not report the same cells" as "the claim is refuted", and closed the issue. Those
        are different findings: one says nobody corroborated it, the other says somebody
        checked and it is not there. On two held-out workbooks the conflation discarded
        three defects that were really present. Refutation now needs evidence, and the
        state for an unsettled disagreement is `UNCONFIRMED`, which denies the job success
        rather than granting it.
        """
        blind_role = _blind_reviewer_role(issue)
        cached = _verdict_for_attempt(
            self.db, issue.issue_id, 0, reviewer_role=blind_role
        )
        verdict = cached or self._ask_blind_corroborator(issue)
        if verdict is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, "block vanished; escalating", state)

        if verdict.decision is ReviewDecision.REVISE:
            # Independently corroborated. Unchanged behaviour, and the only path from a
            # model-only claim to a Writer call that costs no adjudication.
            save_issue(self.db, advance_issue(issue, IssueState.AWAITING_PATCH))
            return StepOutcome(True, f"{issue.issue_id} corroborated", state)

        if verdict.decision is ReviewDecision.HUMAN_REVIEW:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, f"{issue.issue_id} sent to a person", state)

        # Unsettled. Before spending an adjudication call, the one case a model cannot
        # improve on: a sibling repair has already edited these exact cells, and a fresh
        # audit of the result reports nothing wrong with them. That is supersession, and
        # it is decidable from the change ledger.
        changed_targets = {
            (change.row, change.column)
            for change in list_changes(self.db, self.job_id)
            if change.block_id == issue.block_id and change.issue_id != issue.issue_id
        }
        if changed_targets.intersection(issue.cells):
            save_issue(self.db, advance_issue(issue, IssueState.SUPERSEDED))
            record_event(
                self.db,
                self.job_id,
                "issue_superseded",
                f"{issue.issue_id}: an earlier accepted repair resolved the unsupported "
                f"semantic finding in {issue.problem_name}",
            )
            return StepOutcome(
                True, f"{issue.issue_id} resolved by another repair", state
            )

        if cached is None:
            # The blind audit was this step's one model call. The issue stays OPEN, the
            # same queue predicate re-selects it, and the next step adjudicates against a
            # verdict that is now durable.
            return StepOutcome(True, f"{issue.issue_id} awaiting adjudication", state)

        return self._adjudicate(issue, verdict, state)

    def _adjudicate(
        self, issue: Issue, blind_verdict: ReviewVerdict, state: JobState
    ) -> StepOutcome:
        """Settle a disagreement between two audits, on evidence.

        The adjudicator is the one agent shown another agent's conclusion, which is a
        deliberate trade and not an oversight: choosing between two readings of a block is
        not something a blind observer can do, and the blind observer has already been
        asked and could not settle it. What pays for the anchoring risk is that the
        adjudicator must state the check it ran, and that `undecided` is a real answer --
        an adjudicator with only two available answers learns to pick the confident one.

        Its canonical cell list *replaces* the disputed claim's, which is how a claim that
        named the cell where a defect was visible becomes a repair authorised at the cell
        that has to change. The cells travel on the verdict rather than being written
        straight onto the issue: verdict and issue are two writes, a crash can land
        between them, and a resumed job that knew a defect was confirmed but not where
        would have to give the confirmation back.
        """
        existing = _verdict_for_attempt(
            self.db, issue.issue_id, 0, reviewer_role=ReviewerRole.ADJUDICATOR
        )
        verdict = existing or self._ask_adjudicator(issue, blind_verdict)
        if verdict is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, "block vanished; escalating", state)

        if verdict.decision is ReviewDecision.REVISE:
            updated = issue
            if verdict.canonical_cells:
                category = verdict.canonical_category or issue.category
                updated = issue.model_copy(
                    update={
                        "cells": tuple(verdict.canonical_cells),
                        "category": category,
                        "is_structural": _targets_are_structural(
                            verdict.canonical_cells, category
                        ),
                    }
                )
            save_issue(self.db, advance_issue(updated, IssueState.AWAITING_PATCH))
            record_event(
                self.db,
                self.job_id,
                "adjudication_confirmed",
                f"{issue.issue_id} in {issue.problem_name or issue.block_id}: "
                f"repair authorised at {sorted(updated.cells)}",
            )
            return StepOutcome(True, f"{issue.issue_id} confirmed on evidence", state)

        if verdict.decision is ReviewDecision.ACCEPT:
            save_issue(self.db, advance_issue(issue, IssueState.REFUTED))
            record_event(
                self.db,
                self.job_id,
                "issue_refuted",
                f"{issue.issue_id}: an adjudicator showed the content in "
                f"{issue.problem_name} is correct",
            )
            return StepOutcome(True, f"{issue.issue_id} refuted on evidence", state)

        # Undecided, and that is where it stops. Not refuted -- nothing was shown -- and
        # not escalated as a failed repair either, because no repair was attempted. The
        # curator is told two audits disagreed and the question is open.
        save_issue(self.db, advance_issue(issue, IssueState.UNCONFIRMED))
        record_event(
            self.db,
            self.job_id,
            "issue_unconfirmed",
            f"{issue.issue_id}: two independent audits of {issue.problem_name} "
            "disagreed and adjudication settled nothing",
        )
        return StepOutcome(True, f"{issue.issue_id} left unconfirmed", state)

    def _ask_adjudicator(
        self, issue: Issue, blind_verdict: ReviewVerdict
    ) -> ReviewVerdict | None:
        """One adjudication call, recorded as attempt zero under the adjudicator role."""
        current = self.current_workbook()
        block = current.block_by_id(issue.block_id) if issue.block_id else None
        if block is None:
            return None

        context = adjudicator.AdjudicationContext(
            disputed_claim=render_claim(
                cells=issue.cells,
                category=issue.category.value,
                problem=issue.description,
                expected=issue.expected,
            ),
            # Written down by the blind audit precisely so it survives a crash: the second
            # audit's findings are not re-derivable without paying for the call again.
            second_audit=blind_verdict.feedback,
            block=render_block(block),
            conventions=render_conventions(current.conventions),
            deterministic_findings=render_findings(
                self._findings_for_block(current, block)
            ),
            curator_rules="\n".join(f"- {rule}" for rule in self.curator_rules),
        )

        # Both claims shown here are *published* findings, but each was produced by a call
        # that also kept a private note about the same block. An agent's own note and its
        # own finding describe one defect in one sentence, so those two records -- and no
        # others -- are public ground for the text they wrote.
        public_for: dict[str, Sequence[str]] = {}
        origin = known_issue_reviewer.origin_private_label(issue)
        if origin:
            public_for[origin] = (context.disputed_claim,)
        public_for[f"blind-auditor.{issue.issue_id}"] = (context.second_audit,)

        self._spend()
        result = adjudicator.adjudicate(
            self.client,
            context=context,
            block_rows=[row.row for row in block.rows],
            job_id=self.job_id,
            issue_id=issue.issue_id,
            taint=self.taint,
            public_for=public_for,
            prompt_version=self._prompt_version(AgentRole.ADJUDICATOR),
        )

        decision = _ADJUDICATIONS[result.verdict]
        feedback = result.evidence
        if decision is ReviewDecision.REVISE and result.expected:
            feedback = f"{feedback}\n\nRequired result: {result.expected}"
        verdict = ReviewVerdict(
            verdict_id=uuid4().hex,
            issue_id=issue.issue_id,
            reviewer_role=ReviewerRole.ADJUDICATOR,
            attempt_no=0,
            decision=decision,
            feedback=feedback,
            canonical_cells=result.cells,
            canonical_category=result.category,
        )
        insert_verdict(self.db, verdict)
        return verdict

    def _ask_blind_corroborator(self, issue: Issue) -> ReviewVerdict | None:
        """Run a claim-blind audit and record how it relates to the claim.

        This intentionally reuses the two fresh-audit agents rather than introducing a
        prompt that would drift from them.  It costs the same one physical call as the
        former issue-framed pre-check, but removes the accusation from the payload.

        It **classifies and does not conclude**. Exact agreement is corroboration; nothing
        else here is a refutation, because a from-scratch audit that did not mention a
        cell has not examined the claim about it. Whatever it did report is written into
        the verdict's feedback, since that text is what the adjudicator is shown next and
        re-deriving it would mean paying for this call twice.
        """
        current = self.current_workbook()
        block = current.block_by_id(issue.block_id) if issue.block_id else None
        if block is None:
            return None

        findings: tuple[ValidationFinding, ...] = ()
        sound = True
        inconclusive = ""
        reviewer_role = _blind_reviewer_role(issue)
        self._spend()
        try:
            if issue.source is IssueSource.INDEPENDENT_REVIEWER:
                result = initial_auditor.audit_block(
                    self.client,
                    block=block,
                    conventions=current.conventions,
                    deterministic_findings=self._findings_for_block(current, block),
                    seed_claims=(),
                    curator_rules=self.curator_rules,
                    curator_notes=self.curator_notes,
                    job_id=self.job_id,
                    taint=self.taint,
                    prompt_version=self._prompt_version(AgentRole.INITIAL_AUDITOR),
                )
                findings = result.findings
                self._keep_private(
                    AgentRole.INITIAL_AUDITOR,
                    f"blind-auditor.{issue.issue_id}",
                    result.private,
                    issue_id=issue.issue_id,
                )
            else:
                result = independent_reviewer.sweep_block(
                    self.client,
                    block=block,
                    conventions=current.conventions,
                    deterministic_findings=self._findings_for_block(current, block),
                    curator_rules=self.curator_rules,
                    job_id=self.job_id,
                    taint=self.taint,
                    prompt_version=self._prompt_version(
                        AgentRole.INDEPENDENT_REVIEWER
                    ),
                )
                findings = result.findings
                sound = result.block_is_sound
        except FindingAttributionError as error:
            # An out-of-block target means the corroborator did not complete a usable
            # audit.  It is not evidence either for or against the claim.
            inconclusive = str(error)

        outcome, matches = _classify_corroboration(issue, findings)

        if inconclusive:
            decision = ReviewDecision.HUMAN_REVIEW
            feedback = f"The claim-blind audit was inconclusive: {inconclusive}"
            event = "blind_claim_inconclusive"
            rule_codes: tuple[str, ...] = ()
        elif outcome is Corroboration.EXACT:
            match = matches[0]
            expected = str(match.detail.get("expected") or "").strip()
            feedback = (
                "A claim-blind audit independently found the same defect at "
                f"{sorted(issue.cells)}: {match.message}"
                + (f" Required result: {expected}" if expected else "")
            )
            decision = ReviewDecision.REVISE
            event = "blind_claim_corroborated"
            rule_codes = (match.code,)
        else:
            decision = ReviewDecision.UNRESOLVED
            event = "blind_claim_unresolved"
            rule_codes = ()
            reports = [
                render_claim(
                    cells=sorted(_finding_targets(finding)),
                    category=str(finding.detail.get("category", "")) or finding.code,
                    problem=finding.message,
                    expected=str(finding.detail.get("expected") or "").strip(),
                )
                for finding in matches
            ]
            feedback = render_claims(reports)
            if outcome is Corroboration.SILENT and not sound:
                # It called the block unsound and named nothing. That is not a refutation
                # of anything; it is one more reason the question needs settling.
                feedback = (
                    f"{feedback}, but reported the block as not sound without naming a "
                    "defect."
                )

        verdict = ReviewVerdict(
            verdict_id=uuid4().hex,
            issue_id=issue.issue_id,
            reviewer_role=reviewer_role,
            attempt_no=0,
            decision=decision,
            feedback=feedback,
            rule_codes=rule_codes,
        )
        insert_verdict(self.db, verdict)
        record_event(
            self.db,
            self.job_id,
            event,
            f"{issue.issue_id} in {issue.problem_name or issue.block_id}",
        )
        return verdict

    def _ask_reviewer(
        self,
        issue: Issue,
        *,
        attempt_no: int | None = None,
        candidate_patch: Patch | None = None,
        reviewer_role: ReviewerRole | None = None,
    ):
        source_parse = self.source_workbook()
        current = self.current_workbook()
        original_block = source_parse.block_by_id(issue.block_id)
        current_block = current.block_by_id(issue.block_id)
        if original_block is None or current_block is None:
            return None

        review_block = current_block
        review_parse = current
        candidate_edits = ()
        if candidate_patch is not None:
            candidate_edits = candidate_patch.edits
            if self._patch_position(candidate_patch) == "before":
                review_block = simulate_block(current_block, candidate_patch.edits)
                review_parse = current.model_copy(
                    update={
                        "blocks": tuple(
                            review_block if block.block_id == review_block.block_id else block
                            for block in current.blocks
                        )
                    }
                )

        context = known_issue_reviewer.build_context(
            issue=issue,
            original_block=original_block,
            current_block=review_block,
            conventions=current.conventions,
            deterministic_findings=self._findings_for_block(review_parse, review_block),
            curator_rules=self.curator_rules,
            candidate_edits=candidate_edits,
        )
        self._spend()
        role = reviewer_role or issue.reviewer_role
        verdict = known_issue_reviewer.review(
            self.client,
            issue=issue,
            context=context,
            attempt_no=(
                attempt_no
                if attempt_no is not None
                else (issue.attempts_used or 1)
            ),
            job_id=self.job_id,
            taint=self.taint,
            role=role,
            prompt_version=self._prompt_version(
                AgentRole.KNOWN_ISSUE_REVIEWER
                if role is ReviewerRole.KNOWN_ISSUE_REVIEWER
                else AgentRole.INDEPENDENT_REVIEWER
            ),
        )
        insert_verdict(self.db, verdict)
        return verdict

    # -- queries -----------------------------------------------------------------------

    def _next_live_issue(self, role: ReviewerRole | None) -> Issue | None:
        from .persistence import next_issue_for_phase

        return next_issue_for_phase(
            self.db,
            self.job_id,
            sorted(LIVE_ISSUE_STATES, key=str),
            reviewer_role=role.value if role else None,
        )

    def _verify_instructions(self) -> None:
        """The instruction file must still be the one that was read.

        The same argument as the source-hash check, for the same reason: a job's
        conclusions are only meaningful against the inputs it was given. If the document
        changed on disk after its segments were extracted, every claim in the database
        describes a file that no longer exists, and auditing against them would produce a
        report citing provenance that is now wrong.

        Treated as corruption rather than bad input, so it is not resumable -- resuming
        would re-read the same mismatched pair and reach the same place.
        """
        segments = load_instruction_segments(self.db, self.job_id)
        if not segments:
            return
        expected = segments[0].get("document_sha256") or ""
        path = list_artifacts(
            self.db, self.job_id, data_root=self.settings.data_root
        ).get(ArtifactKind.INSTRUCTION_DOCUMENT)
        if not expected or path is None or not Path(path).is_file():
            return
        actual = sha256_of(Path(path))
        if actual != expected:
            raise JobCorrupted(
                "the instruction document changed on disk after its contents were "
                f"read: expected {expected[:12]}, found {actual[:12]}"
            )

    def _record_claim_verdicts(self, block_id: str, result) -> None:
        """What this block concluded about each of the curator's claims.

        Structured rows rather than event lines. The final report has to tell a curator
        whether the defect they described was found, or looked for and not there, and a
        free-text log entry cannot be counted, grouped, or shown per claim.

        Only blocks that said something are recorded. A claim no block mentions is
        *unresolved*, and the difference between that and refuted matters: refuted means
        somebody looked, unresolved means nobody did.
        """
        for finding in result.findings:
            index = finding.detail.get("confirms_claim")
            if index is not None:
                record_claim_result(
                    self.db,
                    self.job_id,
                    segment_index=int(index),
                    block_id=block_id,
                    outcome=ClaimOutcome.CONFIRMED,
                    detail=finding.message,
                )
        for refuted in result.refuted:
            record_claim_result(
                self.db,
                self.job_id,
                segment_index=refuted.claim_index,
                block_id=block_id,
                outcome=ClaimOutcome.REFUTED,
                detail=refuted.why,
            )

    def _findings_for_block(
        self, parsed: ParsedWorkbook, block: ProblemBlock
    ) -> tuple[ValidationFinding, ...]:
        return tuple(
            f for f in run_rules(parsed) if f.block_id == block.block_id
        ) + block.findings

    def _open_issues(
        self,
        findings: Sequence[ValidationFinding],
        *,
        source: IssueSource,
        reviewer_role: ReviewerRole = ReviewerRole.KNOWN_ISSUE_REVIEWER,
    ) -> int:
        opened = 0
        candidates = sorted(
            actionable(tuple(findings)),
            key=lambda finding: (
                finding.block_id or "",
                0 if finding.code in _ROOT_FINDING_CODES else 1,
                finding.row or 0,
                finding.column or 0,
                finding.code,
            ),
        )
        for finding in candidates:
            issue = issue_from_finding(
                finding, job_id=self.job_id, source=source, reviewer_role=reviewer_role
            )
            if insert_issue(self.db, issue) is not None:
                opened += 1
        return opened

    def _open_validation_issues(
        self, findings: Sequence[ValidationFinding]
    ) -> Rediscovery:
        """Route by origin, and decide what a rediscovered defect means.

        A finding on a block that had an original ledger entry goes back to the
        Known-Issue Reviewer; everything else to the Independent Reviewer.

        The interesting case is a finding whose fingerprint matches an issue that is
        already resolved -- the council fixed this, a reviewer accepted it, and the defect
        is still here. **Silently absorbing that is how a job reports success over a
        workbook it never repaired.** So:

        * the issue is *reopened* while it still has attempts, because a rediscovered
          defect is exactly the evidence that the accepted repair did not work;
        * once its attempts are spent the rediscovery is absorbed -- opening it again
          would hand it a fresh budget and loop -- but the issue is moved to
          `NEEDS_HUMAN_REVIEW`, which durably denies the job success. Absorption stops
          the loop; it never launders the defect.
        """
        from .persistence import fingerprint_states

        known_blocks = load_ledger(self.db, self.job_id).blocks_with_ledger_entry
        existing = {i.fingerprint: i for i in list_issues(self.db, self.job_id)}
        result = Rediscovery()

        for finding in actionable(tuple(findings)):
            mark = fingerprint(finding)
            seen = existing.get(mark)
            if seen is not None:
                if not seen.is_terminal:
                    # Already in flight. It will be repaired or escalated on its own.
                    result.still_open += 1
                elif seen.state is IssueState.NEEDS_HUMAN_REVIEW:
                    result.absorbed += 1
                    self._note_rediscovery("finding_absorbed", seen, finding)
                elif self.machine.can_attempt(seen):
                    save_issue(self.db, self.machine.reopen(seen))
                    result.reopened += 1
                    self._note_rediscovery("issue_reopened", seen, finding)
                else:
                    save_issue(self.db, self.machine.reopen(seen))
                    result.absorbed += 1
                    self._note_rediscovery("finding_absorbed", seen, finding)
                continue

            role = (
                ReviewerRole.KNOWN_ISSUE_REVIEWER
                if finding.block_id in known_blocks
                else ReviewerRole.INDEPENDENT_REVIEWER
            )
            issue = issue_from_finding(
                finding,
                job_id=self.job_id,
                source=IssueSource.FINAL_VALIDATION,
                reviewer_role=role,
            )
            if insert_issue(self.db, issue) is not None:
                result.opened += 1
        return result


# --------------------------------------------------------------------------------------
# Small durable lookups
# --------------------------------------------------------------------------------------


def _latest_patch(db: Database, issue_id: str):
    from .models import Patch

    row = db.connection.execute(
        "SELECT payload_json FROM patches WHERE issue_id = ? ORDER BY attempt_no DESC "
        "LIMIT 1",
        (issue_id,),
    ).fetchone()
    return Patch.model_validate_json(row["payload_json"]) if row else None


def _latest_feedback(db: Database, issue_id: str) -> str:
    """The newest actionable response to a failed repair, whatever produced it.

    The old implementation read only reviewer verdicts. A deterministic gate rejection
    therefore vanished before the next Writer call, which made the Writer repeat the
    exact same invalid patch until its attempt budget was exhausted.
    """
    from .models import ReviewVerdict

    row = db.connection.execute(
        """SELECT kind, payload_json FROM (
               SELECT 'review' AS kind, payload_json, decided_at AS happened_at
                 FROM review_verdicts WHERE issue_id = ?
               UNION ALL
               SELECT 'rejection' AS kind, payload_json,
                      COALESCE(finished_at, started_at) AS happened_at
                 FROM repair_attempts
                WHERE issue_id = ? AND outcome = 'patch_rejected'
           ) ORDER BY happened_at DESC LIMIT 1""",
        (issue_id, issue_id),
    ).fetchone()
    if row is None:
        return ""
    if row["kind"] == "review":
        return ReviewVerdict.model_validate_json(row["payload_json"]).feedback

    attempt = RepairAttempt.model_validate_json(row["payload_json"])
    rejection = attempt.rejection
    if rejection is None:
        return ""
    feedback = (
        f"The deterministic patch gate rejected the previous attempt with "
        f"{rejection.code.value}: {rejection.message}."
    )
    if rejection.row is not None:
        feedback += f" It concerns row {rejection.row}"
        if rejection.column is not None:
            feedback += f", column {rejection.column}"
        feedback += "."
    if rejection.code is RejectionCode.MISSING_MATH_VERIFICATION:
        feedback += (
            " In the next response, put a concrete calculation, exact-choice check, or "
            "symbolic-equivalence check in `derivation`; do not leave it empty."
        )
    return feedback


def _verdict_for_attempt(
    db: Database,
    issue_id: str,
    attempt_no: int,
    *,
    reviewer_role: ReviewerRole | None = None,
):
    """A verdict already committed for this exact attempt, if recovery needs it.

    `reviewer_role` matters only at attempt zero, where two different checks now live: the
    claim-blind audit under whichever audit role ran it, and the adjudication under
    `ADJUDICATOR`. Without the filter a resumed job would read the adjudicator's verdict
    as the blind audit's -- and, finding it settled, would never adjudicate at all.
    """
    from .models import ReviewVerdict

    query = "SELECT payload_json FROM review_verdicts WHERE issue_id = ? AND attempt_no = ?"
    parameters: tuple[object, ...] = (issue_id, attempt_no)
    if reviewer_role is not None:
        query += " AND reviewer_role = ?"
        parameters += (reviewer_role.value,)
    row = db.connection.execute(
        f"{query} ORDER BY decided_at DESC LIMIT 1", parameters
    ).fetchone()
    return ReviewVerdict.model_validate_json(row["payload_json"]) if row else None


def _latest_attempt(db: Database, issue_id: str) -> RepairAttempt | None:
    row = db.connection.execute(
        "SELECT payload_json FROM repair_attempts WHERE issue_id = ? "
        "ORDER BY attempt_no DESC LIMIT 1",
        (issue_id,),
    ).fetchone()
    return RepairAttempt.model_validate_json(row["payload_json"]) if row else None


def _settle_reviewed_attempt(db: Database, issue_id: str, verdict) -> None:
    """Attach the review's real terminal outcome to the Writer attempt it judged."""
    attempt = _latest_attempt(db, issue_id)
    if attempt is None or attempt.patch_id is None or attempt.outcome is not None:
        # A prior-repair review has no Writer attempt of its own. A recovered or already
        # settled attempt must also remain exactly as recovery recorded it.
        return
    outcomes = {
        ReviewDecision.ACCEPT: AttemptOutcome.PATCH_ACCEPTED,
        ReviewDecision.REVISE: AttemptOutcome.REVISION_REQUESTED,
        ReviewDecision.HUMAN_REVIEW: AttemptOutcome.ESCALATED,
    }
    settle_attempt(
        db,
        attempt.model_copy(
            update={
                "outcome": outcomes[verdict.decision],
                "verdict_id": verdict.verdict_id,
                "finished_at": verdict.decided_at,
            }
        ),
    )


def _verdicts(db: Database, job_id: str):
    from .persistence import list_verdicts

    return list_verdicts(db, job_id)


def _attempts(db: Database, job_id: str):
    from .persistence import list_attempts

    return list_attempts(db, job_id)


def start_job(
    db: Database,
    settings: Settings,
    *,
    job_id: str,
    source: SourcePath,
    job_dir: Path,
) -> WorkingCopy:
    """Create the job's working copy. The source is made read-only here."""
    return create_working_copy(source, job_dir)
