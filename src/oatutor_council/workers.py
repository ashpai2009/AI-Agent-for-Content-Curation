"""Running jobs: leases, heartbeats, the worker pool, and the poller.

Three pieces, and the division between them is the point.

**`LeaseKeeper`** renews the lease on a fixed interval from its own thread. It has to be a
thread rather than a call between steps, because a single step contains a model call that
can legitimately take longer than the whole lease -- and a lease renewed only at step
boundaries expires in the middle of exactly the work it was protecting.

**`JobRunner`** owns the pool. One lease per job, taken before any work and released after,
with the epoch it acquired so a worker that was fenced out releases nothing rather than
clearing the new owner's lease.

**`JobPoller`** is why a crashed worker is not a lost job. The durable queue is the `jobs`
table: a job whose lease expired is claimable by definition, so recovery needs no
coordination, no cleanup hook, and no knowledge of what the dead process was doing.

**This is a single-process design.** The lease protocol is safe across processes -- epoch
fencing and `BEGIN IMMEDIATE` are not in-process locks -- but SQLite in WAL mode over one
file wants one writer, and the working copies live on a local filesystem. Run one Uvicorn
worker (`--workers 1`). Horizontal scaling means a shared database and shared storage, and
that is a different design rather than a flag.
"""

from __future__ import annotations

import logging
import threading
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .config import ConfigurationError, Settings
from .council import CurationCouncil
from .llm.base import LLMClient, ProviderConfigurationError
from .models import CurationJob, FailureReason, JobState, SourcePath
from .persistence import (
    ConcurrencyError,
    Database,
    acquire_lease,
    claimable_jobs,
    get_job,
    heartbeat,
    record_event,
    release_lease,
    transition_job,
)
from .state_machine import is_resumable

log = logging.getLogger(__name__)

ClientFactory = Callable[[Settings], LLMClient]


def default_client_factory(settings: Settings) -> LLMClient:
    from .llm.provider import GeminiClient

    return GeminiClient(settings)


class LeaseKeeper:
    """Renews one job's lease until told to stop, and notices when it cannot.

    Used as a context manager so the stop is structural: the thread cannot outlive the
    block that started it, which matters because a keeper that kept renewing after its
    worker died would hold a job hostage for as long as the process lived.

    `lost` is the interesting half. A renewal that updates zero rows means somebody else
    owns the job now, and the worker needs to know that between steps rather than find out
    by writing to a job it no longer holds.
    """

    def __init__(
        self,
        db: Database,
        job_id: str,
        owner: str,
        *,
        run_epoch: int,
        lease_seconds: int,
        interval: float,
    ) -> None:
        self.db = db
        self.job_id = job_id
        self.owner = owner
        self.run_epoch = run_epoch
        self.lease_seconds = lease_seconds
        self.interval = max(0.05, interval)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def __enter__(self) -> LeaseKeeper:
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self.job_id[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 2 + 1)
            self._thread = None

    def _loop(self) -> None:
        # `wait` rather than `sleep`: shutdown returns immediately instead of after
        # however much of the interval happened to be left.
        while not self._stop.wait(self.interval):
            try:
                heartbeat(
                    self.db,
                    self.job_id,
                    self.owner,
                    run_epoch=self.run_epoch,
                    lease_seconds=self.lease_seconds,
                )
            except ConcurrencyError:
                # Not an error to log loudly: losing a lease is a normal outcome of a
                # worker that stalled long enough for the job to be reclaimed.
                self._lost.set()
                return
            except Exception:  # pragma: no cover - defence in depth
                log.exception("heartbeat failed for job %s", self.job_id)
                self._lost.set()
                return


class JobRunner:
    """Runs jobs on a bounded pool, one lease each."""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        client_factory: ClientFactory,
    ) -> None:
        self.db = db
        self.settings = settings
        self.client_factory = client_factory
        self._pool = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_jobs, thread_name_prefix="council"
        )
        # Set on shutdown. Councils check it between steps, so a running job stops at a
        # step boundary -- the one place the step contract guarantees is consistent --
        # rather than wherever the interpreter happened to be.
        self._stopping = threading.Event()
        self._running: set[str] = set()
        self._futures: set[futures.Future] = set()
        self._lock = threading.Lock()

    def submit(self, job_id: str, job_dir: Path) -> bool:
        """Queue a job unless this process is already running it, or shutting down.

        The in-process guard is not the safety mechanism -- the lease is -- but it stops a
        poller tick from queueing work behind a job it can already see running, which
        would otherwise pile up futures that each acquire a lease and immediately lose it.
        """
        if self._stopping.is_set():
            return False
        with self._lock:
            if job_id in self._running:
                return False
            self._running.add(job_id)
        future = self._pool.submit(self._run, job_id, job_dir)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(lambda done: self._futures.discard(done))
        return True

    def _run(self, job_id: str, job_dir: Path) -> None:
        worker = f"{job_id[:8]}-{uuid4().hex[:6]}"
        try:
            job = acquire_lease(
                self.db, job_id, worker, lease_seconds=self.settings.lease_seconds
            )
        except Exception:
            # Contended, or gone. Either way this worker has no claim and simply stops;
            # the owner is working on it, or the next poll will find it again.
            log.info("could not acquire a lease for job %s", job_id, exc_info=True)
            self._finished(job_id)
            return

        try:
            resume_from_failure(self.db, job_id, run_epoch=job.run_epoch)
            with LeaseKeeper(
                self.db,
                job_id,
                worker,
                run_epoch=job.run_epoch,
                lease_seconds=self.settings.lease_seconds,
                interval=self.settings.heartbeat_seconds,
            ) as keeper:
                council = CurationCouncil(
                    db=self.db,
                    settings=self.settings,
                    client=self.client_factory(self.settings),
                    job_id=job_id,
                    copy=working_copy_for(job, job_dir),
                    worker_id=worker,
                    run_epoch=job.run_epoch,
                )
                council.run(
                    should_stop=lambda: self._stopping.is_set() or keeper.lost
                )
        except (ConfigurationError, ProviderConfigurationError) as error:
            # The client could not even be built. Left as a generic worker error the job
            # would sit in `created` with an expired lease, be swept, fail the same way,
            # and be swept again -- looking like work in progress forever. `CONFIG` is
            # non-resumable, so the sweep leaves it alone until the settings change.
            log.error("job %s cannot run: %s", job_id, error)
            record_event(self.db, job_id, "provider_misconfigured", str(error))
            fail_job(self.db, job_id, FailureReason.CONFIG, run_epoch=job.run_epoch)
        except ConcurrencyError as error:
            # Fenced out mid-run. Nothing to record against the job: it belongs to
            # somebody else now, and writing to it is precisely what fencing forbids.
            log.info("job %s was taken from %s: %s", job_id, worker, error)
        except Exception as error:  # pragma: no cover - defence in depth
            log.exception("job %s failed", job_id)
            record_event(self.db, job_id, "worker_error", str(error))
        finally:
            # The epoch acquired above, so a worker that was fenced out mid-run releases
            # nothing rather than clearing the new owner's lease.
            release_lease(self.db, job_id, worker, run_epoch=job.run_epoch)
            self._finished(job_id)

    def _finished(self, job_id: str) -> None:
        with self._lock:
            self._running.discard(job_id)

    def sweep(self) -> int:
        """Re-submit anything whose lease has expired.

        The durable queue in action: a job whose worker died reappears here without any
        coordination, because claimability is a property of rows rather than of memory.
        """
        resumed = 0
        for job in claimable_jobs(self.db, limit=self.settings.max_concurrent_jobs):
            if self.submit(job.job_id, job_dir_for(self.settings, job.job_id)):
                resumed += 1
        return resumed

    def shutdown(self, *, wait: float = 5.0) -> None:
        """Ask running councils to stop, then wait briefly for them to reach a boundary.

        Not `cancel_futures` alone. Cancelling only affects work that has not started, and
        a council mid-job is precisely the work worth stopping cleanly: it releases its
        lease on the way out, so a restarted process picks the job up immediately instead
        of waiting for the lease to expire.
        """
        self._stopping.set()
        pending = list(self._futures)
        self._pool.shutdown(wait=False, cancel_futures=True)
        if wait > 0 and pending:
            # Bounded on purpose. A council that will not reach a step boundary in time
            # is left to its lease: it expires, and the next process to run claims the
            # job. Blocking shutdown indefinitely to be tidy would hang the deployment.
            futures.wait(pending, timeout=wait)


class JobPoller:
    """Sweeps for orphaned jobs on an interval, in its own daemon thread.

    Without this, a job whose worker died sits in the database with an expired lease and
    nobody ever looks at it: the `jobs` table is a durable queue only if something drains
    it. It is deliberately dumb -- one call to `runner.sweep()` -- because every decision
    about what is claimable belongs in the SQL predicate, where it is testable without a
    thread.
    """

    def __init__(self, runner: JobRunner, *, interval: float) -> None:
        self.runner = runner
        self.interval = max(0.05, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sweeps = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="job-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 2 + 1)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.runner.sweep()
                self.sweeps += 1
            except Exception:  # pragma: no cover - a poller must not die of one bad tick
                log.exception("job sweep failed")


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def job_dir_for(settings: Settings, job_id: str) -> Path:
    return settings.data_root / job_id


def working_copy_for(job: CurationJob, job_dir: Path):
    """Rebuild the working-copy handle from durable state.

    Nothing about the copy is held in memory between steps, so a resumed job reconstructs
    it from the recorded source path and hash exactly as the first worker did.
    """
    from .workbook.writer import WorkingCopy

    extension = Path(job.source_filename).suffix.lower() or ".xlsx"
    return WorkingCopy(
        source=SourcePath(str(job_dir / "source" / f"workbook{extension}")),
        source_sha256=job.source_sha256,
        path=job_dir / "work" / "working.xlsx",
        tmp_dir=job_dir / "work" / ".tmp",
    )


def resume_from_failure(db: Database, job_id: str, *, run_epoch: int) -> bool:
    """Put a resumably-failed job back on the pipeline, or leave it exactly where it is.

    Without this, `is_resumable` was a promise nothing kept. `POST /jobs/{id}/resume`
    returned 202, a worker started, found the job in `FAILED` -- a state with nowhere to
    go -- reported "nothing to do", and exited. The curator saw an accepted request and a
    job that never moved, which is the worst of both: no error to act on and no progress.

    Done here, under the lease and the epoch this worker acquired, rather than in the HTTP
    handler. A transition made before the lease is taken is a transition made by a process
    that may not be the one that ends up doing the work.
    """
    job = get_job(db, job_id)
    if job is None or job.state is not JobState.FAILED:
        return False
    if not is_resumable(job.state, job.failure_reason):
        return False

    record_event(
        db,
        job_id,
        "resumed_after_failure",
        job.failure_reason.value if job.failure_reason else "unknown",
    )
    transition_job(db, job_id, JobState.INGESTING, run_epoch=run_epoch)
    return True


def fail_job(
    db: Database, job_id: str, reason: FailureReason, *, run_epoch: int | None = None
) -> None:
    job = get_job(db, job_id)
    if job is None or job.is_terminal:
        return
    transition_job(
        db,
        job_id,
        JobState.FAILED,
        run_epoch=job.run_epoch if run_epoch is None else run_epoch,
        failure_reason=reason,
    )
