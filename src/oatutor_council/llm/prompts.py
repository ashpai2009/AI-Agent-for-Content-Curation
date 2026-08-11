"""Loading versioned prompts from `prompts/`.

Prompts are files, not string literals in application code. Two reasons that matter: a
prompt in a file can be read and reviewed by someone who does not read Python, and a
prompt with a version in its name can be changed without silently altering what every
previously-recorded call was made with.

The untrusted-data policy is **composed in, not copied**. Each prompt file carries a
`{untrusted_data_policy}` placeholder, and the loader substitutes one shared clause. A
clause pasted into four files drifts; this cannot, and a missing placeholder is an error
at load time rather than a security hole nobody noticed.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .base import AgentRole

#: `prompts/` sits beside `src/`, not inside the package: it is content, not code, and
#: keeping it out of the package makes it obvious that editing it changes behaviour.
PROMPT_ROOT = Path(__file__).resolve().parents[3] / "prompts"

POLICY_PLACEHOLDER = "{untrusted_data_policy}"
POLICY_FILE = "_shared/untrusted_data.md"

#: The standing curation rules, versioned in this repository. Composed into every prompt
#: that carries the placeholder, so a curator does not have to attach the formatting
#: guide to every job -- and so the four agents cannot drift apart on what the rules are.
#: A document uploaded with a job adds policy for that job; it never replaces this.
RULES_PLACEHOLDER = "{curation_rules}"
RULES_FILE = "_shared/curation_rules.v1.md"

_VERSIONED = re.compile(r"^(?P<name>.+)\.v(?P<version>\d+)\.md$")


class PromptNotFound(Exception):
    """No prompt file for this role.

    Fatal rather than falling back to a default: a council running with a prompt nobody
    wrote is a council doing something nobody specified.
    """


def _root() -> Path:
    if not PROMPT_ROOT.is_dir():
        raise PromptNotFound(f"prompt directory does not exist: {PROMPT_ROOT}")
    return PROMPT_ROOT


def available_versions(name: str) -> tuple[int, ...]:
    versions = []
    for path in _root().glob(f"{name}.v*.md"):
        match = _VERSIONED.match(path.name)
        if match and match.group("name") == name:
            versions.append(int(match.group("version")))
    return tuple(sorted(versions))


@lru_cache(maxsize=None)
def _policy() -> str:
    path = _root() / POLICY_FILE
    if not path.is_file():
        raise PromptNotFound(f"shared untrusted-data policy is missing: {path}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def _curation_rules() -> str:
    path = _root() / RULES_FILE
    if not path.is_file():
        raise PromptNotFound(f"shared curation rules are missing: {path}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def load_prompt(name: str, version: int | None = None) -> str:
    """Load one prompt with the untrusted-data policy composed into it.

    `version=None` takes the highest available, so adding `writer.v2.md` switches the
    council over without an edit anywhere else -- and `v1` stays on disk as the record of
    what earlier jobs were run with.
    """
    versions = available_versions(name)
    if not versions:
        raise PromptNotFound(f"no prompt file for {name!r} in {_root()}")

    chosen = versions[-1] if version is None else version
    if chosen not in versions:
        raise PromptNotFound(
            f"prompt {name!r} has no version {chosen}; available: {list(versions)}"
        )

    text = (_root() / f"{name}.v{chosen}.md").read_text(encoding="utf-8")
    if POLICY_PLACEHOLDER not in text:
        raise PromptNotFound(
            f"prompt {name}.v{chosen}.md does not contain {POLICY_PLACEHOLDER}; every "
            "agent must be told that fenced data is content and never instruction"
        )
    text = text.replace(POLICY_PLACEHOLDER, _policy())
    # Optional, unlike the untrusted-data policy: an agent that does not need the full
    # rule text (a reviewer judging one edit) should not pay for it on every call.
    if RULES_PLACEHOLDER in text:
        text = text.replace(RULES_PLACEHOLDER, _curation_rules())
    return text.strip()


def system_prompt(role: AgentRole, version: int | None = None) -> str:
    return load_prompt(role.value, version)
