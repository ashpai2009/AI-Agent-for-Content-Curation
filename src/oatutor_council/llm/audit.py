"""Recording every model call, at the one place all four agents already pass through.

`RecordingClient` wraps an `LLMClient` and writes an `llm_calls` row per request. It is a
wrapper rather than a call inside each agent because there are four agents and there will
be more: an audit trail that each new agent has to remember to write to is an audit trail
with holes in it, and the holes are invisible -- a missing row looks exactly like a call
that was never made.

**Failed calls are recorded too**, and that is most of the value. A job that spent four
calls on an outage and one on a repair is indistinguishable from a job that made one call,
unless the four are written down.

**One row per physical invocation**, which is why retrying sits *outside* this wrapper.
While the provider retried internally, four `claude` processes produced one row and one
budget charge: the trail understated what the job spent, and the budget bounded a quarter
of what it claimed to. `RetryingClient` now wraps this, so each attempt passes through
here on its own.

What is stored: the role, the model, the status, the pinned prompt version, the token
usage, the prompt hash, and the exact text of both halves of the prompt. The text is what
makes the isolation guarantee auditable after the fact -- a test asserting that a reviewer
never saw the Writer's rationale is only worth something if it reads what was transmitted
rather than what a mock remembers. The **API key is never part of any of that**: it lives
in settings and reaches the SDK client directly, and nothing here has access to it.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..persistence import Database, record_llm_call
from .base import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    ProviderError,
    sanitize_provider_message,
)

#: Prompts are large but bounded -- one block, one issue, the rules. A cap exists anyway,
#: because an audit row is not the right place to discover that some workbook produced a
#: megabyte of context, and a truncated record that says so beats a database that grows
#: without a ceiling.
MAX_RECORDED_CHARACTERS = 200_000


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_RECORDED_CHARACTERS:
        return text, False
    return text[:MAX_RECORDED_CHARACTERS], True


class RecordingClient:
    """An `LLMClient` that writes down what it was asked and what came back."""

    def __init__(
        self,
        inner: LLMClient,
        db: Database,
        job_id: str,
        *,
        prompt_versions: dict[str, int] | None = None,
        behaviour: dict[str, Any] | None = None,
        before_call: Callable[[], None] | None = None,
    ) -> None:
        self._inner = inner
        self._db = db
        self._job_id = job_id
        # Charged **before** the call, and per physical invocation. The council supplies
        # this; it commits one unit of the model-call budget and raises if the job has
        # none left. Charging afterwards would let a crash loop start calls against a
        # counter that never moves -- the same argument that puts attempt reservation
        # before the Writer call rather than after it.
        self._before_call = before_call
        # The job's pinned behaviour settings -- batch size, effort, model, adapter
        # version. Recorded on every call so a run's settings are recoverable from the
        # audit trail rather than inferred from whatever the environment said at the time.
        self.behaviour = behaviour or {}
        # Public and mutable: the council pins the versions on its first step, which is
        # after this client is built. A private copy taken at construction would label
        # every call `None` and quietly make the version column useless.
        self.prompt_versions = prompt_versions or {}

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._before_call is not None:
            self._before_call()
        started = time.monotonic()
        try:
            response = self._inner.complete(request)
        except Exception as error:
            self._record(request, started, status=_status_of(error), error=error)
            raise
        self._record(request, started, status=response.status, response=response)
        return response

    def _record(
        self,
        request: LLMRequest,
        started: float,
        *,
        status: str,
        response: LLMResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        system_prompt, system_clipped = _clip(request.system_prompt)
        payload, payload_clipped = _clip(request.user_payload)
        detail: dict[str, Any] = {
            "system_prompt": system_prompt,
            "user_payload": payload,
            "truncated": system_clipped or payload_clipped,
            "prompt_version": self.prompt_versions.get(request.role.value),
            "behaviour": self.behaviour,
            "seed": request.seed,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "usage": response.usage if response else {},
            "output_characters": len(response.text) if response else 0,
        }
        if error is not None:
            detail["error"] = sanitize_provider_message(str(error))
            detail["error_type"] = type(error).__name__

        record_llm_call(
            self._db,
            self._job_id,
            role=request.role.value,
            model=(response.model if response else "") or "",
            status=status,
            prompt_sha256=request.prompt_sha256,
            issue_id=request.issue_id,
            payload=detail,
        )


def _status_of(error: Exception) -> str:
    """The provider's own status where there is one, so the row says *how* it failed.

    A row that only ever says "error" cannot distinguish an outage from a rejected key
    from a refusal, which is exactly the distinction anybody reading the trail is after.
    """
    if isinstance(error, ProviderError) and error.status:
        return error.status
    return type(error).__name__
