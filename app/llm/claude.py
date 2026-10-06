"""One Claude call: budgeted, checked, priced and recorded.

    structured(...)  a reply that validates against a Pydantic model (structured outputs)
    text(...)        a free-text reply, optionally streamed

Both run the same path:

1. `spend.check_budget()` — `SpendCapReached` before anything is sent.
2. The request goes out through the transport: the real one is the Anthropic
   SDK (`AnthropicTransport`); tests install `testing.FakeClaude`.
3. The call is priced from its reported usage and recorded in the ledger —
   always, refusals and truncations included, because they are billed too.
4. `stop_reason` is checked BEFORE any content is read: "refusal" raises
   `LLMRefused`, "max_tokens" raises `LLMTruncated`.
5. A structured reply is validated against its schema (`LLMInvalidOutput`).

What the request carries, per the Claude API rules for these models:

* Effort is always explicit (`output_config.effort`) on models that take it —
  Claude Opus 5.5 defaults to "medium" otherwise — and never sent to Claude
  Haiku 4.5, which rejects it. Thinking is left to the model: always-on
  adaptive on Opus 5.5, adaptive by default on Sonnet 5.5, off on Haiku 4.5.
  `max_tokens` covers thinking as well as the reply, so size it with room.
* JSON comes from structured outputs (`output_config.format`), never from a
  prefill or forced `tool_choice`, which these models reject.
* The system prompt carries a prompt-cache breakpoint: keep it byte-stable
  (no timestamps, ids or per-request data — put those in `user`), and the
  stable prefix is billed at the cache-read rate on every later call.
* Server-side refusal fallbacks (`fallbacks="default"`, beta
  `server-side-fallback-2026-07-01`) are on for Opus 5.5 and Sonnet 5.5: a
  classifier decline is re-run on Anthropic's recommended model inside the
  same call, and `LLMResult.served_by` names that model when it answered.
* The SDK retries 408/409/429/5xx and connection errors (CLAUDE_MAX_RETRIES)
  within CLAUDE_TIMEOUT_SECONDS per attempt; what is still failing after that
  surfaces as `LLMUnavailable` with the server's Retry-After when it gave one.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import settings
from . import spend
from .errors import (
    LLMInvalidOutput,
    LLMNotConfigured,
    LLMRefused,
    LLMRequestError,
    LLMTruncated,
    LLMUnavailable,
)
from .spend import Usage
from .tiers import EFFORTS, Effort

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
V = TypeVar("V")

# Called with each streamed text delta, in order; may be a coroutine function.
TextCallback = Callable[[str], Awaitable[None] | None]

# The models that accept the server-side `fallbacks="default"` form. Sonnet 5.5
# accepts only that form; both are what this service plans and works on.
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def supports_effort(model: str) -> bool:
    """Whether `output_config.effort` may be sent: every current model except Claude Haiku 4.5."""
    return not model.startswith("claude-haiku-")


@dataclass(frozen=True)
class LLMResult(Generic[V]):
    """A successful call: the value, and what it took to get it."""

    value: V
    model: str  # the model asked
    usage: Usage  # summed across every billed attempt
    cost_usd: float
    latency_ms: int
    served_by: str | None = None  # the fallback model that answered, when one did
    request_id: str | None = None


@dataclass(frozen=True)
class ClaudeRequest:
    """One call as the transport sees it, after normalisation (what FakeClaude records)."""

    purpose: str
    model: str
    system: str
    user: str
    max_tokens: int
    effort: Effort | None  # None: not sent (the model takes no effort)
    json_schema: dict[str, Any] | None = None  # the API-ready schema of a structured call
    schema_name: str | None = None
    stream: bool = False
    cache_system: bool = True
    on_text: TextCallback | None = field(default=None, compare=False)
    # Filled in by the transport as a stream arrives (`_call` attaches one to
    # every streamed request), so a stream cut short can still be billed.
    progress: StreamProgress | None = field(default=None, compare=False)


# Characters per output token when a cut stream's output has to be estimated
# from what arrived: ~3.5 for English prose and code. Rounded up, so the
# estimate errs towards the cap tripping early, never late.
CHARS_PER_TOKEN = 3.5


@dataclass
class StreamProgress:
    """What a stream has delivered so far — billed by Anthropic even if the call never completes.

    `started` turns true at `message_start`, which carries the input and cache
    token counts. `output_tokens` is the latest output count the API itself
    reported (message_start / message_delta), and `streamed_chars` the text
    and thinking characters received, for when the stream stops before the
    API reports a final count.
    """

    started: bool = False
    model: str | None = None
    input_usage: Usage = field(default_factory=Usage)
    output_tokens: int = 0
    streamed_chars: int = 0

    def start(self, model: str | None, usage: Usage) -> None:
        self.started = True
        self.model = model
        self.input_usage = Usage(
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        self.output_tokens = max(self.output_tokens, usage.output_tokens)

    def billed_so_far(self) -> Usage:
        """The usage to record for a stream that stopped here: input as reported,
        output the higher of the API's own count and the character estimate."""
        estimate = math.ceil(self.streamed_chars / CHARS_PER_TOKEN)
        return self.input_usage + Usage(output_tokens=max(self.output_tokens, estimate))


@dataclass(frozen=True)
class Attempt:
    """One billed attempt of a call; a server-side fallback adds one per model that ran."""

    model: str
    usage: Usage


@dataclass(frozen=True)
class Completion:
    """What the transport got back, before any of it is trusted."""

    text: str  # every text block, in order
    stop_reason: str | None
    model: str  # the model that produced the final message
    attempts: tuple[Attempt, ...]
    served_by: str | None = None
    refusal_category: str | None = None
    refusal_explanation: str | None = None
    request_id: str | None = None

    @property
    def usage(self) -> Usage:
        total = Usage()
        for attempt in self.attempts:
            total = total + attempt.usage
        return total

    def cost_usd(self) -> float:
        return sum(spend.cost_usd(a.model, a.usage) for a in self.attempts)


class ClaudeTransport(Protocol):
    async def complete(self, request: ClaudeRequest) -> Completion: ...


# ── the public calls ───────────────────────────────────────────────────────


async def structured(
    *,
    purpose: str,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    max_tokens: int,
    effort: Effort | None = None,
    cache_system: bool = True,
    enforce_cap: bool = True,
) -> LLMResult[T]:
    """Ask `model` for a reply that validates as `schema`.

    `purpose` names the caller in the ledger and the logs ("planner",
    "guard.fallback", "worker.research"...): lowercase, at most 64 chars.
    `effort` defaults to "medium" on models that take one.
    """
    from anthropic import transform_schema  # the SDK's own schema → structured-outputs transform

    request = _request(
        purpose=purpose,
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        effort=effort,
        json_schema=transform_schema(schema),
        schema_name=schema.__name__,
        cache_system=cache_system,
    )
    completion, cost, latency_ms = await _call(request, enforce_cap=enforce_cap)
    try:
        value = schema.model_validate_json(completion.text)
    except ValidationError as e:
        raise LLMInvalidOutput(model=model, schema=schema.__name__, detail=_validation_detail(e)) from e
    return _result(value, request, completion, cost, latency_ms)


async def text(
    *,
    purpose: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    effort: Effort | None = None,
    stream: bool = False,
    on_text: TextCallback | None = None,
    cache_system: bool = True,
    enforce_cap: bool = True,
) -> LLMResult[str]:
    """Ask `model` for free text. `stream=True` streams it (use for long
    generations), calling `on_text` with each delta as it arrives; the result
    is the whole reply either way."""
    if on_text is not None and not stream:
        raise ValueError("on_text needs stream=True")
    request = _request(
        purpose=purpose,
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        effort=effort,
        stream=stream,
        on_text=on_text,
        cache_system=cache_system,
    )
    completion, cost, latency_ms = await _call(request, enforce_cap=enforce_cap)
    return _result(completion.text, request, completion, cost, latency_ms)


def _request(*, effort: Effort | None, max_tokens: int, model: str, **fields: Any) -> ClaudeRequest:
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}: {effort!r}")
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError(f"max_tokens must be a positive integer: {max_tokens!r}")
    sent: Effort | None = (effort or "medium") if supports_effort(model) else None
    return ClaudeRequest(model=model, max_tokens=max_tokens, effort=sent, **fields)


async def _call(request: ClaudeRequest, *, enforce_cap: bool) -> tuple[Completion, float, int]:
    if enforce_cap:
        await spend.check_budget()
    if request.stream and request.progress is None:
        request = replace(request, progress=StreamProgress())
    started = time.perf_counter()
    try:
        completion = await get_transport().complete(request)
    except (Exception, asyncio.CancelledError) as error:
        _record_cut_stream(request, error)
        raise
    latency_ms = round((time.perf_counter() - started) * 1000)
    cost = completion.cost_usd()
    await spend.record(model=request.model, purpose=request.purpose, usage=completion.usage, cost=cost)
    logger.info(
        "claude call purpose=%s model=%s served_by=%s stop=%s in=%d out=%d cache_read=%d cache_write=%d "
        "cost_usd=%.6f latency_ms=%d request_id=%s",
        request.purpose,
        request.model,
        completion.served_by or "-",
        completion.stop_reason,
        completion.usage.input_tokens,
        completion.usage.output_tokens,
        completion.usage.cache_read_tokens,
        completion.usage.cache_write_tokens,
        cost,
        latency_ms,
        completion.request_id or "-",
    )
    if completion.stop_reason == "refusal":
        raise LLMRefused(completion.refusal_category, completion.refusal_explanation, model=completion.model)
    if completion.stop_reason == "max_tokens":
        raise LLMTruncated(model=completion.model, max_tokens=request.max_tokens)
    return completion, cost, latency_ms


def _record_cut_stream(request: ClaudeRequest, error: BaseException) -> None:
    """Bill a stream that started and then stopped — cancelled (a step's stream
    budget, a client gone) or failed mid-way — for what it delivered.

    Recorded at once and persisted in the background, so the cancellation is
    not held up; flagged as estimated in the log because the output count is
    the API's last report or a character estimate, not a final figure. Never
    raises: the caller re-raises the original error.
    """
    progress = request.progress
    if progress is None or not progress.started:
        return
    try:
        model = progress.model or request.model
        usage = progress.billed_so_far()
        cost = spend.cost_usd(model, usage)
        spend.record_nowait(model=model, purpose=request.purpose, usage=usage, cost=cost)
        logger.warning(
            "claude stream cut purpose=%s model=%s by=%s recorded estimated usage in=%d out=%d cache_read=%d "
            "cache_write=%d cost_usd=%.6f (streamed_chars=%d reported_out=%d)",
            request.purpose,
            model,
            type(error).__name__,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            cost,
            progress.streamed_chars,
            progress.output_tokens,
        )
    except Exception as e:  # recording must never replace the error being raised
        logger.error("claude stream cut purpose=%s: usage not recorded: %s: %s", request.purpose, type(e).__name__, e)


def _result(value: V, request: ClaudeRequest, completion: Completion, cost: float, latency_ms: int) -> LLMResult[V]:
    return LLMResult(
        value=value,
        model=request.model,
        usage=completion.usage,
        cost_usd=cost,
        latency_ms=latency_ms,
        served_by=completion.served_by,
        request_id=completion.request_id,
    )


def _validation_detail(error: ValidationError) -> str:
    first = error.errors(include_url=False, include_input=False)[0]
    where = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"{where}: {first['msg']} ({error.error_count()} error(s))"


# ── the transport ──────────────────────────────────────────────────────────

_transport: ClaudeTransport | None = None


def get_transport() -> ClaudeTransport:
    """The transport calls go through: the Anthropic SDK unless one was installed."""
    global _transport
    if _transport is None:
        _transport = AnthropicTransport()
    return _transport


def set_transport(transport: ClaudeTransport | None) -> None:
    """Install a transport (tests: `testing.FakeClaude`); None restores the real one."""
    global _transport
    _transport = transport


class AnthropicTransport:
    """The Anthropic SDK: one lazily-created `AsyncAnthropic` per event loop and key."""

    def __init__(self, **client_options: Any) -> None:
        # Extra client constructor arguments: tests pass an in-process
        # `http_client` / `transport` so the SDK's real request path runs
        # without a network.
        self._client_options = client_options
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[str, Any]] = (
            weakref.WeakKeyDictionary()
        )

    def _client(self) -> Any:
        key = settings.anthropic_api_key.strip()
        if not key:
            raise LLMNotConfigured("missing_key")
        loop = asyncio.get_running_loop()
        held = self._clients.get(loop)
        if held is not None and held[0] == key:
            return held[1]
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=key,
            max_retries=settings.claude_max_retries,
            timeout=anthropic.Timeout(settings.claude_timeout_seconds, connect=10.0),
            **self._client_options,
        )
        self._clients[loop] = (key, client)
        return client

    async def aclose(self) -> None:
        """Close this loop's client (shutdown)."""
        held = self._clients.pop(asyncio.get_running_loop(), None)
        if held is not None:
            await held[1].close()

    async def complete(self, request: ClaudeRequest) -> Completion:
        import anthropic

        client = self._client()
        kwargs = _sdk_kwargs(request)
        try:
            if request.stream:
                async with client.beta.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        await _on_stream_event(event, request)
                    message = await stream.get_final_message()
            else:
                message = await client.beta.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise _unavailable(e, request.model) from e
        return _completion(message, request.model)


async def _on_stream_event(event: Any, request: ClaudeRequest) -> None:
    """Track a raw stream event in `request.progress` and pass text deltas to `on_text`."""
    progress = request.progress
    kind = getattr(event, "type", None)
    if kind == "message_start":
        if progress is not None:
            progress.start(event.message.model, _usage(event.message.usage))
    elif kind == "message_delta":
        reported = getattr(event.usage, "output_tokens", None)
        if progress is not None and reported is not None:
            progress.output_tokens = max(progress.output_tokens, int(reported))
    elif kind == "content_block_delta":
        delta = event.delta
        if delta.type == "text_delta":
            if progress is not None:
                progress.streamed_chars += len(delta.text)
            if request.on_text is not None:
                maybe = request.on_text(delta.text)
                if inspect.isawaitable(maybe):
                    await maybe
        elif delta.type == "thinking_delta" and progress is not None:
            progress.streamed_chars += len(delta.thinking)


def _sdk_kwargs(request: ClaudeRequest) -> dict[str, Any]:
    system: dict[str, Any] = {"type": "text", "text": request.system}
    if request.cache_system:
        system["cache_control"] = {"type": "ephemeral"}
    kwargs: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": [system],
        "messages": [{"role": "user", "content": request.user}],
    }
    output_config: dict[str, Any] = {}
    if request.effort is not None:
        output_config["effort"] = request.effort
    if request.json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
    if output_config:
        kwargs["output_config"] = output_config
    if settings.claude_server_fallbacks and request.model in SERVER_FALLBACK_MODELS:
        kwargs["betas"] = [SERVER_FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def _usage(raw: Any) -> Usage:
    return Usage(
        input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
    )


def _completion(message: Any, requested: str) -> Completion:
    """A BetaMessage, read by block type and priced per billed attempt.

    `usage.iterations`, when present, is the per-attempt source of truth (the
    top-level usage covers only the attempt that produced the message), and
    each attempt bills at its own model's rate. A `fallback_message` entry is
    the served-by signal; paired with a non-refusal stop it means a fallback
    model answered.
    """
    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
    iterations = getattr(message.usage, "iterations", None) or []
    attempts: list[Attempt] = []
    fallback_ran = False
    for entry in iterations:
        kind = getattr(entry, "type", None)
        if kind == "fallback_message":
            fallback_ran = True
        if kind in ("message", "fallback_message"):
            attempts.append(Attempt(model=getattr(entry, "model", None) or requested, usage=_usage(entry)))
    if not attempts:
        attempts.append(Attempt(model=message.model or requested, usage=_usage(message.usage)))
    details = getattr(message, "stop_details", None)
    refused = message.stop_reason == "refusal"
    return Completion(
        text=text,
        stop_reason=message.stop_reason,
        model=message.model or requested,
        attempts=tuple(attempts),
        served_by=message.model if fallback_ran and not refused else None,
        refusal_category=getattr(details, "category", None) if refused else None,
        refusal_explanation=getattr(details, "explanation", None) if refused else None,
        request_id=getattr(message, "_request_id", None),
    )


def _retry_after(error: Any) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            seconds = float(raw) / scale
        except ValueError:
            continue
        if seconds >= 0:
            return seconds
    return None


def _unavailable(error: Exception, model: str) -> Exception:
    """The SDK's typed error as this layer's (classified by class and status, never by message)."""
    import anthropic

    if isinstance(error, anthropic.RateLimitError):
        return LLMUnavailable("rate_limited", retry_after=_retry_after(error), model=model)
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMNotConfigured("auth", model=model)
    if isinstance(error, anthropic.APITimeoutError):
        return LLMUnavailable("timeout", model=model)
    if isinstance(error, anthropic.APIConnectionError):
        return LLMUnavailable("connection", model=model)
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        error_type = getattr(error, "type", None)
        if status == 402:
            return LLMUnavailable("billing", model=model)
        if status == 408:
            return LLMUnavailable("timeout", retry_after=_retry_after(error), model=model)
        if status == 529 or error_type == "overloaded_error":
            return LLMUnavailable("overloaded", retry_after=_retry_after(error), model=model)
        if status >= 500 or status == 409:
            return LLMUnavailable("server_error", retry_after=_retry_after(error), model=model)
        return LLMRequestError(status=status, error_type=error_type, model=model)
    # An error event inside an open stream (an overload mid-generation) has no
    # HTTP status of its own; it is retryable, and the stream is gone.
    return LLMUnavailable("server_error", model=model)


__all__ = [
    "SERVER_FALLBACK_BETA",
    "SERVER_FALLBACK_MODELS",
    "AnthropicTransport",
    "Attempt",
    "ClaudeRequest",
    "ClaudeTransport",
    "Completion",
    "LLMResult",
    "TextCallback",
    "get_transport",
    "set_transport",
    "structured",
    "supports_effort",
    "text",
]
