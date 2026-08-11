"""The model interface. Stateless by construction.

**There is no `Conversation` object anywhere in this codebase, and that is the point.**
Context isolation between the four agents is not a discipline anyone has to remember; it
is a consequence of there being no message list that could carry reasoning forward. Every
call is assembled from durable rows and a system prompt, and nothing survives it.

The provider is asked for one thing: given a system prompt, a rendered payload, and a
JSON schema, return text. Validating that text against a Pydantic model, deciding whether
a malformed response is worth retrying, and recording what was sent all happen here, so
the provider module stays small enough to check against the SDK notes line by line.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class AgentRole(StrEnum):
    """Who is being asked. Each role has its own system prompt and its own context."""

    INITIAL_AUDITOR = "initial_auditor"
    WRITER = "writer"
    KNOWN_ISSUE_REVIEWER = "known_issue_reviewer"
    INDEPENDENT_REVIEWER = "independent_reviewer"


class ProviderError(Exception):
    """The provider did not return a usable completion.

    Distinct from a malformed response: this means the call itself failed or the
    interaction never reached `completed`, which is infrastructure rather than content
    and is therefore eligible for an attempt refund.

    `retry_after` carries a server-supplied delay in seconds when there was one. Honouring
    it matters: backing off less than the provider asked for is how a rate-limited client
    converts one 429 into a sustained stream of them.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str = "",
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class RateLimited(ProviderError):
    """A 429. Retried with exponential backoff, or with the server's own delay."""

    def __init__(
        self, message: str, *, status: str = "rate_limited", retry_after: float | None = None
    ) -> None:
        super().__init__(message, status=status, retryable=True, retry_after=retry_after)


class ProviderTimeout(ProviderError):
    """The call did not return within the per-call timeout.

    Retryable, but bounded like everything else: a model that times out three times is a
    model that will time out on the fourth call too, and the job is better off failing
    with an explanation than spending its budget discovering that.
    """

    def __init__(self, message: str, *, status: str = "timeout") -> None:
        super().__init__(message, status=status, retryable=True)


class ProviderUnavailable(ProviderError):
    """A 5xx, or a transport failure that never reached the model. Retryable."""

    def __init__(
        self, message: str, *, status: str = "unavailable", retry_after: float | None = None
    ) -> None:
        super().__init__(message, status=status, retryable=True, retry_after=retry_after)


class ProviderRefused(ProviderError):
    """The model declined to answer -- a safety block, a recitation stop, a filtered prompt.

    **Never retried**, and deliberately not treated as an outage. The same workbook cell
    will trip the same filter on every call, so retrying spends the budget to arrive at
    the same refusal. It is content, not infrastructure: the right response is to hand
    that block to a person, which is what the caller does with it.
    """

    def __init__(self, message: str, *, status: str = "refused") -> None:
        super().__init__(message, status=status, retryable=False)


class ProviderConfigurationError(ProviderError):
    """The provider cannot be called with the settings this service has.

    A missing or rejected key, an unknown model, a malformed request. Kept apart from an
    outage because the two need opposite handling: an outage is transient and worth
    retrying, while a configuration error will fail identically for as long as the
    settings say what they say. Retrying one is patience; retrying the other is a loop
    that spends a job's budget to reach the same place.

    Never retryable, and it fails the job as `FAILED(CONFIG)`, which is non-resumable
    until somebody changes the settings.
    """

    def __init__(self, message: str, *, status: str = "") -> None:
        super().__init__(message, status=status, retryable=False)


#: Anything shaped like a Google API key. The SDK echoes the failing request URL in some
#: error messages, and that URL can carry `?key=...`.
_KEY_PATTERN = re.compile(r"(AIza[0-9A-Za-z_\-]{10,})|((?i:key|api[_-]?key)=)[^\s&\"']+")

#: How much of a provider message is worth keeping. A provider error can quote the request
#: body back at you, and the request body is a curator's workbook.
MAX_PROVIDER_MESSAGE_CHARACTERS = 400


def sanitize_provider_message(message: str) -> str:
    """Make a provider error safe to persist, log, and serve.

    Two separate hazards, and both are real. The message may contain the API key, because
    the SDK sometimes includes the request URL. It may also contain the payload, which is
    workbook content -- so an error string that gets logged is a copy of a curator's
    material sitting in a log aggregator nobody scoped for it. Redact the first, truncate
    the second, and keep enough to diagnose the failure.
    """
    redacted = _KEY_PATTERN.sub(lambda m: (m.group(2) or "") + "[redacted]", message)
    collapsed = " ".join(redacted.split())
    if len(collapsed) > MAX_PROVIDER_MESSAGE_CHARACTERS:
        return collapsed[:MAX_PROVIDER_MESSAGE_CHARACTERS] + "… [truncated]"
    return collapsed


class MalformedResponse(Exception):
    """The model returned text that does not satisfy the schema.

    Never salvaged out of surrounding prose. A response that has to be excavated is a
    response whose structure the model did not commit to, and accepting it would put
    guessed content into a curator's workbook.
    """


@dataclass(frozen=True)
class LLMRequest:
    role: AgentRole
    system_prompt: str
    user_payload: str
    schema: dict[str, Any]
    #: Reproducibility lever. There is no temperature in this API; determinism comes
    #: from a fixed seed plus schema-constrained output.
    seed: int | None = None
    #: Correlates the call with the job and issue it belongs to, for the audit trail.
    job_id: str = ""
    issue_id: str | None = None

    @property
    def prompt_sha256(self) -> str:
        """Hash of exactly what was sent.

        Isolation tests assert over persisted prompts rather than mock memory, so the
        thing that was actually transmitted has to be identifiable after the fact.
        """
        digest = hashlib.sha256()
        digest.update(self.system_prompt.encode())
        digest.update(b"\x1f")
        digest.update(self.user_payload.encode())
        return digest.hexdigest()


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str = ""
    status: str = "completed"
    usage: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    """One call in, one completion out. No state, no history, no threading."""

    def complete(self, request: LLMRequest) -> LLMResponse: ...


def call_structured(
    client: LLMClient,
    request: LLMRequest,
    response_model: type[T],
    *,
    retries: int = 1,
) -> T:
    """Make the call and validate the result against `response_model`.

    Retried **once** on malformed output and no further. Repeated schema failure is a
    prompt or schema problem, and looping on it burns a curator's budget discovering the
    same thing several times. A provider error is not retried here at all: it is
    infrastructure, and the attempt-refund path upstream is the right place to handle it.
    """
    last: Exception | None = None
    for _ in range(retries + 1):
        response = client.complete(request)
        if response.status != "completed":
            raise ProviderError(
                f"interaction status was {response.status!r}, not 'completed'",
                status=response.status,
            )
        try:
            return response_model.model_validate_json(response.text)
        except ValidationError as error:
            last = error
    raise MalformedResponse(
        f"{request.role} returned output that does not satisfy "
        f"{response_model.__name__} after {retries + 1} attempt(s): {last}"
    )
