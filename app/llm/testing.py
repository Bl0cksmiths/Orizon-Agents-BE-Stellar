"""Fakes and fixtures that keep every test off the Claude and jev networks.

Registered for the whole suite by tests/conftest.py (`pytest_plugins`), which
makes `llm_offline` apply to every test: both keys blanked whatever .env
holds, both transports replaced by ones that answer "not configured", and a
fresh in-memory spend ledger. A test that wants answers asks for a fake:

    def test_plans(fake_claude, fake_jev):
        fake_jev.answer({"injection": 0.02, "harmful": 0.01, "real_request": 0.97, "complexity": "moderate"})
        fake_claude.reply(Spec(goal=..., ...), purpose="improver")
        fake_claude.reply(plan, purpose="planner")
        ...
        assert fake_claude.calls_for("planner")[0].effort == "medium"

Scripted replies are consumed in order, per purpose first and then from the
any-purpose queue; `respond_with` installs a function that answers every
matching call. An unscripted call fails the test with the purpose named, so
a pipeline can never quietly skip a model call it was meant to make.

Replies go through the real `claude.structured` / `claude.text` /
`jev.ask` paths — budget check, pricing, ledger, stop-reason handling, schema
validation — only the network is replaced.
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from ..config import settings
from . import claude, jev, spend
from .claude import Attempt, ClaudeRequest, Completion
from .errors import JevUnavailable, LLMNotConfigured
from .jev import JevAnswer, JevChoice, JevNoul, JevReply, JevRequest, JevScore
from .spend import InMemorySpendStore, SpendLedger, Usage

DEFAULT_USAGE = Usage(input_tokens=1_000, output_tokens=200)
REFUSAL_USAGE = Usage(input_tokens=1_000)
DEFAULT_JEV_INPUT_TOKENS = 400


def _as_text(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


@dataclass
class _Step:
    make: Callable[[ClaudeRequest], Completion]
    persistent: bool = False


class FakeClaude:
    """A `ClaudeTransport` that answers from a script and records every request."""

    def __init__(self) -> None:
        self.calls: list[ClaudeRequest] = []
        self._queues: dict[str | None, deque[_Step]] = {}
        self._responders: dict[str | None, _Step] = {}

    # ── scripting ──

    def _push(self, purpose: str | None, step: _Step) -> FakeClaude:
        if step.persistent:
            self._responders[purpose] = step
        else:
            self._queues.setdefault(purpose, deque()).append(step)
        return self

    def reply(
        self,
        value: Any,
        *,
        purpose: str | None = None,
        usage: Usage = DEFAULT_USAGE,
        served_by: str | None = None,
    ) -> FakeClaude:
        """Answer the next matching call with `value`: a Pydantic model or JSON-able
        value for `structured`, a string for `text`. `served_by` simulates a
        server-side fallback model answering."""
        text = _as_text(value)

        def make(request: ClaudeRequest) -> Completion:
            model = served_by or request.model
            attempts = (
                (Attempt(request.model, Usage()), Attempt(model, usage)) if served_by else (Attempt(model, usage),)
            )
            return Completion(text=text, stop_reason="end_turn", model=model, attempts=attempts, served_by=served_by)

        return self._push(purpose, _Step(make))

    def refuse(
        self,
        *,
        purpose: str | None = None,
        category: str | None = "cyber",
        explanation: str | None = None,
        usage: Usage = REFUSAL_USAGE,
    ) -> FakeClaude:
        """Answer the next matching call with `stop_reason: "refusal"`."""

        def make(request: ClaudeRequest) -> Completion:
            return Completion(
                text="",
                stop_reason="refusal",
                model=request.model,
                attempts=(Attempt(request.model, usage),),
                refusal_category=category,
                refusal_explanation=explanation,
            )

        return self._push(purpose, _Step(make))

    def truncate(
        self, *, purpose: str | None = None, partial: str = '{"goal": "', usage: Usage = DEFAULT_USAGE
    ) -> FakeClaude:
        """Answer the next matching call with `stop_reason: "max_tokens"`."""

        def make(request: ClaudeRequest) -> Completion:
            return Completion(
                text=partial, stop_reason="max_tokens", model=request.model, attempts=(Attempt(request.model, usage),)
            )

        return self._push(purpose, _Step(make))

    def fail(self, error: BaseException, *, purpose: str | None = None) -> FakeClaude:
        """Raise `error` (an `LLMUnavailable`, say) from the next matching call."""

        def make(request: ClaudeRequest) -> Completion:
            raise error

        return self._push(purpose, _Step(make))

    def respond_with(self, fn: Callable[[ClaudeRequest], Any], *, purpose: str | None = None) -> FakeClaude:
        """Answer every matching call with `fn(request)` — a value as for `reply`,
        or a ready `Completion` — after any queued replies are used up."""

        def make(request: ClaudeRequest) -> Completion:
            out = fn(request)
            if isinstance(out, Completion):
                return out
            return Completion(
                text=_as_text(out),
                stop_reason="end_turn",
                model=request.model,
                attempts=(Attempt(request.model, DEFAULT_USAGE),),
            )

        return self._push(purpose, _Step(make, persistent=True))

    # ── the transport ──

    def _next(self, purpose: str) -> _Step:
        for key in (purpose, None):
            queue = self._queues.get(key)
            if queue:
                return queue.popleft()
        for key in (purpose, None):
            if key in self._responders:
                return self._responders[key]
        raise AssertionError(f"FakeClaude: no reply scripted for a {purpose!r} call")

    async def complete(self, request: ClaudeRequest) -> Completion:
        self.calls.append(request)
        completion = self._next(request.purpose).make(request)
        if request.stream and request.on_text is not None and completion.text:
            step = max(1, len(completion.text) // 3)
            for i in range(0, len(completion.text), step):
                maybe = request.on_text(completion.text[i : i + step])
                if inspect.isawaitable(maybe):
                    await maybe
        return completion

    # ── reading back ──

    def calls_for(self, purpose: str) -> list[ClaudeRequest]:
        return [c for c in self.calls if c.purpose == purpose]

    @property
    def pending(self) -> int:
        """Scripted replies not yet used (a test can assert this is 0)."""
        return sum(len(q) for q in self._queues.values())


def noul(p: float) -> JevNoul:
    return JevNoul(noul=p)


def choice(label: str, *, confidence: float = 0.9, probabilities: Mapping[str, float] | None = None) -> JevChoice:
    return JevChoice(choice=label, confidence=confidence, probabilities=dict(probabilities or {label: confidence}))


def score(value: float, *, confidence: float = 0.9, probabilities: Mapping[int, float] | None = None) -> JevScore:
    return JevScore(score=value, confidence=confidence, probabilities=dict(probabilities or {round(value): confidence}))


def _jev_answer(value: Any) -> JevAnswer:
    if isinstance(value, JevNoul | JevChoice | JevScore):
        return value
    if isinstance(value, bool):
        raise TypeError("answer a noul with a probability, not a bool")
    if isinstance(value, int | float):
        return JevNoul(noul=float(value))
    if isinstance(value, str):
        return choice(value)
    raise TypeError(f"not a jev answer: {value!r}")


class FakeJev:
    """A `JevTransport` that answers from a script and records every request.

    Answers: a float is a noul, a string a choice (confidence 0.9), or pass
    `testing.noul / choice / score(...)` for full control. Every question a
    call asks must be answered by its script, or the test fails naming it.
    """

    def __init__(self) -> None:
        self.calls: list[JevRequest] = []
        self._queues: dict[str | None, deque[Callable[[JevRequest], JevReply]]] = {}
        self._responders: dict[str | None, Callable[[JevRequest], JevReply]] = {}

    def answer(
        self,
        answers: Mapping[str, Any],
        *,
        purpose: str | None = None,
        input_tokens: int = DEFAULT_JEV_INPUT_TOKENS,
        persistent: bool = False,
    ) -> FakeJev:
        """Answer the next matching call (every matching call, if `persistent`)."""
        made = {qid: _jev_answer(v) for qid, v in answers.items()}

        def make(request: JevRequest) -> JevReply:
            missing = sorted(set(request.questions) - set(made))
            if missing:
                raise AssertionError(f"FakeJev: {request.purpose!r} asked {missing} with no scripted answer")
            return JevReply(
                answers={q: made[q] for q in request.questions}, model=request.model, input_tokens=input_tokens
            )

        if persistent:
            self._responders[purpose] = make
        else:
            self._queues.setdefault(purpose, deque()).append(make)
        return self

    def fail(self, error: BaseException | None = None, *, purpose: str | None = None) -> FakeJev:
        """Raise `error` (default `JevUnavailable("timeout")`) from the next matching call."""
        raised = error or JevUnavailable("timeout")

        def make(request: JevRequest) -> JevReply:
            raise raised

        self._queues.setdefault(purpose, deque()).append(make)
        return self

    def respond_with(self, fn: Callable[[JevRequest], Mapping[str, Any]], *, purpose: str | None = None) -> FakeJev:
        """Answer every matching call with `fn(request)`'s answers."""

        def make(request: JevRequest) -> JevReply:
            out = {qid: _jev_answer(v) for qid, v in fn(request).items()}
            return JevReply(answers=out, model=request.model, input_tokens=DEFAULT_JEV_INPUT_TOKENS)

        self._responders[purpose] = make
        return self

    async def system_one(self, request: JevRequest) -> JevReply:
        self.calls.append(request)
        for key in (request.purpose, None):
            queue = self._queues.get(key)
            if queue:
                return queue.popleft()(request)
        for key in (request.purpose, None):
            if key in self._responders:
                return self._responders[key](request)
        raise AssertionError(f"FakeJev: no answer scripted for a {request.purpose!r} call")

    def calls_for(self, purpose: str) -> list[JevRequest]:
        return [c for c in self.calls if c.purpose == purpose]

    @property
    def pending(self) -> int:
        return sum(len(q) for q in self._queues.values())


class OfflineClaude:
    """The suite's default transport: every call is "not configured", nothing is dialled."""

    async def complete(self, request: ClaudeRequest) -> Completion:
        raise LLMNotConfigured("missing_key", model=request.model)


class OfflineJev:
    """The suite's default jev transport: every call is unavailable, nothing is dialled."""

    async def system_one(self, request: JevRequest) -> JevReply:
        raise JevUnavailable("missing_key")


def offline_readiness() -> dict[str, Any]:
    """The `orchestrator` object /readiness reports under `llm_offline`, spelled out
    literally so the exact-payload probe tests review every field it carries."""
    return {
        "provider": "openai",
        "anthropic_key": False,
        "typesafe_key": False,
        "models": {
            "planner": "claude-opus-5-5",
            "improver": "claude-sonnet-5-5",
            "guard": "jev-1.13.0",
            "guard_fallback": "claude-haiku-4-5",
            "low": "claude-haiku-4-5",
            "moderate": "claude-sonnet-5-5",
            "complex": "claude-opus-5-5",
        },
        "spend": {"day": datetime.now(UTC).date().isoformat(), "spent_usd": 0.0, "cap_usd": 10.0, "paused": False},
    }


def fresh_ledger() -> SpendLedger:
    """An empty in-memory ledger."""
    return SpendLedger(InMemorySpendStore())


@pytest.fixture(autouse=True)
def llm_offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[SpendLedger]:
    """Every test: no Claude or jev key, offline transports, an empty in-memory ledger,
    and the shipped models and cap whatever a developer's .env says."""
    for name in (
        "anthropic_api_key",
        "typesafe_api_key",
        "orchestrator_provider",
        "typesafe_model",
        "claude_model_low",
        "claude_model_moderate",
        "claude_model_complex",
        "claude_server_fallbacks",
        "llm_daily_spend_cap_usd",
    ):
        monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)
    ledger = fresh_ledger()
    spend.set_ledger(ledger)
    claude.set_transport(OfflineClaude())
    jev.set_transport(OfflineJev())
    yield ledger
    claude.set_transport(None)
    jev.set_transport(None)
    spend.set_ledger(None)


@pytest.fixture
def fake_claude(llm_offline: SpendLedger) -> FakeClaude:
    """A scriptable Claude for this test."""
    fake = FakeClaude()
    claude.set_transport(fake)
    return fake


@pytest.fixture
def fake_jev(llm_offline: SpendLedger) -> FakeJev:
    """A scriptable jev for this test."""
    fake = FakeJev()
    jev.set_transport(fake)
    return fake


@pytest.fixture
def spend_ledger(llm_offline: SpendLedger) -> SpendLedger:
    """This test's (empty, in-memory) spend ledger."""
    return llm_offline
