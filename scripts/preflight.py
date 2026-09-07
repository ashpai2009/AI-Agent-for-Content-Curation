#!/usr/bin/env python3
"""Check the local dependencies required before the council accepts a workbook.

This starts no model session and sends no data. It exists mainly so ``serve.sh`` can show
one concise action instead of burying a configuration failure in a server traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oatutor_council.config import ConfigurationError, load_settings  # noqa: E402
from oatutor_council.llm.claude_cli import require_authentication  # noqa: E402


def main() -> int:
    try:
        require_authentication(load_settings())
    except (ConfigurationError, ValueError) as error:
        print(f"not ready: {error}", file=sys.stderr)
        return 1
    print("ready: Claude subscription login is available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
