"""The Gemini provider.

**Read `docs/gemini-sdk-notes.md` before changing anything here.** The API differs from
every plausible guess: it is `client.interactions.create(...)`, not
`models.generate_content`; the parameter is `input=`, not `contents=`; the schema goes in
`response_format` as a plain JSON schema, not as a Pydantic class; and the response
carries `output_text`, not `parsed`.

Two details are load-bearing:

* **`system_instruction` is a top-level parameter.** That is what lets four agents each
  have a genuinely separate system prompt with no shared conversation object, which is
  the mechanism the whole isolation guarantee rests on.

* **`status` must be checked before `output_text` is read.** It can be `incomplete`,
  which yields truncated JSON that fails schema validation with a confusing parse error
  instead of the real cause -- a response that was cut off, not a model that misbehaved.

`previous_interaction_id` exists in this API and is deliberately never used. Server-side
conversation threading is precisely what context isolation forbids.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Callable

from ..config import Settings
from .base import (
    LLMRequest,
    LLMResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    sanitize_provider_message,
)

#: Signals that the call will fail the same way until the settings change. Matched on the
#: message because the SDK raises a wide, undocumented set of exception types and the
#: alternative -- treating everything as transient -- retries a bad key five times per
#: model call for the length of a job.
_CONFIGURATION_SIGNALS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "permission_denied",
    "unauthenticated",
    "401",
    "403",
    "not found for api version",
    "invalid_argument",
    "is not supported",
    "unknown model",
)

_RATE_LIMIT_SIGNALS = ("429", "resource_exhausted", "rate limit", "quota", "too many requests")

_TIMEOUT_SIGNALS = ("timeout", "timed out", "deadline_exceeded", "deadline exceeded", "504")

_UNAVAILABLE_SIGNALS = (
    "500",
    "502",
    "503",
    "unavailable",
    "internal error",
    "internal server",
    "bad gateway",
    "connection reset",
    "connection aborted",
    "temporarily",
    "overloaded",
)

#: A refusal is content, not infrastructure. Kept apart from an outage because a workbook
#: cell that trips a safety filter trips it on every call, and from a configuration error
#: because the settings are fine -- this one block is the problem.
_REFUSAL_SIGNALS = (
    "safety",
    "blocked",
    "block_reason",
    "prohibited_content",
    "recitation",
    "content filter",
    "harm_category",
)


def _signature(error: Exception) -> str:
    return f"{type(error).__name__} {error}".lower()


#: `retryDelay: "31s"`, `Retry-After: 31`, "retry after 31 seconds". Three spellings
#: because the delay arrives in the message body, in a header the SDK stringifies, or in
#: prose, depending on which layer produced the failure.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[_-]?delay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s?", re.IGNORECASE),
    re.compile(r"retry[- _]?after[\"']?\s*[:=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
)


def retry_after_of(error: Exception) -> float | None:
    """The delay the server asked for, if it asked for one.

    Checked as an attribute first: an SDK that models the header properly is more
    trustworthy than a regular expression over a message. The patterns are the fallback,
    because `google-genai` surfaces most failures as a stringified API error.
    """
    for attribute in ("retry_after", "retry_delay", "retry_after_seconds"):
        value = getattr(error, attribute, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)

    text = str(error)
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    return None


def classify_provider_error(error: Exception) -> ProviderError:
    """Turn whatever the SDK raised into one of the errors the council knows how to handle.

    The order is deliberate. Configuration is checked before the transient classes because
    a 403 is also an HTTP error and retrying it is a loop; a refusal is checked before the
    generic fallback because "blocked" is content rather than an outage. Everything
    unrecognised becomes a retryable `ProviderError` -- the conservative reading, since
    treating an unknown outage as permanent fails jobs that would have succeeded, while
    treating an unknown permanent failure as transient costs a bounded number of retries.
    """
    if isinstance(error, ProviderError):
        return error

    text = _signature(error)
    message = sanitize_provider_message(str(error) or type(error).__name__)
    delay = retry_after_of(error)

    if any(signal in text for signal in _CONFIGURATION_SIGNALS):
        return ProviderConfigurationError(message, status="configuration")
    if any(signal in text for signal in _RATE_LIMIT_SIGNALS):
        return RateLimited(message, retry_after=delay)
    if any(signal in text for signal in _TIMEOUT_SIGNALS):
        return ProviderTimeout(message)
    if any(signal in text for signal in _REFUSAL_SIGNALS):
        return ProviderRefused(message)
    if any(signal in text for signal in _UNAVAILABLE_SIGNALS):
        return ProviderUnavailable(message, retry_after=delay)
    return ProviderError(message, status="unknown", retry_after=delay)


#: Interaction statuses that mean "not finished yet" rather than "went wrong". Worth one
#: more call; the rest are terminal for this interaction.
_TRANSIENT_STATUSES = frozenset({"incomplete", "queued", "in_progress"})


class GeminiClient:
    """A thin, stateless wrapper over `client.interactions.create`.

    Deliberately thin. Everything that could be done here -- validation, retry on
    malformed output, prompt assembly -- is done in `base` and `context` instead, so this
    module stays short enough to check against the SDK notes line by line.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client = client
        # Injected so the retry policy can be tested for what it *decides* -- how many
        # attempts, how long each wait, whether the server's delay was honoured -- without
        # a test suite that actually waits that long.
        self._sleep = sleep
        if client is None:
            settings.require_credentials()

    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai

            # The key is passed explicitly rather than left to the SDK's environment
            # lookup. Settings can be supplied programmatically -- a test, an embedded
            # runner, a deployment that reads its secrets from somewhere other than the
            # process environment -- and a client that silently reads `os.environ`
            # instead would use a different key from the one this service was configured
            # with, or none at all.
            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        """One completion, retrying only what retrying can fix.

        The loop is explicit rather than a `tenacity` decorator because the policy has to
        make three decisions the decorator cannot: whether *this* error is transient at
        all, how long the server asked us to wait, and when to stop honouring a delay that
        would outlast the job's patience. A decorator that retries a class of exception
        would retry a rejected API key exactly as eagerly as a 503.
        """
        attempts = max(1, self._settings.provider_max_attempts)
        last: ProviderError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._call_once(request)
            except ProviderError as error:
                last = error
                if not error.retryable or attempt == attempts:
                    raise
                self._sleep(self._backoff(attempt, error.retry_after))

        raise last or ProviderError("the provider was never called")

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential with jitter, or the server's own delay when it gave one.

        The server's delay wins where it is longer -- it knows about the quota window and
        we do not -- but it is capped, because a provider asking for a five-minute wait
        during a job with a deadline is a failure to report, not a wait to sit through.
        """
        ceiling = self._settings.provider_backoff_ceiling_seconds
        jittered = min(ceiling, 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
        if retry_after is None:
            return jittered
        return min(max(retry_after, jittered), ceiling)

    def _call_once(self, request: LLMRequest) -> LLMResponse:
        generation_config: dict[str, Any] = {"thinking_level": "medium"}
        if request.seed is not None:
            generation_config["seed"] = request.seed

        try:
            interaction = self.client.interactions.create(
                model=self._settings.gemini_model,
                system_instruction=request.system_prompt,
                input=request.user_payload,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    # The wire alias is `schema`; the Python field is `schema_`. A plain
                    # dict accepts either because the model has `populate_by_name`.
                    "schema": request.schema,
                },
                generation_config=generation_config,
                # Nothing needs the interaction to exist server-side after the response
                # comes back. `previous_interaction_id` is the only feature that would use
                # it, and threading interactions is exactly what context isolation
                # forbids -- so retention here would be a copy of curator workbook content
                # held by a third party for a capability this system will never use.
                store=False,
                # A call with no timeout is a worker that can hang for the lifetime of the
                # process holding a lease over a job nobody else may touch.
                timeout=self._settings.provider_timeout_seconds,
            )
        except Exception as error:
            raise classify_provider_error(error) from error

        status = str(getattr(interaction, "status", "unknown"))
        if status != "completed":
            # Never read `output_text` here. An `incomplete` interaction returns
            # truncated JSON, and letting it through surfaces as a schema error that
            # blames the model for a response that was cut off.
            if status == "failed" and _refusal_in(interaction):
                raise ProviderRefused(
                    "Gemini declined to answer for this content", status=status
                )
            raise ProviderError(
                f"Gemini interaction did not complete: status={status!r}",
                status=status,
                retryable=status in _TRANSIENT_STATUSES,
            )

        return LLMResponse(
            text=interaction.output_text or "",
            model=self._settings.gemini_model,
            status=status,
            usage=_usage(interaction),
        )


def _refusal_in(interaction: Any) -> bool:
    """Whether a failed interaction failed because the model declined.

    Read defensively. The SDK's error shape for a filtered response is not part of the
    contract we verified, so this checks for the signals wherever they are and treats
    their absence as an ordinary failure rather than guessing.
    """
    detail = getattr(interaction, "error", None) or getattr(interaction, "incomplete_details", None)
    text = str(detail or "").lower()
    return any(signal in text for signal in _REFUSAL_SIGNALS)


def _usage(interaction: Any) -> dict[str, Any]:
    usage = getattr(interaction, "usage", None)
    if usage is None:
        return {}
    return {
        field: getattr(usage, field, None)
        for field in (
            "total_input_tokens",
            "total_output_tokens",
            "total_thought_tokens",
            "total_tokens",
        )
    }
