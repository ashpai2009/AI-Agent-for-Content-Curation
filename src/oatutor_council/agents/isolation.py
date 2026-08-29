"""Context isolation, enforced by types and checked before every dispatch.

Reviewers judge the artefact, not the argument for it. A reviewer told *why* an edit was
made reviews the reasoning instead of the mathematics, and a confident rationale is
exactly what a wrong edit tends to have.

Three mechanisms, in increasing order of paranoia:

1. **`PrivateText` is not a `str`.** It deliberately does not subclass `str`, because a
   subclass interpolates silently into an f-string and the leak is invisible. This wraps
   the text, renders as `<private>`, and gives it up only through `reveal()` -- so a leak
   requires someone to type the word.

2. **`ReviewerContext` cannot name a private type.** `assert_no_private_fields` walks the
   annotations of the context class at import time, including inside containers, and
   raises if any private model appears anywhere in the closure. A reviewer prompt built
   from a type that cannot hold private data cannot leak it by construction.

3. **`TaintRegistry.assert_clean` runs before every dispatch.** Types catch the leak
   someone declared; this catches the one someone pasted -- an **exact copy** of
   registered private text.

   Exact equality is strong evidence of transmission, **not proof of it**. Two agents can
   independently produce the same short sentence ("the answer should be 6 rather than 5"),
   and that would fail a job it should not. The trade is deliberate: at five tokens or more
   an exact match is rare enough to act on, where the 12-token *overlap* it replaced fired
   on ordinary agreement. When it does misfire, the fix is to give the text a public origin
   via `public_for`, never a longer threshold -- a threshold moves the coincidence instead
   of removing it.

**Mechanisms 1 and 2 are the guarantee. 3 is a backstop, and its fuzzy half is only a
signal.** That ordering was inverted for most of this module's life, and the cost was
paid in real jobs: 12-token shingle overlap was treated as proof of a leak and used to
kill jobs outright. It never caught a leak. It did kill two held-out runs after 42 and 45
paid model calls -- one of them because the Initial Auditor privately wrote that a body
"says subtract 8 from both sides to obtain y=21" and the Independent Reviewer later
reported the same defect in the same words. Two agents agreeing about one equation is the
system working, and the detector called it a breach.

The reason no threshold fixes this: a paraphrase detector cannot distinguish *agreement*
from *transmission*. Two agents reasoning correctly about the same cell produce the same
sentence, and that is not a signal that can be tuned away -- it is the intended behaviour
of the pipeline. So overlap is recorded as a `Suspicion` for a human to read, and only an
exact copy raises.

What actually keeps the Writer's rationale away from a reviewer is that `ReviewerContext`
**cannot name a private type**, checked at import. That is structural, and no amount of
text agreement weakens it.

A `ContextIsolationError` still terminates the job as `FAILED(ISOLATION_VIOLATION)`, never
a warning and never a retry -- a retry would send the same tainted payload again. It is
now raised only where the evidence is conclusive.
"""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Iterable

from pydantic import BaseModel

#: Shingle width. Long enough that ordinary shared vocabulary -- a problem name, a rule
#: code, a repeated cell value -- does not collide; short enough that a paraphrase which
#: keeps any twelve consecutive words is caught.
SHINGLE_SIZE = 12

_TOKEN = re.compile(r"[A-Za-z0-9]+")

#: Below this, a private string carries no argument worth protecting, and matching it
#: produces false positives on ordinary vocabulary. A one-word derivation of `"d"` would
#: otherwise match every payload containing the letter d in any word, and a two-word one
#: would match any payload that happens to use the same two words. The leak this guard
#: exists to stop -- a reviewer receiving the Writer's reasoning -- cannot fit in four
#: tokens.
MIN_PRIVATE_TOKENS = 5

#: Ceiling on suspicions held in memory for one job. A suspicion is a diagnostic, and a
#: diagnostic that can grow without bound is a memory leak wearing a lab coat: one auditor
#: note colliding with thirty blocks would otherwise accumulate thirty entries per sweep,
#: for the whole life of a long job. Duplicates are dropped at append time and the list
#: stops growing here; the count of what was dropped is kept so the record still says that
#: more happened than is listed.
MAX_SUSPICIONS = 200


class ContextIsolationError(Exception):
    """Private reasoning reached a context that must not contain it."""

    def __init__(self, message: str, *, label: str = "", context: str = "") -> None:
        super().__init__(message)
        self.label = label
        self.context = context


@dataclass(frozen=True)
class Suspicion:
    """A shared span that is *consistent with* a leak but does not establish one.

    Recorded rather than raised. The distinction is the whole lesson of this module: an
    exact copy of registered private text is evidence, because nothing else produces it;
    a shared twelve-token span is not, because two agents examining the same equation
    reach the same sentence about it all the time. Treating the second as proof killed
    two held-out jobs after 42 and 45 paid model calls, and the leak it was hunting has
    never once occurred.

    These are surfaced so a human can look, which is the appropriate response to a
    signal that is suggestive and not conclusive.
    """

    label: str
    context: str
    shared_tokens: int

    def describe(self) -> str:
        return (
            f"{self.context} payload shares a {self.shared_tokens}-token sequence with "
            f"private text {self.label!r} that public ground does not explain. This is "
            "not proof of a leak -- two agents describing the same defect produce the "
            "same sentence -- but it is worth a look."
        )


class PrivateText:
    """Text that must never reach a reviewer.

    Not a `str` subclass. That is the whole design: `f"{private}"` on a `str` subclass
    silently emits the content, and this emits `<private>` instead.
    """

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def reveal(self) -> str:
        """Get the underlying text. Deliberately verbose at the call site."""
        return self._text

    def __str__(self) -> str:
        return "<private>"

    def __repr__(self) -> str:
        return "PrivateText(<redacted>)"

    def __len__(self) -> int:
        return len(self._text)

    def __bool__(self) -> bool:
        return bool(self._text)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PrivateText) and other._text == self._text

    def __hash__(self) -> int:
        return hash(("PrivateText", self._text))


class PrivateModel(BaseModel):
    """Base for every model holding agent reasoning.

    Marked by its type rather than by a naming convention, so `assert_no_private_fields`
    can recognise one structurally instead of by guessing from field names.
    """


class WriterPrivate(PrivateModel):
    """The Writer's rationale. Persisted for the human change log, never sent onward."""

    reasoning: str = ""
    derivation: str = ""
    confidence: float = 1.0


class AuditorPrivate(PrivateModel):
    """The Initial Auditor's reasoning about what it saw."""

    reasoning: str = ""


# --------------------------------------------------------------------------------------
# Structural check
# --------------------------------------------------------------------------------------


def _annotations_of(cls: type) -> dict[str, Any]:
    """Resolved field annotations for a dataclass, Pydantic model, or plain class.

    Resolution matters more than it looks. Under `from __future__ import annotations`
    every annotation in this codebase is a **string** at runtime, so
    `dataclasses.fields(...).type` yields `"WriterPrivate"` rather than the class. A walk
    over those strings can only pattern-match on names, which means a private type
    reached through an alias or a wrapper would pass unnoticed. `get_type_hints`
    evaluates them back into types so the closure walk sees what is really there.
    """
    if isinstance(cls, type) and issubclass(cls, BaseModel):
        return {name: f.annotation for name, f in cls.model_fields.items()}
    try:
        return typing.get_type_hints(cls)
    except Exception:
        # A locally-defined type the module namespace cannot resolve. The string check in
        # `assert_no_private_fields` is the remaining defence; it is weaker, so this
        # falls back rather than pretending the field was verified.
        if is_dataclass(cls):
            return {f.name: f.type for f in fields(cls)}
        return getattr(cls, "__annotations__", {})


def _walk(annotation: Any, seen: set[Any]) -> Iterable[type]:
    """Yield every concrete type reachable from an annotation, containers included."""
    if annotation in seen:
        return
    seen.add(annotation)

    if isinstance(annotation, str):
        # A postponed annotation we cannot resolve. Yielding nothing here would be a
        # silent hole, so the name itself is checked by the caller.
        return

    origin = typing.get_origin(annotation)
    if origin is not None:
        for argument in typing.get_args(annotation):
            yield from _walk(argument, seen)
        return

    if isinstance(annotation, type):
        yield annotation
        for nested in _annotations_of(annotation).values():
            yield from _walk(nested, seen)


def assert_no_private_fields(cls: type) -> type:
    """Refuse a type whose field closure can reference private reasoning.

    Run at import time on `ReviewerContext`, so a field added in future that reintroduces
    the leak fails to import rather than shipping.
    """
    offenders: list[str] = []
    for name, annotation in _annotations_of(cls).items():
        if isinstance(annotation, str):
            if "Private" in annotation:
                offenders.append(f"{name}: {annotation}")
            continue
        for found in _walk(annotation, set()):
            if found is PrivateText or (
                isinstance(found, type) and issubclass(found, PrivateModel)
            ):
                offenders.append(f"{name}: {found.__name__}")
    if offenders:
        raise ContextIsolationError(
            f"{cls.__name__} can reference private reasoning through {offenders}; "
            "a reviewer context must be unable to hold it at all"
        )
    return cls


# --------------------------------------------------------------------------------------
# Taint registry
# --------------------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def _shingles(tokens: list[str], size: int = SHINGLE_SIZE) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return set()
    return {tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


@dataclass
class TaintRegistry:
    """Everything private this job has produced, checked against outgoing payloads.

    Registered per job rather than globally: two jobs curating different workbooks share
    no reasoning, and a global registry would make one job's rationale a false positive
    in another.
    """

    entries: dict[str, str] = None  # label -> text
    #: Non-fatal shared spans, accumulated for a human to review. Not a failure channel:
    #: nothing reads this to decide whether the job may continue. Deduplicated by
    #: (label, context) and bounded by `MAX_SUSPICIONS`.
    suspicions: list = None
    #: How many were dropped as duplicates or past the cap. Kept so a drained record can
    #: say "and more", rather than implying the listed ones are all that happened.
    suspicions_suppressed: int = 0

    def __post_init__(self) -> None:
        if self.entries is None:
            self.entries = {}
        if self.suspicions is None:
            self.suspicions = []

    def _note_suspicion(self, suspicion: Suspicion) -> None:
        """Record one, unless it is a repeat or the job has already produced plenty."""
        key = (suspicion.label, suspicion.context)
        if any((s.label, s.context) == key for s in self.suspicions):
            self.suspicions_suppressed += 1
            return
        if len(self.suspicions) >= MAX_SUSPICIONS:
            self.suspicions_suppressed += 1
            return
        self.suspicions.append(suspicion)

    def drain_suspicions(self) -> tuple[list, int]:
        """Hand over what has accumulated and reset, for a caller that will persist it.

        Draining rather than reading, so the same suspicion is not written to the audit
        trail once per step for the rest of the job.
        """
        drained, suppressed = list(self.suspicions), self.suspicions_suppressed
        self.suspicions, self.suspicions_suppressed = [], 0
        return drained, suppressed

    def register(self, label: str, text: str | PrivateText) -> None:
        value = text.reveal() if isinstance(text, PrivateText) else text
        if value and value.strip():
            self.entries[label] = value

    def register_model(self, label: str, model: PrivateModel) -> None:
        for name, value in model.model_dump().items():
            if isinstance(value, str):
                self.register(f"{label}.{name}", value)

    def labelled_entries(self) -> tuple[tuple[str, str], ...]:
        """Everything registered, for persisting. The only way text leaves this object."""
        return tuple(self.entries.items())

    @classmethod
    def rebuilt(cls, entries: Iterable[tuple[str, str]]) -> TaintRegistry:
        """Reconstruct a registry from durable rows.

        **A registry that only lives in the worker is a guarantee that ends at the first
        crash.** The Writer's rationale from before the crash is still in the database and
        still on the patch, so a resumed job could hand it to a reviewer with nothing left
        to object -- and the leak would be invisible, because a registry with no entries
        passes every check it is asked to make.
        """
        registry = cls()
        for label, text in entries:
            registry.register(label, text)
        return registry

    def assert_clean(
        self,
        payload: str,
        *,
        context: str,
        public: Iterable[str] = (),
        #: Exact private-record labels mapped to text that is public *for that record
        #: alone* -- the agent's own published finding about its own block. Never a prefix.
        public_for: dict[str, Iterable[str]] | None = None,
    ) -> None:
        """Refuse to dispatch a payload carrying registered private text.

        Exact containment catches a copy-paste. Shingle overlap catches the paraphrase,
        the partial quote, and the summary -- all of which hand the reviewer the argument
        while defeating an exact match.

        **`public` is the ground truth that stops a quotation looking like a leak.** A
        Writer's derivation quotes the cells it is reasoning about -- that is what a
        derivation *is* -- and those same cells are rendered into the reviewer's block,
        because the reviewer is judging them. Twelve consecutive tokens of shared
        mathematics is therefore the normal case rather than evidence of anything, and
        treating it as a violation failed a live job whose first repair was correct.

        So a span is disqualifying only when it is **not** explained by public ground:
        `public` shingles are subtracted from each private entry before the comparison,
        and an entry that is wholly a public quotation is skipped.

        What `public` must never be is *the payload itself*. Passing the outgoing text
        back in would subtract everything from everything and leave a check that cannot
        fail -- mechanism 3 deleted while still appearing to run. Callers pass only text
        whose **provenance** is the curator's workbook, the curator's own document, or the
        deterministic rule engine: sources that contain no agent prose and so cannot
        launder a rationale. Agent-authored free text (an issue summary an auditor wrote)
        is deliberately not public ground, because that is exactly where a rationale could
        be smuggled and then declared exempt.
        """
        if not self.entries:
            return

        # Padded so containment matches on token boundaries. Without the padding, the
        # private string `"d"` matches inside the word `"old"` and every payload is a
        # violation.
        payload_tokens = _tokens(payload)
        haystack = f" {' '.join(payload_tokens)} "
        payload_shingles = _shingles(payload_tokens)

        public_tokens = _tokens("\n".join(public))
        public_haystack = f" {' '.join(public_tokens)} " if public_tokens else ""
        public_shingles = _shingles(public_tokens)

        for label, private in self.entries.items():
            private_tokens = _tokens(private)
            if len(private_tokens) < MIN_PRIVATE_TOKENS:
                continue

            # Ground that is public *for one exact private record*. An agent's own public
            # output is not a leak of that agent's own private note about the same block --
            # they describe one finding in one sentence -- but it is not ground for any
            # other note, so the key is the originating record, never a prefix.
            #
            # Prefix matching was the first version and it was too broad by a long way:
            # `"auditor."` exempted every block's auditor reasoning for every issue,
            # including issues raised by a different agent about a different block. The
            # keys here are exact labels; `label.startswith(key + ".")` only admits the
            # per-field suffix `register_model` appends (`.reasoning`, `.derivation`).
            own_haystack, own_shingles = public_haystack, public_shingles
            for key, texts in (public_for or {}).items():
                if label == key or label.startswith(f"{key}."):
                    own_tokens = public_tokens + _tokens("\n".join(texts))
                    own_haystack = f" {' '.join(own_tokens)} "
                    own_shingles = _shingles(own_tokens)
                    break

            # Reasoning shorter than a shingle produces none, so exact containment is the
            # only check available for it -- and is sufficient, since there is little to
            # paraphrase in a sentence that short.
            sequence = f" {' '.join(private_tokens)} "
            if sequence in haystack:
                # The entry reached the payload -- but if the identical run of tokens is
                # also in the workbook, what reached it was the workbook.
                if own_haystack and sequence in own_haystack:
                    continue
                raise ContextIsolationError(
                    f"{context} payload contains private text registered as {label!r}",
                    label=label,
                    context=context,
                )

            # Only the spans this private text does not share with public ground can
            # testify that private text is what arrived.
            distinctive = _shingles(private_tokens) - own_shingles
            overlap = distinctive & payload_shingles
            if overlap:
                # **Recorded, not raised.** See the class docstring: a shared span is
                # equally consistent with a leak and with two agents reaching the same
                # conclusion about the same equation, and this check cannot tell them
                # apart. Killing the job on it destroyed two held-out runs after 42 and
                # 45 paid model calls, and never once caught a real leak.
                self._note_suspicion(
                    Suspicion(
                        label=label,
                        context=context,
                        shared_tokens=len(next(iter(overlap))),
                    )
                )
