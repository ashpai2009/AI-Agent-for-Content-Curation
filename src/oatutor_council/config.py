"""Settings, read from the environment in exactly one place.

The model name and the API key are read here and nowhere else. A provider module that
reaches for `os.environ` itself is a module that cannot be tested without credentials and
cannot be pointed at a different model without an edit.

Every budget is a setting rather than a constant because they are the system's fuses:
they exist to be tightened in an incident, and a fuse you have to redeploy to change is
not a fuse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: The three-attempt cap from the specification. Enforced in exactly one function --
#: `state_machine.IssueMachine.reserve_attempt` -- so there is one place to audit.
DEFAULT_MAX_REPAIR_ATTEMPTS = 3

#: Liveness and provider-failure defaults. Named constants rather than literals in two
#: places, so the dataclass default and the environment default cannot drift apart.
DEFAULT_HEARTBEAT_DIVISOR = 4
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 120.0
DEFAULT_PROVIDER_MAX_ATTEMPTS = 4
DEFAULT_PROVIDER_BACKOFF_CEILING_SECONDS = 60.0
DEFAULT_PROVIDER_FAILURE_BUDGET = 12
DEFAULT_RUN_DEADLINE_SECONDS = 21_600.0


class ConfigurationError(RuntimeError):
    """The service is not configured well enough to do the work it accepts.

    Raised at startup rather than at the first model call. A service that starts happily
    without credentials accepts uploads it can never process, and the curator finds out
    after the wait rather than before the upload.
    """


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, got {raw!r}") from error


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    data_root: Path

    max_repair_attempts: int
    max_validation_rounds: int
    step_budget: int
    llm_call_budget: int
    interrupted_retry_budget: int

    max_concurrent_jobs: int
    max_upload_bytes: int
    lease_seconds: int

    #: How often a running worker renews its lease. Must be comfortably shorter than
    #: `lease_seconds` or a worker loses a job it is actively working on -- see
    #: `heartbeat_seconds` below, which enforces exactly that rather than trusting it.
    heartbeat_divisor: int = DEFAULT_HEARTBEAT_DIVISOR
    #: How often the poller looks for jobs whose worker died.
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS

    #: Per model call. A call with no ceiling can hold a lease for the life of the process.
    provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    #: Total attempts per call, including the first. Bounded because every class of
    #: transient failure this system retries is one that repeats when it is not transient.
    provider_max_attempts: int = DEFAULT_PROVIDER_MAX_ATTEMPTS
    provider_backoff_ceiling_seconds: float = DEFAULT_PROVIDER_BACKOFF_CEILING_SECONDS
    #: How many model calls a job may lose to provider failure before the job is failed.
    #: Counted from durable events, so it survives the crash of the worker that spent them.
    provider_failure_budget: int = DEFAULT_PROVIDER_FAILURE_BUDGET
    #: Wall-clock ceiling for one worker's run of a job. Zero disables it.
    run_deadline_seconds: float = DEFAULT_RUN_DEADLINE_SECONDS

    #: Decision 1: reviewers judge the artefact, not the Writer's argument for it. Kept
    #: as a flag so the opposite reading stays testable rather than unimaginable.
    reviewer_sees_writer_rationale: bool = False

    @property
    def heartbeat_seconds(self) -> float:
        """The renewal interval, derived so it *cannot* be longer than the lease.

        Kept as a derived property rather than its own setting on purpose. Two independent
        numbers where one must stay under the other is a configuration mistake waiting to
        be made in an incident, and the failure it produces is subtle: a worker that is
        alive and mid-model-call loses its job to the poller, which starts a second worker
        on the same job. One number, and the relationship holds by construction.
        """
        divisor = max(2, self.heartbeat_divisor)
        return max(1.0, self.lease_seconds / divisor)

    @property
    def provider_configured(self) -> bool:
        return bool(self.gemini_api_key.strip() and self.gemini_model.strip())

    def require_credentials(self) -> None:
        """Fail loudly and early rather than at the first model call.

        A missing key discovered halfway through a job has already cost the curator the
        upload and the wait -- and worse, the job sits in `created` looking like work in
        progress rather than work that was never possible.
        """
        if not self.gemini_api_key.strip():
            raise ConfigurationError(
                "GEMINI_API_KEY is not set. The curation council cannot run without "
                "credentials for the Gemini API. Set it in the environment or in .env, "
                "or start the service with an explicit offline client."
            )
        if not self.gemini_model.strip():
            raise ConfigurationError(
                "GEMINI_MODEL is empty. Unset it to use the default, or name a model."
            )

    def describe_provider(self) -> dict[str, object]:
        """What is configured, in a form that is safe to serve over HTTP.

        The key itself never appears -- not truncated, not fingerprinted, not its length.
        The only question an operator needs answered here is *is one present*, and every
        further detail is material for someone who should not have any.
        """
        return {
            "provider": "google-gemini",
            "model": self.gemini_model,
            "credentials_present": bool(self.gemini_api_key.strip()),
        }


def load_settings(*, env_file: str | Path | None = ".env") -> Settings:
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    return Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip(),
        data_root=Path(os.environ.get("DATA_ROOT", "./jobs")).expanduser(),
        max_repair_attempts=_int("MAX_REPAIR_ATTEMPTS", DEFAULT_MAX_REPAIR_ATTEMPTS),
        max_validation_rounds=_int("MAX_VALIDATION_ROUNDS", 2),
        step_budget=_int("STEP_BUDGET", 2000),
        llm_call_budget=_int("LLM_CALL_BUDGET", 1500),
        interrupted_retry_budget=_int("INTERRUPTED_RETRY_BUDGET", 2),
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 52_428_800),
        lease_seconds=_int("LEASE_SECONDS", 120),
        heartbeat_divisor=_int("HEARTBEAT_DIVISOR", DEFAULT_HEARTBEAT_DIVISOR),
        poll_interval_seconds=_float(
            "POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
        ),
        provider_timeout_seconds=_float(
            "PROVIDER_TIMEOUT_SECONDS", DEFAULT_PROVIDER_TIMEOUT_SECONDS
        ),
        provider_max_attempts=_int(
            "PROVIDER_MAX_ATTEMPTS", DEFAULT_PROVIDER_MAX_ATTEMPTS
        ),
        provider_backoff_ceiling_seconds=_float(
            "PROVIDER_BACKOFF_CEILING_SECONDS", DEFAULT_PROVIDER_BACKOFF_CEILING_SECONDS
        ),
        provider_failure_budget=_int(
            "PROVIDER_FAILURE_BUDGET", DEFAULT_PROVIDER_FAILURE_BUDGET
        ),
        run_deadline_seconds=_float("RUN_DEADLINE_SECONDS", DEFAULT_RUN_DEADLINE_SECONDS),
        reviewer_sees_writer_rationale=_bool("REVIEWER_SEES_WRITER_RATIONALE", False),
    )
