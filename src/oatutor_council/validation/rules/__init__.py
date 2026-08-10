"""The rule engine.

A package rather than the single `rules.py` the original sketch called for: roughly forty
rules in one module would be unmaintainable, and grouping them by subject means a change
to multiple-choice handling touches one file. The import surface is identical.

Importing this package registers every rule as a side effect, which is why the module
imports below look unused. `REGISTRY` is populated by decorator at import time, so a rule
module that is never imported is a rule that silently never runs.
"""

from __future__ import annotations

from .registry import (  # noqa: F401
    REGISTRY,
    Rule,
    RuleContext,
    describe_rules,
    finding,
    rule,
    run_rules,
)

# Registration side effects. Order is irrelevant -- findings are sorted by location --
# but every module must be listed or its rules do not exist.
from . import appearance as appearance  # noqa: E402,F401
from . import dependencies as dependencies  # noqa: E402,F401
from . import latex as latex  # noqa: E402,F401
from . import mc as mc  # noqa: E402,F401
from . import notation as notation  # noqa: E402,F401
from . import rowtypes as rowtypes  # noqa: E402,F401
from . import structure as structure  # noqa: E402,F401

__all__ = [
    "REGISTRY",
    "Rule",
    "RuleContext",
    "describe_rules",
    "finding",
    "rule",
    "run_rules",
]
