"""Turning workbook content into the fenced data sections an agent sees.

Everything here produces `DataSection`s, never raw strings spliced into a prompt. That is
what keeps the untrusted-data guarantee in one place: an agent module cannot accidentally
interpolate a cell, because it never holds one as text destined for a prompt.

Blocks are rendered as a table with real row numbers down the side. The row numbers are
the whole point -- a Writer's patch has to name the spreadsheet row, and a model given
1-based positions within the block will confidently name the wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..llm.context import DataSection
from ..models import (
    ColumnKey,
    Issue,
    ProblemBlock,
    ValidationFinding,
    WorkbookConventions,
)
from .isolation import assert_no_private_fields

#: Every fixed A-P column an agent is allowed to name. Hiding a column while leaving it in
#: the response schema asks the Writer to invent its exact `before` value and leaves both
#: auditors unable to inspect it. `images` is sent only as the cell's text/reference; the
#: service does not fetch, render, OCR, or otherwise inspect image content.
DISPLAY_COLUMNS = (
    ColumnKey.PROBLEM_NAME,
    ColumnKey.ROW_TYPE,
    ColumnKey.TITLE,
    ColumnKey.BODY_TEXT,
    ColumnKey.ANSWER,
    ColumnKey.ANSWER_TYPE,
    ColumnKey.HINT_ID,
    ColumnKey.DEPENDENCY,
    ColumnKey.MC_CHOICES,
    ColumnKey.IMAGES,
    ColumnKey.PARENT,
    ColumnKey.OER_SRC,
    ColumnKey.OPENSTAX_KC,
    ColumnKey.KC,
    ColumnKey.TAXONOMY,
    ColumnKey.LICENSE,
)


def render_block(block: ProblemBlock) -> str:
    lines = ["row | " + " | ".join(key.value for key in DISPLAY_COLUMNS)]
    for row in block.rows:
        cells = [row.get(key).replace("\n", "\\n") for key in DISPLAY_COLUMNS]
        lines.append(f"{row.row} | " + " | ".join(cells))
    return "\n".join(lines)


def render_conventions(conventions: WorkbookConventions) -> str:
    """What this workbook consistently does, so an agent does not treat it as a defect.

    Six of eleven real workbooks use the `h` scaffold namespace where the written rules
    say `s`. An agent not told that will report every scaffold in the file.
    """
    return "\n".join(
        [
            f"notation: {conventions.notation.value}",
            f"dependency numbering: {conventions.dependency_convention.value}",
            f"scaffold id namespace: {conventions.dominant_scaffold_namespace or 'none'}"
            f" (consistent: {conventions.scaffold_namespace_is_consistent})",
            f"problem name stems: {', '.join(conventions.naming_stems) or 'none'}",
        ]
    )


def render_findings(findings: Sequence[ValidationFinding]) -> str:
    if not findings:
        return "none"
    return "\n".join(
        f"row {f.row or '-'}: [{f.severity.value}] {f.code} — {f.message}"
        for f in findings
    )


def render_issue(issue: Issue) -> str:
    """The issue as a reviewer and the Writer both see it.

    Note what is absent: no rationale, no confidence, nothing about previous attempts
    beyond the count. The claim and its location, and nothing that argues for it.
    """
    cells = ", ".join(f"row {row} column {column}" for row, column in issue.cells)
    return "\n".join(
        [
            f"severity: {issue.severity.value}",
            f"category: {issue.category.value}",
            f"structural: {issue.is_structural}",
            f"cells: {cells or 'not specified'}",
            f"claim: {issue.description}",
            f"expected: {issue.expected or 'not stated'}",
            f"rules cited: {', '.join(issue.rule_codes) or 'none'}",
        ]
    )


def render_block_diff(original: ProblemBlock, current: ProblemBlock) -> str:
    """The whole block, source to current -- not just this issue's edits.

    A reviewer judging one edit in isolation cannot see that a sibling edit broke it: a
    repaired answer that no longer matches its choice list looks perfect on its own line.
    Showing the complete block is what makes that visible.
    """
    before = {row.row: row for row in original.rows}
    after = {row.row: row for row in current.rows}

    lines: list[str] = []
    for row_number in sorted(set(before) | set(after)):
        old, new = before.get(row_number), after.get(row_number)
        if old is None:
            lines.append(f"row {row_number}: ADDED")
            continue
        if new is None:
            lines.append(f"row {row_number}: REMOVED")
            continue
        for key in DISPLAY_COLUMNS:
            if old.get(key) != new.get(key):
                lines.append(
                    f"row {row_number} {key.value}: {old.get(key)!r} -> {new.get(key)!r}"
                )
    return "\n".join(lines) if lines else "no changes"


def render_candidate_edits(edits) -> str:
    """The public patch artefact: locations and values, never Writer reasoning."""
    if not edits:
        return "none"
    return "\n".join(
        f"row {edit.row} {edit.column_key.value if edit.column_key else edit.column}: "
        f"{edit.before!r} -> {edit.after!r}"
        for edit in edits
    )


# --------------------------------------------------------------------------------------
# Reviewer context
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerContext:
    """Everything a reviewer is given, and nothing else.

    Checked at import time by `assert_no_private_fields`: if a field is ever added whose
    type closure can reach `PrivateText` or a `PrivateModel`, this module fails to import
    rather than shipping a reviewer that can read the Writer's argument.
    """

    issue_summary: str
    original_block: str
    current_block: str
    block_diff: str
    conventions: str
    deterministic_findings: str
    candidate_edits: str = ""
    rules_reminder: str = ""
    #: Policy the curator supplied. A reviewer has to judge a repair against the rules it
    #: was made under, so these travel here -- unlike the errata claims, which are
    #: hypotheses for the auditor and would invite a reviewer to re-litigate them.
    curator_rules: str = ""

    def sections(self) -> tuple[DataSection, ...]:
        sections = [
            DataSection("The issue under review", self.issue_summary),
            DataSection("The block as originally submitted", self.original_block),
            DataSection("The block as it stands now", self.current_block),
            DataSection("What changed, across the whole block", self.block_diff),
            DataSection("Conventions this workbook follows", self.conventions),
            DataSection("Deterministic findings still open", self.deterministic_findings),
        ]
        if self.candidate_edits.strip():
            sections.append(
                DataSection(
                    "Candidate edits being reviewed (artifact only)",
                    self.candidate_edits,
                )
            )
        if self.curator_rules.strip():
            sections.append(
                DataSection("Curation rules the curator supplied", self.curator_rules)
            )
        return tuple(sections)


assert_no_private_fields(ReviewerContext)


# --------------------------------------------------------------------------------------
# Adjudication context
# --------------------------------------------------------------------------------------


def render_claim(
    *,
    cells: Sequence[tuple[int, int]],
    category: str,
    problem: str,
    expected: str = "",
) -> str:
    """One audit's published finding, in the form both sides of a dispute are shown in.

    Rendered identically for both claims on purpose. The adjudicator is told which audit
    raised which, but nothing about the layout should suggest that one of them is the
    accusation and the other the check -- the question is which reading of the block is
    right, not whether the second agent agrees with the first.
    """
    located = ", ".join(f"row {row} column {column}" for row, column in cells)
    lines = [
        f"cells: {located or 'not specified'}",
        f"category: {category}",
        f"says: {problem}",
    ]
    if expected:
        lines.append(f"expected: {expected}")
    return "\n".join(lines)


def render_claims(claims: Sequence[str]) -> str:
    if not claims:
        return "nothing about these cells"
    return "\n\n".join(f"[{index + 1}] {claim}" for index, claim in enumerate(claims))


@dataclass(frozen=True)
class AdjudicationContext:
    """Everything the adjudicator is given, and nothing else.

    Deliberately **not** claim-blind, which is the one place this pipeline shows an agent
    another agent's conclusion. That is the whole job: deciding between two readings of a
    block is not something a blind observer can do, and the blind check that came before
    it has already been made and has already failed to settle the question.

    The anchoring risk is real and is paid for elsewhere -- the prompt requires the
    adjudicator to re-derive the mathematics itself and to state the check it ran, and
    `undecided` is an available answer precisely so that agreeing with whichever claim
    sounds more confident is never the cheapest route to a decision.

    Both claims here are *published findings*. No private reasoning can reach this type:
    `assert_no_private_fields` checks that at import, exactly as it does for reviewers.
    """

    disputed_claim: str
    second_audit: str
    block: str
    conventions: str
    deterministic_findings: str
    curator_rules: str = ""

    def sections(self) -> tuple[DataSection, ...]:
        sections = [
            DataSection("The disputed claim, from the first audit", self.disputed_claim),
            DataSection(
                "What a second, independent audit of the same block reported",
                self.second_audit,
            ),
            DataSection("The block as it stands now", self.block),
            DataSection("Conventions this workbook follows", self.conventions),
            DataSection("Deterministic findings still open", self.deterministic_findings),
        ]
        if self.curator_rules.strip():
            sections.append(
                DataSection("Curation rules the curator supplied", self.curator_rules)
            )
        return tuple(sections)


assert_no_private_fields(AdjudicationContext)


# --------------------------------------------------------------------------------------
# Final verification context
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalVerificationContext:
    """The narrowest context in the council, and narrow on purpose.

    The block as it now stands, the workbook's own conventions, and the curation rules.
    **No deterministic findings, no issue ledger, no repair history, no earlier finding,
    no answer.** Every one of those would tell this agent where somebody already looked,
    and the whole value of a final pass is that it does not know.

    That is a stricter diet than the Independent Reviewer's, which does see the
    deterministic findings so it can avoid re-reporting what the rule engine already
    owns. Here the duplication is worth paying for: a rule-engine finding is a hint about
    which rows are interesting, and a verifier that has been given hints is no longer
    checking the rows nobody flagged -- which is exactly the population the eight missed
    defects were in.
    """

    block: str
    conventions: str
    curator_rules: str = ""

    def sections(self) -> tuple[DataSection, ...]:
        sections = [
            DataSection("The problem block, as the workbook now stands", self.block),
            DataSection("Conventions this workbook follows", self.conventions),
        ]
        if self.curator_rules.strip():
            sections.append(
                DataSection("Curation rules the curator supplied", self.curator_rules)
            )
        return tuple(sections)


assert_no_private_fields(FinalVerificationContext)
