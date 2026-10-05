"""Per-route rate limits for the write and expensive endpoints, on a pluggable backend.

`RateLimitMiddleware` (app/security.py) is one sliding-window budget over
every route alike, sized for a dashboard polling reads. That is the wrong
budget for a route whose every call costs something real — a paid run, an
RPC simulation, a chain scan, an outbound probe — so those routes take a
budget of their own here, on top of it:

  * per CLIENT (`security.client_key`, the same key the global limiter and the
    access log use, with the same TRUSTED_PROXY_HOPS caveat: at the default of
    0 every caller behind one edge address shares it, so the numbers below are
    sized as service-wide ceilings until the hop count is tuned);
  * and, where the request names one, per WALLET — the G-address in the body
    that the route builds for, pays from, or signs as. That is the fairness
    half: one wallet cannot spend everyone's budget for a route, however many
    addresses it sends from. The wallet is read before the route has verified
    it, so anyone naming a wallet spends that wallet's budget too; the budgets
    are sized well above what one honest wallet does in a minute so that
    costs a griefer far more than it costs the wallet.

The backend is a `RateLimitBackend`: an in-process token bucket by default —
the deployment is one uvicorn worker on one instance, so that is the whole
truth — and the seam a shared store plugs into before a second instance
exists (`set_backend`). Nothing above the backend changes when it does.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol


class RateLimitBackend(Protocol):
    async def take(self, key: str, capacity: int, per_seconds: float) -> float | None:
        """Spend one token of `key`'s bucket: None if admitted, else seconds until one refills.

        The bucket holds at most `capacity` tokens and refills `capacity` every
        `per_seconds`. A capacity of 0 or less disables the limit.
        """
        ...


class InMemoryTokenBucket:
    """A token bucket per key, in this process. Bounded, lock-free.

    Bounded because the keys are caller-influenced — a client address, a
    claimed wallet — so the table has a hard cap with least-recently-used
    eviction, like every other caller-keyed map here (`app/stellar/cache.py`).
    Evicting a key forgets its spend, which is the generous direction.

    Lock-free for the reason `app/state.py` gives: `take` has no `await` in
    its body, so on one event loop no two calls can interleave inside it.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        # key -> (tokens, last refill time)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    async def take(self, key: str, capacity: int, per_seconds: float) -> float | None:
        if capacity <= 0 or per_seconds <= 0:
            return None
        now = self._clock()
        rate = capacity / per_seconds
        tokens, last = self._buckets.pop(key, (float(capacity), now))
        tokens = min(float(capacity), tokens + (now - last) * rate)
        admitted = tokens >= 1.0
        if admitted:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return None if admitted else (1.0 - tokens) / rate

    def size(self) -> int:
        return len(self._buckets)


_backend: RateLimitBackend = InMemoryTokenBucket()


def get_backend() -> RateLimitBackend:
    return _backend


def set_backend(backend: RateLimitBackend) -> None:
    """Swap the store every per-route limit spends from (a shared one, or a test's)."""
    global _backend
    _backend = backend
