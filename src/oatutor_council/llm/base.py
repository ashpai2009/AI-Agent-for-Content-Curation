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
import json
import random
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def canonical_schema_json(schema: dict[str, Any]) -> str:
    """The exact stable JSON bytes passed to the CLI and included in the call hash."""
    return json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


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


class ProviderUsageLimited(ProviderError):
    """The account's allowance is spent -- a plan limit, not a burst.

    The distinction is the trap, and it survived the change of provider. A burst limit says
    *slow down* and comes back in seconds; an allowance says *this account is finished for
    now* and comes back when a window rolls over. Retrying the first is patience; retrying
    the second spends real time to arrive at the same wall.

    Learned from a live pilot against the previous provider: it burned four and a half
    minutes on twelve calls, each retried four times, against a limit that had already been
    reached twenty calls earlier. The lesson is provider-independent, which is why this
    class is named for the condition rather than for whoever reported it -- a Claude Pro
    subscription has session and weekly allowances that behave exactly the same way.

    Not `ProviderConfigurationError`: nothing about the settings is wrong and the job **is**
    worth resuming -- just not now. `resets_at` carries the provider's own reset time when
    it supplies one, and stays `None` when it does not. **Never guessed**: a job that wakes
    on an invented timestamp spends a call to rediscover it is still limited.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str = "usage_limited",
        resets_at: str | None = None,
    ) -> None:
        super().__init__(message, status=status, retryable=False)
        self.resets_at = resets_at


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


class ProviderOutputTooLarge(ProviderError):
    """The provider produced more output than this process will hold.

    **Never retried.** The same prompt against the same model produces the same runaway
    output, so a retry spends a subscription allowance to arrive at the same wall. It is
    also not a refusal and not an outage: something about the request made the model talk
    without stopping, and that is worth failing loudly about rather than absorbing.
    """

    def __init__(self, message: str, *, status: str = "output_too_large") -> None:
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


#: Anything shaped like a credential. This service holds no API key -- the CLI
#: authenticates against the user's subscription through the keychain -- but a redactor
#: that only covers the secrets we *expect* to see is a redactor that fails on the one that
#: turns up. Covers Anthropic keys and OAuth tokens, bearer headers, and any `key=` /
#: `token=` query parameter, whoever emitted it.
_KEY_PATTERN = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{10,})"
    r"|(AIza[0-9A-Za-z_\-]{10,})"
    r"|((?i:bearer)\s+[A-Za-z0-9._\-]{10,})"
    r"|((?i:key|api[_-]?key|token|auth)=)[^\s&\"']+"
)

#: How much of a provider message is worth keeping. A provider error can quote the request
#: body back at you, and the request body is a curator's workbook.
MAX_PROVIDER_MESSAGE_CHARACTERS = 400

#: How much of the *end* survives truncation. Diagnostics that matter -- a reset time, a
#: quota period, the actual cause after a wrapper's preamble -- live at the end at least as
#: often as at the start.
MESSAGE_TAIL_CHARACTERS = 120


def sanitize_provider_message(message: str) -> str:
    """Make a provider error safe to persist, log, and serve.

    Two separate hazards, and both are real. The message may contain the API key, because
    the SDK sometimes includes the request URL. It may also contain the payload, which is
    workbook content -- so an error string that gets logged is a copy of a curator's
    material sitting in a log aggregator nobody scoped for it. Redact the first, truncate
    the second, and keep enough to diagnose the failure.
    """
    redacted = _KEY_PATTERN.sub(_redact, message)
    collapsed = " ".join(redacted.split())
    if len(collapsed) <= MAX_PROVIDER_MESSAGE_CHARACTERS:
        return collapsed

    # Keep both ends. A live pilot's quota error was cut at `"limit: 20, model:…"` and the
    # part that got dropped was exactly the part naming the quota *period* -- the one thing
    # needed to decide whether the job could be resumed in a minute or a day. The bound is
    # the point and stays; which end is discarded is what changes.
    head = MAX_PROVIDER_MESSAGE_CHARACTERS - MESSAGE_TAIL_CHARACTERS
    return f"{collapsed[:head]}… [truncated] …{collapsed[-MESSAGE_TAIL_CHARACTERS:]}"


def _redact(match: re.Match[str]) -> str:
    """Keep the label, drop the secret, so a redacted message still says what was there."""
    label = match.group(5)  # the `key=` / `token=` prefix, when that is what matched
    return f"{label}[redacted]" if label else "[redacted]"


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
    #: Audit metadata retained for provider-neutral callers. The Claude CLI adapter has
    #: no seed flag and does not transmit this value, so it must not be described as a
    #: reproducibility control for production calls.
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
        digest.update(b"\x1f")
        digest.update(canonical_schema_json(self.schema).encode())
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


def retry_call(
    call: Callable[[], LLMResponse],
    *,
    attempts: int,
    backoff_ceiling: float,
    sleep: Callable[[float], None],
) -> LLMResponse:
    """Retry only what retrying can fix.

    An explicit loop rather than a decorator, and provider-independent on purpose: the
    policy makes three decisions a decorator cannot -- whether *this* failure is transient
    at all, how long the provider asked us to wait, and when a requested delay outlasts our
    patience. A decorator that retries an exception class retries a rejected credential
    exactly as eagerly as a transport blip.

    `sleep` is injected so the policy can be tested for what it *decides* without a suite
    that actually waits.
    """
    attempts = max(1, attempts)
    last: ProviderError | None = None

    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ProviderError as error:
            last = error
            if not error.retryable or attempt == attempts:
                raise
            sleep(backoff_delay(attempt, error.retry_after, ceiling=backoff_ceiling))

    raise last or ProviderError("the provider was never called")


def backoff_delay(attempt: int, retry_after: float | None, *, ceiling: float) -> float:
    """Exponential with jitter, or the provider's own delay where it gave a longer one.

    Capped either way: a provider asking for a five-minute wait during a job with a
    deadline is a failure to report, not a wait to sit through.
    """
    jittered = min(ceiling, 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
    if retry_after is None:
        return jittered
    return min(max(retry_after, jittered), ceiling)


class RetryingClient:
    """Retries an inner client's transient failures. **Deliberately the outermost layer.**

    Retrying used to live inside the provider, where it was invisible: one logical call
    could start four `claude` processes, and only the last one was recorded and only one
    was charged against the job's model-call budget. A run that spent four calls on an
    outage was then indistinguishable in the audit trail from one that spent one, and the
    budget bounded a quarter of what it claimed to.

    So the order is fixed and it is the whole point: **retry wraps recording, recording
    wraps the provider, and the provider starts exactly one process.** Every physical
    invocation passes through the recorder on its own, gets its own `llm_calls` row, and
    is charged before it starts.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        attempts: int,
        backoff_ceiling: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._backoff_ceiling = backoff_ceiling
        self._sleep = sleep

    def complete(self, request: LLMRequest) -> LLMResponse:
        return retry_call(
            lambda: self._inner.complete(request),
            attempts=self._attempts,
            backoff_ceiling=self._backoff_ceiling,
            sleep=self._sleep,
        )

    # The council pins prompt versions and behaviour settings after the client is built,
    # and it holds the outermost wrapper. Delegating rather than shadowing keeps one copy
    # of each: a second copy here would be the one that is set and the recorder's would be
    # the one that is written, which is how a version column quietly becomes all `None`.

    @property
    def prompt_versions(self) -> dict[str, int]:
        return getattr(self._inner, "prompt_versions", {})

    @prompt_versions.setter
    def prompt_versions(self, value: dict[str, int]) -> None:
        setattr(self._inner, "prompt_versions", value)

    @property
    def behaviour(self) -> dict[str, Any]:
        return getattr(self._inner, "behaviour", {})

    @behaviour.setter
    def behaviour(self, value: dict[str, Any]) -> None:
        setattr(self._inner, "behaviour", value)


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
