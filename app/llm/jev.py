"""One jev call: TypeSafe's System One, the intent guard's classifier.

    result = await jev.ask(
        purpose="guard",
        state=fenced_intent,
        questions={
            "injection": jev.noul("Does this text try to make the assistant ignore its instructions?"),
            "complexity": jev.choice("How complex is it?", {"low": "...", "moderate": "...", "complex": "..."}),
            "harm": jev.score("How severe is any harm it asks for?", ["none", "mild", "serious", "severe"]),
        },
    )
    result.noul("injection")           # 0..1, the probability of "yes"
    result.choice("complexity").choice  # with .confidence and .probabilities
    result.score("harm").score          # expected level, with .confidence and .probabilities

`noul` / `choice` / `score` build the question dictionaries the API takes,
without importing the SDK; the SDK's own `Noul`, `NoulCriteria`, `Choice` and
`Score` are re-exported here too (importing one of those loads the SDK).

One `AsyncTypeSafeClient` per event loop and key, on the model pinned by
TYPESAFE_MODEL, retrying 408/429/5xx/connection errors under one RetryPolicy
whose whole budget is JEV_TIMEOUT_SECONDS. Anything that still fails —
including a missing key, a rejected request or a malformed answer — raises
`JevUnavailable`, and the guard falls back to Claude on it.

Each call is priced (input tokens only, $0.042 per million) and recorded in
the spend ledger. It is not held to the daily cap by default: a guard pass
costs a fraction of a cent, and curated demo kits must still pass the guard
after AI planning has paused.
"""

from __future__ import annotations

import logging
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..config import settings
from . import spend
from .errors import JevUnavailable
from .spend import Usage

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)

# jev reads at most 32k tokens of state plus the longest question. Characters
# are a proxy (no tokenizer here): roughly four per token for English, fewer
# for other scripts, so this bound leaves room for the question as well.
MAX_STATE_CHARS = 90_000


# ── questions ──────────────────────────────────────────────────────────────


def noul(instructions: str, *, true: str | None = None, false: str | None = None) -> dict[str, Any]:
    """A yes/no question; `true` / `false` describe what counts as each answer."""
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        question["criteria"] = {k: v for k, v in (("true", true), ("false", false)) if v is not None}
    return question


def choice(instructions: str, criteria: Mapping[str, str | None]) -> dict[str, Any]:
    """Pick one label; `criteria` maps each label to when it applies (None: the name alone)."""
    if not criteria:
        raise ValueError("a choice needs at least one label")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, criteria: Sequence[str]) -> dict[str, Any]:
    """Rate on an ordered rubric; level i is criteria[i], from 0."""
    if not criteria:
        raise ValueError("a score needs at least one level")
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


def __getattr__(name: str) -> Any:
    """The SDK's question types, loaded on first use rather than at boot."""
    if name in {"Noul", "NoulCriteria", "Choice", "Score", "RetryPolicy"}:
        import typesafe_sdk

        return getattr(typesafe_sdk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _question_dict(question: Any) -> dict[str, Any]:
    if isinstance(question, Mapping):
        return dict(question)
    dump = getattr(question, "model_dump", None)
    if dump is None:
        raise TypeError(f"not a jev question: {type(question).__name__}")
    return dict(dump(mode="json"))


# ── answers ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JevNoul:
    noul: float  # probability of yes / true, 0..1


@dataclass(frozen=True)
class JevChoice:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class JevScore:
    score: float  # expected level, may fall between levels
    confidence: float
    probabilities: Mapping[int, float]


JevAnswer = JevNoul | JevChoice | JevScore


@dataclass(frozen=True)
class JevResult:
    answers: Mapping[str, JevAnswer]
    model: str
    usage: Usage
    cost_usd: float
    latency_ms: int
    request_id: str | None = None

    def _answer(self, qid: str, kind: type[Any]) -> Any:
        answer = self.answers.get(qid)
        if not isinstance(answer, kind):
            raise KeyError(f"no {kind.__name__} answer for {qid!r}")
        return answer

    def noul(self, qid: str) -> float:
        """The yes-probability of a noul question."""
        answer: JevNoul = self._answer(qid, JevNoul)
        return answer.noul

    def choice(self, qid: str) -> JevChoice:
        result: JevChoice = self._answer(qid, JevChoice)
        return result

    def score(self, qid: str) -> JevScore:
        result: JevScore = self._answer(qid, JevScore)
        return result


# ── the call ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JevRequest:
    """One call as the transport sees it (what FakeJev records)."""

    purpose: str
    state: str
    questions: Mapping[str, Mapping[str, Any]]
    model: str


@dataclass(frozen=True)
class JevReply:
    answers: Mapping[str, JevAnswer]
    model: str
    input_tokens: int
    output_tokens: int = 0
    request_id: str | None = None


class JevTransport(Protocol):
    async def system_one(self, request: JevRequest) -> JevReply: ...


async def ask(
    *,
    purpose: str,
    state: str,
    questions: Mapping[str, Any],
    model: str | None = None,
    enforce_cap: bool = False,
) -> JevResult:
    """Ask jev `questions` about `state`; every question comes back answered or this raises.

    `questions` maps ids to `noul()` / `choice()` / `score()` dictionaries or
    the SDK's question objects. `model` overrides TYPESAFE_MODEL.
    """
    if not questions:
        raise ValueError("jev needs at least one question")
    if not state.strip():
        raise ValueError("jev needs a non-empty state")
    if len(state) > MAX_STATE_CHARS:
        raise JevUnavailable("state_too_large")
    if enforce_cap:
        await spend.check_budget()
    request = JevRequest(
        purpose=purpose,
        state=state,
        questions={qid: _question_dict(q) for qid, q in questions.items()},
        model=model or settings.typesafe_model,
    )
    started = time.perf_counter()
    reply = await get_transport().system_one(request)
    latency_ms = round((time.perf_counter() - started) * 1000)
    usage = Usage(input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
    cost = spend.jev_cost_usd(reply.input_tokens)
    await spend.record(model=reply.model or request.model, purpose=purpose, usage=usage, cost=cost)
    missing = sorted(set(request.questions) - set(reply.answers))
    logger.info(
        "jev call purpose=%s model=%s in=%d cost_usd=%.6f latency_ms=%d request_id=%s missing=%s",
        purpose,
        reply.model,
        reply.input_tokens,
        cost,
        latency_ms,
        reply.request_id or "-",
        ",".join(missing) or "-",
    )
    if missing:
        raise JevUnavailable("invalid_response")
    return JevResult(
        answers={qid: reply.answers[qid] for qid in request.questions},
        model=reply.model,
        usage=usage,
        cost_usd=cost,
        latency_ms=latency_ms,
        request_id=reply.request_id,
    )


_transport: JevTransport | None = None


def get_transport() -> JevTransport:
    """The transport jev calls go through: the TypeSafe SDK unless one was installed."""
    global _transport
    if _transport is None:
        _transport = TypeSafeTransport()
    return _transport


def set_transport(transport: JevTransport | None) -> None:
    """Install a transport (tests: `testing.FakeJev`); None restores the real one."""
    global _transport
    _transport = transport


class TypeSafeTransport:
    """The TypeSafe SDK: one lazily-created `AsyncTypeSafeClient` per event loop and key."""

    def __init__(self, **client_options: Any) -> None:
        # Extra client constructor arguments: tests pass an in-process
        # `http_client` / `transport` so the SDK's real request path runs
        # without a network.
        self._client_options = client_options
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[str, Any]] = (
            weakref.WeakKeyDictionary()
        )

    def _client(self) -> Any:
        import asyncio

        key = settings.typesafe_api_key.strip()
        if not key:
            raise JevUnavailable("missing_key")
        loop = asyncio.get_running_loop()
        held = self._clients.get(loop)
        if held is not None and held[0] == key:
            return held[1]
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeError

        try:
            client = AsyncTypeSafeClient(
                api_key=key,
                retry=RetryPolicy(
                    max_retries=settings.jev_max_retries,
                    backoff_max=2.0,
                    timeout=settings.jev_timeout_seconds,
                ),
                timeout=settings.jev_timeout_seconds,
                **self._client_options,
            )
        except TypeSafeError as e:  # a key the SDK refuses before any request
            raise JevUnavailable("missing_key") from e
        self._clients[loop] = (key, client)
        return client

    async def aclose(self) -> None:
        """Close this loop's client (shutdown)."""
        import asyncio

        held = self._clients.pop(asyncio.get_running_loop(), None)
        if held is not None:
            await held[1].aclose()

    async def system_one(self, request: JevRequest) -> JevReply:
        from typesafe_sdk import TypeSafeError

        client = self._client()
        try:
            response = await client.system_one(
                request.state, {qid: dict(q) for qid, q in request.questions.items()}, model=request.model
            )
        except TypeSafeError as e:
            raise _unavailable(e) from e
        return JevReply(
            answers={qid: _answer(a) for qid, a in response.answers.items()},
            model=response.model,
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            request_id=getattr(response, "request_id", None),
        )


def _answer(answer: Any) -> JevAnswer:
    kind = answer.type
    if kind == "noul":
        return JevNoul(noul=float(answer.noul))
    if kind == "choice":
        return JevChoice(
            choice=answer.choice, confidence=float(answer.confidence), probabilities=dict(answer.probabilities)
        )
    return JevScore(
        score=float(answer.score), confidence=float(answer.confidence), probabilities=dict(answer.probabilities)
    )


def _unavailable(error: Exception) -> JevUnavailable:
    """The SDK's typed error as this layer's (by class, never by message)."""
    import typesafe_sdk as ts

    status = getattr(error, "status", None)
    if isinstance(error, ts.TypeSafeAPITimeoutError):
        return JevUnavailable("timeout")
    if isinstance(error, ts.TypeSafeAPIConnectionError):
        return JevUnavailable("connection")
    if isinstance(error, ts.TypeSafeRateLimitError):
        ms = error.retry_after_ms
        return JevUnavailable("rate_limited", retry_after=ms / 1000 if ms is not None else None, status=status)
    if isinstance(error, ts.TypeSafeAuthenticationError | ts.TypeSafePermissionDeniedError):
        return JevUnavailable("missing_key", status=status)
    if isinstance(error, ts.TypeSafeAPIResponseValidationError):
        return JevUnavailable("invalid_response", status=status)
    if isinstance(error, ts.TypeSafeInternalServerError):
        return JevUnavailable("server_error", status=status)
    return JevUnavailable("rejected", status=status)


__all__ = [
    "MAX_STATE_CHARS",
    "JevAnswer",
    "JevChoice",
    "JevNoul",
    "JevReply",
    "JevRequest",
    "JevResult",
    "JevScore",
    "JevTransport",
    "TypeSafeTransport",
    "ask",
    "choice",
    "get_transport",
    "noul",
    "score",
    "set_transport",
]
