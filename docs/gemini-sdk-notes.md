# google-genai SDK notes

Verified by direct introspection of the installed package, not from memory or docs alone.

| | |
| --- | --- |
| Package | `google-genai` **2.17.0** |
| Default model | `gemini-3.6-flash` (override with `GEMINI_MODEL`) |
| Credentials | `GEMINI_API_KEY`, read by `genai.Client()` from the environment |
| Verified on | CPython 3.12.13, x86_64 macOS 13.7.8 |

## The call we make

```python
from google import genai

client = genai.Client()                       # reads GEMINI_API_KEY

interaction = client.interactions.create(
    model=settings.gemini_model,
    system_instruction=system_prompt,          # top-level, NOT part of `input`
    input=user_payload,
    response_format={
        "type": "text",
        "mime_type": "application/json",
        "schema": WriterResponse.model_json_schema(),
    },
    generation_config={"seed": 7, "thinking_level": "medium"},
)

if interaction.status != "completed":
    raise ProviderError(interaction.status, interaction.errors)

result = WriterResponse.model_validate_json(interaction.output_text)
```

## What it is *not*

Every one of these is a plausible-looking guess that would fail:

- Not `client.models.generate_content(...)` — the surface is `client.interactions.create(...)`.
- Not `types.GenerateContentConfig(response_schema=...)`.
- Not `contents=` — the parameter is `input=`.
- Not `response.parsed` — the response carries `output_text`, which we validate ourselves.
- The Pydantic **class** is not passed directly. Pass `Model.model_json_schema()`.

## Details that bite

**`system_instruction` is a top-level parameter.** This was the open question from planning, and it is
the reason the four agents can each have a genuinely separate system prompt without any shared
conversation object.

**`schema` vs `schema_`.** The Python field on `TextResponseFormat` is `schema_` (trailing underscore,
since `schema` collides with a Pydantic method) but its wire alias is `schema`. Both keys are accepted
when passing a plain dict because the model has `populate_by_name`. We use `"schema"` — the wire form.

**`create()` is `create(request=None, *, api_version, extra_headers, extra_query, extra_body, timeout,
**body)`.** All request fields arrive through `**body` and are validated against
`CreateModelInteractionParamsNonStreaming`, whose fields are: `model`, `input`, `stream`, `store`,
`background`, `system_instruction`, `tools`, `response_modalities`, `response_mime_type`,
`previous_interaction_id`, `service_tier`, `webhook_config`, `response_format`, `environment`,
`generation_config`, `safety_settings`, `labels`.

**Status must be checked before reading `output_text`.** `InteractionStatus` is one of
`in_progress`, `requires_action`, `completed`, `failed`, `cancelled`, `incomplete`, `budget_exceeded`,
`queued`. Only `completed` may be trusted. Reading `output_text` unconditionally is the Gemini
equivalent of ignoring a refusal — `incomplete` in particular yields truncated JSON that fails
validation with a confusing parse error rather than the real cause.

**`generation_config` gives us determinism levers.** There is no `temperature` here; instead:
`seed` (reproducibility — we set it), `thinking_level` (`minimal`/`low`/`medium`/`high`),
`max_output_tokens`, `stop_sequences`, `tool_choice`. Determinism otherwise comes from
schema-constrained output.

**`previous_interaction_id` exists and we deliberately never use it.** Server-side conversation
threading is exactly what the council's context-isolation guarantee forbids. Every agent call is
constructed from database rows; there is no message list that could carry reasoning forward.

**Token accounting** is on `interaction.usage`: `total_input_tokens`, `total_output_tokens`,
`total_thought_tokens`, `total_tokens`, plus per-modality breakdowns.

## Install constraint

`google-genai` → `google-auth` → `cryptography`. `cryptography` 50.x publishes no x86_64 macOS cp312
wheel, so an unpinned resolve tries to build Rust from source and fails without a toolchain.
`pyproject.toml` pins `cryptography<50` (resolves to 48.0.1) as a **build constraint, not an import**.

## Still unverified — first live call will confirm

`input` accepts a plain string in the published example, and the SDK also models structured turns
(`Turn{role, content}`). `InteractionsInput` is an opaque alias that does not resolve to an
introspectable union, so the exact accepted shapes cannot be pinned down offline. The provider sends a
plain string; if the API rejects it, the fix is local to `llm/provider.py`.
