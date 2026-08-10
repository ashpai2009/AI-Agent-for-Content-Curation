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


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error


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

    #: Decision 1: reviewers judge the artefact, not the Writer's argument for it. Kept
    #: as a flag so the opposite reading stays testable rather than unimaginable.
    reviewer_sees_writer_rationale: bool = False

    def require_credentials(self) -> None:
        """Fail loudly and early rather than at the first model call.

        A missing key discovered halfway through a job has already cost the curator the
        upload and the wait.
        """
        if not self.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. The curation council cannot run without "
                "credentials for the Gemini API."
            )


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
        reviewer_sees_writer_rationale=_bool("REVIEWER_SEES_WRITER_RATIONALE", False),
    )
