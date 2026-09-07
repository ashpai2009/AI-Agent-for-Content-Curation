"""One live call through the Claude Code CLI, with invented arithmetic and no workbook.

The only thing in the repository that exercises the real provider surface. It exists
because everything else about the CLI integration is tested against a fake executable, and
a fake cannot tell you that the envelope shape is right, that `--json-schema` produces
structured output where the parser looks for it, or that the subscription login is what
actually answers.

**It sends no workbook content.** The prompt is two numbers nobody has ever curated. If
this file ever needs a real block to be useful, that is a sign the abstraction leaked.

Exit codes: 0 it works · 2 misconfigured or not logged in · 3 the call failed.

    .venv/bin/python scripts/smoke_claude_cli.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel  # noqa: E402

from oatutor_council.config import ConfigurationError, load_settings  # noqa: E402
from oatutor_council.llm.base import (  # noqa: E402
    AgentRole,
    LLMRequest,
    MalformedResponse,
    ProviderError,
    call_structured,
)
from oatutor_council.llm.claude_cli import (  # noqa: E402
    _STRUCTURED_KEYS,
    ClaudeCLIClient,
    auth_status,
    build_command,
    describe_authentication,
)


class Arithmetic(BaseModel):
    """Invented, checkable, and nothing to do with any curator's material."""

    sum_of_the_numbers: int
    the_larger_one: int


SYSTEM = (
    "You answer arithmetic questions about two numbers. Reply only with the requested "
    "JSON object."
)
PROMPT = "The two numbers are 17 and 25. What is their sum, and which is larger?"


def _describe_envelope(stdout: str) -> None:
    """Report the envelope's shape, so `docs/claude-cli-notes.md` can stop guessing.

    Names and types only, never values. The point is to record where the structured output
    lives and what usage the CLI reports -- and the payload came back from a model that was
    asked about two invented numbers, so there is nothing here worth printing anyway.
    """
    if not stdout.strip():
        print("\nenvelope: nothing was captured")
        return
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"\nenvelope: not JSON ({stdout[:120]!r})")
        return
    if not isinstance(envelope, dict):
        print(f"\nenvelope: a JSON {type(envelope).__name__}, not an object")
        return

    print("\nenvelope observed (record this in docs/claude-cli-notes.md):")
    print(f"  top-level keys: {sorted(envelope)}")

    source = next(
        (key for key in (*_STRUCTURED_KEYS, "result") if envelope.get(key) not in (None, "")),
        None,
    )
    kind = type(envelope.get(source)).__name__ if source else "—"
    print(f"  structured output came from: {source!r} (a {kind})")

    usage = envelope.get("usage")
    if isinstance(usage, dict):
        print(f"  usage fields: {sorted(usage)}")
    else:
        print(f"  usage: absent or not an object ({type(usage).__name__})")

    model_usage = envelope.get("modelUsage") or envelope.get("model_usage")
    if isinstance(model_usage, dict):
        print(f"  exact model ids: {sorted(str(name) for name in model_usage)}")

    for field in ("subtype", "is_error", "model", "duration_ms", "num_turns"):
        if field in envelope:
            value = envelope[field]
            shown = value if isinstance(value, (int, float, bool)) else type(value).__name__
            print(f"  {field}: {shown}")


def main() -> int:
    settings = load_settings()

    described = describe_authentication(auth_status(settings))
    print(f"provider: {json.dumps(settings.describe_provider())}")
    print(f"auth: {json.dumps(described)}")

    try:
        settings.require_credentials()
    except ConfigurationError as error:
        print(f"\nnot ready: {error}", file=sys.stderr)
        return 2

    request = LLMRequest(
        role=AgentRole.INITIAL_AUDITOR,
        system_prompt=SYSTEM,
        user_payload=PROMPT,
        schema=Arithmetic.model_json_schema(),
        job_id="smoke",
    )

    # Printed so what is about to run is visible before it runs. The schema is elided
    # because it is long, not because it is secret.
    shown = [
        "…schema…" if part.startswith("{") and '"properties"' in part else part
        for part in build_command(settings, request)
    ]
    print(f"\ncommand: {' '.join(repr(part) if ' ' in part else part for part in shown)}")
    print("sending one call with no workbook content ...")

    # The envelope is captured on the way past, through the `runner` seam that already
    # exists for the tests. The full client still does the work -- this observes it rather
    # than reimplementing it -- so what is reported below is the shape the real parser
    # actually read, not a second guess at it.
    captured: dict[str, str] = {}

    def capturing_runner(command, *, input, cwd, timeout):
        completed = ClaudeCLIClient._run_process(
            command, input=input, cwd=cwd, timeout=timeout
        )
        captured["stdout"] = completed.stdout
        return completed

    client = ClaudeCLIClient(settings, runner=capturing_runner)

    try:
        answer = call_structured(client, request, Arithmetic)
    except MalformedResponse as error:
        print(f"\nthe CLI returned something the schema rejects: {error}", file=sys.stderr)
        _describe_envelope(captured.get("stdout", ""))
        return 3
    except ProviderError as error:
        print(f"\nthe call failed [{error.status}]: {error}", file=sys.stderr)
        _describe_envelope(captured.get("stdout", ""))
        return 3

    print(f"parsed: {answer.model_dump()}")
    _describe_envelope(captured.get("stdout", ""))
    correct = answer.sum_of_the_numbers == 42 and answer.the_larger_one == 25
    print(f"arithmetic correct: {correct}")
    print("\nprovider OK" if correct else "\nprovider answered, but got the sum wrong")
    return 0 if correct else 3


if __name__ == "__main__":
    raise SystemExit(main())
