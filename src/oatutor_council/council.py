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

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .agents import independent_reviewer, initial_auditor, known_issue_reviewer, writer
from .agents.isolation import ContextIsolationError, TaintRegistry
from .config import Settings
from .llm.base import LLMClient, ProviderError
from .models import (
    ArtifactKind,
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
    Database,
    blocks_done,
    get_job,
    increment_counters,
    insert_attempt,
    insert_issue,
    insert_patch,
    insert_verdict,
    list_changes,
    list_issues,
    load_ledger,
    mark_block_done,
    next_attempt_number,
    record_artifact,
    record_event,
    record_findings,
    save_issue,
    settle_attempt,
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
from .workbook.writer import EditRejected, WorkingCopy, create_working_copy

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


class BudgetExhausted(Exception):
    """A global fuse blew. Always terminal, never retried."""


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
        seed_claims: Sequence[initial_auditor.SeedClaim] = (),
    ) -> None:
        self.db = db
        self.settings = settings
        self.client = client
        self.job_id = job_id
        self.copy = copy
        self.worker_id = worker_id
        self.seed_claims = tuple(seed_claims)
        self.machine = IssueMachine(
            max_attempts=settings.max_repair_attempts,
            interrupted_retry_budget=settings.interrupted_retry_budget,
        )
        # One registry per job. Two jobs share no reasoning, and a global one would make
        # a different job's rationale a false positive here.
        self.taint = TaintRegistry()
        self._source_parse: ParsedWorkbook | None = None

    # -- helpers ----------------------------------------------------------------------

    @property
    def job(self) -> CurationJob:
        return get_job(self.db, self.job_id)

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

    def _spend(self, *, steps: int = 1, llm_calls: int = 0) -> None:
        job = self.job
        if job.steps_used + steps > self.settings.step_budget:
            raise BudgetExhausted(
                f"job exceeded its step budget of {self.settings.step_budget}"
            )
        if job.llm_calls_used + llm_calls > self.settings.llm_call_budget:
            raise BudgetExhausted(
                f"job exceeded its model-call budget of {self.settings.llm_call_budget}"
            )
        increment_counters(
            self.db, self.job_id, run_epoch=job.run_epoch, steps=steps, llm_calls=llm_calls
        )

    def _advance(self, target: JobState, reason: FailureReason | None = None) -> None:
        transition_job(
            self.db, self.job_id, target, run_epoch=self.job.run_epoch, failure_reason=reason
        )

    # -- the loop ---------------------------------------------------------------------

    def run(self, *, max_steps: int | None = None) -> CurationJob:
        """Drain steps until the job reaches a terminal state."""
        taken = 0
        while not self.job.is_terminal:
            if max_steps is not None and taken >= max_steps:
                break
            self.step()
            taken += 1
        return self.job

    def step(self) -> StepOutcome:
        """One unit of progress. See the module docstring for the contract."""
        job = self.job
        try:
            return self._dispatch(job)
        except BudgetExhausted as error:
            record_event(self.db, self.job_id, "budget_exhausted", str(error))
            self._advance(JobState.FAILED, FailureReason.BUDGET_EXHAUSTED)
            return StepOutcome(False, str(error), self.job.state)
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
        recover_job(
            self.db, self.job, self.copy, self.machine, list_issues(self.db, self.job_id)
        )

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

        block = pending[0]
        self._spend(llm_calls=1)
        result = initial_auditor.audit_block(
            self.client,
            block=block,
            conventions=parsed.conventions,
            deterministic_findings=self._findings_for_block(parsed, block),
            seed_claims=self.seed_claims,
            job_id=self.job_id,
            taint=self.taint,
        )
        opened = self._open_issues(result.findings, source=IssueSource.INITIAL_AUDITOR)
        for refuted in result.refuted:
            record_event(
                self.db, self.job_id, "claim_refuted",
                f"claim {refuted.claim_index}: {refuted.why}",
            )
        mark_block_done(self.db, self.job_id, block.block_id, "audited")
        return StepOutcome(
            True,
            f"audited {block.problem_name}: {opened} issue(s), "
            f"{len(result.refuted)} claim(s) refuted",
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
            block = pending[0]
            self._spend(llm_calls=1)
            result = independent_reviewer.sweep_block(
                self.client,
                block=block,
                conventions=parsed.conventions,
                deterministic_findings=self._findings_for_block(parsed, block),
                job_id=self.job_id,
                taint=self.taint,
            )
            opened = self._open_issues(
                result.findings,
                source=IssueSource.INDEPENDENT_REVIEWER,
                reviewer_role=ReviewerRole.INDEPENDENT_REVIEWER,
            )
            mark_block_done(self.db, self.job_id, block.block_id, "swept")
            return StepOutcome(
                True, f"swept {block.problem_name}: {opened} issue(s)",
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
            self.db, self.job_id, ArtifactKind.CORRECTED_WORKBOOK, str(corrected)
        )

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
        )
        report_path = outputs / "report.md"
        report_path.write_text(render_markdown(reports), encoding="utf-8")
        record_artifact(
            self.db, self.job_id, ArtifactKind.VALIDATION_REPORT, str(report_path)
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
        self._spend(llm_calls=1)

        try:
            result = writer.propose_patch(
                self.client,
                issue=issue,
                block=block,
                conventions=parsed.conventions,
                attempt_no=attempt.attempt_no,
                deterministic_findings=self._findings_for_block(parsed, block),
                reviewer_feedback=_latest_feedback(self.db, issue.issue_id),
                job_id=self.job_id,
                taint=self.taint,
            )
        except (ProviderError, writer.WriterProposedNothing) as error:
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
            return StepOutcome(True, f"writer call failed: {error}", state)

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

        try:
            apply_patch(self.db, self.job, self.copy, patch, block_id=issue.block_id)
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

        source_parse = self.source_workbook()
        current = self.current_workbook()
        original_block = source_parse.block_by_id(issue.block_id)
        current_block = current.block_by_id(issue.block_id)
        if original_block is None or current_block is None:
            save_issue(self.db, self.machine.exhausted(issue))
            return StepOutcome(True, "block vanished; escalating", state)

        context = known_issue_reviewer.build_context(
            issue=issue,
            original_block=original_block,
            current_block=current_block,
            conventions=current.conventions,
            deterministic_findings=self._findings_for_block(current, current_block),
        )
        self._spend(llm_calls=1)
        verdict = known_issue_reviewer.review(
            self.client,
            issue=issue,
            context=context,
            attempt_no=issue.attempts_used or 1,
            job_id=self.job_id,
            taint=self.taint,
            role=issue.reviewer_role,
        )
        insert_verdict(self.db, verdict)

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

    # -- queries -----------------------------------------------------------------------

    def _next_live_issue(self, role: ReviewerRole | None) -> Issue | None:
        from .persistence import next_issue_for_phase

        return next_issue_for_phase(
            self.db,
            self.job_id,
            sorted(LIVE_ISSUE_STATES, key=str),
            reviewer_role=role.value if role else None,
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
                elif self.machine.can_attempt(seen):
                    save_issue(self.db, self.machine.reopen(seen))
                    result.reopened += 1
                else:
                    save_issue(self.db, self.machine.reopen(seen))
                    result.absorbed += 1
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
    from .models import ReviewVerdict

    row = db.connection.execute(
        "SELECT payload_json FROM review_verdicts WHERE issue_id = ? "
        "ORDER BY decided_at DESC LIMIT 1",
        (issue_id,),
    ).fetchone()
    if row is None:
        return ""
    return ReviewVerdict.model_validate_json(row["payload_json"]).feedback


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
