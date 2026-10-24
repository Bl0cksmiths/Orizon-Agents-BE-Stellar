"""
When this API last answered a request somebody meant — so background work can
stop while nobody is using it.

The dashboard's read snapshots are rebuilt in the background (snapshots.py),
and two of those builds read Postgres. Neon suspends a compute only after a
fixed five minutes without a query, so a rebuild every 30 s kept the database
running around the clock on an instance that nobody was looking at, until the
free plan's compute quota ran out. This module is the switch for that: the
keep-warm schedules that read the database pause once no meaningful request
has arrived for KEEP_WARM_IDLE_PAUSE_SECONDS, and the next one resumes them.

"Meaningful" is a request to the API proper: under /api/, with a method that
reads or writes. Liveness and readiness probes, the docs and the bare root are
answered for machines (an uptime pinger must not keep the database awake),
and HEAD/OPTIONS are a pinger's or a browser's preflight. The middleware sits
innermost (app/main.py), so a request the origin lock or a rate limiter
refused is not counted either.

Boot counts as activity: a process that has just started — usually because a
request woke it — keeps its schedules running for one full idle window, which
is what fills the snapshots after a restart.

Single event loop, like the snapshots it gates: a float written and read on
the loop thread.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from ..config import settings

# Requests to these are not anybody using the API: `/api/health` is the
# liveness probe re-served under the proxy's prefix (routers/health.py).
IGNORED_PATHS = frozenset({"/api/health"})

# The methods a client uses the API with. HEAD is a pinger's, OPTIONS a
# preflight's.
COUNTED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

API_PREFIX = "/api/"


def is_meaningful(scope: MutableMapping[str, Any]) -> bool:
    """Whether this ASGI request counts as somebody using the API."""
    if scope.get("type") != "http" or scope.get("method") not in COUNTED_METHODS:
        return False
    path = scope.get("path", "")
    return isinstance(path, str) and path.startswith(API_PREFIX) and path not in IGNORED_PATHS


class ActivityTracker:
    """The monotonic time of the last meaningful request; boot counts as one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._last = clock()

    def note(self) -> None:
        """Record a meaningful request, now."""
        self._last = self.clock()

    def seconds_since_last_request(self) -> float:
        return max(0.0, self.clock() - self._last)

    def idle(self) -> bool:
        """Whether no meaningful request has arrived for KEEP_WARM_IDLE_PAUSE_SECONDS."""
        return self.seconds_since_last_request() >= settings.keep_warm_idle_pause_seconds


# The process's tracker. A test replaces it with one on a clock it controls.
tracker = ActivityTracker()


def note_request() -> None:
    tracker.note()


def idle() -> bool:
    return tracker.idle()


ASGIApp = Callable[[MutableMapping[str, Any], Any, Any], Awaitable[None]]


class ActivityMiddleware:
    """Pure-ASGI: notes every meaningful request before passing it on.

    Noted on arrival rather than completion, so a long request (a workflow
    stream) resumes the schedules at once, and nothing about the response is
    read or changed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if is_meaningful(scope):
            tracker.note()
        await self.app(scope, receive, send)
