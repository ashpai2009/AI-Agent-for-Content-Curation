"""API tests.

The upload-safety tests carry the most weight. They are the boundary a stranger controls
completely, and the assertions are about what the service *refuses* and about what its
responses never contain.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import problem, scaffold, step, write_workbook
from documents import write_image_only_pdf, write_markdown
from oatutor_council.agents.schemas import (
    AuditorResponse,
    IndependentReviewResponse,
    ReviewerResponse,
    WriterResponse,
)
from oatutor_council.api import create_app
from oatutor_council.config import Settings
from oatutor_council.llm.base import AgentRole
from oatutor_council.llm.mock import ScriptedLLMClient
from oatutor_council.persistence import Database


def settings_for(tmp_path: Path, **kwargs) -> Settings:
    defaults = dict(
        claude_cli_path="fake-claude",
        claude_model="mock",
        claude_effort="medium",
        data_root=tmp_path / "jobs",
        max_repair_attempts=3,
        max_validation_rounds=2,
        step_budget=400,
        llm_call_budget=200,
        interrupted_retry_budget=2,
        max_concurrent_jobs=1,
        max_upload_bytes=5 * 1024 * 1024,
        lease_seconds=60,
        # A scripted mock is not a provider; retrying one tests nothing.
        provider_max_attempts=1,
    )
    return Settings(**{**defaults, **kwargs})


def mock_client(_settings) -> ScriptedLLMClient:
    replies = {
        AgentRole.INITIAL_AUDITOR: AuditorResponse(),
        AgentRole.INDEPENDENT_REVIEWER: IndependentReviewResponse(block_is_sound=True),
        AgentRole.WRITER: WriterResponse(
            derivation="a scaffold row must carry a graded answer",
            edits=[{"row": 4, "column": "answer", "before": "", "after": "30"}],
        ),
        AgentRole.KNOWN_ISSUE_REVIEWER: ReviewerResponse(decision="accept"),
    }
    client = ScriptedLLMClient()
    client.default = lambda request: replies[request.role]
    return client


@pytest.fixture
def workbook_bytes(tmp_path) -> bytes:
    path = write_workbook(
        tmp_path / "src.xlsx",
        [
            problem("angles1", title="Convert", oer_src="s", license="CC"),
            step("angles1", answer="pi/6", answer_type="algebra"),
            scaffold("angles1", "s1", answer="", answer_type="numeric"),
        ],
    )
    return path.read_bytes()


@pytest.fixture
def client(tmp_path):
    """Autostart off, so submission and execution are asserted separately."""
    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=mock_client,
        autostart=False,
    )
    with TestClient(app) as test_client:
        test_client.app_settings = settings
        yield test_client


def submit(client, data: bytes, name: str = "workbook.xlsx", *, headers=None, **extra):
    files = {"workbook": (name, data, "application/octet-stream")}
    files.update(extra)
    return client.post("/jobs", files=files, headers=headers)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_the_service_refuses_to_start_without_a_usable_cli(tmp_path):
    """Before the first upload, not at the first model call. A service that starts
    unconfigured accepts work it can never do, and the curator pays the upload and the
    wait to learn something that was knowable at boot."""
    from oatutor_council.config import ConfigurationError

    with pytest.raises(ConfigurationError) as error:
        create_app(
            settings=settings_for(tmp_path, claude_cli_path="/nonexistent/claude"),
            db=Database(tmp_path / "c.db"),
        )
    message = str(error.value)
    assert "claude" in message
    # The message tells the operator what to do and names no secret.
    assert "COUNCIL_CLAUDE_CLI_PATH" in message or "auth login" in message


def test_an_explicit_offline_client_is_the_only_way_past_that(tmp_path):
    """The escape hatch is a parameter rather than an environment flag, because an
    environment variable that disables a credential check ends up set in production."""
    settings = settings_for(tmp_path, claude_cli_path="/nonexistent/claude")
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "c.db"),
        client_factory=mock_client,
        autostart=False,
    )
    with TestClient(app) as test_client:
        assert test_client.get("/readyz").status_code == 200


def test_readiness_reports_the_provider_without_exposing_the_account(client):
    """There is no API key to leak now, but there is an account: the CLI knows the user's
    email, organisation and org id, and none of them are anybody's business here."""
    body = client.get("/readyz").json()
    assert body["ready"] is True
    assert body["provider"] == "claude-code-cli"
    assert body["model"] == "mock"
    assert body["checks"]["authenticated"] is True

    serialised = str(body)
    assert "@" not in serialised          # no email
    assert "orgId" not in serialised and "orgName" not in serialised
    assert "api_key" not in serialised and "token" not in serialised
    assert "/" not in str(body.get("model"))  # and never a filesystem path


def test_a_process_with_no_worker_pool_is_alive_but_not_ready(tmp_path):
    """Where `/health` and `/readyz` part company. Without the lifespan the pool never
    starts, so the service accepts uploads and does nothing with them -- which is exactly
    what a readiness probe exists to catch and a liveness probe cannot see."""
    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "c.db"),
        client_factory=mock_client,
        autostart=False,
    )
    # Deliberately not used as a context manager: no lifespan, no runner.
    unstarted = TestClient(app)
    assert unstarted.get("/health").json() == {"status": "ok"}

    readiness = unstarted.get("/readyz")
    assert readiness.status_code == 503
    assert readiness.json()["checks"]["worker_pool"] is False


def test_a_configuration_failure_fails_the_job_and_is_not_resumable(
    tmp_path, workbook_bytes
):
    """A key the provider rejects must not look like work in progress. Left as a generic
    worker error the job sits in `created` with an expired lease, is swept, fails the
    same way, and is swept again -- forever."""
    from oatutor_council.llm.base import ProviderConfigurationError

    def broken_factory(_settings):
        raise ProviderConfigurationError("API key not valid")

    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=broken_factory,
        autostart=True,
    )
    with TestClient(app) as test_client:
        job_id = submit(test_client, workbook_bytes).json()["job_id"]
        _wait_for_terminal(test_client, job_id)
        status = test_client.get(f"/jobs/{job_id}").json()
        assert status["state"] == "failed"
        assert status["failure_reason"] == "config"

        resumed = test_client.post(f"/jobs/{job_id}/resume")
        assert resumed.status_code == 409


# --------------------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------------------


def test_a_valid_workbook_is_accepted_immediately(client, workbook_bytes):
    response = submit(client, workbook_bytes)
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "created"
    assert body["source_filename"] == "workbook.xlsx"


def test_the_client_filename_never_reaches_the_filesystem(client, workbook_bytes):
    """The single decision that removes the whole traversal class: the directory is a
    UUID and the file inside it has a fixed name."""
    response = submit(client, workbook_bytes, name="../../etc/passwd.xlsx")
    assert response.status_code == 202

    job_id = response.json()["job_id"]
    root = client.app_settings.data_root
    assert (root / job_id / "source" / "workbook.xlsx").is_file()
    assert not list(root.glob("**/passwd*"))
    # Stored for display only, reduced to its last component.
    assert response.json()["source_filename"] == "passwd.xlsx"


@pytest.mark.parametrize(
    "name", ["notes.txt", "workbook.xls", "archive.zip", "noextension"]
)
def test_a_workbook_with_the_wrong_extension_is_refused(client, workbook_bytes, name):
    response = submit(client, workbook_bytes, name=name)
    assert response.status_code == 400
    assert ".xlsx" in response.json()["error"]


def test_content_type_is_not_trusted_over_the_bytes(client):
    """`Content-Type` is attacker controlled. The first four bytes are what every reader
    will actually act on."""
    response = client.post(
        "/jobs",
        files={
            "workbook": (
                "workbook.xlsx",
                b"this is not a spreadsheet",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 400
    assert "does not look like" in response.json()["error"]


def test_an_empty_upload_is_refused(client):
    assert submit(client, b"").status_code == 400


def test_an_oversized_upload_is_refused(tmp_path, workbook_bytes):
    settings = settings_for(tmp_path, max_upload_bytes=100)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=mock_client,
        autostart=False,
    )
    with TestClient(app) as client:
        response = submit(client, workbook_bytes)
    assert response.status_code == 400
    assert "larger than" in response.json()["error"]


def test_an_archive_with_a_traversal_entry_is_refused(client):
    """A member with a `..` path escapes the extraction directory in any library that
    extracts naively."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../escape.txt", "payload")
    response = submit(client, buffer.getvalue())
    assert response.status_code == 400
    assert "outside itself" in response.json()["error"]


def test_a_decompression_bomb_is_refused(client):
    """Checking the upload size alone defends against the wrong number."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"\0" * (40 * 1024 * 1024))
    response = submit(client, buffer.getvalue())
    assert response.status_code == 400
    assert "expands" in response.json()["error"]


# --------------------------------------------------------------------------------------
# Instruction documents
# --------------------------------------------------------------------------------------


def test_an_instruction_document_seeds_the_job(client, workbook_bytes, tmp_path):
    notes = write_markdown(tmp_path / "notes.md").read_bytes()
    response = submit(
        client,
        workbook_bytes,
        instructions=("notes.md", notes, "text/markdown"),
    )
    assert response.status_code == 202
    assert response.json()["seed_claims"] > 0
    assert response.json()["instruction_filename"] == "notes.md"


def test_the_instructions_are_durable_before_the_job_is_queued(
    client, workbook_bytes, tmp_path
):
    """The claims exist in the database by the time any worker could pick the job up.

    Passing them to the runner instead means a job resumed by another process audits
    against no instructions -- and that failure is silent, because a job with zero claims
    looks exactly like a job whose claims were all refuted.
    """
    from oatutor_council.persistence import load_instruction_segments

    notes = write_markdown(tmp_path / "notes.md").read_bytes()
    job_id = submit(
        client, workbook_bytes, instructions=("notes.md", notes, "text/markdown")
    ).json()["job_id"]

    database = Database(client.app_settings.data_root / "council.db")
    segments = load_instruction_segments(database, job_id)
    assert segments
    assert all(s["text"].strip() for s in segments)
    assert all(s["provenance"] for s in segments)
    assert len({s["document_sha256"] for s in segments}) == 1
    assert [s["segment_index"] for s in segments] == sorted(
        s["segment_index"] for s in segments
    )


def test_a_council_built_fresh_reads_the_same_instructions(
    client, workbook_bytes, tmp_path
):
    """The resume case, stated directly: a council constructed with no knowledge of the
    upload finds the same claims, because the only place they ever lived is the
    database."""
    from oatutor_council.council import CurationCouncil
    from oatutor_council.workbook.writer import WorkingCopy
    from oatutor_council.models import SourcePath

    notes = write_markdown(tmp_path / "notes.md").read_bytes()
    job_id = submit(
        client, workbook_bytes, instructions=("notes.md", notes, "text/markdown")
    ).json()["job_id"]

    root = client.app_settings.data_root
    database = Database(root / "council.db")
    council = CurationCouncil(
        db=database,
        settings=client.app_settings,
        client=mock_client(client.app_settings),
        job_id=job_id,
        copy=WorkingCopy(
            source=SourcePath(str(root / job_id / "source" / "workbook.xlsx")),
            source_sha256="",
            path=root / job_id / "work" / "working.xlsx",
            tmp_dir=root / job_id / "work" / ".tmp",
        ),
    )
    assert len(council.seed_claims) > 0
    assert all(claim.text.strip() for claim in council.seed_claims)


def test_an_instruction_document_that_changes_under_the_job_is_refused(
    client, workbook_bytes, tmp_path
):
    """The same argument as the source hash. A job's conclusions are only meaningful
    against the inputs it was given, and every stored claim cites provenance in a file
    that would no longer exist."""
    notes = write_markdown(tmp_path / "notes.md").read_bytes()
    job_id = submit(
        client, workbook_bytes, instructions=("notes.md", notes, "text/markdown")
    ).json()["job_id"]

    root = client.app_settings.data_root
    (root / job_id / "source" / "instructions.md").write_text(
        "Something else entirely.", encoding="utf-8"
    )

    client.post(f"/jobs/{job_id}/resume")
    _wait_for_terminal(client, job_id)
    status = client.get(f"/jobs/{job_id}").json()
    assert status["state"] == "failed"
    assert status["failure_reason"] == "corruption"


def test_an_image_only_pdf_is_a_clear_error_and_starts_no_job(
    client, workbook_bytes, tmp_path
):
    """The requirement this endpoint exists to honour: an unreadable document must never
    be reported as a document containing no instructions."""
    scan = write_image_only_pdf(tmp_path / "scan.pdf").read_bytes()
    response = submit(
        client, workbook_bytes, instructions=("scan.pdf", scan, "application/pdf")
    )
    assert response.status_code == 400

    error = response.json()["error"]
    assert "no readable text layer" in error
    assert "OCR" in error
    assert "DOCX" in error


def test_no_job_is_left_behind_when_the_document_is_rejected(client, workbook_bytes, tmp_path):
    scan = write_image_only_pdf(tmp_path / "scan.pdf").read_bytes()
    submit(client, workbook_bytes, instructions=("scan.pdf", scan, "application/pdf"))
    assert client.get("/jobs").status_code in (404, 405)  # no listing endpoint
    # Nothing was recorded, so nothing can be polled or downloaded.
    from oatutor_council.persistence import claimable_jobs

    assert claimable_jobs(Database(client.app_settings.data_root / "council.db")) == ()


def test_an_unsupported_instruction_format_is_refused(client, workbook_bytes):
    response = submit(
        client,
        workbook_bytes,
        instructions=("notes.rtf", b"some notes", "application/rtf"),
    )
    assert response.status_code == 400
    assert ".docx" in response.json()["error"]


# --------------------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------------------


def test_status_is_pollable_before_the_job_runs(client, workbook_bytes):
    job_id = submit(client, workbook_bytes).json()["job_id"]
    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "created"
    assert body["succeeded"] is False


def test_an_unknown_job_is_a_404(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_the_full_run_is_observable_through_the_api(client, workbook_bytes):
    job_id = submit(client, workbook_bytes).json()["job_id"]
    client.app.state.runner.submit(
        job_id, client.app_settings.data_root / job_id
    )
    _wait_for_terminal(client, job_id)

    status = client.get(f"/jobs/{job_id}").json()
    assert status["state"] == "succeeded"
    assert status["changes"] == 1

    assert client.get(f"/jobs/{job_id}/issues").json()["issue_count"] >= 1
    assert client.get(f"/jobs/{job_id}/changes").json()["changes"][0]["after"] == "30"
    assert client.get(f"/jobs/{job_id}/reviews").json()["issues"]
    assert client.get(f"/jobs/{job_id}/report").json()["succeeded"] is True

    download = client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.content[:4] == b"PK\x03\x04"
    assert "corrected.xlsx" in download.headers["content-disposition"]


def _wait_for_terminal(client, job_id: str, limit: int = 200) -> None:
    import time

    for _ in range(limit):
        if client.get(f"/jobs/{job_id}").json()["state"] in {
            "succeeded",
            "needs_human_attention",
            "failed",
            "cancelled",
        }:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_downloading_before_the_workbook_is_ready_is_a_409(client, workbook_bytes):
    job_id = submit(client, workbook_bytes).json()["job_id"]
    response = client.get(f"/jobs/{job_id}/download")
    assert response.status_code == 409


def test_no_response_ever_leaks_a_filesystem_path(client, workbook_bytes, tmp_path):
    """A curator has no use for the storage layout, and an attacker probing for it should
    learn nothing."""
    root = str(client.app_settings.data_root)
    responses = [
        submit(client, workbook_bytes),
        submit(client, b"not a workbook"),
        submit(client, workbook_bytes, name="../../etc/passwd.xlsx"),
        client.get("/jobs/nope"),
    ]
    job_id = responses[0].json()["job_id"]
    responses += [
        client.get(f"/jobs/{job_id}"),
        client.get(f"/jobs/{job_id}/report"),
        client.get(f"/jobs/{job_id}/download"),
    ]
    for response in responses:
        assert root not in response.text
        assert "/tmp" not in response.text
        assert str(tmp_path) not in response.text


# --------------------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------------------


def test_a_finished_job_cannot_be_resumed(client, workbook_bytes):
    job_id = submit(client, workbook_bytes).json()["job_id"]
    client.app.state.runner.submit(job_id, client.app_settings.data_root / job_id)
    _wait_for_terminal(client, job_id)

    response = client.post(f"/jobs/{job_id}/resume")
    assert response.status_code == 409
    assert "cannot be resumed" in response.json()["detail"]


def test_a_job_with_a_live_lease_cannot_be_resumed(client, workbook_bytes):
    from oatutor_council.persistence import acquire_lease

    job_id = submit(client, workbook_bytes).json()["job_id"]
    db = Database(client.app_settings.data_root / "council.db")
    acquire_lease(db, job_id, "someone-else", lease_seconds=300)

    response = client.post(f"/jobs/{job_id}/resume")
    assert response.status_code == 409
    assert "already being worked on" in response.json()["detail"]


def test_an_interrupted_job_can_be_resumed(client, workbook_bytes):
    job_id = submit(client, workbook_bytes).json()["job_id"]
    response = client.post(f"/jobs/{job_id}/resume")
    assert response.status_code == 202
    _wait_for_terminal(client, job_id)
    assert client.get(f"/jobs/{job_id}").json()["state"] == "succeeded"


def test_the_lifespan_starts_a_poller(tmp_path):
    """Without one, a job whose worker died sits in the database with an expired lease
    and nobody ever looks at it -- crash recovery would exist in `recover_job` and never
    actually run."""
    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=mock_client,
        autostart=True,
    )
    with TestClient(app) as test_client:
        assert test_client.get("/readyz").json()["checks"]["poller"] is True
        assert app.state.poller._thread is not None
    assert app.state.poller._thread is None


def test_a_job_orphaned_by_a_dead_worker_is_picked_up_at_startup(tmp_path, workbook_bytes):
    """The durable queue in action. The submitting process is gone; the row is not."""
    from oatutor_council.persistence import acquire_lease

    settings = settings_for(tmp_path, poll_interval_seconds=0.05)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_root / "council.db")

    submitter = create_app(
        settings=settings, db=db, client_factory=mock_client, autostart=False
    )
    with TestClient(submitter) as first:
        job_id = submit(first, workbook_bytes).json()["job_id"]
    # A worker took it and died: the lease is expired and nothing is running.
    acquire_lease(db, job_id, "a-worker-that-died", lease_seconds=0)

    restarted = create_app(
        settings=settings, db=db, client_factory=mock_client, autostart=True
    )
    with TestClient(restarted) as second:
        _wait_for_terminal(second, job_id)
        assert second.get(f"/jobs/{job_id}").json()["state"] == "succeeded"


def test_resuming_a_failed_job_actually_moves_it(tmp_path, workbook_bytes):
    """`is_resumable` was a promise nothing kept: the endpoint returned 202, the worker
    found `FAILED` -- a state with no outgoing edge -- reported "nothing to do", and the
    job never moved. The curator saw an accepted request and no progress."""
    from oatutor_council.models import FailureReason, JobState
    from oatutor_council.persistence import get_job, transition_job

    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_root / "council.db")
    app = create_app(
        settings=settings, db=db, client_factory=mock_client, autostart=False
    )
    with TestClient(app) as test_client:
        job_id = submit(test_client, workbook_bytes).json()["job_id"]
        job = get_job(db, job_id)
        transition_job(
            db,
            job_id,
            JobState.FAILED,
            run_epoch=job.run_epoch,
            failure_reason=FailureReason.PROVIDER,
        )

        assert test_client.post(f"/jobs/{job_id}/resume").status_code == 202
        # Deliberately not `_wait_for_terminal`: `failed` *is* terminal, so it would
        # return at once and the assertion would pass on a job that never moved.
        _wait_for_state(test_client, job_id, "succeeded")


def _wait_for_state(client, job_id: str, expected: str, limit: int = 200) -> None:
    import time

    for _ in range(limit):
        if client.get(f"/jobs/{job_id}").json()["state"] == expected:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"job stayed in {client.get(f'/jobs/{job_id}').json()['state']}, "
        f"never reached {expected}"
    )


def test_the_report_endpoint_shows_the_findings_the_report_file_shows(tmp_path):
    """The bug this replaces: findings were passed as `()` here, so `GET /report`
    answered with an empty `remaining_findings` for a job whose own `report.md` listed
    them -- the API quietly reassuring a curator that a workbook needing work was
    finished."""
    from conftest import write_workbook

    # A workbook with a defect no scripted repair will fix, so findings survive to the end.
    path = write_workbook(
        tmp_path / "broken.xlsx",
        [
            problem("angles1", title="Convert", oer_src="s", license="CC"),
            step("angles1", answer="pi/6", answer_type="algebra"),
            scaffold("angles1", "s1", answer="", answer_type="numeric"),
        ],
    )
    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_root / "council.db")
    app = create_app(
        settings=settings, db=db, client_factory=_client_that_never_repairs, autostart=True
    )
    with TestClient(app) as client:
        job_id = submit(client, path.read_bytes()).json()["job_id"]
        _wait_for_terminal(client, job_id)

        report = client.get(f"/jobs/{job_id}/report").json()
        rendered = (settings.data_root / job_id / "outputs" / "report.md").read_text()

        assert report["state"] == "needs_human_attention"
        assert report["succeeded"] is False
        assert report["remaining_findings"], "the endpoint reported no findings"
        assert report["remaining_findings"][0]["code"] in rendered


def test_the_report_endpoint_reports_nothing_when_the_final_round_is_clean(tmp_path):
    """The same lie as the test above, pointed the other way — and this one reached a real
    curator.

    Final validation of a repaired workbook records an empty list at `FINAL_GATE_ROUND`,
    which wrote no rows, so the latest round was derived from the findings and landed on
    round 0. The endpoint answered with the defects the job had already fixed: a finished
    workbook presented as still carrying every fault it arrived with.
    """
    from oatutor_council.council import FINAL_GATE_ROUND
    from oatutor_council.models import CurationJob, ValidationFinding
    from oatutor_council.persistence import create_job, latest_findings, record_findings

    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_root / "council.db")
    create_job(db, CurationJob(job_id="job-1", source_filename="w.xlsx"))

    stale = ValidationFinding(
        code="MC_ANSWER_NOT_IN_CHOICES",
        message="the answer matches no choice",
        severity="error",
        row=4,
        column=9,
    )
    record_findings(db, "job-1", 0, [stale], ["fp-1"])
    record_findings(db, "job-1", FINAL_GATE_ROUND, [], [])

    assert latest_findings(db, "job-1", kind="content") == ()


def _client_that_never_repairs(_settings) -> ScriptedLLMClient:
    """A Writer that escalates instead of editing, so the defect is still there at the end."""
    replies = {
        AgentRole.INITIAL_AUDITOR: AuditorResponse(),
        AgentRole.INDEPENDENT_REVIEWER: IndependentReviewResponse(block_is_sound=True),
        AgentRole.WRITER: WriterResponse(
            derivation="",
            needs_human_review=True, human_review_reason="I cannot determine the answer"
        ),
        AgentRole.KNOWN_ISSUE_REVIEWER: ReviewerResponse(decision="accept"),
    }
    client = ScriptedLLMClient()
    client.default = lambda request: replies[request.role]
    return client


def test_the_report_accounts_for_the_files_it_produced(client, workbook_bytes):
    """Hashes, not paths: a curator checks the file they downloaded is the file the
    report is about, and the storage layout is nobody's business."""
    job_id = submit(client, workbook_bytes).json()["job_id"]
    client.app.state.runner.submit(job_id, client.app_settings.data_root / job_id)
    _wait_for_terminal(client, job_id)

    report = client.get(f"/jobs/{job_id}/report").json()
    kinds = {a["kind"]: a for a in report["artifacts"]}
    assert "corrected_workbook" in kinds
    assert len(kinds["corrected_workbook"]["sha256"]) == 64
    assert not any("/" in str(value) for value in kinds["corrected_workbook"].values())


def test_the_status_and_report_agree_about_what_the_job_cost(client, workbook_bytes):
    job_id = submit(client, workbook_bytes).json()["job_id"]
    client.app.state.runner.submit(job_id, client.app_settings.data_root / job_id)
    _wait_for_terminal(client, job_id)

    status = client.get(f"/jobs/{job_id}").json()
    report = client.get(f"/jobs/{job_id}/report").json()
    assert status["usage"]["calls"] == report["model_usage"]["calls"] > 0
    assert status["usage"]["calls"] == status["llm_calls_used"]


# --------------------------------------------------------------------------------------
# Production concerns
# --------------------------------------------------------------------------------------


def test_an_upload_larger_than_the_limit_is_refused_without_being_buffered(tmp_path):
    """The cap is applied as the bytes arrive. Reading the whole body first and *then*
    comparing its length means a client wanting to exhaust memory simply sends more than
    the limit -- the check runs after the damage."""
    settings = settings_for(tmp_path, max_upload_bytes=64 * 1024)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=mock_client,
        autostart=False,
    )
    oversized = b"PK\x03\x04" + b"x" * (512 * 1024)
    with TestClient(app) as client:
        response = submit(client, oversized)
        assert response.status_code == 400
        assert "limit" in response.json()["error"]

    # And nothing is left behind: a stranger must not be able to fill the disk with the
    # leading megabytes of files the service refused.
    stored = list(settings.data_root.rglob("workbook.xlsx"))
    assert stored == []


def test_a_rejected_upload_never_reveals_where_it_would_have_been_stored(client):
    response = submit(client, b"not a spreadsheet at all", name="evil.xlsx")
    assert response.status_code == 400
    assert "/" not in response.json()["error"]


def test_every_route_but_health_needs_the_token_when_one_is_set(tmp_path, workbook_bytes):
    """Opt-in, because this normally sits behind something that already authenticates --
    but the open case must not be invisible, so `/readyz` reports it."""
    settings = settings_for(tmp_path, api_token="s3cret-token")
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=mock_client,
        autostart=False,
    )
    with TestClient(app) as client:
        # Liveness stays open: a probe that needs a secret is a probe that reports the
        # process as dead when the secret is wrong.
        assert client.get("/health").status_code == 200

        assert client.get("/readyz").status_code == 401
        assert submit(client, workbook_bytes).status_code == 401
        assert client.get("/jobs/anything").status_code == 401

        auth = {"Authorization": "Bearer s3cret-token"}
        assert client.get("/readyz", headers=auth).status_code == 200
        assert client.get("/readyz", headers=auth).json()["authentication_required"] is True
        assert submit(client, workbook_bytes, headers=auth).status_code == 202

        assert client.get(
            "/readyz", headers={"Authorization": "Bearer s3cret-tokes"}
        ).status_code == 401
        assert client.get(
            "/readyz", headers={"Authorization": "s3cret-token"}
        ).status_code == 401


def test_an_open_service_says_so(client):
    assert client.get("/readyz").json()["authentication_required"] is False


def test_artifacts_are_found_again_after_the_data_root_moves(tmp_path, workbook_bytes):
    """The column has been called `relative_path` since the first schema and was being
    handed absolute paths, which tied every job to the filesystem of the machine that made
    it. Move `DATA_ROOT` -- a mounted volume, a restored backup, another container -- and
    every artefact lookup failed on rows that looked perfectly healthy."""
    import shutil

    settings = settings_for(tmp_path)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        settings=settings,
        db=Database(settings.data_root / "council.db"),
        client_factory=mock_client,
        autostart=True,
    )
    with TestClient(app) as client:
        job_id = submit(client, workbook_bytes).json()["job_id"]
        _wait_for_terminal(client, job_id)
        assert client.get(f"/jobs/{job_id}/download").status_code == 200

    # The storage moves wholesale, as a restore or a remount would move it.
    moved = tmp_path / "somewhere-else"
    shutil.move(str(settings.data_root), str(moved))

    relocated = settings_for(tmp_path, data_root=moved)
    app = create_app(
        settings=relocated,
        db=Database(moved / "council.db"),
        client_factory=mock_client,
        autostart=False,
    )
    with TestClient(app) as client:
        assert client.get(f"/jobs/{job_id}/download").status_code == 200
