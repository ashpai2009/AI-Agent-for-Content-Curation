"""Turning a batched response back into per-block results, safely.

Everything here exists because of one failure mode: **a block the model silently omitted
looks exactly like a block it examined and found clean.** Both produce no findings. If the
phase marks both done, the second is a workbook reported as reviewed when nothing looked at
it — the "never falsely report success" property, defeated by an optimisation.

So attribution is by an opaque `batch_item_id` generated per call, validated here, and a
block is marked done only when its own result is present and valid. Everything ambiguous
sends the block back to the queue, because re-examining a block costs one call and
wrongly clearing one costs a curator their trust in the report.

The id is generated per *call*, not derived from the block, so a response cannot construct
a plausible id for a block it was never given.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..models import ProblemBlock


class FindingAttributionError(ValueError):
    """A finding names no target cell in the block whose result contains it."""


@dataclass(frozen=True)
class BatchItem:
    """One block as dispatched: the block, its opaque id, and the claims it was shown."""

    block: ProblemBlock
    item_id: str
    #: Indices of the seed claims this block was actually given. Both refutations *and*
    #: confirmations are filtered against it -- see `claims_shown`.
    claims_shown: frozenset[int] = frozenset()

    @property
    def label(self) -> str:
        """The section label. **Generated data only.**

        No problem name: names come from the workbook, are duplicated across blocks in real
        files, and are corrupted in exactly the files this system exists to repair. The
        name goes inside the neutralised body where it belongs.
        """
        return f"batch_item={self.item_id}; rows={self.block.start_row}-{self.block.end_row}"


def make_items(
    blocks: Sequence[ProblemBlock], claims_for: Any = None
) -> tuple[BatchItem, ...]:
    """Assign a fresh opaque id to each block for this call."""
    items = []
    for block in blocks:
        shown = frozenset(claims_for(block)) if claims_for else frozenset()
        items.append(
            BatchItem(block=block, item_id=secrets.token_hex(4), claims_shown=shown)
        )
    return tuple(items)


@dataclass
class Attribution:
    """What a batched response turned out to say, per block.

    `resolved` maps an item id to its result. `requeue` holds the blocks that must be
    examined again -- omitted, duplicated, or invalidated by a finding that could not
    belong to them.
    """

    resolved: dict[str, Any] = field(default_factory=dict)
    requeue: list[BatchItem] = field(default_factory=list)
    discarded_results: int = 0
    discarded_findings: int = 0

    def note(self, reason: str) -> None:  # pragma: no cover - trivial
        pass


def attribute(
    items: Sequence[BatchItem], results: Iterable[Any]
) -> Attribution:
    """Match results to dispatched blocks, refusing every ambiguous case.

    * **Exactly one** result per dispatched id, or the block is requeued.
    * An **unknown** id is discarded -- it names a block this call never sent.
    * A **duplicated** id invalidates both copies: one of them is wrong and nothing here
      can tell which, so the block goes back rather than a coin being tossed.
    """
    by_id: dict[str, list[Any]] = {}
    known = {item.item_id for item in items}
    attribution = Attribution()

    for result in results:
        item_id = str(getattr(result, "batch_item_id", "") or "").strip()
        if item_id not in known:
            attribution.discarded_results += 1
            continue
        by_id.setdefault(item_id, []).append(result)

    for item in items:
        matches = by_id.get(item.item_id, [])
        if len(matches) != 1:
            # Zero: the model did not answer for this block. More than one: it answered
            # twice and disagreed with itself. Both mean "unknown", and unknown means the
            # block has not been examined.
            if len(matches) > 1:
                attribution.discarded_results += len(matches)
            attribution.requeue.append(item)
            continue
        attribution.resolved[item.item_id] = matches[0]

    return attribution


def cells_for_block(cells: Sequence[Any], block: ProblemBlock) -> list[Any] | None:
    """Accept exact targets only when every one belongs to the attributed block.

    Dropping only the outside members would turn one coordinated repair into a different,
    incomplete repair. The whole finding is unattributable, so its block must be requeued.
    """
    inside = [cell for cell in cells if block.contains_row(cell.row)]
    return inside if cells and len(inside) == len(cells) else None
