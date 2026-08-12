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

    try:
        answer = call_structured(ClaudeCLIClient(settings), request, Arithmetic)
    except MalformedResponse as error:
        print(f"\nthe CLI returned something the schema rejects: {error}", file=sys.stderr)
        return 3
    except ProviderError as error:
        print(f"\nthe call failed [{error.status}]: {error}", file=sys.stderr)
        return 3

    print(f"parsed: {answer.model_dump()}")
    correct = answer.sum_of_the_numbers == 42 and answer.the_larger_one == 25
    print(f"arithmetic correct: {correct}")
    print("\nprovider OK" if correct else "\nprovider answered, but got the sum wrong")
    return 0 if correct else 3


if __name__ == "__main__":
    raise SystemExit(main())
