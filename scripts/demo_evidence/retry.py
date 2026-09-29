"""Bounded retries with backoff. Every call the evidence tool makes is a read.

The pattern is the lifecycle harness's (`scripts/lifecycle/retry.py`),
re-stated rather than imported so the tools can change independently. The
public Soroban RPC and Horizon drop the odd request, so a read that fails once
is worth asking again — a bounded number of times, never forever. A 4xx other
than 429 is an answer, and asking again gets the same one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class RetryableStatus(Exception):
    """An HTTP answer worth asking again, carrying the server's Retry-After."""

    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 2.0
    max_delay: float = 20.0
    sleep: Callable[[float], None] = time.sleep

    def run(self, call: Callable[[], T]) -> T:
        """`call()`, retried on a transport error or a `RetryableStatus`; the last failure is re-raised."""
        delay = self.base_delay
        for attempt in range(1, self.attempts + 1):
            try:
                return call()
            except (httpx.TransportError, RetryableStatus) as exc:
                if attempt == self.attempts:
                    raise
                wait = delay
                if isinstance(exc, RetryableStatus) and exc.retry_after is not None:
                    wait = exc.retry_after
                self.sleep(min(wait, self.max_delay))
                delay = min(delay * 2, self.max_delay)
        raise AssertionError("unreachable")  # pragma: no cover


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None
