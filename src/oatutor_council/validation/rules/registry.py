"""The rule registry.

A rule is a pure function from context to findings. It never edits, never calls a model,
and never decides what to do about what it found -- that separation is what lets the
whole engine run in a test with no credentials and no I/O.

Rules are registered by decorator and carry their own metadata, so the catalogue is
derivable from the code rather than maintained beside it. `describe_rules()` exists for
exactly that: a report listing what was checked, generated from what actually ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from ...models import (
    FindingScope,
    IssueCategory,
    ParsedWorkbook,
    ProblemBlock,
    Severity,
    ValidationFinding,
)


@dataclass(frozen=True)
class RuleContext:
    """What a rule is allowed to see.

    A block-scoped rule still gets the whole workbook, because several checks are only
    decidable against workbook-wide convention -- a scaffold namespace is a defect or a
    house style depending on what the other thirty blocks do.
    """

    parsed: ParsedWorkbook
    block: ProblemBlock | None = None

    @property
    def conventions(self):
        return self.parsed.conventions


RuleCheck = Callable[[RuleContext], Iterable[ValidationFinding]]


@dataclass(frozen=True)
class Rule:
    code: str
    severity: Severity
    category: IssueCategory
    scope: FindingScope
    description: str
    check: RuleCheck
    per_block: bool
    repairable: bool = True


REGISTRY: dict[str, Rule] = {}


def rule(
    code: str,
    *,
    severity: Severity,
    category: IssueCategory,
    scope: FindingScope = FindingScope.CELL,
    description: str,
    per_block: bool = True,
    repairable: bool = True,
) -> Callable[[RuleCheck], RuleCheck]:
    """Register a rule. Duplicate codes are a programming error, not a warning.

    Two rules sharing a code would make findings ambiguous to the fingerprint dedup that
    stops the validation loop cycling, so the collision is refused at import time.
    """

    def decorate(check: RuleCheck) -> RuleCheck:
        if code in REGISTRY:
            raise ValueError(f"rule code {code!r} is already registered")
        REGISTRY[code] = Rule(
            code=code,
            severity=severity,
            category=category,
            scope=scope,
            description=description,
            check=check,
            per_block=per_block,
            repairable=repairable,
        )
        return check

    return decorate


def finding(
    context: RuleContext,
    code: str,
    message: str,
    *,
    row: int | None = None,
    column: int | None = None,
    column_key=None,
    severity: Severity | None = None,
    scope: FindingScope | None = None,
    repairable: bool | None = None,
    **detail,
) -> ValidationFinding:
    """Build a finding, inheriting severity and scope from the registered rule.

    Severity is overridable for the handful of rules whose seriousness depends on
    context -- a scaffold namespace deviation is an error in a workbook that mixes
    conventions and a warning in one that consistently uses another.
    """
    registered = REGISTRY.get(code)
    return ValidationFinding(
        code=code,
        severity=severity or (registered.severity if registered else Severity.ERROR),
        scope=scope or (registered.scope if registered else FindingScope.CELL),
        message=message,
        row=row,
        column=column,
        column_key=column_key,
        block_id=context.block.block_id if context.block else None,
        problem_name=context.block.problem_name if context.block else None,
        repairable=(
            repairable
            if repairable is not None
            else (registered.repairable if registered else True)
        ),
        detail=detail,
    )


def run_rules(
    parsed: ParsedWorkbook, *, only: Iterable[str] | None = None
) -> tuple[ValidationFinding, ...]:
    """Run every registered rule and return the findings in document order.

    Findings are sorted by location so a report reads down the workbook rather than by
    whichever rule happened to be registered first -- the order rules import in is an
    implementation detail and must not be visible to a curator.
    """
    wanted = set(only) if only is not None else None
    findings: list[ValidationFinding] = []

    for code, registered in REGISTRY.items():
        if wanted is not None and code not in wanted:
            continue
        if registered.per_block:
            for block in parsed.blocks:
                findings.extend(registered.check(RuleContext(parsed, block)))
        else:
            findings.extend(registered.check(RuleContext(parsed, None)))

    return tuple(
        sorted(findings, key=lambda f: (f.row or 0, f.column or 0, f.code))
    )


def describe_rules() -> tuple[Rule, ...]:
    return tuple(sorted(REGISTRY.values(), key=lambda r: r.code))
