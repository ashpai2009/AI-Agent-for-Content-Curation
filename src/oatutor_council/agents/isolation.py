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
   someone declared; this catches the one someone pasted. It compares exact text *and*
   12-token shingles, because a paraphrase or a partial quote defeats exact matching
   while still handing the reviewer the Writer's argument.

A violation is `ContextIsolationError` and terminates the job as
`FAILED(ISOLATION_VIOLATION)`. Never a warning, never a retry: a retry would send the
same tainted payload again, and a warning would let a leaked review count as a review.
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


class ContextIsolationError(Exception):
    """Private reasoning reached a context that must not contain it."""

    def __init__(self, message: str, *, label: str = "", context: str = "") -> None:
        super().__init__(message)
        self.label = label
        self.context = context


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

    def __post_init__(self) -> None:
        if self.entries is None:
            self.entries = {}

    def register(self, label: str, text: str | PrivateText) -> None:
        value = text.reveal() if isinstance(text, PrivateText) else text
        if value and value.strip():
            self.entries[label] = value

    def register_model(self, label: str, model: PrivateModel) -> None:
        for name, value in model.model_dump().items():
            if isinstance(value, str):
                self.register(f"{label}.{name}", value)

    def assert_clean(self, payload: str, *, context: str) -> None:
        """Refuse to dispatch a payload carrying registered private text.

        Exact containment catches a copy-paste. Shingle overlap catches the paraphrase,
        the partial quote, and the summary -- all of which hand the reviewer the argument
        while defeating an exact match.
        """
        if not self.entries:
            return

        haystack = " ".join(_tokens(payload))
        payload_shingles = _shingles(_tokens(payload))

        for label, private in self.entries.items():
            private_tokens = _tokens(private)
            if not private_tokens:
                continue

            # Short reasoning produces no shingles, so exact containment is the only
            # check available for it -- and is sufficient, since there is little to
            # paraphrase.
            if " ".join(private_tokens) in haystack:
                raise ContextIsolationError(
                    f"{context} payload contains private text registered as {label!r}",
                    label=label,
                    context=context,
                )

            overlap = _shingles(private_tokens) & payload_shingles
            if overlap:
                raise ContextIsolationError(
                    f"{context} payload shares a {SHINGLE_SIZE}-token sequence with "
                    f"private text {label!r}, which means a paraphrase or partial quote "
                    "reached it",
                    label=label,
                    context=context,
                )
