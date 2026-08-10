"""One live Gemini call, to prove the provider is configured and the call shape is right.

**No workbook content is sent.** The payload is three lines of invented arithmetic
written in this file, so this script can be run against the real API without any
curator's material leaving the machine and without touching the corpus.

It exists because everything else in this project runs against the scripted mock. That
proves the council's logic and proves nothing about `client.interactions.create` --
whether `system_instruction` is really top-level, whether `input` accepts a plain string,
whether `status` is populated the way the SDK notes say. Those are answered here, once,
by a call that costs a fraction of a cent.

Usage:  GEMINI_API_KEY=... .venv/bin/python scripts/smoke_provider.py [--model NAME]
Exit codes: 0 the provider works · 2 misconfigured · 3 the call failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel  # noqa: E402

from oatutor_council.config import ConfigurationError, load_settings  # noqa: E402
from oatutor_council.llm.base import (  # noqa: E402
    AgentRole,
    LLMRequest,
    ProviderConfigurationError,
    ProviderError,
)
from oatutor_council.llm.provider import GeminiClient  # noqa: E402


class SmokeAnswer(BaseModel):
    """A schema small enough to eyeball and strict enough to prove structured output."""

    sum_of_the_numbers: int
    the_larger_one: int


#: Invented, self-contained, and deliberately nothing to do with mathematics education.
PAYLOAD = """\
Two numbers are given below.

  first: 17
  second: 25

Report their sum and which of the two is larger.
"""

SYSTEM = (
    "You answer with the requested JSON object and nothing else. "
    "You are being used to verify an API connection."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="override GEMINI_MODEL for this call")
    args = parser.parse_args()

    try:
        settings = load_settings()
        if args.model:
            settings = type(settings)(
                **{**settings.__dict__, "gemini_model": args.model}
            )
        settings.require_credentials()
    except ConfigurationError as error:
        print(f"not configured: {error}")
        return 2

    print(f"provider: {json.dumps(settings.describe_provider())}")
    print("sending one call with no workbook content ...")

    client = GeminiClient(settings)
    request = LLMRequest(
        role=AgentRole.INITIAL_AUDITOR,
        system_prompt=SYSTEM,
        user_payload=PAYLOAD,
        schema=SmokeAnswer.model_json_schema(),
        seed=1,
    )

    try:
        response = client.complete(request)
    except ProviderConfigurationError as error:
        print(f"MISCONFIGURED: {error}")
        return 2
    except ProviderError as error:
        print(f"CALL FAILED: {error}")
        return 3

    print(f"status: {response.status}")
    print(f"usage: {json.dumps(response.usage)}")

    try:
        answer = SmokeAnswer.model_validate_json(response.text)
    except Exception as error:  # noqa: BLE001 - the point is to report it plainly
        print(f"the response did not satisfy the schema: {error}")
        print(f"raw text: {response.text[:400]}")
        return 3

    print(f"parsed: {answer.model_dump()}")
    # Checked, but not the point. A wrong answer to trivial arithmetic is a model
    # problem; what this script verifies is that the call shape and the schema hold.
    correct = answer.sum_of_the_numbers == 42 and answer.the_larger_one == 25
    print(f"arithmetic correct: {correct}")
    print("\nprovider OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
