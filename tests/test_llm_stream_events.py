"""How one raw stream event is tracked: progress counts and text callbacks.

Pins `_on_stream_event` for every event kind, with and without a progress
tracker and a text callback, so a stream with neither still runs cleanly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from app.llm import claude


def _request(progress: claude.StreamProgress | None = None, on_text: Any = None) -> claude.ClaudeRequest:
    return claude.ClaudeRequest(
        purpose="test",
        model="claude-opus-5-5",
        system="s",
        user="u",
        max_tokens=10,
        effort=None,
        stream=True,
        on_text=on_text,
        progress=progress,
    )


def _event(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _start() -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=7, output_tokens=2, cache_read_input_tokens=3)
    return _event(type="message_start", message=SimpleNamespace(model="claude-opus-5-5", usage=usage))


def _delta(kind: str, **kw: Any) -> SimpleNamespace:
    return _event(type="content_block_delta", delta=SimpleNamespace(type=kind, **kw))


def _feed(request: claude.ClaudeRequest, *events: SimpleNamespace) -> None:
    async def go() -> None:
        for event in events:
            await claude._on_stream_event(event, request)

    asyncio.run(go())


def test_message_start_starts_the_progress() -> None:
    progress = claude.StreamProgress()
    _feed(_request(progress), _start())
    assert (progress.started, progress.model, progress.output_tokens) == (True, "claude-opus-5-5", 2)
    assert (progress.input_usage.input_tokens, progress.input_usage.cache_read_tokens) == (7, 3)


def test_message_delta_keeps_the_highest_reported_output_count() -> None:
    progress = claude.StreamProgress(output_tokens=50)
    _feed(
        _request(progress),
        _event(type="message_delta", usage=SimpleNamespace(output_tokens=40)),
        _event(type="message_delta", usage=SimpleNamespace(output_tokens=None)),
        _event(type="message_delta", usage=SimpleNamespace()),
    )
    assert progress.output_tokens == 50
    _feed(_request(progress), _event(type="message_delta", usage=SimpleNamespace(output_tokens="60")))
    assert progress.output_tokens == 60


def test_text_and_thinking_deltas_are_counted_and_text_is_passed_on() -> None:
    progress = claude.StreamProgress()
    seen: list[str] = []
    _feed(
        _request(progress, on_text=seen.append),
        _delta("text_delta", text="hello"),
        _delta("thinking_delta", thinking="hmm"),
        _delta("input_json_delta", partial_json="{}"),
    )
    assert progress.streamed_chars == len("hello") + len("hmm")
    assert seen == ["hello"]


def test_an_async_text_callback_is_awaited() -> None:
    seen: list[str] = []

    async def on_text(text: str) -> None:
        seen.append(text)

    _feed(_request(on_text=on_text), _delta("text_delta", text="hi"))
    assert seen == ["hi"]


def test_a_stream_with_no_progress_and_no_callback_ignores_every_event() -> None:
    request = _request()
    _feed(
        request,
        _start(),
        _event(type="message_delta", usage=SimpleNamespace(output_tokens=5)),
        _delta("text_delta", text="hello"),
        _delta("thinking_delta", thinking="hmm"),
        _event(type="content_block_stop"),
        SimpleNamespace(),
    )
    assert request.progress is None


def test_text_without_a_callback_is_only_counted() -> None:
    progress = claude.StreamProgress()
    _feed(_request(progress), _delta("text_delta", text="abc"), _event(type="message_stop"))
    assert progress.streamed_chars == 3
