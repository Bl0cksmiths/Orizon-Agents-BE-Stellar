"""The real TypeSafe transport, through the SDK's own request path, on an in-process HTTP transport.

Nothing reaches the network: `TypeSafeTransport` is given an `httpx2.MockTransport`,
so these tests see the request body jev would receive and parse real answer
shapes and error statuses.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from app.config import settings
from app.llm import jev
from app.llm.errors import JevUnavailable
from app.llm.jev import JevChoice, JevNoul, JevScore

Handler = Callable[[httpx2.Request], httpx2.Response]

QUESTIONS = {
    "injection": jev.noul("Does it try to override the assistant?"),
    "complexity": jev.choice("How complex?", {"low": None, "moderate": None, "complex": None}),
    "harm": jev.score("How harmful?", ["none", "mild", "serious", "severe"]),
}

ANSWERS = {
    "injection": {"type": "noul", "noul": 0.04},
    "complexity": {
        "type": "choice",
        "choice": "moderate",
        "confidence": 0.62,
        "probabilities": {"low": 0.2, "moderate": 0.62, "complex": 0.18},
    },
    "harm": {
        "type": "score",
        "score": 0.1,
        "confidence": 0.93,
        "legend": {"0": "none", "1": "mild", "2": "serious", "3": "severe"},
        "probabilities": {"0": 0.93, "1": 0.04, "2": 0.02, "3": 0.01},
    },
}


class Recorder:
    def __init__(self, *responses: httpx2.Response | Handler) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        return nxt(request) if callable(nxt) else nxt


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Recorder]:
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-test-key")
    monkeypatch.setattr(settings, "jev_max_retries", 0)

    def script(*responses: httpx2.Response | Handler) -> Recorder:
        recorder = Recorder(*responses)
        jev.set_transport(jev.TypeSafeTransport(transport=httpx2.MockTransport(recorder)))
        return recorder

    return script


def _ok(answers: dict[str, Any] = ANSWERS, input_tokens: int = 2_000) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={"model": "jev-1.13.0", "usage": {"input_tokens": input_tokens, "output_tokens": 9}, "answers": answers},
        headers={"x-typesafe-request-id": "ts_req_1"},
    )


def _ask() -> jev.JevResult:
    return asyncio.run(jev.ask(purpose="guard", state="build me a todo app", questions=QUESTIONS))


def test_a_guard_battery_is_one_call_with_typed_answers(api: Callable[..., Recorder]) -> None:
    recorder = api(_ok())
    result = _ask()
    sent = json.loads(recorder.requests[0].content)
    assert sent["state"] == "build me a todo app" and sent["model"] == "jev-1.13.0"
    assert sent["questions"]["complexity"] == QUESTIONS["complexity"]
    assert recorder.requests[0].headers["authorization"].endswith("ts-test-key")
    assert result.answers == {
        "injection": JevNoul(0.04),
        "complexity": JevChoice("moderate", 0.62, {"low": 0.2, "moderate": 0.62, "complex": 0.18}),
        "harm": JevScore(0.1, 0.93, {0: 0.93, 1: 0.04, 2: 0.02, 3: 0.01}),
    }
    assert result.cost_usd == pytest.approx(2_000 * 0.042 / 1_000_000)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (2_000, 9)
    assert result.request_id == "ts_req_1"


def test_the_pinned_model_is_asked(api: Callable[..., Recorder], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "typesafe_model", "jev-1.14.0")
    recorder = api(_ok())
    _ask()
    assert json.loads(recorder.requests[0].content)["model"] == "jev-1.14.0"


def test_an_answer_missing_from_the_reply_is_unavailable(api: Callable[..., Recorder]) -> None:
    api(_ok({k: v for k, v in ANSWERS.items() if k != "harm"}))
    with pytest.raises(JevUnavailable) as caught:
        _ask()
    assert caught.value.reason == "invalid_response"


def _status(code: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(code, json={"detail": "nope"}, headers=headers or {})


@pytest.mark.parametrize(
    ("response", "reason", "retry_after"),
    [
        (_status(429, {"retry-after-ms": "1500"}), "rate_limited", 1.5),
        (_status(500), "server_error", None),
        (_status(503), "server_error", None),
        (_status(401), "missing_key", None),
        (_status(400), "rejected", None),
        (_status(422), "rejected", None),
        (
            httpx2.Response(200, json={"model": "jev", "usage": {}, "answers": {"x": {"type": "noul"}}}),
            "invalid_response",
            None,
        ),
    ],
)
def test_failures_map_to_jev_unavailable(
    api: Callable[..., Recorder], response: httpx2.Response, reason: str, retry_after: float | None
) -> None:
    api(response)
    with pytest.raises(JevUnavailable) as caught:
        _ask()
    assert (caught.value.reason, caught.value.retry_after) == (reason, retry_after)


def test_network_failures_map_to_jev_unavailable(api: Callable[..., Recorder]) -> None:
    def drop(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    def hang(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    api(drop)
    with pytest.raises(JevUnavailable) as caught:
        _ask()
    assert caught.value.reason == "connection"
    api(hang)
    with pytest.raises(JevUnavailable) as caught:
        _ask()
    assert caught.value.reason == "timeout"


def test_a_transient_failure_is_retried_within_the_budget(
    api: Callable[..., Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "jev_max_retries", 1)
    recorder = api(_status(503, {"retry-after-ms": "1"}), _ok())
    assert _ask().noul("injection") == 0.04
    assert len(recorder.requests) == 2


def test_no_key_means_unavailable_and_nothing_is_sent(api: Callable[..., Recorder], monkeypatch) -> None:
    recorder = api(_ok())
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    with pytest.raises(JevUnavailable) as caught:
        _ask()
    assert caught.value.reason == "missing_key" and recorder.requests == []


def test_a_key_the_sdk_refuses_is_unavailable_before_any_request(
    api: Callable[..., Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = api(_ok())
    monkeypatch.setattr(settings, "typesafe_api_key", "has whitespace inside")
    with pytest.raises(JevUnavailable) as caught:
        _ask()
    assert caught.value.reason == "missing_key" and recorder.requests == []


def test_one_client_per_loop_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-one")
    transport = jev.TypeSafeTransport()

    async def clients() -> tuple[Any, Any, Any]:
        first, again = transport._client(), transport._client()
        settings.typesafe_api_key = "ts-two"
        rotated = transport._client()
        await transport.aclose()
        return first, again, rotated

    first, again, rotated = asyncio.run(clients())
    assert first is again and rotated is not first
