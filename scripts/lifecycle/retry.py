"""Bounded retries with backoff — for reads and idempotent calls ONLY.

Render's free tier sleeps and wakes slowly, and Soroban RPC drops the odd
request, so a read that fails once is worth asking again. A WRITE is not:
`/stellar/submit`, `/orchestrator/execute` and `/disputes/{id}/uphold` may have
taken effect before the connection dropped, and a second attempt is a second
payment, a second workflow or a second refund. Those calls never go through
here; the API client calls them once and turns an unknown answer into
`UnknownOutcome`, which the runner answers by reading state and stopping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

T = TypeVar("T")

# What a retry may be spent on: the service is asleep, overloaded or in a
# deploy, or the request never got an answer. A 4xx other than 429 is an
# answer, and asking again gets the same one.
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
        """`call()`, retried on a transport error or a `RetryableStatus`.

        The last failure is re-raised unchanged, so the caller sees what
        actually went wrong rather than a wrapper that hides it.
        """
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
