"""A scripted client that drives the whole council offline.

The mock exists so that every path -- rejection, revision, retry exhaustion, escalation,
and adversarial non-termination -- is exercised in tests with no credentials and no
network. Anything the mock cannot express is a path the test suite cannot reach, so it
supports failure deliberately rather than only success.

It also **records every request**, and that recording is what the isolation tests assert
over. Asserting that a reviewer never saw the Writer's rationale has to be a claim about
the bytes that were sent, not about which object was passed to which function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from pydantic import BaseModel

from .base import AgentRole, LLMRequest, LLMResponse, ProviderError

#: A scripted reply: JSON text, or a callable given the request, or an exception to raise.
Script = str | BaseModel | Exception | Callable[[LLMRequest], "str | BaseModel | Exception"]


def _as_text(value: str | BaseModel) -> str:
    return value.model_dump_json() if isinstance(value, BaseModel) else value


@dataclass
class ScriptedLLMClient:
    """Replies are looked up by `(role, key, call_number)`.

    `key` is whatever the caller uses to identify the subject -- a block id for the
    auditor, an issue id for the writer and reviewers. The call number makes a repair
    loop scriptable: attempt 1 returns a bad patch, attempt 2 a good one.
    """

    replies: dict[tuple[AgentRole, str, int], Script] = field(default_factory=dict)
    default: Script | None = None
    #: Every request, in order. The isolation tests read this.
    requests: list[LLMRequest] = field(default_factory=list)
    #: Per `(role, key)` call counts, so a script can vary by attempt.
    counts: dict[tuple[AgentRole, str], int] = field(default_factory=dict)

    def script(
        self, role: AgentRole, key: str, *replies: Script
    ) -> ScriptedLLMClient:
        """Queue replies for successive calls about one subject."""
        for index, reply in enumerate(replies, start=1):
            self.replies[(role, key, index)] = reply
        return self

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)

        key = request.issue_id or request.job_id or ""
        counter = (request.role, key)
        self.counts[counter] = self.counts.get(counter, 0) + 1
        call_number = self.counts[counter]

        reply = self.replies.get((request.role, key, call_number))
        if reply is None:
            reply = self.replies.get((request.role, key, 0))  # any-call fallback
        if reply is None:
            reply = self.default
        if reply is None:
            raise ProviderError(
                f"no scripted reply for {request.role} / {key!r} call {call_number}; "
                "the mock refuses to invent one, because a test passing on an "
                "improvised response is testing nothing",
                retryable=False,
            )

        if callable(reply) and not isinstance(reply, BaseModel):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=_as_text(reply), model="mock", status="completed")

    # -- inspection --------------------------------------------------------------------

    def requests_for(self, role: AgentRole) -> tuple[LLMRequest, ...]:
        return tuple(r for r in self.requests if r.role is role)

    def payloads_for(self, role: AgentRole) -> tuple[str, ...]:
        return tuple(r.user_payload for r in self.requests_for(role))

    def call_count(self, role: AgentRole | None = None) -> int:
        if role is None:
            return len(self.requests)
        return len(self.requests_for(role))


@dataclass
class AlwaysReviseClient:
    """An adversary that never accepts and always finds something new.

    Used to prove termination. Under this client the council must still stop -- via the
    attempt cap, the validation-round budget, and the global fuses -- rather than looping
    until a person notices the bill.
    """

    verdict: BaseModel
    findings: Callable[[int], BaseModel]
    round_number: int = 0
    requests: list[LLMRequest] = field(default_factory=list)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.role in (
            AgentRole.KNOWN_ISSUE_REVIEWER,
            AgentRole.INDEPENDENT_REVIEWER,
        ):
            return LLMResponse(text=self.verdict.model_dump_json(), model="adversary")
        self.round_number += 1
        return LLMResponse(
            text=self.findings(self.round_number).model_dump_json(), model="adversary"
        )


def json_of(model: BaseModel) -> str:
    return model.model_dump_json()


def sequence(*replies: Script) -> Callable[[LLMRequest], Any]:
    """Return successive replies on successive calls, regardless of key."""
    remaining: list[Script] = list(replies)

    def next_reply(_: LLMRequest) -> Any:
        if not remaining:
            raise ProviderError("scripted sequence exhausted", retryable=False)
        return remaining.pop(0)

    return next_reply
