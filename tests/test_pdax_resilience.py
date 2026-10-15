"""Transport resilience: retry only on transient failures, token-bucket
rate limiting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.pdax import resilience
from app.pdax.errors import PdaxError
from app.pdax.resilience import RateLimiter, is_retryable, with_retries


def _fn(failures: list[PdaxError | None]):
    """Build fn(attempt) that raises the scripted error per call (None = ok)."""
    calls = {"n": 0}

    async def fn(attempt: int) -> str:
        idx = calls["n"]
        calls["n"] += 1
        err = failures[idx] if idx < len(failures) else None
        if err is not None:
            raise err
        return f"ok@{attempt}"

    return fn, calls


def _no_sleep(monkeypatch) -> None:
    async def fake_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(resilience, "asyncio", SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))


def test_is_retryable_policy():
    assert is_retryable(PdaxError("net down")) is True  # transport: no status
    assert is_retryable(PdaxError("throttled", http_status=429)) is True
    assert is_retryable(PdaxError("bad gw", http_status=503)) is True
    assert is_retryable(PdaxError("bad input", http_status=400)) is False
    assert is_retryable(PdaxError("rate", code="OT010032", http_status=200)) is True


def test_no_retry_on_400(monkeypatch):
    _no_sleep(monkeypatch)
    fn, calls = _fn([PdaxError("bad input", http_status=400)])
    with pytest.raises(PdaxError):
        asyncio.run(with_retries(fn, attempts=3))
    assert calls["n"] == 1  # a validation 4xx is never retried


def test_retries_then_succeeds_on_5xx(monkeypatch):
    _no_sleep(monkeypatch)
    fn, calls = _fn([PdaxError("boom", http_status=503), PdaxError("boom", http_status=502), None])
    result = asyncio.run(with_retries(fn, attempts=3))
    assert result == "ok@3"
    assert calls["n"] == 3


def test_retries_exhausted_raises_last(monkeypatch):
    _no_sleep(monkeypatch)
    always = [PdaxError("boom", http_status=503)] * 5
    fn, calls = _fn(always)
    retrying = with_retries(fn, attempts=3)
    with pytest.raises(PdaxError):
        asyncio.run(retrying)
    assert calls["n"] == 3


def test_token_bucket_burst_then_wait(monkeypatch):
    now = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        now[0] += d

    monkeypatch.setattr(resilience, "asyncio", SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))
    limiter = RateLimiter(2.0, 2)  # 2 tokens/sec, burst of 2

    async def run() -> None:
        await limiter.acquire()
        await limiter.acquire()
        assert sleeps == []  # burst capacity absorbs the first two
        await limiter.acquire()  # bucket empty: must wait 1/rate = 0.5s
        assert sleeps == [0.5]

    asyncio.run(run())


def test_token_bucket_refills_over_time(monkeypatch):
    now = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        now[0] += d

    monkeypatch.setattr(resilience, "asyncio", SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))
    limiter = RateLimiter(2.0, 2)

    async def run() -> None:
        await limiter.acquire()
        await limiter.acquire()
        now[0] += 1.0  # 1s elapses → 2 tokens refill (capped at burst)
        await limiter.acquire()
        await limiter.acquire()
        assert sleeps == []

    asyncio.run(run())
