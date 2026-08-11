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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings, load_settings
from .ingestion.instruction_documents import (
    SegmentPurpose,
    UnsupportedDocumentError,
    read_instruction_document,
)
from .models import ArtifactKind, CurationJob, JobState, SourcePath
from .persistence import (
    Database,
    create_job,
    get_job,
    list_artifacts,
    list_changes,
    list_claim_results,
    list_events,
    list_verdicts,
    load_instruction_segments,
    load_ledger,
    record_artifact,
    save_instruction_segments,
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
from .workbook.writer import create_working_copy, sha256_of
from .workers import (
    ClientFactory,
    JobPoller,
    JobRunner,
    default_client_factory,
    job_dir_for,
)

log = logging.getLogger(__name__)


def _declared_purpose(value: str) -> SegmentPurpose | None:
    """`auto` means classify per passage; anything else labels the whole document."""
    cleaned = (value or "auto").strip().lower()
    if cleaned in ("", "auto"):
        return None
    try:
        return SegmentPurpose(cleaned)
    except ValueError:
        raise UploadRejected(
            f"instructions_purpose must be one of auto, rules, errata, notes "
            f"(got {cleaned!r})"
        ) from None


def _database_reachable(db: Database) -> bool:
    try:
        db.connection.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001 - any failure here means the same thing
        return False
    return True


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

    # Credentials are checked **here**, before the first upload rather than at the first
    # model call. A service that starts without them accepts work it cannot do: the job
    # is created, the workbook is stored, the worker fails on its first call, and the
    # curator has paid the upload and the wait to learn something that was knowable at
    # boot.
    #
    # The escape hatch is explicit and narrow: supplying a `client_factory` says you are
    # running against something other than the live provider -- the scripted mock, a
    # recorded transcript, a local stub -- and the check does not apply. It is a
    # parameter rather than an environment flag on purpose, because an environment
    # variable that disables a credential check is a thing that ends up set in
    # production.
    if client_factory is None:
        resolved.require_credentials()

    resolved.data_root.mkdir(parents=True, exist_ok=True)
    database = db or Database(resolved.data_root / "council.db")
    factory = client_factory or default_client_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner = JobRunner(db=database, settings=resolved, client_factory=factory)
        app.state.runner = runner
        # The poller is what makes the `jobs` table a queue rather than a log. Without it
        # a job whose worker died sits with an expired lease and nobody ever looks: crash
        # recovery would exist in `recover_job` and never actually run. One sweep on
        # startup too, so a process restarted after a crash resumes immediately instead of
        # waiting out the first interval.
        poller = JobPoller(runner, interval=resolved.poll_interval_seconds)
        app.state.poller = poller
        if autostart:
            runner.sweep()
            poller.start()
        yield
        poller.stop()
        runner.shutdown()

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

    # -- health -----------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness. Answers "is this process up", and nothing that needs a secret."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readiness() -> JSONResponse:
        """Readiness: could a job submitted right now actually be run?

        Separate from `/health` because the answers differ in the case that matters. A
        process that is up but whose worker pool never started, or whose database has
        gone away, accepts uploads and does nothing with them -- and a load balancer that
        cannot tell that apart from working routes every upload into it.

        The provider is *described*, never contacted. A readiness probe that made a paid
        model call would be a bill that scales with how often it is polled. Credentials
        are checked at startup instead, which is why this reports rather than enforces.
        """
        offline = client_factory is not None
        checks = {
            "provider_configured": offline or resolved.provider_configured,
            "database": _database_reachable(database),
            "worker_pool": getattr(app.state, "runner", None) is not None,
            # A process with a pool but no poller accepts work and runs it, then never
            # picks up anything its predecessor left behind. That is a half-working
            # service, and a probe that called it ready would hide the half that is not.
            "poller": getattr(app.state, "poller", None) is not None,
        }
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "ready": ready,
                "checks": checks,
                "offline_client": offline,
                **resolved.describe_provider(),
            },
        )

    # -- submission -------------------------------------------------------------------

    @app.post("/jobs", status_code=202)
    async def submit_job(
        workbook: UploadFile = File(...),
        instructions: UploadFile | None = File(default=None),
        instructions_purpose: str = Form(default="auto"),
    ) -> dict[str, Any]:
        """`instructions_purpose` says what the attached document *is*.

        `auto` classifies each passage on its own wording, which is right for the mixed
        notes curators actually write. Naming `rules`, `errata` or `notes` overrides that
        for the whole document -- someone who uploads a formatting guide and says so
        knows something the phrasing does not always reveal.
        """
        workbook_bytes = await workbook.read()
        checked = validate_upload(
            filename=workbook.filename or "",
            data=workbook_bytes,
            allowed=WORKBOOK_EXTENSIONS,
            kind="workbook",
            max_bytes=resolved.max_upload_bytes,
        )

        job_id = uuid4().hex
        job_dir = job_dir_for(resolved, job_id)
        source_dir = job_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        # The client's filename never reaches the filesystem. The directory is a UUID and
        # the file inside it has a fixed name.
        source_path = source_dir / f"workbook{checked.extension}"
        source_path.write_bytes(workbook_bytes)
        check_archive(source_path, checked.extension)

        parsed_document = None
        document_path: Path | None = None
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

            parsed_document = read_instruction_document(
                document_path,
                display_name=document.display_name,
                declared_purpose=_declared_purpose(instructions_purpose),
            )
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

        # The document is decomposed and stored **before the job is queued**, so the
        # claims exist durably by the time any worker can pick it up. Passing them to
        # the runner instead would mean a job resumed by a different process audits
        # against no instructions at all -- and that failure is silent, because a job
        # with zero claims looks exactly like a job whose claims were all refuted.
        seed_claim_count = 0
        if parsed_document is not None and document_path is not None:
            document_hash = sha256_of(document_path)
            seed_claim_count = save_instruction_segments(
                database,
                job_id,
                segments=parsed_document.segments,
                document_format=parsed_document.format.value,
                document_sha256=document_hash,
                truncated=parsed_document.truncated,
            )
            record_artifact(
                database,
                job_id,
                ArtifactKind.INSTRUCTION_DOCUMENT,
                str(document_path),
                sha256=document_hash,
            )

        if app.state.autostart:
            app.state.runner.submit(job_id, job_dir)

        return {
            "job_id": job.job_id,
            "state": job.state.value,
            "source_filename": job.source_filename,
            "instruction_filename": job.instruction_filename,
            "seed_claims": seed_claim_count,
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
            claims=load_instruction_segments(database, job_id),
            claim_results=list_claim_results(database, job_id),
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

        app.state.runner.submit(job_id, job_dir_for(resolved, job_id))
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
