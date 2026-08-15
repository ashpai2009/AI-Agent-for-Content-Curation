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
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .agents import independent_reviewer, initial_auditor, known_issue_reviewer, writer
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
from .llm.prompts import current_prompt_versions
from .models import (
    ArtifactKind,
    ClaimOutcome,
    AttemptOutcome,
    CurationJob,
    FailureReason,
    Issue,
    IssueSource,
    IssueState,
    JobState,
    ParsedWorkbook,
    ProblemBlock,
    RejectionCode,
    RepairAttempt,
    ReviewDecision,
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
    target_findings,
    validate_patch,
)
from .validation.rules import run_rules
from .workbook.reader import read_workbook
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
_NEEDS_APPLY = frozenset({IssueState.PATCH_PROPOSED, IssueState.APPLYING})
_NEEDS_REVIEW = frozenset({IssueState.PATCH_APPLIED, IssueState.AWAITING_REVIEW})
LIVE_ISSUE_STATES = _NEEDS_WRITER | _NEEDS_APPLY | _NEEDS_REVIEW

#: How much curator-supplied policy may ride along in every repair and review call. These
#: travel on every one of them for the whole job, so an uncapped section turns a long
#: document into a cost paid hundreds of times for text that mostly repeats.
MAX_CURATOR_RULE_CHARACTERS = 4_000


#: The durable event kind counted against `provider_failure_budget`.
PROVIDER_FAILURE_EVENT = "provider_failure"

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
CLI_ADAPTER_VERSION = 1


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
    if recorded is None or int(recorded) == CLI_ADAPTER_VERSION:
        return None
    return (
        f"this job was pinned to CLI adapter version {int(recorded)} and this process "
        f"is running version {CLI_ADAPTER_VERSION}. The adapter decides what flags each "
        "model call carries, so continuing would finish the job under different "
        "instructions from the ones it started under. Resume it on a process running "
        f"adapter {int(recorded)}, or submit the workbook again."
    )


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

        Policy, not hypotheses: these reach the Writer and both reviewers, who have to
        *apply* them, and never the claim machinery, which would ask thirty blocks to
        confirm or refute a statement that is true of all of them.

        Capped, because these travel in every repair and review call for the whole job.
        An uncapped policy section turns a long document into a per-call cost paid
        hundreds of times.
        """
        rules = [
            row["text"].strip()
            for row in load_instruction_segments(self.db, self.job_id)
            if row.get("purpose") == SegmentPurpose.RULES and row["text"].strip()
        ]
        kept: list[str] = []
        budget = MAX_CURATOR_RULE_CHARACTERS
        for rule in rules:
            if len(rule) > budget:
                break
            kept.append(rule)
            budget -= len(rule)
        return tuple(kept)

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
        used = 0
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
        """Refuse to continue a job under an adapter it was not pinned to."""
        if self._settings_migration:
            raise JobSettingsMigrationRequired(self._settings_migration)

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
        """Sweep every block without a surviving ledger entry, then repair what it finds."""
        parsed = self.current_workbook()
        ledger = load_ledger(self.db, self.job_id)
        done = blocks_done(self.db, self.job_id, "swept")

        pending = [
            block
            for block in independent_reviewer.blocks_to_sweep(
                parsed.blocks, ledger.blocks_with_ledger_entry
            )
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
        ledger = load_ledger(self.db, self.job_id)
        changes = list_changes(self.db, self.job_id)

        gate = run_final_gate(
            source=self.copy.source,
            source_sha256=self.copy.source_sha256,
            output=self.copy.path,
            changes=changes,
        )

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
            and any(
                change.block_id == issue.block_id and change.issue_id != issue.issue_id
                for change in list_changes(self.db, self.job_id)
            )
        ):
            return self._review_prior_repair(issue, state)

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

        issue = advance_issue(issue, IssueState.PATCH_APPLIED)
        save_issue(self.db, advance_issue(issue, IssueState.AWAITING_REVIEW))
        return StepOutcome(True, f"patch applied for {issue.issue_id}", state)

    def _review(self, issue: Issue, state: JobState) -> StepOutcome:
        if issue.state is IssueState.PATCH_APPLIED:
            save_issue(self.db, advance_issue(issue, IssueState.AWAITING_REVIEW))
            issue = advance_issue(issue, IssueState.AWAITING_REVIEW)

        verdict = self._ask_reviewer(issue)
        if verdict is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, "block vanished; escalating", state)
        _settle_reviewed_attempt(self.db, issue.issue_id, verdict)

        if verdict.decision is ReviewDecision.ACCEPT:
            save_issue(self.db, advance_issue(issue, IssueState.ACCEPTED))
            return StepOutcome(True, f"{issue.issue_id} accepted", state)
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

    def _review_prior_repair(self, issue: Issue, state: JobState) -> StepOutcome:
        """Check whether another accepted edit already resolved a semantic duplicate.

        This is deliberately a reviewer call, not a cell-overlap heuristic. Two genuine
        semantic defects can concern the same answer, and silently superseding one merely
        because that cell changed would lose it. The reviewer sees source versus current
        and decides whether this particular issue survived the earlier repair.
        """
        verdict = self._ask_reviewer(issue)
        if verdict is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, "block vanished; escalating", state)

        if verdict.decision is ReviewDecision.ACCEPT:
            save_issue(self.db, advance_issue(issue, IssueState.SUPERSEDED))
            record_event(
                self.db,
                self.job_id,
                "issue_superseded",
                f"{issue.issue_id}: reviewer confirmed an earlier repair resolved "
                f"the semantic finding in {issue.problem_name}",
            )
            return StepOutcome(True, f"{issue.issue_id} resolved by another repair", state)

        if verdict.decision is ReviewDecision.HUMAN_REVIEW:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, f"{issue.issue_id} sent to a person", state)

        # OPEN cannot transition directly to REVISION_REQUESTED. AWAITING_PATCH means
        # exactly what is true now: the reviewer has supplied actionable feedback and
        # the next phase step should ask the Writer for the first patch.
        save_issue(self.db, advance_issue(issue, IssueState.AWAITING_PATCH))
        return StepOutcome(True, f"{issue.issue_id} still needs a patch", state)

    def _ask_reviewer(self, issue: Issue):
        source_parse = self.source_workbook()
        current = self.current_workbook()
        original_block = source_parse.block_by_id(issue.block_id)
        current_block = current.block_by_id(issue.block_id)
        if original_block is None or current_block is None:
            return None

        context = known_issue_reviewer.build_context(
            issue=issue,
            original_block=original_block,
            current_block=current_block,
            conventions=current.conventions,
            deterministic_findings=self._findings_for_block(current, current_block),
            curator_rules=self.curator_rules,
        )
        self._spend()
        verdict = known_issue_reviewer.review(
            self.client,
            issue=issue,
            context=context,
            attempt_no=issue.attempts_used or 1,
            job_id=self.job_id,
            taint=self.taint,
            role=issue.reviewer_role,
            prompt_version=self._prompt_version(
                AgentRole.KNOWN_ISSUE_REVIEWER
                if issue.reviewer_role is ReviewerRole.KNOWN_ISSUE_REVIEWER
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
        for finding in actionable(tuple(findings)):
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
