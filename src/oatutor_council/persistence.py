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
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS validation_findings_round ON validation_findings(job_id, round_no);

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

-- Agent reasoning, stored apart from anything a reviewer prompt is built from. The
-- separation is structural: no reviewer context type has a field that could reference
-- this table.
CREATE TABLE IF NOT EXISTS private_blobs (
    blob_id    TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    issue_id   TEXT,
    role       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    text       TEXT NOT NULL
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
                    AND busy.state IN ('awaiting_patch','patch_proposed','applying',
                                       'patch_applied','awaiting_review')
              )
            ORDER BY i.created_at LIMIT 1""",
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
                json.dumps({"edits": list(edits)}),
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
    return tuple(
        {
            "intent_id": row["intent_id"],
            "issue_id": row["issue_id"],
            "patch_id": row["patch_id"],
            "run_epoch": row["run_epoch"],
            "edits": json.loads(row["payload_json"])["edits"],
        }
        for row in rows
    )


# --------------------------------------------------------------------------------------
# Findings, artifacts, events
# --------------------------------------------------------------------------------------


def record_findings(
    db: Database,
    job_id: str,
    round_no: int,
    findings: Sequence[ValidationFinding],
    fingerprints: Sequence[str],
) -> None:
    with db.write() as connection:
        connection.execute(
            "DELETE FROM validation_findings WHERE job_id = ? AND round_no = ?",
            (job_id, round_no),
        )
        for finding, mark in zip(findings, fingerprints):
            connection.execute(
                """INSERT INTO validation_findings (finding_id, job_id, round_no, code,
                       severity, fingerprint, payload_json) VALUES (?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    job_id,
                    round_no,
                    finding.code,
                    finding.severity.value,
                    mark,
                    finding.model_dump_json(),
                ),
            )


def list_findings(
    db: Database, job_id: str, round_no: int | None = None
) -> tuple[ValidationFinding, ...]:
    if round_no is None:
        rows = db.connection.execute(
            "SELECT payload_json FROM validation_findings WHERE job_id = ? "
            "ORDER BY round_no DESC",
            (job_id,),
        ).fetchall()
    else:
        rows = db.connection.execute(
            "SELECT payload_json FROM validation_findings WHERE job_id = ? AND round_no = ?",
            (job_id, round_no),
        ).fetchall()
    return tuple(ValidationFinding.model_validate_json(r["payload_json"]) for r in rows)


def record_artifact(
    db: Database, job_id: str, kind: ArtifactKind, relative_path: str, sha256: str = ""
) -> None:
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


def list_artifacts(db: Database, job_id: str) -> dict[ArtifactKind, str]:
    rows = db.connection.execute(
        "SELECT kind, relative_path FROM job_artifacts WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {ArtifactKind(row["kind"]): row["relative_path"] for row in rows}


def mark_block_done(db: Database, job_id: str, block_id: str, phase: str) -> None:
    with db.write() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO block_progress (job_id, block_id, phase, at) "
            "VALUES (?,?,?,?)",
            (job_id, block_id, phase, _iso(_now())),
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
                        document_sha256, truncated)
                   VALUES (?,?,?,?,?,?,?)
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
                ),
            )
    return len(segments)


def load_instruction_segments(db: Database, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = db.connection.execute(
        """SELECT segment_index, text, provenance, document_format, document_sha256,
                  truncated
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


def list_events(db: Database, job_id: str) -> tuple[dict[str, str], ...]:
    rows = db.connection.execute(
        "SELECT at, kind, detail FROM job_events WHERE job_id = ? ORDER BY at", (job_id,)
    ).fetchall()
    return tuple(dict(row) for row in rows)
