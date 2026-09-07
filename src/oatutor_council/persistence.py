"""Durable state. Stdlib `sqlite3`, no ORM.

The pragmas are not defaults, they are the crash-safety argument:

* `WAL` so a status poll never blocks on an in-flight write.
* `synchronous=FULL` because the whole intent-then-write-then-commit design rests on a
  commit actually being durable. `NORMAL` can lose the last transactions on power loss,
  which would make an apply intent a suggestion rather than a record.
* `foreign_keys=ON` so an orphaned attempt or verdict is impossible rather than merely
  unlikely.
* `busy_timeout` so concurrent workers wait instead of failing.

**Every write uses `BEGIN IMMEDIATE`.** A deferred transaction that upgrades to a write
lock mid-way is the classic SQLite deadlock: two readers both decide to write, and
neither can. Taking the lock up front turns that into a short wait.

**Every write is fenced by `run_epoch`.** A worker whose lease expired may still be alive
and mid-step. Its updates carry the epoch it started with, so once the job has been
stolen and the epoch bumped, that worker's writes match zero rows and it exits instead of
clobbering the new owner's work.

Pydantic models are the domain format; rows are a *projection*. A field gets its own
column only when it is queried, filtered, indexed, or part of an invariant. Everything
else rides in `payload_json`, so adding a field to a model does not require a migration.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import uuid4

from .models import (
    ArtifactKind,
    ChangeRecord,
    CurationJob,
    FailureReason,
    Issue,
    IssueLedger,
    IssueState,
    JobState,
    Patch,
    RepairAttempt,
    ReviewVerdict,
    ValidationFinding,
)
from .state_machine import assert_job_transition

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS jobs (
    job_id                 TEXT PRIMARY KEY,
    state                  TEXT NOT NULL,
    failure_reason         TEXT,
    source_filename        TEXT NOT NULL DEFAULT '',
    source_sha256          TEXT NOT NULL DEFAULT '',
    instruction_filename   TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    -- Liveness, deliberately separate from pipeline position.
    run_epoch              INTEGER NOT NULL DEFAULT 0,
    lease_owner            TEXT,
    lease_expires_at       TEXT,
    heartbeat_at           TEXT,
    -- Fuses.
    validation_rounds_used INTEGER NOT NULL DEFAULT 0,
    steps_used             INTEGER NOT NULL DEFAULT 0,
    llm_calls_used         INTEGER NOT NULL DEFAULT 0,
    payload_json           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, lease_expires_at);

CREATE TABLE IF NOT EXISTS job_artifacts (
    job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256       TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, kind)
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id      TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    block_id      TEXT,
    state         TEXT NOT NULL,
    source        TEXT NOT NULL,
    severity      TEXT NOT NULL,
    reviewer_role TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    attempts_used INTEGER NOT NULL DEFAULT 0,
    interrupted_retries_used INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS issues_queue ON issues(job_id, state, reviewer_role);
-- One in-flight issue per block, so two issues never race on the same cell.
CREATE INDEX IF NOT EXISTS issues_block ON issues(job_id, block_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS issues_fingerprint ON issues(job_id, fingerprint);

CREATE TABLE IF NOT EXISTS repair_attempts (
    attempt_id  TEXT PRIMARY KEY,
    issue_id    TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    attempt_no  INTEGER NOT NULL,
    outcome     TEXT,
    patch_id    TEXT,
    verdict_id  TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (issue_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id    TEXT PRIMARY KEY,
    issue_id    TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    attempt_no  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cell_edits (
    edit_id    TEXT PRIMARY KEY,
    patch_id   TEXT NOT NULL REFERENCES patches(patch_id) ON DELETE CASCADE,
    row_index  INTEGER NOT NULL,
    column_index INTEGER NOT NULL,
    column_key TEXT,
    before_text TEXT NOT NULL,
    after_text  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_verdicts (
    verdict_id    TEXT PRIMARY KEY,
    issue_id      TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    reviewer_role TEXT NOT NULL,
    attempt_no    INTEGER NOT NULL,
    decision      TEXT NOT NULL,
    decided_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_records (
    change_id  TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    issue_id   TEXT,
    patch_id   TEXT,
    block_id   TEXT,
    row_index  INTEGER NOT NULL,
    column_index INTEGER NOT NULL,
    column_key TEXT,
    before_text TEXT NOT NULL,
    after_text  TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS change_records_job ON change_records(job_id);

-- Committed BEFORE any byte is written. Recovery reads the target cells to decide
-- roll-forward or re-apply; it never hashes the file, because openpyxl output is not
-- byte-reproducible and a hash could therefore never be precomputed.
CREATE TABLE IF NOT EXISTS apply_intents (
    intent_id  TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    issue_id   TEXT,
    patch_id   TEXT,
    run_epoch  INTEGER NOT NULL,
    state      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS apply_intents_open ON apply_intents(job_id, state);

CREATE TABLE IF NOT EXISTS validation_findings (
    finding_id TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    round_no   INTEGER NOT NULL,
    code       TEXT NOT NULL,
    severity   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    -- `content` (the workbook is wrong) or `integrity` (the output is not an accounted-for
    -- descendant of the source). Kept apart because they mean opposite things to a
    -- curator: one is work still to do, the other is a reason not to use the file at all.
    kind       TEXT NOT NULL DEFAULT 'content',
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS validation_findings_round ON validation_findings(job_id, round_no);

-- Which validation rounds actually ran, recorded whether or not they found anything.
--
-- The bug this exists for: a clean final round writes **no finding rows at all**, so
-- deriving "the latest round" from `MAX(round_no)` over `validation_findings` skipped it
-- entirely and answered with the previous round's rows. A job whose final validation found
-- nothing wrong reported the six findings it had already repaired -- the API telling a
-- curator their finished workbook was still broken, which is `GET /report` returning
-- `findings=()` all over again with the sign flipped.
--
-- A round that found nothing is a fact about the workbook and has to be stored as one.
-- Absence of rows cannot carry it: it is indistinguishable from a round that never ran.
CREATE TABLE IF NOT EXISTS validation_rounds (
    job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    round_no    INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'content',
    finding_count INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (job_id, kind, round_no)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id    TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    issue_id   TEXT,
    role       TEXT NOT NULL,
    model      TEXT NOT NULL,
    status     TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- What each scan claimed to have checked, row by row. Written for every graded row of
-- every scanned block, not only for the rows that failed.
--
-- **The failures were events long before the successes were rows, and that was the wrong
-- way round.** A job could report that nothing went unaccounted for and still be unable to
-- show what any auditor computed for any row -- so the schema described these records as
-- inspectable while the only inspectable ones were the missing ones.
--
-- `call_id` is the point. A coverage row that cannot name the physical invocation behind
-- it is an assertion with no provenance, and the trail already stores the exact prompt of
-- every call, so the two together answer "what was this model shown, and what did it say
-- it checked" for any row of any block.
CREATE TABLE IF NOT EXISTS coverage_records (
    record_id   TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    -- Nullable: an offline client records no call, and a coverage row is still worth
    -- keeping without one.
    call_id     TEXT,
    block_id    TEXT NOT NULL,
    phase       TEXT NOT NULL,
    row_no      INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS coverage_lookup
    ON coverage_records(job_id, phase, block_id);

-- Agent reasoning, stored apart from anything a reviewer prompt is built from. The
-- separation is structural: no reviewer context type has a field that could reference
-- this table.
CREATE TABLE IF NOT EXISTS private_blobs (
    blob_id    TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    issue_id   TEXT,
    role       TEXT NOT NULL,
    -- The taint-registry label. Stored so a resumed job can rebuild the registry with
    -- the same labels it had, which is what makes a violation message name the thing
    -- that leaked rather than an anonymous blob.
    label      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS private_blobs_job ON private_blobs(job_id);

-- Behaviour settings pinned **for this job**, the same way prompt versions are. Without
-- this, a worker restarted after an `.env` edit gives one job half its blocks at batch 1
-- and the other half at batch 10, and the report describes a run that never happened as a
-- whole.
CREATE TABLE IF NOT EXISTS job_settings (
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, name)
);

-- Which prompt version each role is pinned to **for this job**. Without this, adding
-- `writer.v2.md` mid-flight would mean a job's first repair was made under v1 and its
-- second under v2, and the audit trail would say v2 for both.
CREATE TABLE IF NOT EXISTS job_prompts (
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, role)
);

-- Which blocks each phase has finished with. A block audited with zero findings leaves
-- no issue behind, so without this row the phase could not tell "already examined" from
-- "not yet reached" and would re-audit it on every resume.
CREATE TABLE IF NOT EXISTS block_progress (
    job_id   TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    block_id TEXT NOT NULL,
    phase    TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (job_id, block_id, phase)
);

-- The curator's instruction document, decomposed. Held here rather than in the worker
-- because a job that resumes on another process must audit against the *same*
-- instructions: seed claims living only in worker memory means a resumed job silently
-- audits against none, and reports "no issues found" for a document nobody read.
--
-- `document_sha256` is stored per segment so the file cannot be swapped mid-job without
-- the change being visible against what was actually read.
CREATE TABLE IF NOT EXISTS instruction_segments (
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    segment_index   INTEGER NOT NULL,
    text            TEXT NOT NULL,
    provenance      TEXT NOT NULL DEFAULT '',
    document_format TEXT NOT NULL DEFAULT '',
    document_sha256 TEXT NOT NULL DEFAULT '',
    truncated       INTEGER NOT NULL DEFAULT 0,
    -- rules | errata | notes. What the passage is *for*, which decides who sees it: a
    -- governing rule goes to the auditor, Writer and reviewers as policy; a suspected
    -- defect goes to the auditor as a claim; background goes only to the auditor as
    -- context. Sending a rule to thirty blocks as a claim is how a
    -- policy statement ends up marked refuted by twenty-nine of them.
    purpose         TEXT NOT NULL DEFAULT 'errata',
    PRIMARY KEY (job_id, segment_index)
);

-- What each block concluded about each claim. Structured records rather than log lines:
-- the final report has to tell a curator that the defect they described was found, or
-- looked for and not there, and a free-text event cannot be counted or grouped.
--
-- Keyed by (job, claim, block) so re-auditing a block after a crash overwrites its own
-- verdict instead of appending a second one.
CREATE TABLE IF NOT EXISTS claim_results (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    block_id      TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    at            TEXT NOT NULL,
    PRIMARY KEY (job_id, segment_index, block_id)
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id   TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, at);
"""


class ConcurrencyError(Exception):
    """A write was fenced out, or a job was taken by someone else."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    """One SQLite file, one connection per thread, one in-process write lock.

    SQLite connections are not safe to share across threads, and the write lock keeps
    two threads in *this* process from racing each other into a busy-timeout wait that
    the database would otherwise have to arbitrate.
    """

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._initialise()

    # -- connections ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = sqlite3.connect(
                self.path, isolation_level=None, check_same_thread=False
            )
            existing.row_factory = sqlite3.Row
            existing.execute("PRAGMA journal_mode=WAL")
            existing.execute("PRAGMA synchronous=FULL")
            existing.execute("PRAGMA foreign_keys=ON")
            existing.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            self._local.connection = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. `BEGIN IMMEDIATE`, always."""
        with self._write_lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def _initialise(self) -> None:
        # `executescript` implicitly commits any open transaction before it runs, so it
        # cannot live inside `write()`. The connection is in autocommit mode
        # (`isolation_level=None`), and every statement here is `IF NOT EXISTS`, so
        # concurrent initialisation is safe.
        self.connection.executescript(SCHEMA)
        with self.write() as connection:
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )


# --------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------


def create_job(db: Database, job: CurationJob) -> CurationJob:
    with db.write() as connection:
        connection.execute(
            """INSERT INTO jobs (job_id, state, failure_reason, source_filename,
                   source_sha256, instruction_filename, created_at, updated_at,
                   run_epoch, validation_rounds_used, steps_used, llm_calls_used)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.job_id,
                job.state.value,
                job.failure_reason.value if job.failure_reason else None,
                job.source_filename,
                job.source_sha256,
                job.instruction_filename,
                _iso(job.created_at),
                _iso(job.updated_at),
                job.run_epoch,
                job.validation_rounds_used,
                job.steps_used,
                job.llm_calls_used,
            ),
        )
    record_event(db, job.job_id, "created", job.source_filename)
    return job


def get_job(db: Database, job_id: str) -> CurationJob | None:
    row = db.connection.execute(
        "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return _job_from_row(row) if row else None


def list_jobs(db: Database, *, limit: int = 20) -> tuple[CurationJob, ...]:
    """Newest durable jobs for the local curator's history page.

    This deliberately returns job metadata only. Workbook contents, private model
    reasoning, prompts, and filesystem paths belong behind the per-job endpoints and
    never become part of a convenient list response.
    """
    bounded = max(1, min(int(limit), 100))
    rows = db.connection.execute(
        """SELECT * FROM jobs
           ORDER BY created_at DESC, job_id DESC
           LIMIT ?""",
        (bounded,),
    ).fetchall()
    return tuple(_job_from_row(row) for row in rows)


def _job_from_row(row: sqlite3.Row) -> CurationJob:
    return CurationJob(
        job_id=row["job_id"],
        state=JobState(row["state"]),
        failure_reason=FailureReason(row["failure_reason"])
        if row["failure_reason"]
        else None,
        source_filename=row["source_filename"],
        source_sha256=row["source_sha256"],
        instruction_filename=row["instruction_filename"],
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
        run_epoch=row["run_epoch"],
        lease_owner=row["lease_owner"],
        lease_expires_at=_parse(row["lease_expires_at"]),
        heartbeat_at=_parse(row["heartbeat_at"]),
        validation_rounds_used=row["validation_rounds_used"],
        steps_used=row["steps_used"],
        llm_calls_used=row["llm_calls_used"],
    )


def transition_job(
    db: Database,
    job_id: str,
    target: JobState,
    *,
    run_epoch: int,
    failure_reason: FailureReason | None = None,
) -> CurationJob:
    """Move a job forward, refusing an illegal transition and a stale epoch.

    The legality check reads the current state inside the same transaction as the write,
    so two workers cannot both see `AUDITING` and both advance it.
    """
    with db.write() as connection:
        row = connection.execute(
            "SELECT state, run_epoch FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ConcurrencyError(f"job {job_id} does not exist")
        if row["run_epoch"] != run_epoch:
            raise ConcurrencyError(
                f"job {job_id} moved to epoch {row['run_epoch']}; this worker holds "
                f"{run_epoch} and must stop"
            )
        assert_job_transition(JobState(row["state"]), target)
        connection.execute(
            """UPDATE jobs SET state = ?, failure_reason = ?, updated_at = ?
               WHERE job_id = ? AND run_epoch = ?""",
            (
                target.value,
                failure_reason.value if failure_reason else None,
                _iso(_now()),
                job_id,
                run_epoch,
            ),
        )
    record_event(db, job_id, "transition", target.value)
    return get_job(db, job_id)


def acquire_lease(
    db: Database, job_id: str, owner: str, *, lease_seconds: int
) -> CurationJob:
    """Take ownership of a job, bumping `run_epoch` to fence out the previous owner.

    A lease is only stealable once it has expired. The epoch bump is what makes stealing
    safe: the previous worker may still be alive and mid-step, but every write it makes
    carries the old epoch and will match zero rows.
    """
    now = _now()
    with db.write() as connection:
        row = connection.execute(
            "SELECT state, run_epoch, lease_owner, lease_expires_at FROM jobs "
            "WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ConcurrencyError(f"job {job_id} does not exist")

        expires = _parse(row["lease_expires_at"])
        if row["lease_owner"] and expires and expires > now and row["lease_owner"] != owner:
            raise ConcurrencyError(
                f"job {job_id} is leased by {row['lease_owner']} until {expires.isoformat()}"
            )

        epoch = row["run_epoch"] + 1
        connection.execute(
            """UPDATE jobs SET lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?,
                   run_epoch = ?, updated_at = ? WHERE job_id = ?""",
            (
                owner,
                _iso(now + timedelta(seconds=lease_seconds)),
                _iso(now),
                epoch,
                _iso(now),
                job_id,
            ),
        )
    record_event(db, job_id, "lease_acquired", owner)
    return get_job(db, job_id)


def heartbeat(
    db: Database, job_id: str, owner: str, *, run_epoch: int, lease_seconds: int
) -> None:
    now = _now()
    with db.write() as connection:
        cursor = connection.execute(
            """UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ?
               WHERE job_id = ? AND lease_owner = ? AND run_epoch = ?""",
            (
                _iso(now),
                _iso(now + timedelta(seconds=lease_seconds)),
                job_id,
                owner,
                run_epoch,
            ),
        )
        if cursor.rowcount == 0:
            raise ConcurrencyError(
                f"job {job_id} is no longer owned by {owner} at epoch {run_epoch}"
            )


def assert_lease_held(db: Database, job_id: str, owner: str, *, run_epoch: int) -> None:
    """Refuse to continue if this worker no longer owns the job.

    Every *database* write is already fenced by `run_epoch`, so a fenced worker cannot
    corrupt durable state. The working copy is not a database write: `os.replace` does not
    consult the `jobs` table, so a worker whose lease was stolen mid-step could still
    rewrite a workbook the new owner is reading. This is the check that closes that gap,
    and it belongs immediately before the file is touched rather than at the top of the
    step -- the whole point is the time that passed since then.

    Ownership is only compared when the job *has* an owner. A job with no lease at all is
    a job nobody is competing for -- an embedded run, the offline demo, a test driving the
    council directly -- and demanding a lease there would make leasing mandatory to do any
    work rather than mandatory to do it concurrently. The epoch is checked unconditionally,
    because a bumped epoch means somebody took the job whatever the row says now.
    """
    row = db.connection.execute(
        "SELECT run_epoch, lease_owner FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise ConcurrencyError(f"job {job_id} does not exist")
    stolen = row["lease_owner"] and row["lease_owner"] != owner
    if row["run_epoch"] != run_epoch or stolen:
        raise ConcurrencyError(
            f"job {job_id} is now owned by {row['lease_owner']!r} at epoch "
            f"{row['run_epoch']}; this worker holds {owner!r} at {run_epoch} and must stop"
        )


def release_lease(db: Database, job_id: str, owner: str, *, run_epoch: int) -> None:
    with db.write() as connection:
        connection.execute(
            """UPDATE jobs SET lease_owner = NULL, lease_expires_at = NULL
               WHERE job_id = ? AND lease_owner = ? AND run_epoch = ?""",
            (job_id, owner, run_epoch),
        )


def claimable_jobs(db: Database, *, limit: int = 10) -> tuple[CurationJob, ...]:
    """Jobs with no live lease that are not finished.

    This is the durable queue. A submission lost to a crash reappears here without any
    coordination, because nothing about a job's claimability is held in memory.
    """
    now = _iso(_now())
    rows = db.connection.execute(
        """SELECT * FROM jobs
           WHERE state NOT IN ('succeeded','needs_human_attention','failed','cancelled')
             AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?)
           ORDER BY created_at LIMIT ?""",
        (now, limit),
    ).fetchall()
    return tuple(_job_from_row(row) for row in rows)


def increment_counters(
    db: Database,
    job_id: str,
    *,
    run_epoch: int,
    steps: int = 0,
    llm_calls: int = 0,
    validation_rounds: int = 0,
) -> None:
    with db.write() as connection:
        cursor = connection.execute(
            """UPDATE jobs SET steps_used = steps_used + ?,
                   llm_calls_used = llm_calls_used + ?,
                   validation_rounds_used = validation_rounds_used + ?,
                   updated_at = ?
               WHERE job_id = ? AND run_epoch = ?""",
            (steps, llm_calls, validation_rounds, _iso(_now()), job_id, run_epoch),
        )
        if cursor.rowcount == 0:
            raise ConcurrencyError(f"job {job_id} is not at epoch {run_epoch}")


# --------------------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------------------


def insert_issue(db: Database, issue: Issue) -> Issue | None:
    """Insert an issue, or return `None` if its fingerprint is already tracked.

    The unique index on `(job_id, fingerprint)` is the termination guarantee expressed as
    a constraint rather than as a convention: a rediscovered defect cannot open a second
    issue with a second attempt budget, however many code paths try.
    """
    try:
        with db.write() as connection:
            connection.execute(
                """INSERT INTO issues (issue_id, job_id, block_id, state, source,
                       severity, reviewer_role, fingerprint, attempts_used,
                       interrupted_retries_used, created_at, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    issue.issue_id,
                    issue.job_id,
                    issue.block_id,
                    issue.state.value,
                    issue.source.value,
                    issue.severity.value,
                    issue.reviewer_role.value,
                    issue.fingerprint,
                    issue.attempts_used,
                    issue.interrupted_retries_used,
                    _iso(issue.created_at),
                    issue.model_dump_json(),
                ),
            )
    except sqlite3.IntegrityError:
        return None
    return issue


def save_issue(db: Database, issue: Issue) -> None:
    with db.write() as connection:
        connection.execute(
            """UPDATE issues SET state = ?, reviewer_role = ?, attempts_used = ?,
                   interrupted_retries_used = ?, payload_json = ?
               WHERE issue_id = ?""",
            (
                issue.state.value,
                issue.reviewer_role.value,
                issue.attempts_used,
                issue.interrupted_retries_used,
                issue.model_dump_json(),
                issue.issue_id,
            ),
        )


def get_issue(db: Database, issue_id: str) -> Issue | None:
    row = db.connection.execute(
        "SELECT payload_json FROM issues WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    return Issue.model_validate_json(row["payload_json"]) if row else None


def list_issues(db: Database, job_id: str) -> tuple[Issue, ...]:
    rows = db.connection.execute(
        "SELECT payload_json FROM issues WHERE job_id = ? ORDER BY created_at", (job_id,)
    ).fetchall()
    return tuple(Issue.model_validate_json(row["payload_json"]) for row in rows)


def load_ledger(db: Database, job_id: str) -> IssueLedger:
    return IssueLedger(job_id=job_id, issues=list_issues(db, job_id))


def next_issue_for_phase(
    db: Database, job_id: str, states: Sequence[IssueState], reviewer_role: str | None = None
) -> Issue | None:
    """The phase queue predicate, as a query.

    A phase is not a loop with a cursor. It is this predicate, drained until it returns
    nothing -- which is why a phase is idempotent, resumable, and impossible to restart
    in the wrong place after a crash.

    The `NOT EXISTS` clause enforces one in-flight issue per block, so two issues never
    race on the same cell and burn an attempt on a confusing `BEFORE_MISMATCH`.
    """
    placeholders = ",".join("?" for _ in states)
    parameters: list[Any] = [job_id, *[s.value for s in states]]
    role_clause = ""
    if reviewer_role is not None:
        role_clause = " AND i.reviewer_role = ?"
        parameters.append(reviewer_role)

    row = db.connection.execute(
        f"""SELECT i.payload_json FROM issues i
            WHERE i.job_id = ? AND i.state IN ({placeholders}){role_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM issues busy
                  WHERE busy.job_id = i.job_id
                    AND busy.block_id IS NOT NULL
                    AND busy.block_id = i.block_id
                    AND busy.issue_id <> i.issue_id
                    AND busy.state IN ('awaiting_patch','patch_proposed','patch_approved','applying',
                                       'patch_applied','awaiting_review')
              )
            ORDER BY
              CASE
                -- A displaced row or broken block boundary can make every semantic cell
                -- below it look wrong, so the small root set remains first.
                WHEN json_extract(i.payload_json, '$.rule_codes[0]') IN (
                    'ROW_SHIFT_RIGHT','COLUMN_SHIFT','BLOCK_BOUNDARY_DISAGREEMENT',
                    'PROBLEM_NAME_MISMATCH_IN_BLOCK','MISSING_PROBLEM_NAME',
                    'ROW_HAS_FORBIDDEN_CONTENT','PROBLEM_ROW_HAS_GRADED_CONTENT'
                ) THEN 0
                WHEN i.severity = 'blocking' THEN 1
                -- Remaining deterministic errors precede a same-cell model duplicate.
                WHEN json_extract(i.payload_json, '$.rule_codes[0]') NOT IN (
                    'AUDITOR_FINDING','INDEPENDENT_FINDING','FINAL_VERIFICATION_FINDING'
                ) AND i.severity = 'error' THEN 2
                -- On the first large blind workbook, semantic findings also sat behind
                -- routine warnings until subscription usage was exhausted. Once known
                -- errors are settled, math and instruction defects come before polish.
                WHEN json_extract(i.payload_json, '$.rule_codes[0]') IN (
                    'AUDITOR_FINDING','INDEPENDENT_FINDING','FINAL_VERIFICATION_FINDING'
                ) THEN 3
                ELSE 4
              END,
              i.created_at
            LIMIT 1""",
        parameters,
    ).fetchone()
    return Issue.model_validate_json(row["payload_json"]) if row else None


def count_issues_in_states(
    db: Database, job_id: str, states: Sequence[IssueState]
) -> int:
    placeholders = ",".join("?" for _ in states)
    row = db.connection.execute(
        f"SELECT COUNT(*) AS n FROM issues WHERE job_id = ? AND state IN ({placeholders})",
        [job_id, *[s.value for s in states]],
    ).fetchone()
    return row["n"]


def fingerprint_states(db: Database, job_id: str) -> dict[str, IssueState]:
    rows = db.connection.execute(
        "SELECT fingerprint, state FROM issues WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {row["fingerprint"]: IssueState(row["state"]) for row in rows}


# --------------------------------------------------------------------------------------
# Attempts, patches, verdicts
# --------------------------------------------------------------------------------------


def insert_attempt(db: Database, attempt: RepairAttempt) -> None:
    """Record an attempt. Called **before** the Writer is invoked, never after."""
    with db.write() as connection:
        connection.execute(
            """INSERT INTO repair_attempts (attempt_id, issue_id, attempt_no, outcome,
                   patch_id, verdict_id, started_at, finished_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                attempt.attempt_id,
                attempt.issue_id,
                attempt.attempt_no,
                attempt.outcome.value if attempt.outcome else None,
                attempt.patch_id,
                attempt.verdict_id,
                _iso(attempt.started_at),
                _iso(attempt.finished_at),
                attempt.model_dump_json(),
            ),
        )


def next_attempt_number(db: Database, issue_id: str) -> int:
    """The next sequence number for this issue's attempts.

    Deliberately **not** `attempts_used + 1`. Those are different quantities: the budget
    can be refunded when an attempt is lost to infrastructure, but the sequence number
    records what actually happened and must keep climbing, or a refunded attempt collides
    with the row already written for the one it replaces.
    """
    row = db.connection.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) AS highest FROM repair_attempts "
        "WHERE issue_id = ?",
        (issue_id,),
    ).fetchone()
    return row["highest"] + 1


def settle_attempt(db: Database, attempt: RepairAttempt) -> None:
    with db.write() as connection:
        connection.execute(
            """UPDATE repair_attempts SET outcome = ?, patch_id = ?, verdict_id = ?,
                   finished_at = ?, payload_json = ? WHERE attempt_id = ?""",
            (
                attempt.outcome.value if attempt.outcome else None,
                attempt.patch_id,
                attempt.verdict_id,
                _iso(attempt.finished_at or _now()),
                attempt.model_dump_json(),
                attempt.attempt_id,
            ),
        )


def list_attempts(db: Database, job_id: str) -> tuple[RepairAttempt, ...]:
    rows = db.connection.execute(
        """SELECT a.payload_json FROM repair_attempts a
           JOIN issues i ON i.issue_id = a.issue_id
           WHERE i.job_id = ? ORDER BY a.started_at""",
        (job_id,),
    ).fetchall()
    return tuple(RepairAttempt.model_validate_json(r["payload_json"]) for r in rows)


def open_attempts(db: Database, job_id: str) -> tuple[RepairAttempt, ...]:
    """Attempts that started and never finished -- the signature of a crash mid-call."""
    rows = db.connection.execute(
        """SELECT a.payload_json FROM repair_attempts a
           JOIN issues i ON i.issue_id = a.issue_id
           WHERE i.job_id = ? AND a.finished_at IS NULL""",
        (job_id,),
    ).fetchall()
    return tuple(RepairAttempt.model_validate_json(r["payload_json"]) for r in rows)


def insert_patch(db: Database, patch: Patch) -> None:
    with db.write() as connection:
        connection.execute(
            "INSERT INTO patches (patch_id, issue_id, attempt_no, created_at, payload_json) "
            "VALUES (?,?,?,?,?)",
            (
                patch.patch_id,
                patch.issue_id,
                patch.attempt_no,
                _iso(_now()),
                patch.model_dump_json(),
            ),
        )
        for edit in patch.edits:
            connection.execute(
                """INSERT INTO cell_edits (edit_id, patch_id, row_index, column_index,
                       column_key, before_text, after_text) VALUES (?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    patch.patch_id,
                    edit.row,
                    edit.column,
                    edit.column_key.value if edit.column_key else None,
                    edit.before,
                    edit.after,
                ),
            )


def get_patch(db: Database, patch_id: str) -> Patch | None:
    row = db.connection.execute(
        "SELECT payload_json FROM patches WHERE patch_id = ?", (patch_id,)
    ).fetchone()
    return Patch.model_validate_json(row["payload_json"]) if row else None


def insert_verdict(db: Database, verdict: ReviewVerdict) -> None:
    with db.write() as connection:
        connection.execute(
            """INSERT INTO review_verdicts (verdict_id, issue_id, reviewer_role,
                   attempt_no, decision, decided_at, payload_json) VALUES (?,?,?,?,?,?,?)""",
            (
                verdict.verdict_id,
                verdict.issue_id,
                verdict.reviewer_role.value,
                verdict.attempt_no,
                verdict.decision.value,
                _iso(verdict.decided_at),
                verdict.model_dump_json(),
            ),
        )


def insert_verdicts(db: Database, verdicts: Sequence[ReviewVerdict]) -> None:
    """Persist one coordinated review response atomically.

    A block reviewer returns several decisions in one physical response. Committing them
    one at a time creates a crash state in which the response was paid for but only some
    of its evidence exists; the replacement worker then has no safe way to reconstruct
    the missing decisions. Either the whole response is durable or none of it is.
    """
    with db.write() as connection:
        for verdict in verdicts:
            connection.execute(
                """INSERT INTO review_verdicts (verdict_id, issue_id, reviewer_role,
                       attempt_no, decision, decided_at, payload_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    verdict.verdict_id,
                    verdict.issue_id,
                    verdict.reviewer_role.value,
                    verdict.attempt_no,
                    verdict.decision.value,
                    _iso(verdict.decided_at),
                    verdict.model_dump_json(),
                ),
            )


def list_verdicts(db: Database, job_id: str) -> tuple[ReviewVerdict, ...]:
    rows = db.connection.execute(
        """SELECT v.payload_json FROM review_verdicts v
           JOIN issues i ON i.issue_id = v.issue_id
           WHERE i.job_id = ? ORDER BY v.decided_at""",
        (job_id,),
    ).fetchall()
    return tuple(ReviewVerdict.model_validate_json(r["payload_json"]) for r in rows)


# --------------------------------------------------------------------------------------
# Changes and apply intents
# --------------------------------------------------------------------------------------


def record_changes(
    db: Database, job_id: str, changes: Sequence[ChangeRecord]
) -> None:
    with db.write() as connection:
        for change in changes:
            connection.execute(
                """INSERT INTO change_records (change_id, job_id, issue_id, patch_id,
                       block_id, row_index, column_index, column_key, before_text,
                       after_text, applied_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    change.change_id,
                    job_id,
                    change.issue_id,
                    change.patch_id,
                    change.block_id,
                    change.row,
                    change.column,
                    change.column_key.value if change.column_key else None,
                    change.before,
                    change.after,
                    _iso(change.applied_at),
                ),
            )


def list_changes(db: Database, job_id: str) -> tuple[ChangeRecord, ...]:
    rows = db.connection.execute(
        "SELECT * FROM change_records WHERE job_id = ? ORDER BY applied_at, row_index",
        (job_id,),
    ).fetchall()
    from .models import ColumnKey

    return tuple(
        ChangeRecord(
            change_id=row["change_id"],
            issue_id=row["issue_id"],
            patch_id=row["patch_id"],
            block_id=row["block_id"],
            row=row["row_index"],
            column=row["column_index"],
            column_key=ColumnKey(row["column_key"]) if row["column_key"] else None,
            before=row["before_text"],
            after=row["after_text"],
            applied_at=_parse(row["applied_at"]),
        )
        for row in rows
    )


def open_apply_intent(
    db: Database,
    *,
    job_id: str,
    issue_id: str | None,
    patch_id: str | None,
    run_epoch: int,
    edits: Sequence[dict[str, Any]],
    block_id: str | None = None,
) -> str:
    """Commit the intent to write **before** any byte is written.

    This is step one of intent-then-file-then-commit. If the process dies after the file
    lands but before the changes are committed, this row is the only evidence the write
    was ever authorised -- recovery reads the target cells and decides whether to roll
    forward or re-apply.
    """
    intent_id = uuid4().hex
    with db.write() as connection:
        connection.execute(
            """INSERT INTO apply_intents (intent_id, job_id, issue_id, patch_id,
                   run_epoch, state, created_at, payload_json) VALUES (?,?,?,?,?,?,?,?)""",
            (
                intent_id,
                job_id,
                issue_id,
                patch_id,
                run_epoch,
                "open",
                _iso(_now()),
                json.dumps({"edits": list(edits), "block_id": block_id}),
            ),
        )
    return intent_id


def settle_apply_intent(db: Database, intent_id: str, state: str) -> None:
    with db.write() as connection:
        connection.execute(
            "UPDATE apply_intents SET state = ?, settled_at = ? WHERE intent_id = ?",
            (state, _iso(_now()), intent_id),
        )


def open_apply_intents(db: Database, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = db.connection.execute(
        "SELECT * FROM apply_intents WHERE job_id = ? AND state = 'open'", (job_id,)
    ).fetchall()
    intents = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        intents.append({
            "intent_id": row["intent_id"],
            "issue_id": row["issue_id"],
            "patch_id": row["patch_id"],
            "run_epoch": row["run_epoch"],
            "edits": payload["edits"],
            # Absent on intents created before this field existed.
            "block_id": payload.get("block_id"),
        })
    return tuple(intents)


# --------------------------------------------------------------------------------------
# Findings, artifacts, events
# --------------------------------------------------------------------------------------


def record_findings(
    db: Database,
    job_id: str,
    round_no: int,
    findings: Sequence[ValidationFinding],
    fingerprints: Sequence[str],
    *,
    kind: str = "content",
) -> None:
    """Replace this round's findings of this kind.

    Replace rather than append: a round re-run after a crash must not double every
    finding it already recorded, and a half-finished round has nothing worth keeping.
    """
    with db.write() as connection:
        connection.execute(
            "DELETE FROM validation_findings WHERE job_id = ? AND round_no = ? AND kind = ?",
            (job_id, round_no, kind),
        )
        for finding, mark in zip(findings, fingerprints):
            connection.execute(
                """INSERT INTO validation_findings (finding_id, job_id, round_no, code,
                       severity, fingerprint, kind, payload_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    job_id,
                    round_no,
                    finding.code,
                    finding.severity.value,
                    mark,
                    kind,
                    finding.model_dump_json(),
                ),
            )
        # The marker, written in the same transaction and **unconditionally**. A round
        # that found nothing has to leave a trace, or `latest_findings` cannot tell it
        # from a round that never happened and answers with stale rows. `REPLACE` for the
        # same reason the delete above exists: a re-run round overwrites its own marker.
        connection.execute(
            """INSERT OR REPLACE INTO validation_rounds
                   (job_id, round_no, kind, finding_count, recorded_at)
               VALUES (?,?,?,?,?)""",
            (job_id, round_no, kind, len(findings), _iso(_now())),
        )


def list_findings(
    db: Database, job_id: str, round_no: int | None = None, *, kind: str = "content"
) -> tuple[ValidationFinding, ...]:
    if round_no is None:
        rows = db.connection.execute(
            "SELECT payload_json FROM validation_findings WHERE job_id = ? AND kind = ? "
            "ORDER BY round_no DESC",
            (job_id, kind),
        ).fetchall()
    else:
        rows = db.connection.execute(
            "SELECT payload_json FROM validation_findings WHERE job_id = ? "
            "AND round_no = ? AND kind = ?",
            (job_id, round_no, kind),
        ).fetchall()
    return tuple(ValidationFinding.model_validate_json(r["payload_json"]) for r in rows)


def latest_findings(
    db: Database, job_id: str, *, kind: str = "content"
) -> tuple[ValidationFinding, ...]:
    """The most recent round's findings, which are the only ones still true.

    Every round re-runs the whole rule set over the whole workbook, so round two's
    findings are not additional to round one's -- they are what is left after the repairs
    round one asked for. Concatenating rounds would report defects that were fixed two
    rounds ago as though they were still there, which is the same lie as reporting
    success over a broken workbook, pointed the other way.

    **The latest round is read from `validation_rounds`, not from the findings.** Deriving
    it from `MAX(round_no)` over `validation_findings` meant the one round that matters
    most -- a final validation that found nothing -- was invisible, because it writes no
    rows. The query then landed on the previous round and returned the findings the job
    had already repaired, so a clean workbook was reported as still carrying every defect
    it arrived with. An empty round is a result; it just is not a row.
    """
    row = db.connection.execute(
        "SELECT MAX(round_no) AS latest FROM validation_rounds "
        "WHERE job_id = ? AND kind = ?",
        (job_id, kind),
    ).fetchone()
    if row is not None and row["latest"] is not None:
        return list_findings(db, job_id, int(row["latest"]), kind=kind)

    # No marker: a job recorded before this table existed. Fall back to the old derivation
    # rather than claiming the job had no findings -- for those jobs it is the only
    # evidence there is, and it errs towards reporting work that may already be done,
    # which is the safe direction. New rounds always leave a marker.
    row = db.connection.execute(
        "SELECT MAX(round_no) AS latest FROM validation_findings "
        "WHERE job_id = ? AND kind = ?",
        (job_id, kind),
    ).fetchone()
    if row is None or row["latest"] is None:
        return ()
    return list_findings(db, job_id, int(row["latest"]), kind=kind)


def token_usage(db: Database, job_id: str) -> dict[str, int]:
    """What this job actually cost, added up from the recorded calls.

    Derived rather than stored: a running total kept on the job would be a second source
    of truth that a crash between the call and the increment could put permanently out of
    step with the rows it is meant to summarise.
    """
    totals = {
        "calls": 0,
        "failed_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        # Carried through to the report rather than left in the per-call blob: part of any
        # apparent saving comes from the provider's own prompt caching rather than from
        # batching, and a number nobody aggregates cannot answer which.
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "duration_ms": 0,
    }
    for call in list_llm_calls(db, job_id):
        totals["calls"] += 1
        if call["status"] != "completed":
            totals["failed_calls"] += 1
        usage = call["payload"].get("usage") or {}
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total_tokens",
            "duration_ms",
        ):
            value = usage.get(name)
            if isinstance(value, (int, float)):
                totals[name] += int(value)
    return totals


def output_tokens_used(db: Database, job_id: str) -> int:
    """Provider-reported generated tokens without loading historical prompt payloads.

    The call fuse asks this before every physical invocation. Reusing `token_usage` there
    would deserialize every system prompt and user payload on every call, turning a
    long-running job into quadratic local work merely to read one number.

    Usage values are normalized by the recorder before persistence. Older or failed rows
    may have no value; SQLite's numeric cast plus `COALESCE` treats those as zero, matching
    `token_usage`'s historical behavior.
    """
    row = db.connection.execute(
        """SELECT COALESCE(SUM(
                   CASE WHEN json_type(payload_json, '$.usage.output_tokens')
                                  IN ('integer', 'real')
                        THEN CAST(json_extract(payload_json, '$.usage.output_tokens') AS INTEGER)
                        ELSE 0 END
               ), 0) AS total
           FROM llm_calls
           WHERE job_id = ?""",
        (job_id,),
    ).fetchone()
    return int(row["total"]) if row else 0


def relative_to_data_root(path: str | Path, data_root: Path) -> str:
    """Store where a file is *within the data root*, never where it is on this machine.

    The column has been called `relative_path` since the first schema; it was being handed
    absolute paths anyway, which quietly tied every job to the filesystem layout of the
    machine that created it. Move `DATA_ROOT` -- to a mounted volume, a restored backup, a
    different container -- and every artefact lookup fails, on rows that look perfectly
    healthy.

    A path outside the data root is stored as it is. That should not happen, and storing a
    misleading relative path would make it harder to see when it does.
    """
    try:
        return str(Path(path).resolve().relative_to(Path(data_root).resolve()))
    except ValueError:
        return str(path)


def record_artifact(
    db: Database,
    job_id: str,
    kind: ArtifactKind,
    relative_path: str,
    sha256: str = "",
    *,
    data_root: Path | None = None,
) -> None:
    if data_root is not None:
        relative_path = relative_to_data_root(relative_path, data_root)
    with db.write() as connection:
        connection.execute(
            """INSERT INTO job_artifacts (job_id, kind, relative_path, sha256, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id, kind) DO UPDATE SET
                   relative_path = excluded.relative_path,
                   sha256 = excluded.sha256,
                   created_at = excluded.created_at""",
            (job_id, kind.value, relative_path, sha256, _iso(_now())),
        )


def list_artifacts(
    db: Database, job_id: str, *, data_root: Path | None = None
) -> dict[ArtifactKind, str]:
    """Artefact paths, resolved against the data root the caller is using now.

    Passing `data_root` is what makes a job survive its storage moving. Omitting it
    returns the stored value unchanged, which is what a caller wants when it is asking
    what the row says rather than where the file is.
    """
    rows = db.connection.execute(
        "SELECT kind, relative_path FROM job_artifacts WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {
        ArtifactKind(row["kind"]): (
            str(Path(data_root) / row["relative_path"])
            if data_root is not None and not Path(row["relative_path"]).is_absolute()
            else row["relative_path"]
        )
        for row in rows
    }


def mark_block_done(db: Database, job_id: str, block_id: str, phase: str) -> None:
    with db.write() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO block_progress (job_id, block_id, phase, at) "
            "VALUES (?,?,?,?)",
            (job_id, block_id, phase, _iso(_now())),
        )


def clear_block_done(db: Database, job_id: str, block_id: str, phase: str) -> None:
    """Un-finish a block, because something happened that its completion predates.

    Only final semantic verification uses this, and the reason is the whole point of that
    phase: a verification is a statement about the file as it stood, and an accepted repair
    makes it a statement about a file nobody is handing over. Re-using the old marker would
    let a block be certified on the strength of a check that ran before its last edit.
    """
    with db.write() as connection:
        connection.execute(
            "DELETE FROM block_progress WHERE job_id = ? AND block_id = ? AND phase = ?",
            (job_id, block_id, phase),
        )


def blocks_done(db: Database, job_id: str, phase: str) -> frozenset[str]:
    rows = db.connection.execute(
        "SELECT block_id FROM block_progress WHERE job_id = ? AND phase = ?",
        (job_id, phase),
    ).fetchall()
    return frozenset(row["block_id"] for row in rows)


# --------------------------------------------------------------------------------------
# Instruction documents and the claims they carry
# --------------------------------------------------------------------------------------


def save_instruction_segments(
    db: Database,
    job_id: str,
    *,
    segments: Sequence[Any],
    document_format: str,
    document_sha256: str,
    truncated: bool = False,
) -> int:
    """Store the decomposed document. Idempotent, keyed by segment index.

    Written once at submission, before the job is queued, so a worker that picks the job
    up -- the first one or the fourth after three crashes -- reads the same instructions
    from the same place. There is no path by which a resumed job audits against fewer
    claims than the original did.
    """
    with db.write() as connection:
        for segment in segments:
            connection.execute(
                """INSERT INTO instruction_segments
                       (job_id, segment_index, text, provenance, document_format,
                        document_sha256, truncated, purpose)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id, segment_index) DO UPDATE SET
                       text = excluded.text,
                       provenance = excluded.provenance""",
                (
                    job_id,
                    segment.index,
                    segment.text,
                    segment.provenance,
                    document_format,
                    document_sha256,
                    1 if truncated else 0,
                    getattr(segment, "purpose", "errata"),
                ),
            )
    return len(segments)


def load_instruction_segments(db: Database, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = db.connection.execute(
        """SELECT segment_index, text, provenance, document_format, document_sha256,
                  truncated, purpose
           FROM instruction_segments WHERE job_id = ? ORDER BY segment_index""",
        (job_id,),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def record_claim_result(
    db: Database,
    job_id: str,
    *,
    segment_index: int,
    block_id: str,
    outcome: str,
    detail: str = "",
) -> None:
    """One block's verdict on one claim.

    `INSERT OR REPLACE` rather than append: a block re-audited after a crash must end up
    with one verdict, not two. Which is also why the key is (job, claim, block) and not a
    surrogate id -- the natural key *is* the identity of the judgment.
    """
    with db.write() as connection:
        connection.execute(
            """INSERT INTO claim_results
                   (job_id, segment_index, block_id, outcome, detail, at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id, segment_index, block_id) DO UPDATE SET
                   outcome = excluded.outcome,
                   detail = excluded.detail,
                   at = excluded.at""",
            (job_id, segment_index, block_id, outcome, detail, _iso(_now())),
        )


def list_claim_results(db: Database, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = db.connection.execute(
        """SELECT segment_index, block_id, outcome, detail, at FROM claim_results
           WHERE job_id = ? ORDER BY segment_index, block_id""",
        (job_id,),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def record_event(db: Database, job_id: str, kind: str, detail: str = "") -> None:
    """Append-only audit trail. Recovery writes here to say what it did."""
    with db.write() as connection:
        connection.execute(
            "INSERT INTO job_events (event_id, job_id, at, kind, detail) VALUES (?,?,?,?,?)",
            (uuid4().hex, job_id, _iso(_now()), kind, detail),
        )


# --------------------------------------------------------------------------------------
# The model-call audit trail
# --------------------------------------------------------------------------------------


def record_llm_call(
    db: Database,
    job_id: str,
    *,
    role: str,
    model: str,
    status: str,
    prompt_sha256: str,
    issue_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """One row per request, successful or not.

    Failures are recorded too, and that is most of the value: a job that spent four calls
    on an outage and one on a repair looks identical to a job that made one call, unless
    the four are written down.
    """
    call_id = uuid4().hex
    with db.write() as connection:
        connection.execute(
            """INSERT INTO llm_calls
               (call_id, job_id, issue_id, role, model, status, prompt_sha256,
                created_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                call_id,
                job_id,
                issue_id,
                role,
                model,
                status,
                prompt_sha256,
                _iso(_now()),
                json.dumps(payload or {}),
            ),
        )
    return call_id


def list_llm_calls(db: Database, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = db.connection.execute(
        "SELECT * FROM llm_calls WHERE job_id = ? ORDER BY created_at, rowid", (job_id,)
    ).fetchall()
    return tuple(
        {**dict(row), "payload": json.loads(row["payload_json"])} for row in rows
    )


def save_private_blob(
    db: Database,
    job_id: str,
    *,
    role: str,
    label: str,
    text: str,
    issue_id: str | None = None,
) -> None:
    """Persist agent reasoning where no reviewer context can reach it.

    The separation is structural rather than careful: `ReviewerContext` has no field whose
    type closure could name this table's contents, and `assert_no_private_fields` fails at
    import if one is ever added.
    """
    if not text.strip():
        return
    with db.write() as connection:
        # **Idempotent by `(job_id, label)`.** A crash between two blocks of a batch sends
        # the unmarked ones round again, and an append-only write would accumulate a second
        # copy of the same reasoning on every retry -- growing the table with duplicates
        # that say nothing new and making "what did the Writer argue on attempt 2" a
        # question with several identical answers.
        existing = connection.execute(
            "SELECT blob_id FROM private_blobs WHERE job_id = ? AND label = ?",
            (job_id, label),
        ).fetchone()
        if existing is not None:
            connection.execute(
                "UPDATE private_blobs SET text = ?, created_at = ? WHERE blob_id = ?",
                (text, _iso(_now()), existing["blob_id"]),
            )
            return
        connection.execute(
            """INSERT INTO private_blobs
               (blob_id, job_id, issue_id, role, label, created_at, text)
               VALUES (?,?,?,?,?,?,?)""",
            (uuid4().hex, job_id, issue_id, role, label, _iso(_now()), text),
        )


def load_private_blobs(db: Database, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = db.connection.execute(
        "SELECT * FROM private_blobs WHERE job_id = ? ORDER BY created_at, rowid",
        (job_id,),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def pin_prompt_versions(
    db: Database, job_id: str, versions: dict[str, tuple[int, str]]
) -> None:
    """Fix this job's prompt versions on first use, and never move them again.

    `INSERT OR IGNORE`, so a resumed job keeps what it started with. The failure this
    prevents is quiet: deploy `writer.v2.md` while a job is mid-repair and its first
    attempt was made under one set of instructions and its second under another, with
    nothing in the record to say so.
    """
    with db.write() as connection:
        connection.executemany(
            """INSERT OR IGNORE INTO job_prompts (job_id, role, version, sha256, created_at)
               VALUES (?,?,?,?,?)""",
            [
                (job_id, role, version, digest, _iso(_now()))
                for role, (version, digest) in versions.items()
            ],
        )


def pin_job_settings(db: Database, job_id: str, values: dict[str, Any]) -> None:
    """Fix this job's behaviour settings on first use, and never move them again.

    `INSERT OR IGNORE`, exactly like the prompt versions: a resumed job keeps what it
    started with rather than adopting whatever the environment says now. Recording these
    per call would be auditing; pinning them is what stops a job changing behaviour
    halfway through.
    """
    with db.write() as connection:
        connection.executemany(
            """INSERT OR IGNORE INTO job_settings (job_id, name, value, created_at)
               VALUES (?,?,?,?)""",
            [
                (job_id, name, json.dumps(value), _iso(_now()))
                for name, value in values.items()
            ],
        )


def load_job_settings(db: Database, job_id: str) -> dict[str, Any]:
    rows = db.connection.execute(
        "SELECT name, value FROM job_settings WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {row["name"]: json.loads(row["value"]) for row in rows}


def load_prompt_versions(db: Database, job_id: str) -> dict[str, int]:
    rows = db.connection.execute(
        "SELECT role, version FROM job_prompts WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {row["role"]: row["version"] for row in rows}


def load_prompt_pins(db: Database, job_id: str) -> dict[str, tuple[int, str]]:
    """Versions plus composed-text hashes, for enforcing rather than merely recording."""
    rows = db.connection.execute(
        "SELECT role, version, sha256 FROM job_prompts WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {row["role"]: (row["version"], row["sha256"]) for row in rows}


def expired_jobs(db: Database, *, retention_days: float, limit: int = 50) -> tuple[str, ...]:
    """Finished jobs old enough to delete.

    Only terminal ones, and only by `updated_at`. A job still in flight is not old, however
    long ago it was submitted -- and a retention sweep that deleted a running job's working
    copy would be a data-loss bug wearing a compliance hat.
    """
    if retention_days <= 0:
        return ()
    cutoff = _iso(_now() - timedelta(days=retention_days))
    rows = db.connection.execute(
        """SELECT job_id FROM jobs
           WHERE state IN ('succeeded','needs_human_attention','failed','cancelled')
             AND updated_at < ?
           ORDER BY updated_at LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return tuple(row["job_id"] for row in rows)


def delete_job(db: Database, job_id: str) -> None:
    """Remove a job and everything hanging off it.

    The child tables all cascade, so this is one statement -- which is the reason
    `foreign_keys=ON` is a pragma rather than a preference. Deleting a job by hand across
    fourteen tables is how orphaned private reasoning outlives the job it belonged to.
    """
    with db.write() as connection:
        connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def describe_artifacts(db: Database, job_id: str) -> list[dict[str, Any]]:
    """What this job produced, by kind and hash -- **never by path**.

    The same dictionary feeds the markdown report and the HTTP report, which is why the
    filesystem layout is absent: a curator has no use for it and anyone probing for it
    should learn nothing. The hash is the useful part, since it is how someone checks the
    file they downloaded is the file the report is about.
    """
    rows = db.connection.execute(
        "SELECT kind, sha256, created_at FROM job_artifacts WHERE job_id = ? ORDER BY kind",
        (job_id,),
    ).fetchall()
    return [
        {"kind": row["kind"], "sha256": row["sha256"] or "", "created_at": row["created_at"]}
        for row in rows
    ]


def rediscovery_counts(db: Database, job_id: str) -> dict[str, int]:
    """How many closed issues had their defect found again, and how many were given up on."""
    return {
        kind: count_events(db, job_id, kind)
        # `isolation_suspicion` rides along here because it is the same kind of fact: an
        # event the final states cannot express. A job can succeed with suspicions
        # outstanding -- they are not failures -- so unless the report counts them, the
        # only signal that anything was flagged is a row nobody queries.
        for kind in (
            "issue_reopened",
            "finding_absorbed",
            "isolation_suspicion",
            # Graded rows no scan ever accounted for. The one fact in this list that
            # denies the job success outright, and the only place a curator can learn
            # that part of the workbook was never examined at all.
            "rows_never_verified",
            # Blocks whose last independent check predates their last edit. Distinct from
            # `rows_never_verified`, which is about rows nobody examined at all.
            "final_verification_incomplete",
            # A scan whose own record disagrees with itself. Counted for every phase, not
            # only the final one: an initial audit that reported a row correct while its
            # computed and submitted answers differed is a reason to look at that row, and
            # the phase it happened in does not change that.
            "coverage_self_contradicting",
        )
    }


def count_events(db: Database, job_id: str, kind: str) -> int:
    """How many times something has happened to this job, durably.

    Used for the provider-failure budget. A counter in worker memory would reset every
    time the worker that was burning it died -- which is precisely the case the budget
    exists to bound, since a provider failure and a crashed worker often have the same
    cause. The events are already written for the audit trail; counting them costs a row
    scan on an indexed column and needs no second source of truth to keep in step.
    """
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM job_events WHERE job_id = ? AND kind = ?",
        (job_id, kind),
    ).fetchone()
    return int(row["n"]) if row else 0


def record_coverage(
    db: Database,
    job_id: str,
    *,
    block_id: str,
    phase: str,
    call_id: str,
    records: Sequence[Any],
) -> int:
    """Write down what one scan said it checked, row by row.

    Appended rather than replacing an earlier scan of the same block: a re-scan is a
    different call with a different answer, and overwriting would destroy the more
    interesting of the two -- the one that was short. Readers that want the current
    picture ask for the latest call (`latest_coverage`).
    """
    if not records:
        return 0
    at = _iso(_now())
    with db.write() as connection:
        for record in records:
            connection.execute(
                """INSERT INTO coverage_records
                   (record_id, job_id, call_id, block_id, phase, row_no, recorded_at,
                    payload_json) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    job_id,
                    call_id or None,
                    block_id,
                    phase,
                    int(record.row),
                    at,
                    record.model_dump_json(),
                ),
            )
    return len(records)


def latest_coverage(
    db: Database, job_id: str, *, phase: str, block_id: str
) -> tuple[dict[str, Any], ...]:
    """The most recent scan's coverage for one block in one phase.

    Scoped to a single `call_id` rather than to the newest row per line number. Mixing two
    calls would manufacture a complete record out of two incomplete ones and report
    coverage no single scan ever had.
    """
    row = db.connection.execute(
        "SELECT call_id, recorded_at FROM coverage_records "
        "WHERE job_id = ? AND phase = ? AND block_id = ? "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (job_id, phase, block_id),
    ).fetchone()
    if row is None:
        return ()
    if row["call_id"] is None:
        rows = db.connection.execute(
            "SELECT payload_json FROM coverage_records WHERE job_id = ? AND phase = ? "
            "AND block_id = ? AND call_id IS NULL AND recorded_at = ? ORDER BY row_no",
            (job_id, phase, block_id, row["recorded_at"]),
        ).fetchall()
    else:
        rows = db.connection.execute(
            "SELECT payload_json FROM coverage_records WHERE job_id = ? AND phase = ? "
            "AND block_id = ? AND call_id = ? ORDER BY row_no",
            (job_id, phase, block_id, row["call_id"]),
        ).fetchall()
    return tuple(json.loads(item["payload_json"]) for item in rows)


def count_coverage_records(db: Database, job_id: str, phase: str | None = None) -> int:
    query = "SELECT COUNT(*) AS n FROM coverage_records WHERE job_id = ?"
    parameters: tuple[Any, ...] = (job_id,)
    if phase is not None:
        query += " AND phase = ?"
        parameters += (phase,)
    row = db.connection.execute(query, parameters).fetchone()
    return int(row["n"]) if row else 0


def count_block_events(db: Database, job_id: str, kind: str, block_id: str) -> int:
    """How many times something has happened to one block, durably.

    The same argument as `count_events`, one level finer: a re-scan budget held in worker
    memory is no budget, because the crash that loses the counter is exactly the event the
    budget is meant to survive. Callers write the block id as the first token of the
    event detail, which is what this matches on.
    """
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM job_events "
        "WHERE job_id = ? AND kind = ? AND (detail = ? OR detail LIKE ?)",
        (job_id, kind, block_id, f"{block_id} %"),
    ).fetchone()
    return int(row["n"]) if row else 0


def list_events(db: Database, job_id: str) -> tuple[dict[str, str], ...]:
    rows = db.connection.execute(
        "SELECT at, kind, detail FROM job_events WHERE job_id = ? ORDER BY at", (job_id,)
    ).fetchall()
    return tuple(dict(row) for row in rows)
