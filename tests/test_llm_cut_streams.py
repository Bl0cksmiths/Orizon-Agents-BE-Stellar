"""A stream cut short is still billed, so it is still counted against the cap.

Anthropic bills a stream for what it generated before it was cancelled — by
the worker's stream budget (claude_step.STREAM_BUDGET_SECONDS) or by an error
in the middle. Those calls never complete, so they used to reach the ledger
not at all and the daily cap undercounted. Now the usage known when the
stream stopped is recorded under the same purpose, flagged as estimated, and
the original cancellation or error is re-raised unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from typing import Any

import anthropic  # noqa: F401 — loaded up front, so its import is not timed against the budget
import httpx2
import pytest

from app.config import settings
from app.llm import claude, spend
from app.llm.errors import LLMUnavailable
from app.llm.spend import Usage
from app.llm.testing import FakeClaude

PARTIAL = "<html><body>" + "x" * 688  # 700 characters


def _stream(**kwargs: Any) -> claude.LLMResult[str]:
    return asyncio.run(_stream_async(**kwargs))


async def _stream_async(
    *, budget: float | None = None, on_text: Callable[[str], Any] | None = None, purpose: str = "worker.code.gen"
) -> claude.LLMResult[str]:
    call = claude.text(
        purpose=purpose,
        model="claude-opus-5-5",
        system="Write HTML.",
        user="a page",
        max_tokens=9_000,
        effort="high",
        stream=True,
        on_text=on_text or (lambda delta: None),
    )
    return await (asyncio.wait_for(call, budget) if budget is not None else call)


def _rows(ledger: spend.SpendLedger) -> dict[tuple[str, str], tuple[int, Usage, float]]:
    store = ledger.store
    assert isinstance(store, spend.InMemorySpendStore)
    return {(model, purpose): row for (_day, model, purpose), row in store.rows.items()}


# ── through the fake transport ────────────────────────────────────────────


def test_a_stream_cancelled_by_its_budget_is_recorded_as_estimated_and_still_cancelled(
    fake_claude: FakeClaude, spend_ledger: spend.SpendLedger, caplog: pytest.LogCaptureFixture
) -> None:
    fake_claude.cut_stream(PARTIAL, usage=Usage(input_tokens=2_000, cache_read_tokens=8_000))
    with caplog.at_level(logging.WARNING, logger="app.llm.claude"), pytest.raises(TimeoutError):
        _stream(budget=0.05)
    out = math.ceil(len(PARTIAL) / claude.CHARS_PER_TOKEN)  # 200, rounded up
    calls, usage, cost = _rows(spend_ledger)[("claude-opus-5-5", "worker.code.gen")]
    assert calls == 1 and usage == Usage(input_tokens=2_000, output_tokens=out, cache_read_tokens=8_000)
    assert cost == pytest.approx(spend.cost_usd("claude-opus-5-5", usage))
    assert spend_ledger.snapshot().spent_usd == pytest.approx(cost, abs=1e-6)
    (line,) = [r.getMessage() for r in caplog.records if "estimated" in r.getMessage()]
    assert "worker.code.gen" in line and "CancelledError" in line


def test_a_stream_that_errors_midway_is_recorded_and_the_error_re_raised(
    fake_claude: FakeClaude, spend_ledger: spend.SpendLedger
) -> None:
    fake_claude.cut_stream(PARTIAL, usage=Usage(input_tokens=500), error=LLMUnavailable("overloaded"))
    with pytest.raises(LLMUnavailable) as caught:
        _stream()
    assert caught.value.reason == "overloaded"
    _calls, usage, _cost = _rows(spend_ledger)[("claude-opus-5-5", "worker.code.gen")]
    assert usage.input_tokens == 500 and usage.output_tokens == 200


def test_a_client_error_in_the_text_callback_is_recorded_too(
    fake_claude: FakeClaude, spend_ledger: spend.SpendLedger
) -> None:
    fake_claude.reply(PARTIAL)

    def explode(delta: str) -> None:
        raise RuntimeError("the client went away")

    with pytest.raises(RuntimeError, match="went away"):
        _stream(on_text=explode)
    assert ("claude-opus-5-5", "worker.code.gen") in _rows(spend_ledger)


def test_the_api_s_own_output_count_wins_when_it_is_higher(
    fake_claude: FakeClaude, spend_ledger: spend.SpendLedger
) -> None:
    """Thinking is billed but not streamed as text, so characters undercount it;
    whatever the API itself reported is the floor."""
    fake_claude.cut_stream("short", usage=Usage(input_tokens=100), output_tokens=1_500)
    with pytest.raises(TimeoutError):
        _stream(budget=0.05)
    _calls, usage, _cost = _rows(spend_ledger)[("claude-opus-5-5", "worker.code.gen")]
    assert usage.output_tokens == 1_500


def test_a_stream_cancelled_before_it_started_records_nothing(
    fake_claude: FakeClaude, spend_ledger: spend.SpendLedger
) -> None:
    async def never(request: claude.ClaudeRequest) -> claude.Completion:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fake_claude.complete = never  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        _stream(budget=0.05)
    assert _rows(spend_ledger) == {}


def test_a_completed_stream_is_recorded_once_and_not_estimated(
    fake_claude: FakeClaude, spend_ledger: spend.SpendLedger, caplog: pytest.LogCaptureFixture
) -> None:
    fake_claude.reply(PARTIAL)
    with caplog.at_level(logging.WARNING, logger="app.llm.claude"):
        assert _stream().value == PARTIAL
    calls, usage, _cost = _rows(spend_ledger)[("claude-opus-5-5", "worker.code.gen")]
    assert calls == 1 and usage.output_tokens == 200  # FakeClaude's DEFAULT_USAGE, the reported figure
    assert not [r for r in caplog.records if "estimated" in r.getMessage()]


# ── through the real Anthropic transport ──────────────────────────────────


def _sse(*events: dict[str, Any]) -> httpx2.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _events(*deltas: dict[str, Any]) -> list[dict[str, Any]]:
    start = {
        "id": "msg_cut",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 3_000, "output_tokens": 1, "cache_read_input_tokens": 1_000},
    }
    return [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "t" * 35}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        *deltas,
    ]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Callable[[httpx2.Response], None]:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "claude_max_retries", 0)

    def script(response: httpx2.Response) -> None:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: response))
        claude.set_transport(claude.AnthropicTransport(http_client=http))

    return script


def test_the_sdk_stream_cut_by_the_budget_records_what_it_delivered(
    api: Callable[[httpx2.Response], None], spend_ledger: spend.SpendLedger
) -> None:
    text = {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "a" * 70}}
    api(_sse(*_events(text, text)))
    seen: list[str] = []

    async def slow(delta: str) -> None:
        seen.append(delta)
        await asyncio.sleep(10)  # the reader stalls after the first delta: the budget cuts it

    with pytest.raises(TimeoutError):
        _stream(budget=1.0, on_text=slow)
    assert seen == ["a" * 70]
    _calls, usage, _cost = _rows(spend_ledger)[("claude-opus-5-5", "worker.code.gen")]
    # message_start's input and cache tokens; output estimated from the 35
    # thinking + 70 text characters seen, 105 / 3.5 = 30.
    assert usage == Usage(input_tokens=3_000, output_tokens=30, cache_read_tokens=1_000)


def test_the_sdk_stream_s_reported_output_count_is_used_when_it_arrived(
    api: Callable[[httpx2.Response], None], spend_ledger: spend.SpendLedger
) -> None:
    text = {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "b" * 7}}
    usage = {
        "type": "message_delta",
        "delta": {"stop_reason": None, "stop_sequence": None},
        "usage": {"output_tokens": 900},
    }
    api(_sse(*_events(text, usage, text)))
    calls = 0

    def second_delta_fails(delta: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("the client went away")

    with pytest.raises(RuntimeError):
        _stream(on_text=second_delta_fails)
    _calls, recorded, _cost = _rows(spend_ledger)[("claude-opus-5-5", "worker.code.gen")]
    assert recorded.output_tokens == 900


def test_the_character_estimate_rounds_up() -> None:
    """Conservative: a part-token of output is a whole token, so the cap trips early, never late."""
    progress = claude.StreamProgress()
    progress.start("claude-opus-5-5", Usage(input_tokens=10, output_tokens=1, cache_write_tokens=5))
    progress.streamed_chars = 8  # 8 / 3.5 = 2.29
    assert progress.billed_so_far() == Usage(input_tokens=10, output_tokens=3, cache_write_tokens=5)
