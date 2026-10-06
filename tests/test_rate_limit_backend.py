"""The pluggable rate-limit backend and its in-memory token bucket.

The service runs one Render instance, so an in-process bucket is the whole
truth today; the `RateLimitBackend` protocol is the seam a shared store
(Redis, Postgres) plugs into the day a second instance exists, without any
route or middleware changing.
"""

from __future__ import annotations

import asyncio

import pytest

from app import rate_limit
from app.rate_limit import InMemoryTokenBucket


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _take(bucket: InMemoryTokenBucket, key: str, capacity: int = 3, per: float = 60.0) -> float | None:
    return asyncio.run(bucket.take(key, capacity, per))


def test_a_full_bucket_admits_its_capacity_then_refuses_with_a_wait() -> None:
    clock = _Clock()
    bucket = InMemoryTokenBucket(clock=clock)

    assert [_take(bucket, "k") for _ in range(3)] == [None, None, None]
    wait = _take(bucket, "k")

    # Three per minute refill one token every 20 s.
    assert wait == pytest.approx(20.0)


def test_tokens_refill_at_the_configured_rate() -> None:
    clock = _Clock()
    bucket = InMemoryTokenBucket(clock=clock)
    for _ in range(3):
        _take(bucket, "k")

    clock.now += 20.0
    assert _take(bucket, "k") is None
    assert _take(bucket, "k") is not None


def test_a_bucket_never_holds_more_than_its_capacity() -> None:
    # An idle hour must not bank an hour of burst.
    clock = _Clock()
    bucket = InMemoryTokenBucket(clock=clock)
    _take(bucket, "k")
    clock.now += 3_600.0

    assert [_take(bucket, "k") for _ in range(4)][-1] is not None


def test_keys_are_independent() -> None:
    bucket = InMemoryTokenBucket(clock=_Clock())
    for _ in range(3):
        _take(bucket, "a")

    assert _take(bucket, "a") is not None
    assert _take(bucket, "b") is None


def test_a_zero_capacity_disables_the_limit() -> None:
    bucket = InMemoryTokenBucket(clock=_Clock())

    assert all(_take(bucket, "k", capacity=0) is None for _ in range(50))


def test_the_key_table_is_bounded() -> None:
    # Keys are caller-influenced (an IP, a claimed wallet), so the table has a
    # hard cap with least-recently-used eviction.
    bucket = InMemoryTokenBucket(clock=_Clock(), max_keys=10)
    for i in range(25):
        _take(bucket, f"k{i}")

    assert bucket.size() == 10


def test_the_backend_is_pluggable() -> None:
    calls: list[tuple[str, int, float]] = []

    class _Recorder:
        async def take(self, key: str, capacity: int, per_seconds: float) -> float | None:
            calls.append((key, capacity, per_seconds))
            return None

    previous = rate_limit.get_backend()
    try:
        rate_limit.set_backend(_Recorder())
        assert asyncio.run(rate_limit.get_backend().take("x", 5, 60.0)) is None
    finally:
        rate_limit.set_backend(previous)

    assert calls == [("x", 5, 60.0)]
