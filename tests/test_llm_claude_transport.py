"""The real Anthropic transport, through the SDK's own request path, on an in-process HTTP transport.

Nothing reaches the network: `AnthropicTransport` is given an SDK client whose
HTTP transport is an `httpx2.MockTransport`, so these tests see the exact
request body the API would receive (effort, structured-output schema, cache
breakpoint, fallback beta) and parse real response shapes — refusals,
truncation, server-side fallbacks, streams and error statuses.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.config import settings
from app.llm import claude, spend
from app.llm.errors import (
    LLMNotConfigured,
    LLMRefused,
    LLMRequestError,
    LLMTruncated,
    LLMUnavailable,
)
from app.llm.spend import Usage

Handler = Callable[[httpx2.Request], httpx2.Response]


class Plan(BaseModel):
    steps: list[str]


def _message(
    *,
    model: str = "claude-opus-5-5",
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": '{"steps": ["a", "b"]}'}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0},
    }


class Recorder:
    """An in-process Anthropic API: answers from `responses` in order and keeps every request."""

    def __init__(self, *responses: httpx2.Response | Handler) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        return nxt(request) if callable(nxt) else nxt

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Recorder]:
    """Install the real transport on a recorder; returns a function that scripts it."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "claude_max_retries", 0)

    def script(*responses: httpx2.Response | Handler) -> Recorder:
        recorder = Recorder(*responses)
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(recorder))
        claude.set_transport(claude.AnthropicTransport(http_client=http))
        return recorder

    return script


def _ok(payload: dict[str, Any], headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(200, json=payload, headers={"request-id": "req_test", **(headers or {})})


def _plan(model: str = "claude-opus-5-5", **kwargs: Any) -> claude.LLMResult[Plan]:
    return asyncio.run(
        claude.structured(
            purpose="planner",
            model=model,
            system="Plan the work.",
            user="build a todo app",
            schema=Plan,
            max_tokens=16_000,
            **kwargs,
        )
    )


def test_a_structured_opus_call_sends_effort_schema_cache_and_fallbacks(api: Callable[..., Recorder]) -> None:
    recorder = api(_ok(_message(usage={"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 4000})))
    result = _plan(effort="high")
    assert result.value == Plan(steps=["a", "b"])
    sent = recorder.body()
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] == 16_000
    assert sent["system"] == [{"type": "text", "text": "Plan the work.", "cache_control": {"type": "ephemeral"}}]
    assert sent["messages"] == [{"role": "user", "content": "build a todo app"}]
    assert sent["output_config"]["effort"] == "high"
    schema = sent["output_config"]["format"]
    assert schema["type"] == "json_schema" and schema["schema"]["additionalProperties"] is False
    assert sent["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in recorder.requests[0].headers["anthropic-beta"]
    # Never sent: thinking (always-on adaptive on Opus 5.5), forced tool_choice, a prefill.
    assert "thinking" not in sent and "tool_choice" not in sent
    # 1000 in at $4 + 500 out at $20 + 4000 cache reads at $0.20, per million.
    assert result.usage == Usage(input_tokens=1000, output_tokens=500, cache_read_tokens=4000)
    assert result.cost_usd == pytest.approx(0.0148)
    assert result.request_id == "req_test"


def test_haiku_gets_no_effort_and_no_fallbacks(api: Callable[..., Recorder]) -> None:
    recorder = api(_ok(_message(model="claude-haiku-4-5")))
    _plan(model="claude-haiku-4-5", effort="low")
    sent = recorder.body()
    assert "effort" not in sent["output_config"] and "fallbacks" not in sent
    assert "anthropic-beta" not in recorder.requests[0].headers


def test_the_dated_id_the_api_serves_haiku_under_is_priced_as_haiku(api: Callable[..., Recorder]) -> None:
    """The API answers a `claude-haiku-4-5` request as `claude-haiku-4-5-20251001`."""
    api(_ok(_message(model="claude-haiku-4-5-20251001", usage={"input_tokens": 1000, "output_tokens": 500})))
    result = _plan(model="claude-haiku-4-5")
    # $1/MTok in + $5/MTok out — not the highest known rate.
    assert result.cost_usd == pytest.approx(0.0035)


def test_fallbacks_can_be_switched_off(api: Callable[..., Recorder], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "claude_server_fallbacks", False)
    recorder = api(_ok(_message()))
    _plan()
    assert "fallbacks" not in recorder.body()


def test_a_server_side_fallback_is_served_by_and_priced_per_attempt(api: Callable[..., Recorder]) -> None:
    api(
        _ok(
            _message(
                model="claude-opus-5",
                content=[
                    {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}},
                    {"type": "text", "text": '{"steps": ["x"]}'},
                ],
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "iterations": [
                        {
                            "type": "message",
                            "model": "claude-opus-5-5",
                            "input_tokens": 1000,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                        {
                            "type": "fallback_message",
                            "model": "claude-opus-5",
                            "input_tokens": 1000,
                            "output_tokens": 100,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    ],
                },
            )
        )
    )
    result = _plan()
    assert result.value == Plan(steps=["x"]) and result.served_by == "claude-opus-5"
    # Declined attempt on Opus 5.5 ($4/MTok in) + serving attempt on Opus 5 ($5 in, $25 out).
    assert result.cost_usd == pytest.approx(0.004 + 0.005 + 0.0025)
    assert result.usage == Usage(input_tokens=2000, output_tokens=100)


def test_a_refusal_is_read_before_the_content(api: Callable[..., Recorder]) -> None:
    api(
        _ok(
            _message(
                content=[],
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "exploit development"},
                usage={"input_tokens": 800, "output_tokens": 0},
            )
        )
    )
    with pytest.raises(LLMRefused) as caught:
        _plan()
    assert (caught.value.category, caught.value.explanation) == ("cyber", "exploit development")
    assert spend.snapshot().spent_usd == pytest.approx(0.0032)


def test_a_reply_cut_at_max_tokens_is_not_parsed(api: Callable[..., Recorder]) -> None:
    api(_ok(_message(content=[{"type": "text", "text": '{"steps": ["a'}], stop_reason="max_tokens")))
    with pytest.raises(LLMTruncated):
        _plan()


def test_thinking_and_text_blocks_are_read_by_type(api: Callable[..., Recorder]) -> None:
    api(
        _ok(
            _message(
                content=[
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": "Hello, "},
                    {"type": "text", "text": "world."},
                ]
            )
        )
    )
    result = asyncio.run(
        claude.text(purpose="worker.writer", model="claude-opus-5-5", system="s", user="u", max_tokens=1000)
    )
    assert result.value == "Hello, world."


def _sse(*events: dict[str, Any]) -> httpx2.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def test_a_streamed_reply_reaches_the_callback_and_the_final_message(api: Callable[..., Recorder]) -> None:
    start = _message(content=[], usage={"input_tokens": 50, "output_tokens": 1})
    start["stop_reason"] = None
    recorder = api(
        _sse(
            {"type": "message_start", "message": start},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "<html>"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "</html>"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 900},
            },
            {"type": "message_stop"},
        )
    )
    seen: list[str] = []
    result = asyncio.run(
        claude.text(
            purpose="worker.code.gen",
            model="claude-opus-5-5",
            system="Write HTML.",
            user="a page",
            max_tokens=64_000,
            effort="high",
            stream=True,
            on_text=seen.append,
        )
    )
    assert seen == ["<html>", "</html>"] and result.value == "<html></html>"
    assert result.usage.output_tokens == 900
    assert recorder.body()["stream"] is True


def _status(code: int, error_type: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        code, json={"type": "error", "error": {"type": error_type, "message": "nope"}}, headers=headers or {}
    )


@pytest.mark.parametrize(
    ("response", "error", "reason", "retry_after"),
    [
        (_status(429, "rate_limit_error", {"retry-after": "7"}), LLMUnavailable, "rate_limited", 7.0),
        (_status(529, "overloaded_error"), LLMUnavailable, "overloaded", None),
        (_status(500, "api_error"), LLMUnavailable, "server_error", None),
        (_status(402, "billing_error"), LLMUnavailable, "billing", None),
        (_status(401, "authentication_error"), LLMNotConfigured, "auth", None),
        (_status(403, "permission_error"), LLMNotConfigured, "auth", None),
    ],
)
def test_error_statuses_map_to_typed_errors(
    api: Callable[..., Recorder], response: httpx2.Response, error: type[Exception], reason: str, retry_after: Any
) -> None:
    api(response)
    with pytest.raises(error) as caught:
        _plan()
    assert isinstance(caught.value, LLMUnavailable)
    assert (caught.value.reason, caught.value.retry_after) == (reason, retry_after)


@pytest.mark.parametrize("code", [400, 404, 413])
def test_a_rejected_request_is_a_request_error_not_an_outage(api: Callable[..., Recorder], code: int) -> None:
    api(_status(code, "invalid_request_error"))
    with pytest.raises(LLMRequestError) as caught:
        _plan()
    assert caught.value.status == code and caught.value.error_type == "invalid_request_error"


def test_a_dropped_connection_is_unavailable(api: Callable[..., Recorder]) -> None:
    def drop(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    api(drop)
    with pytest.raises(LLMUnavailable) as caught:
        _plan()
    assert caught.value.reason == "connection"


def test_a_timeout_is_unavailable(api: Callable[..., Recorder]) -> None:
    def hang(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    api(hang)
    with pytest.raises(LLMUnavailable) as caught:
        _plan()
    assert caught.value.reason == "timeout"


def test_the_sdk_retries_an_overload_before_giving_up(
    api: Callable[..., Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "claude_max_retries", 1)
    recorder = api(_status(529, "overloaded_error", {"retry-after-ms": "1"}), _ok(_message()))
    assert _plan().value == Plan(steps=["a", "b"])
    assert len(recorder.requests) == 2


def test_no_key_means_not_configured_and_nothing_is_sent(api: Callable[..., Recorder], monkeypatch) -> None:
    recorder = api(_ok(_message()))
    monkeypatch.setattr(settings, "anthropic_api_key", "  ")
    with pytest.raises(LLMNotConfigured) as caught:
        _plan()
    assert caught.value.reason == "missing_key" and recorder.requests == []
    assert spend.snapshot().spent_usd == 0.0


def test_one_client_per_loop_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-one")
    transport = claude.AnthropicTransport()

    async def clients() -> tuple[Any, Any, Any]:
        first, again = transport._client(), transport._client()
        settings.anthropic_api_key = "sk-ant-two"  # a rotated key gets a client of its own
        rotated = transport._client()
        await transport.aclose()
        return first, again, rotated

    first, again, rotated = asyncio.run(clients())
    assert first is again and rotated is not first
    assert isinstance(first, anthropic.AsyncAnthropic) and first.max_retries == settings.claude_max_retries
