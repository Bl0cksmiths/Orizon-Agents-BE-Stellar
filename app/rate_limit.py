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

import json
import logging
import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .security import client_key, request_id_var

logger = logging.getLogger(__name__)


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


# ── the policies ────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutePolicy:
    """One budget: which requests it covers, and how many a minute each key gets."""

    name: str
    methods: frozenset[str]
    path: re.Pattern[str]
    per_client: int
    per_wallet: int = 0
    # JSON body fields that may hold the request's wallet (a G-address).
    wallet_fields: tuple[str, ...] = ()


_WINDOW_SECONDS = 60.0
_WALLET = re.compile(r"^G[A-Z2-7]{55}$")


def _policy(
    name: str, methods: set[str], path: str, per_client: int, per_wallet: int = 0, wallet_fields: tuple[str, ...] = ()
) -> RoutePolicy:
    return RoutePolicy(name, frozenset(methods), re.compile(path), per_client, per_wallet, wallet_fields)


# Per minute. Sized as SERVICE-WIDE ceilings, because at TRUSTED_PROXY_HOPS=0
# every browser behind the frontend's proxy is one client (see the module
# docstring); each is still several times what a busy demo does.
POLICIES: tuple[RoutePolicy, ...] = (
    # A paid or simulated run: every step a dispatch, some an LLM call.
    _policy("execute", {"POST"}, r"/api/orchestrator/execute", 30, 6, ("payer",)),
    # Each build is a Soroban simulation (and the reclaim one a chain read).
    _policy("stellar_build", {"POST"}, r"/api/stellar/build/[a-z-]+", 60, 20, ("payer", "owner")),
    _policy("stellar_submit", {"POST"}, r"/api/stellar/submit", 30),
    # A full registry scan, however single-flight it is inside.
    _policy("registry_sync", {"POST"}, r"/api/stellar/agents/sync", 6),
    # Challenge mints hold a slot of a shared budget for minutes; a bind or an
    # unbind reads the chain and writes the store.
    _policy("binding", {"POST", "DELETE"}, r"/api/agents/[^/]+/(bind|bind/challenge|unbind/challenge)", 30),
    _policy("disputes", {"POST"}, r"/api/disputes(/challenge|/read-challenge|/read-grant)?", 30, 10, ("payer",)),
    _policy("x402", {"POST"}, r"/api/payments/x402", 30),
    # Reads that reach outside: an outbound probe of an operator's endpoint,
    # and a DNS resolution of a caller's URL.
    _policy("readiness", {"GET"}, r"/api/agents/[^/]+/readiness", 30),
    _policy("endpoint_check", {"GET"}, r"/api/agents/bind/endpoint-check", 30),
)


def policy_for(method: str, path: str) -> RoutePolicy | None:
    """The budget a request falls under, if any. Read per request, so a test or a
    deployment patching `POLICIES` takes effect at once."""
    for policy in POLICIES:
        if method in policy.methods and policy.path.fullmatch(path):
            return policy
    return None


# ── the middleware ──────────────────────────────────────────────

# The most body this layer will hold to find a wallet. Every route with a
# wallet field takes a small JSON body; anything bigger is passed through
# unread (and the route, or BodyLimitMiddleware, answers it).
SNIFF_LIMIT_BYTES = 16_384

Receive = Callable[[], Awaitable[dict[str, Any]]]


async def _sniff(scope: dict[str, Any], receive: Receive) -> tuple[bytes | None, Receive]:
    """Read the body (up to SNIFF_LIMIT_BYTES) and a `receive` that replays it.

    Returns None for the body when it was too big to hold, or did not arrive
    whole; the replay still hands the route every byte, read or not.
    """
    declared = dict(scope.get("headers") or []).get(b"content-length")
    try:
        if declared is not None and int(declared) > SNIFF_LIMIT_BYTES:
            return None, receive
    except ValueError:
        return None, receive
    held: deque[dict[str, Any]] = deque()
    size = 0
    complete = False
    while True:
        message = await receive()
        held.append(message)
        if message["type"] != "http.request":
            break
        size += len(message.get("body", b""))
        if not message.get("more_body", False):
            complete = True
            break
        if size > SNIFF_LIMIT_BYTES:
            break
    body = b"".join(m.get("body", b"") for m in held if m["type"] == "http.request") if complete else None

    async def replay() -> dict[str, Any]:
        if held:
            return held.popleft()
        return await receive()

    return body, replay


def _wallet(body: bytes, fields: tuple[str, ...]) -> str | None:
    """The first well-formed G-address among `fields` of a JSON object body.

    Only a well-formed address becomes a key, so junk in the field cannot mint
    a bucket per value; the route's own validation answers junk.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and _WALLET.fullmatch(value):
            return value
    return None


async def _refuse(send: Any, wait_seconds: float) -> None:
    """429 in the service's error envelope — the same body the global limiter sends."""
    body = json.dumps(
        {
            "detail": "rate_limited",
            "error": {
                "code": "rate_limited",
                "message": "too many requests to this route; retry after the indicated delay",
                "request_id": request_id_var.get(),
            },
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(max(1, math.ceil(wait_seconds))).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RouteRateLimitMiddleware:
    """Spend the matching `POLICIES` budget, per client then per wallet (pure ASGI).

    Registered inside the global limiter and outside BodyLimitMiddleware: a
    request the global limiter refused never spends a route budget, and the
    body this layer holds is replayed through the body limiter, which still
    meters every byte. Preflights are never counted.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Any) -> None:
        method = scope.get("method", "")
        policy = policy_for(method, scope.get("path", "")) if scope["type"] == "http" else None
        if policy is None or method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        backend = get_backend()
        wait = await backend.take(f"{policy.name}:client:{client_key(scope)}", policy.per_client, _WINDOW_SECONDS)
        if wait is not None:
            logger.warning("route rate limit: policy=%s scope=client", policy.name)
            await _refuse(send, wait)
            return
        if policy.per_wallet > 0 and policy.wallet_fields:
            body, receive = await _sniff(scope, receive)
            wallet = _wallet(body, policy.wallet_fields) if body is not None else None
            if wallet is not None:
                wait = await backend.take(f"{policy.name}:wallet:{wallet}", policy.per_wallet, _WINDOW_SECONDS)
                if wait is not None:
                    logger.warning("route rate limit: policy=%s scope=wallet wallet=%s", policy.name, wallet)
                    await _refuse(send, wait)
                    return
        await self.app(scope, receive, send)
