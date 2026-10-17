"""
Transport resilience — client-side rate limiting + retry with backoff.

`RateLimiter` is an async token bucket that smooths bursts under PDAX's rate
limits. `with_retries` re-runs a coroutine on *transient* failures only
(network errors, 429/5xx, PDAX rate-limit code) using exponential backoff with
full jitter — never on 4xx validation errors, which won't succeed on retry.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .. import pacing
from .errors import PdaxError

T = TypeVar("T")

# HTTP statuses worth retrying (transient server / throttling).
RETRYABLE_HTTP = frozenset({429, 502, 503, 504})
# PDAX rate-limit error code.
RATE_LIMIT_CODE = "OT010032"


class RateLimiter:
    """Async token bucket: `rate` tokens/sec, up to `burst` in reserve."""

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = max(0.1, rate)
        self._capacity = max(1, burst)
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self._rate)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


def is_retryable(exc: PdaxError) -> bool:
    if exc.http_status is None:  # transport / network error
        return True
    if exc.http_status in RETRYABLE_HTTP:
        return True
    return exc.code == RATE_LIMIT_CODE


async def with_retries(
    fn: Callable[[int], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.3,
    max_delay: float = 4.0,
) -> T:
    """Run `fn(attempt)` with retries. `fn` receives the 1-based attempt number."""
    last: PdaxError | None = None
    for i in range(attempts):
        try:
            return await fn(i + 1)
        except PdaxError as e:
            last = e
            if i == attempts - 1 or not is_retryable(e):
                raise
            delay = min(max_delay, base_delay * (2**i)) * pacing.jitter(0.5, 1.0)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
