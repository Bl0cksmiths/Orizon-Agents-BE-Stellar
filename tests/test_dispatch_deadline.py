"""The dispatch deadline — story 2.02 (ADR 0004 D2).

`httpx` has no total-request timeout. `httpx.Timeout(110.0, connect=5.0)`
resolves to `read=write=pool=110`, and `read` is only the IDLE GAP BETWEEN
READS — so an operator that emits one byte every 109 s never trips it and runs
until `execution_svc`'s 120 s step ceiling. That is precisely the "ambiguous
outer timeout" the worker's docstring and ADR 0001 promised could not happen.

These tests pin the real monotonic deadline that makes the promise true. The
trickling-operator case is the one that mattered: it passes trivially against a
naive implementation that only sets httpx timeouts, so without it a regression
would be invisible.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.agents.workers import external_http as eh


def _worker(handler, endpoint: str = "https://operator.example/run") -> eh.ExternalHttpWorker:
    return eh.ExternalHttpWorker(
        "ext_slow",
        "external.ext_slow",
        endpoint,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture()
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> float:
    """A 1 s budget so the suite stays fast; the mechanism is identical."""
    monkeypatch.setattr(eh, "DISPATCH_DEADLINE_SECONDS", 1.0)
    return 1.0


def test_a_trickling_operator_is_cut_off_at_the_deadline(short_deadline: float) -> None:
    # One byte at a time, forever, with no idle gap long enough to trip any
    # httpx read timeout. Only a real deadline stops this.
    async def trickle(_request: httpx.Request) -> httpx.Response:
        async def gen():
            while True:
                await asyncio.sleep(0.05)
                yield b" "

        return httpx.Response(200, content=gen())

    async def go() -> None:
        started = time.monotonic()
        with pytest.raises(eh.ExternalDispatchError, match="no response within"):
            await _worker(trickle).run("x", "y")
        elapsed = time.monotonic() - started
        # Bounded by the deadline, not by the 120 s step ceiling above us.
        assert elapsed < short_deadline * 4

    asyncio.run(go())


def test_a_slow_operator_is_not_retried(short_deadline: float) -> None:
    # The request was on the wire and may have executed, so a retry would risk
    # billing the operator twice for one job. Timeout is a failure, not a
    # reason to try again.
    calls = {"n": 0}

    async def slow(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(5)
        return httpx.Response(200, json={"summary": "too late"})

    async def go() -> None:
        with pytest.raises(eh.ExternalDispatchError):
            await _worker(slow).run("x", "y")

    asyncio.run(go())
    assert calls["n"] == 1


def test_a_prompt_operator_is_unaffected(short_deadline: float) -> None:
    async def prompt(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"summary": "done"})

    async def go() -> dict:
        return await _worker(prompt).run("x", "y")

    assert asyncio.run(go())["summary"] == "done"
