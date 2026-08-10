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

from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ..config import Settings
from .base import (
    LLMRequest,
    LLMResponse,
    ProviderConfigurationError,
    ProviderError,
)


class RateLimited(ProviderError):
    """A 429. Retried with exponential backoff and jitter."""


def _is_rate_limit(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text


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


def _is_configuration_error(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(signal in text for signal in _CONFIGURATION_SIGNALS)


class GeminiClient:
    """A thin, stateless wrapper over `client.interactions.create`.

    Deliberately thin. Everything that could be done here -- validation, retry on
    malformed output, prompt assembly -- is done in `base` and `context` instead, so this
    module stays short enough to check against the SDK notes line by line.
    """

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        self._settings = settings
        self._client = client
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

    @retry(
        retry=retry_if_exception_type(RateLimited),
        wait=wait_random_exponential(multiplier=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def complete(self, request: LLMRequest) -> LLMResponse:
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
            )
        except Exception as error:
            if _is_rate_limit(error):
                raise RateLimited(str(error), status="rate_limited") from error
            if _is_configuration_error(error):
                raise ProviderConfigurationError(
                    str(error), status="configuration"
                ) from error
            raise ProviderError(str(error)) from error

        status = getattr(interaction, "status", "unknown")
        if status != "completed":
            # Never read `output_text` here. An `incomplete` interaction returns
            # truncated JSON, and letting it through surfaces as a schema error that
            # blames the model for a response that was cut off.
            raise ProviderError(
                f"Gemini interaction did not complete: status={status!r}",
                status=str(status),
                retryable=status in {"incomplete", "queued", "in_progress"},
            )

        return LLMResponse(
            text=interaction.output_text or "",
            model=self._settings.gemini_model,
            status=str(status),
            usage=_usage(interaction),
        )


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
