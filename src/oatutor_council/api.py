"""The HTTP surface.

`POST /jobs` returns `202` immediately and the council runs on a worker thread. Progress
is observable through `GET /jobs/{id}`, which never blocks on an in-flight write because
the database is in WAL mode.

Work runs on a `ThreadPoolExecutor` created in the lifespan, **not** on FastAPI's
`BackgroundTasks`. Background tasks are tied to the request lifetime and give no
concurrency cap, no cancellation, and no way to observe what is running -- none of which
is acceptable for a job that makes paid model calls for several minutes. The durable queue
is the `jobs` table, so a submission lost to a crash is picked up again with no
coordination.

**No response or error message ever contains a filesystem path.** Artefacts are addressed
by `(job_id, kind)`, and a curator has no use for the storage layout while an attacker
probing for it should learn nothing.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .agents.initial_auditor import SeedClaim
from .config import Settings, load_settings
from .council import CurationCouncil
from .ingestion.instruction_documents import (
    UnsupportedDocumentError,
    read_instruction_document,
)
from .llm.base import LLMClient
from .models import ArtifactKind, CurationJob, JobState, SourcePath
from .persistence import (
    Database,
    acquire_lease,
    claimable_jobs,
    create_job,
    get_job,
    list_artifacts,
    list_changes,
    list_events,
    list_issues,
    list_verdicts,
    load_ledger,
    record_artifact,
    record_event,
    release_lease,
)
from .reporting.reports import build_reports
from .state_machine import is_resumable
from .uploads import (
    INSTRUCTION_EXTENSIONS,
    WORKBOOK_EXTENSIONS,
    UploadRejected,
    assert_contained,
    check_archive,
    validate_upload,
)
from .workbook.writer import create_working_copy

log = logging.getLogger(__name__)

ClientFactory = Callable[[Settings], LLMClient]


def _default_client_factory(settings: Settings) -> LLMClient:
    from .llm.provider import GeminiClient

    return GeminiClient(settings)


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

    def submit(self, job_id: str, job_dir: Path, seed_claims=()) -> None:
        self._pool.submit(self._run, job_id, job_dir, tuple(seed_claims))

    def _run(self, job_id: str, job_dir: Path, seed_claims) -> None:
        worker = f"{job_id[:8]}-{uuid4().hex[:6]}"
        try:
            job = acquire_lease(
                self.db, job_id, worker, lease_seconds=self.settings.lease_seconds
            )
        except Exception:
            log.exception("could not acquire a lease for job %s", job_id)
            return

        try:
            copy = _working_copy_for(self.db, job, job_dir)
            council = CurationCouncil(
                db=self.db,
                settings=self.settings,
                client=self.client_factory(self.settings),
                job_id=job_id,
                copy=copy,
                worker_id=worker,
                seed_claims=seed_claims,
            )
            council.run()
        except Exception as error:  # pragma: no cover - defence in depth
            log.exception("job %s failed", job_id)
            record_event(self.db, job_id, "worker_error", str(error))
        finally:
            # The epoch acquired above, so a worker that was fenced out mid-run releases
            # nothing rather than clearing the new owner's lease.
            release_lease(self.db, job_id, worker, run_epoch=job.run_epoch)

    def sweep(self) -> int:
        """Re-submit anything whose lease has expired.

        The durable queue in action: a job whose worker died reappears here without any
        coordination, because claimability is a property of rows rather than of memory.
        """
        resumed = 0
        for job in claimable_jobs(self.db, limit=self.settings.max_concurrent_jobs):
            self.submit(job.job_id, _job_dir(self.settings, job.job_id))
            resumed += 1
        return resumed

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _job_dir(settings: Settings, job_id: str) -> Path:
    return settings.data_root / job_id


def _working_copy_for(db: Database, job: CurationJob, job_dir: Path):
    """Rebuild the working-copy handle from durable state.

    Nothing about the copy is held in memory between steps, so a resumed job reconstructs
    it from the recorded source path and hash exactly as the first worker did.
    """
    from .workbook.writer import WorkingCopy

    source = SourcePath(str(job_dir / "source" / f"workbook{_extension(job)}"))
    return WorkingCopy(
        source=source,
        source_sha256=job.source_sha256,
        path=job_dir / "work" / "working.xlsx",
        tmp_dir=job_dir / "work" / ".tmp",
    )


def _extension(job: CurationJob) -> str:
    return Path(job.source_filename).suffix.lower() or ".xlsx"


# --------------------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------------------


def create_app(
    *,
    settings: Settings | None = None,
    db: Database | None = None,
    client_factory: ClientFactory | None = None,
    autostart: bool = True,
) -> FastAPI:
    resolved = settings or load_settings()
    resolved.data_root.mkdir(parents=True, exist_ok=True)
    database = db or Database(resolved.data_root / "council.db")
    factory = client_factory or _default_client_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runner = JobRunner(
            db=database, settings=resolved, client_factory=factory
        )
        yield
        app.state.runner.shutdown()

    app = FastAPI(title="OATutor Curation Council", lifespan=lifespan)
    app.state.db = database
    app.state.settings = resolved
    app.state.autostart = autostart

    @app.exception_handler(UploadRejected)
    async def _upload_rejected(_: Request, error: UploadRejected) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": error.user_message})

    @app.exception_handler(UnsupportedDocumentError)
    async def _document_rejected(
        _: Request, error: UnsupportedDocumentError
    ) -> JSONResponse:
        # Deliberately 400 with the reason spelled out. An unreadable document must never
        # be reported as a document containing no instructions.
        return JSONResponse(status_code=400, content={"error": error.user_message})

    # -- submission -------------------------------------------------------------------

    @app.post("/jobs", status_code=202)
    async def submit_job(
        workbook: UploadFile = File(...),
        instructions: UploadFile | None = File(default=None),
    ) -> dict[str, Any]:
        workbook_bytes = await workbook.read()
        checked = validate_upload(
            filename=workbook.filename or "",
            data=workbook_bytes,
            allowed=WORKBOOK_EXTENSIONS,
            kind="workbook",
            max_bytes=resolved.max_upload_bytes,
        )

        job_id = uuid4().hex
        job_dir = _job_dir(resolved, job_id)
        source_dir = job_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        # The client's filename never reaches the filesystem. The directory is a UUID and
        # the file inside it has a fixed name.
        source_path = source_dir / f"workbook{checked.extension}"
        source_path.write_bytes(workbook_bytes)
        check_archive(source_path, checked.extension)

        seed_claims: list[SeedClaim] = []
        instruction_name: str | None = None
        if instructions is not None and instructions.filename:
            document_bytes = await instructions.read()
            document = validate_upload(
                filename=instructions.filename,
                data=document_bytes,
                allowed=INSTRUCTION_EXTENSIONS,
                kind="instruction document",
                max_bytes=resolved.max_upload_bytes,
            )
            document_path = source_dir / f"instructions{document.extension}"
            document_path.write_bytes(document_bytes)
            check_archive(document_path, document.extension)

            parsed = read_instruction_document(
                document_path, display_name=document.display_name
            )
            seed_claims = [
                SeedClaim(index=segment.index, text=segment.text,
                          provenance=segment.provenance)
                for segment in parsed.segments
            ]
            instruction_name = document.display_name

        copy = create_working_copy(SourcePath(str(source_path)), job_dir)
        job = create_job(
            database,
            CurationJob(
                job_id=job_id,
                source_filename=checked.display_name,
                source_sha256=copy.source_sha256,
                instruction_filename=instruction_name,
            ),
        )
        record_artifact(
            database, job_id, ArtifactKind.SOURCE_WORKBOOK, str(source_path)
        )

        if app.state.autostart:
            app.state.runner.submit(job_id, job_dir, seed_claims)

        return {
            "job_id": job.job_id,
            "state": job.state.value,
            "source_filename": job.source_filename,
            "instruction_filename": job.instruction_filename,
            "seed_claims": len(seed_claims),
        }

    # -- observation ------------------------------------------------------------------

    def _job_or_404(job_id: str) -> CurationJob:
        job = get_job(database, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.get("/jobs/{job_id}")
    async def job_status(job_id: str) -> dict[str, Any]:
        job = _job_or_404(job_id)
        ledger = load_ledger(database, job_id)
        return {
            "job_id": job.job_id,
            "state": job.state.value,
            "failure_reason": job.failure_reason.value if job.failure_reason else None,
            "succeeded": job.state is JobState.SUCCEEDED,
            "issues": len(ledger.issues),
            "issues_open": len(ledger.open_issues),
            "changes": len(list_changes(database, job_id)),
            "validation_rounds_used": job.validation_rounds_used,
            "steps_used": job.steps_used,
            "llm_calls_used": job.llm_calls_used,
        }

    @app.get("/jobs/{job_id}/issues")
    async def job_issues(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        return _reports(job_id).issue_ledger

    @app.get("/jobs/{job_id}/changes")
    async def job_changes(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        return _reports(job_id).change_log

    @app.get("/jobs/{job_id}/reviews")
    async def job_reviews(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        return _reports(job_id).review_history

    @app.get("/jobs/{job_id}/report")
    async def job_report(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        return _reports(job_id).validation_report

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        return {"job_id": job_id, "events": list(list_events(database, job_id))}

    def _reports(job_id: str):
        from .persistence import list_attempts

        job = get_job(database, job_id)
        return build_reports(
            job_id=job_id,
            state=job.state,
            ledger=load_ledger(database, job_id),
            changes=list_changes(database, job_id),
            verdicts=list_verdicts(database, job_id),
            attempts=list_attempts(database, job_id),
            findings=(),
        )

    @app.get("/jobs/{job_id}/download")
    async def download(job_id: str) -> FileResponse:
        """Serve the corrected workbook by `(job_id, kind)`, never by client path."""
        _job_or_404(job_id)
        artifacts = list_artifacts(database, job_id)
        path = artifacts.get(ArtifactKind.CORRECTED_WORKBOOK)
        if path is None:
            raise HTTPException(
                status_code=409,
                detail="the corrected workbook is not ready yet",
            )
        resolved_path = assert_contained(Path(path), resolved.data_root)
        return FileResponse(
            resolved_path,
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            # A generic download name: the storage layout is not the client's business.
            filename="corrected.xlsx",
        )

    # -- control ----------------------------------------------------------------------

    @app.post("/jobs/{job_id}/resume", status_code=202)
    async def resume(job_id: str) -> dict[str, Any]:
        job = _job_or_404(job_id)
        if not is_resumable(job.state, job.failure_reason):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a job in state {job.state.value} cannot be resumed"
                    + (
                        f" after {job.failure_reason.value}"
                        if job.failure_reason
                        else ""
                    )
                ),
            )
        if _lease_is_live(job):
            raise HTTPException(
                status_code=409, detail="the job is already being worked on"
            )

        app.state.runner.submit(job_id, _job_dir(resolved, job_id))
        return {"job_id": job_id, "state": job.state.value, "resumed": True}

    return app


def _lease_is_live(job: CurationJob) -> bool:
    from datetime import datetime, timezone

    return bool(
        job.lease_owner
        and job.lease_expires_at
        and job.lease_expires_at > datetime.now(timezone.utc)
    )


# There is deliberately no module-level `app`. Building one at import time would read
# settings and open the database as a side effect of importing this module, which makes it
# untestable and makes a missing API key an import error. Serve it with:
#   uvicorn "oatutor_council.api:create_app" --factory --app-dir src
