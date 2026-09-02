"""Leases, heartbeats, the poller, and dying at every stage.

The question every test here asks is the same one: *what happens when the worker is not
the only thing that exists?* A second process, a stolen lease, a shutdown mid-job, a crash
between two steps. The answers have to come from durable rows, because a crashed worker
takes its memory with it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from conftest import compliant, problem, scaffold, step
from oatutor_council.agents.schemas import (
    AuditorResponse,
    IndependentReviewResponse,
    ReviewerResponse,
    WriterResponse,
)
from oatutor_council.config import Settings
from oatutor_council.council import CurationCouncil
from oatutor_council.llm.base import AgentRole
from oatutor_council.llm.mock import ScriptedLLMClient
from oatutor_council.models import (
    ColumnKey,
    CurationJob,
    JobState,
    SourcePath,
)
from oatutor_council.persistence import (
    ConcurrencyError,
    Database,
    acquire_lease,
    assert_lease_held,
    claimable_jobs,
    create_job,
    get_job,
    list_changes,
    list_events,
    release_lease,
)
from oatutor_council.workbook.reader import read_workbook
from oatutor_council.workbook.writer import create_working_copy
from oatutor_council.workers import (
    JobPoller,
    JobRunner,
    LeaseKeeper,
    resume_from_failure,
)


def settings(**overrides) -> Settings:
    defaults = dict(
        claude_cli_path="fake-claude",
        claude_model="mock",
        claude_effort="medium",
        data_root=Path("."),
        max_repair_attempts=3,
        max_validation_rounds=2,
        step_budget=400,
        llm_call_budget=200,
        interrupted_retry_budget=2,
        max_concurrent_jobs=2,
        max_upload_bytes=1024,
        lease_seconds=60,
        # One physical call per logical call. A scripted mock is not a provider, and
        # retrying one tests nothing -- while an absorbed failure would silently change
        # what the failure-handling tests below are asserting about. The retry layer has
        # its own tests, against a client that actually fails.
        provider_max_attempts=1,
        repair_batch_size=1,
        scan_batch_size=1,
    )
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def source(make_workbook) -> Path:
    return make_workbook(
        [
            problem("angles1", title="Convert", oer_src="s", license="CC"),
            step("angles1", answer="pi/6", answer_type="algebra"),
            scaffold("angles1", "s1", answer="", answer_type="numeric"),
        ]
    )


@pytest.fixture
def job_dir(tmp_path) -> Path:
    # Named for the job, as `job_dir_for` names it in production: `data_root/<job_id>`.
    # A fixture that put it anywhere else would pass tests that production layout fails.
    return tmp_path / "job-1"


@pytest.fixture
def setup(source, tmp_path, job_dir):
    """A job directory laid out exactly as `POST /jobs` lays one out.

    That matters here and nowhere else: the runner rebuilds the working-copy handle from
    durable state -- `job_dir/source/workbook.xlsx` -- because a resumed job has no memory
    of where the first worker put anything. A fixture that put the source somewhere else
    would test a path production never takes.
    """
    import shutil

    db = Database(tmp_path / "council.db")
    source_dir = job_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    stored = source_dir / "workbook.xlsx"
    shutil.copy2(source, stored)
    copy = create_working_copy(SourcePath(str(stored)), job_dir)
    create_job(
        db,
        CurationJob(
            job_id="job-1",
            source_filename="workbook.xlsx",
            source_sha256=copy.source_sha256,
        ),
    )
    return db, copy


def quiet_client(**overrides) -> ScriptedLLMClient:
    replies = {
        AgentRole.INITIAL_AUDITOR: AuditorResponse(),
        AgentRole.INDEPENDENT_REVIEWER: IndependentReviewResponse(block_is_sound=True),
        AgentRole.WRITER: WriterResponse(
            derivation="a scaffold needs an answer",
            edits=[{"row": 4, "column": "answer", "before": "", "after": "30"}],
        ),
        AgentRole.KNOWN_ISSUE_REVIEWER: ReviewerResponse(decision="accept"),
    }
    replies.update(overrides)
    client = ScriptedLLMClient()
    client.default = compliant(lambda request: replies[request.role])
    return client


def council(setup, client, *, worker_id="worker", run_epoch=None, **kwargs):
    db, copy = setup
    return CurationCouncil(
        db=db,
        settings=settings(**kwargs),
        client=client,
        job_id="job-1",
        copy=copy,
        worker_id=worker_id,
        run_epoch=run_epoch,
    )


# --------------------------------------------------------------------------------------
# Heartbeats
# --------------------------------------------------------------------------------------


def test_the_heartbeat_interval_cannot_be_configured_longer_than_the_lease():
    """Two independent numbers where one must stay under the other is a mistake waiting
    to be made in an incident, and the failure is subtle: a worker that is alive and
    mid-model-call loses its job to the poller, which starts a second worker on it."""
    for lease in (5, 60, 600):
        for divisor in (-1, 0, 1, 4, 50):
            resolved = settings(lease_seconds=lease, heartbeat_divisor=divisor)
            assert resolved.heartbeat_seconds < lease


def test_a_lease_survives_a_model_call_longer_than_the_lease(setup):
    """The reason the keeper is a thread. A lease renewed only between steps expires in
    the middle of exactly the work it exists to protect: one model call can legitimately
    outlast the whole lease."""
    db, _ = setup
    job = acquire_lease(db, "job-1", "worker-a", lease_seconds=1)

    with LeaseKeeper(
        db, "job-1", "worker-a", run_epoch=job.run_epoch, lease_seconds=1, interval=0.1
    ) as keeper:
        time.sleep(1.4)  # longer than the lease, as a slow model call would be
        assert not keeper.lost
        assert claimable_jobs(db) == ()

    assert get_job(db, "job-1").lease_owner == "worker-a"


def test_a_keeper_whose_job_was_taken_says_so(setup):
    db, _ = setup
    first = acquire_lease(db, "job-1", "worker-a", lease_seconds=1)

    with LeaseKeeper(
        db, "job-1", "worker-a", run_epoch=first.run_epoch, lease_seconds=1, interval=0.05
    ) as keeper:
        # Reassigned while the keeper is still renewing. A real steal waits for the lease
        # to expire, which cannot happen while the keeper is doing its job -- so the case
        # under test is the one that follows: the job moved on and this worker has to
        # find out between steps rather than by writing to a job it no longer holds.
        release_lease(db, "job-1", "worker-a", run_epoch=first.run_epoch)
        acquire_lease(db, "job-1", "worker-b", lease_seconds=60)
        for _ in range(40):
            if keeper.lost:
                break
            time.sleep(0.05)
        assert keeper.lost


def test_a_keeper_stops_when_its_block_ends(setup):
    """Structural, not disciplinary: a keeper that outlived its worker would hold a job
    hostage for as long as the process lived."""
    db, _ = setup
    job = acquire_lease(db, "job-1", "worker-a", lease_seconds=30)
    before = threading.active_count()

    with LeaseKeeper(
        db, "job-1", "worker-a", run_epoch=job.run_epoch, lease_seconds=30, interval=0.05
    ):
        time.sleep(0.15)
        assert threading.active_count() > before

    assert threading.active_count() == before


# --------------------------------------------------------------------------------------
# Fencing
# --------------------------------------------------------------------------------------


def test_a_fenced_worker_writes_nothing_to_the_database(setup):
    """The epoch is pinned at construction. Re-reading it at each write -- which is what
    this used to do -- always agrees with itself and fences nothing."""
    db, _ = setup
    first = acquire_lease(db, "job-1", "worker-a", lease_seconds=1)
    machine = council(setup, quiet_client(), worker_id="worker-a", run_epoch=first.run_epoch)

    machine.step()  # created -> ingesting, under the epoch it holds
    time.sleep(1.1)
    acquire_lease(db, "job-1", "worker-b", lease_seconds=60)

    with pytest.raises(ConcurrencyError):
        machine.step()


def test_a_fenced_worker_stops_rather_than_failing_the_job(setup):
    """Losing a lease is not a failure of the job. Somebody else owns it now, and marking
    it failed would overwrite the new owner's work with a verdict about a run that no
    longer matters."""
    db, _ = setup
    first = acquire_lease(db, "job-1", "worker-a", lease_seconds=1)
    machine = council(setup, quiet_client(), worker_id="worker-a", run_epoch=first.run_epoch)
    machine.step()

    time.sleep(1.1)
    acquire_lease(db, "job-1", "worker-b", lease_seconds=60)
    result = machine.run()

    assert not result.is_terminal
    assert result.failure_reason is None
    assert any(event["kind"] == "lease_lost" for event in list_events(db, "job-1"))


def test_a_fenced_worker_cannot_touch_the_workbook(setup):
    """Database writes are fenced by the epoch, and a fenced worker simply updates
    nothing. `os.replace` consults no table -- so without an explicit check, a worker
    whose lease was stolen mid-step could still rewrite the file the new owner is
    reading."""
    db, copy = setup
    first = acquire_lease(db, "job-1", "worker-a", lease_seconds=1)
    machine = council(setup, quiet_client(), worker_id="worker-a", run_epoch=first.run_epoch)

    # Drive up to the point where a patch is waiting to be applied.
    for _ in range(40):
        if any(
            issue.state.value in ("patch_proposed", "applying")
            for issue in _issues(db)
        ):
            break
        machine.step()

    time.sleep(1.1)
    acquire_lease(db, "job-1", "worker-b", lease_seconds=60)
    digest_before = copy.path.read_bytes()

    with pytest.raises(ConcurrencyError):
        machine.step()
    assert copy.path.read_bytes() == digest_before
    assert list_changes(db, "job-1") == ()


def _issues(db):
    from oatutor_council.persistence import list_issues

    return list_issues(db, "job-1")


def test_an_unleased_job_can_still_be_worked_on(setup):
    """Leasing is mandatory to work *concurrently*, not to work at all. The offline demo,
    an embedded runner and these tests drive the council with no lease in sight."""
    db, _ = setup
    assert_lease_held(db, "job-1", "anybody", run_epoch=0)
    assert council(setup, quiet_client()).run().state is JobState.SUCCEEDED


# --------------------------------------------------------------------------------------
# The poller
# --------------------------------------------------------------------------------------


def test_the_poller_picks_up_a_job_whose_worker_died(setup, job_dir, monkeypatch):
    """Without this, crash recovery exists in `recover_job` and never actually runs: a
    job whose worker died sits in the database with an expired lease and nobody looks."""
    db, copy = setup
    acquire_lease(db, "job-1", "the-worker-that-died", lease_seconds=0)

    runner = JobRunner(
        db=db, settings=settings(data_root=job_dir.parent), client_factory=lambda _: quiet_client()
    )
    monkeypatch.setattr(
        "oatutor_council.workers.job_dir_for", lambda settings, job_id: job_dir
    )
    poller = JobPoller(runner, interval=0.05)
    poller.start()
    try:
        _wait_until(lambda: get_job(db, "job-1").is_terminal)
    finally:
        poller.stop()
        runner.shutdown(wait=2)

    assert get_job(db, "job-1").state is JobState.SUCCEEDED
    assert read_workbook(copy.path).blocks[0].rows[2].get(ColumnKey.ANSWER) == "30"


def test_the_poller_does_not_queue_a_job_it_is_already_running(setup, job_dir, monkeypatch):
    """The lease is the safety mechanism, but a poller that piled futures behind a
    running job would have each one acquire a lease and immediately lose it."""
    db, _ = setup
    started = threading.Event()
    release = threading.Event()

    def blocking_client(_settings):
        client = quiet_client()
        inner = client.default

        def slow(request):
            started.set()
            release.wait(5)
            return inner(request)

        client.default = compliant(slow)
        return client

    runner = JobRunner(db=db, settings=settings(), client_factory=blocking_client)
    monkeypatch.setattr(
        "oatutor_council.workers.job_dir_for", lambda settings, job_id: job_dir
    )
    try:
        assert runner.submit("job-1", job_dir) is True
        started.wait(5)
        assert runner.sweep() == 0
        assert runner.submit("job-1", job_dir) is False
    finally:
        release.set()
        runner.shutdown(wait=5)


def test_shutdown_stops_a_running_council_at_a_step_boundary(setup, job_dir, monkeypatch):
    """Cancelling futures only affects work that has not started. A council mid-job is
    exactly the work worth stopping cleanly: it releases its lease on the way out, so a
    restarted process picks the job up immediately instead of waiting out the lease."""
    db, _ = setup
    runner = JobRunner(db=db, settings=settings(), client_factory=lambda _: quiet_client())
    monkeypatch.setattr(
        "oatutor_council.workers.job_dir_for", lambda settings, job_id: job_dir
    )
    runner.submit("job-1", job_dir)
    runner.shutdown(wait=5)

    job = get_job(db, "job-1")
    assert job.lease_owner is None
    if not job.is_terminal:
        assert claimable_jobs(db) != ()


def _wait_until(predicate, limit: float = 10.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was never reached")


# --------------------------------------------------------------------------------------
# Dying at each stage
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("killed_after", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15])
def test_a_worker_killed_at_step_n_resumes_where_it_stopped(setup, killed_after):
    """The step contract in practice: at most one model call, at most one workbook
    mutation, exactly one committed unit of progress. That makes every boundary a
    consistent point, so a crash at any of them is recoverable -- and the second worker
    reconstructs everything it needs from durable rows, because the first one's memory
    went with it."""
    db, copy = setup
    first = council(setup, quiet_client())
    first.run(max_steps=killed_after)

    if get_job(db, "job-1").is_terminal:
        pytest.skip("the job finished before this cut-off")

    # A genuinely new worker: new council, new taint registry, nothing carried over.
    second = council(setup, quiet_client())
    result = second.run()

    assert result.state is JobState.SUCCEEDED, result.failure_reason
    # Exactly one repair, applied once -- not lost to the crash, not applied twice.
    assert [(c.row, c.after) for c in list_changes(db, "job-1")] == [(4, "30")]
    assert read_workbook(copy.path).blocks[0].rows[2].get(ColumnKey.ANSWER) == "30"


def test_a_run_that_overruns_its_deadline_fails_and_stays_resumable(setup):
    """Bounds this *run*, not the job's whole existence. Anchoring to the job's creation
    was the first reading and it is a trap: the job would fail again the instant anyone
    resumed it, since the age that tripped the ceiling only grows."""
    from oatutor_council.models import FailureReason
    from oatutor_council.state_machine import is_resumable

    db, _ = setup
    machine = council(setup, quiet_client(), run_deadline_seconds=0.001)
    time.sleep(0.01)
    result = machine.run()

    assert result.state is JobState.FAILED
    assert result.failure_reason is FailureReason.TIMEOUT
    assert is_resumable(result.state, result.failure_reason)

    # And resuming actually moves it. `is_resumable` was a promise nothing kept until
    # `FAILED` had somewhere to go: the endpoint returned 202, the worker found a state
    # with no outgoing edge, reported "nothing to do", and the job never moved.
    assert resume_from_failure(db, "job-1", run_epoch=get_job(db, "job-1").run_epoch)
    assert council(setup, quiet_client()).run().state is JobState.SUCCEEDED


# --------------------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------------------


def test_a_finished_job_is_deleted_once_it_is_past_the_retention_window(setup, job_dir):
    """Files first, then rows. The other order can leave a directory nobody has a record
    of, which is the worst outcome available: a curator's workbook on a disk with nothing
    left to say whose it was or that it should have been deleted."""
    from oatutor_council.persistence import get_job
    from oatutor_council.workers import purge_expired_jobs

    db, _ = setup
    council(setup, quiet_client()).run()
    assert job_dir.is_dir()

    _age_job(db, "job-1", days=90)
    purged = purge_expired_jobs(
        db, settings(data_root=job_dir.parent, retention_days=30)
    )

    assert purged == 1
    assert get_job(db, "job-1") is None
    assert not job_dir.exists()


def test_a_running_job_is_never_purged_however_old_the_submission(setup, job_dir):
    """A job still in flight is not old, however long ago it was submitted. A retention
    sweep that deleted a running job's working copy would be a data-loss bug wearing a
    compliance hat."""
    from oatutor_council.persistence import get_job
    from oatutor_council.workers import purge_expired_jobs

    db, _ = setup
    council(setup, quiet_client()).run(max_steps=3)
    assert not get_job(db, "job-1").is_terminal

    _age_job(db, "job-1", days=900)
    assert purge_expired_jobs(db, settings(data_root=job_dir.parent, retention_days=1)) == 0
    assert get_job(db, "job-1") is not None
    assert job_dir.is_dir()


def test_retention_can_be_switched_off_but_only_deliberately(setup, job_dir):
    """`RETENTION_DAYS=0` keeps everything. It is a decision somebody has to make, not
    the default, because this service holds other people's course material."""
    from oatutor_council.persistence import get_job
    from oatutor_council.workers import purge_expired_jobs

    db, _ = setup
    council(setup, quiet_client()).run()
    _age_job(db, "job-1", days=9999)

    assert purge_expired_jobs(db, settings(data_root=job_dir.parent, retention_days=0)) == 0
    assert get_job(db, "job-1") is not None


def test_deleting_a_job_takes_its_private_reasoning_with_it(setup, job_dir):
    """The child tables cascade, which is why `foreign_keys=ON` is a pragma rather than a
    preference: deleting a job by hand across fourteen tables is how orphaned agent
    reasoning outlives the job it belonged to."""
    from oatutor_council.persistence import (
        delete_job,
        list_llm_calls,
        load_private_blobs,
    )

    db, _ = setup
    council(setup, quiet_client()).run()
    assert load_private_blobs(db, "job-1")

    delete_job(db, "job-1")
    assert load_private_blobs(db, "job-1") == ()
    assert list_llm_calls(db, "job-1") == ()


def _age_job(db, job_id: str, *, days: float) -> None:
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.write() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ?", (stale, job_id)
        )
