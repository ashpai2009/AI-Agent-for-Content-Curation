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
        gemini_api_key="test",
        gemini_model="mock",
        data_root=tmp_path / "jobs",
        max_repair_attempts=3,
        max_validation_rounds=2,
        step_budget=400,
        llm_call_budget=200,
        interrupted_retry_budget=2,
        max_concurrent_jobs=1,
        max_upload_bytes=5 * 1024 * 1024,
        lease_seconds=60,
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


def submit(client, data: bytes, name: str = "workbook.xlsx", **extra):
    files = {"workbook": (name, data, "application/octet-stream")}
    files.update(extra)
    return client.post("/jobs", files=files)


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
    client.app.state.runner.shutdown()
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
    client.app.state.runner.shutdown()
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
    client.app.state.runner.shutdown()
    _wait_for_terminal(client, job_id)
    assert client.get(f"/jobs/{job_id}").json()["state"] == "succeeded"
