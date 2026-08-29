#!/usr/bin/env python3
"""Fail if a built wheel omits runtime content or resurrects retired providers."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


REQUIRED_MEMBERS = {
    "oatutor_council/prompts/initial_auditor.v6.md",
    "oatutor_council/prompts/writer.v3.md",
    "oatutor_council/prompts/known_issue_reviewer.v3.md",
    "oatutor_council/prompts/independent_reviewer.v5.md",
    "oatutor_council/prompts/_shared/curation_rules.v1.md",
    "oatutor_council/prompts/_shared/untrusted_data.md",
}
FORBIDDEN_MEMBERS = {
    # Deleted during the Gemini-to-CLI migration. A stale local `build/` directory once
    # put it back into a wheel even though it no longer existed under `src/`.
    "oatutor_council/llm/provider.py",
}
FORBIDDEN_REQUIREMENTS = ("google-genai", "anthropic")


def verify(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = sorted(REQUIRED_MEMBERS - names)
        forbidden = sorted(FORBIDDEN_MEMBERS & names)
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        metadata = "\n".join(
            archive.read(name).decode("utf-8", errors="replace")
            for name in metadata_names
        ).lower()

    bad_requirements = [name for name in FORBIDDEN_REQUIREMENTS if name in metadata]
    failures = []
    if missing:
        failures.append(f"missing required package data: {', '.join(missing)}")
    if forbidden:
        failures.append(f"contains retired modules: {', '.join(forbidden)}")
    if bad_requirements:
        failures.append(f"contains retired dependencies: {', '.join(bad_requirements)}")
    if failures:
        raise SystemExit(f"{path}: " + "; ".join(failures))
    print(f"wheel contents OK: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    verify(args.wheel)


if __name__ == "__main__":
    main()
