"""Origin lock — /api/* answers our frontend, and nobody who walks around it (ADR 0017).

The public entry point is https://orizons.xyz: Vercel's firewall sits in front
of it, and its Next.js middleware forwards every `/api/*` request here with
X-Frontend-Proxy-Token. Render's own hostname is just as reachable, though,
and a caller who uses it skips the firewall entirely. This layer closes that
door: a request under `/api/` that does not carry the token (checked by
`security.is_frontend`, in constant time) is one the lock would refuse.

What happens to it is ORIGIN_LOCK_MODE's call:

  * `off` — nothing; every request is served as before.
  * `log` — served, but counted and logged, so /readiness and the log show
    who would be locked out before anyone is. The default, and the rollout's
    first step.
  * `enforce` — answered 403 `origin_forbidden` in the unified error envelope.
    `config` refuses to boot this mode without a token, since then nothing
    could pass.

Never locked: anything outside `/api/` (the probes, the root ping, the docs,
which DOCS_ENABLED governs), CORS preflights, and the allowlist below.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .security import client_identity, header_secret_matches, is_frontend, request_id_var

logger = logging.getLogger(__name__)

# Only what the frontend proxies is locked. Everything else on this host is a
# probe, the root ping or the docs, none of which the frontend forwards.
API_PREFIX = "/api/"

_OPERATOR_KEY_HEADER = b"x-api-key"

# The refusal, in the unified error envelope. The message names the way in and
# nothing about why: not which header was missing, not the mode, never a
# header value — so a refusal teaches a prober nothing it could use.
REFUSAL_CODE = "origin_forbidden"
REFUSAL_MESSAGE = "This API is only available through orizons.xyz."


def template_regex(template: str) -> re.Pattern[str]:
    """A route template as a regex to full-match a path: each `{param}` is exactly one segment.

    The same reading Starlette gives a plain `{param}`, so an exemption covers
    precisely the paths its route serves — `/api/disputes/{dispute_id}/uphold`
    admits `/api/disputes/d_1/uphold` and never `/api/disputes/d/1/uphold`.
    """
    parts = re.split(r"(\{[^/{}]+\})", template)
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts))


@dataclass(frozen=True)
class Exemption:
    """One route a caller may reach without our frontend: a method and an exact template.

    `keyed` routes are open to a direct caller only while it presents the
    operator's X-API-Key: the route checks that key itself, and requiring it
    here too keeps a deployment whose API_KEY is empty — where
    `require_api_key` waves everyone through — from leaving those routes open
    to the world behind the lock's back.
    """

    method: str
    template: str
    keyed: bool
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pattern", template_regex(self.template))

    def admits(self, scope: dict) -> bool:
        if scope.get("method") != self.method or self.pattern.fullmatch(str(scope.get("path", ""))) is None:
            return False
        if not self.keyed:
            return True
        supplied = dict(scope.get("headers") or []).get(_OPERATOR_KEY_HEADER)
        return header_secret_matches(
            supplied.decode("latin-1") if supplied is not None else None,
            settings.api_key,
        )


def _open(method: str, template: str) -> Exemption:
    return Exemption(method, template, keyed=False)


def _keyed(method: str, template: str) -> Exemption:
    return Exemption(method, template, keyed=True)


# Everyone who may reach /api/* without our frontend, and why. Exact templates,
# never prefixes, so nothing new is exempt by being added next to one of these.
# tests/test_origin_lock.py pins the keyed list to the operations the OpenAPI
# says are behind the operator key, so a newly keyed route — or a key removed —
# fails a test instead of drifting.
EXEMPTIONS: tuple[Exemption, ...] = (
    # PDAX's own servers deliver deposit and withdrawal events here. They cannot
    # hold our frontend's token; the receiver authenticates every delivery by
    # its HMAC signature (PDAX_WEBHOOK_SECRET) instead, and caps the body.
    _open("POST", "/api/pdax/webhooks/receive"),
    # The liveness probe re-served under /api for monitors polling through the
    # proxy. It says nothing /health does not already say to anyone.
    _open("GET", "/api/health"),
    # Dispute adjudication — `require_adjudicator`. An operator decides these
    # from a terminal, not from the console.
    _keyed("POST", "/api/disputes/{dispute_id}/uphold"),
    _keyed("POST", "/api/disputes/{dispute_id}/reject"),
    # Reputation cache invalidation — `require_operator_key`. Called directly by
    # scripts/uphold_dispute.py after a rating lands.
    _keyed("POST", "/api/stellar/reputation/{agent_id}/invalidate"),
    # Backend-signed contract calls — `require_api_key` / `require_seal_key`.
    _keyed("POST", "/api/stellar/server/charge"),
    _keyed("POST", "/api/stellar/server/seal"),
    # PDAX trading, funding and ramps — the `secured` router's `require_api_key`.
    # Money-moving and account-revealing, driven by operator tooling.
    _keyed("GET", "/api/pdax/health/deep"),
    _keyed("GET", "/api/pdax/trade/price"),
    _keyed("GET", "/api/pdax/trade/price/v2"),
    _keyed("POST", "/api/pdax/trade/quote"),
    _keyed("POST", "/api/pdax/trade/quote/v2"),
    _keyed("POST", "/api/pdax/trade/order"),
    _keyed("GET", "/api/pdax/trade/orders/{order_id}"),
    _keyed("GET", "/api/pdax/trade/orders"),
    _keyed("GET", "/api/pdax/crypto/deposit"),
    _keyed("POST", "/api/pdax/fiat/deposit"),
    _keyed("POST", "/api/pdax/fiat/withdraw"),
    _keyed("POST", "/api/pdax/fiat/user-info-upload"),
    _keyed("POST", "/api/pdax/crypto/withdraw"),
    _keyed("GET", "/api/pdax/fiat/transactions"),
    _keyed("GET", "/api/pdax/crypto/transactions"),
    _keyed("GET", "/api/pdax/balances"),
    _keyed("POST", "/api/pdax/webhooks/register"),
    _keyed("POST", "/api/pdax/ramp/estimate"),
    _keyed("POST", "/api/pdax/ramp/funding-quote"),
    _keyed("POST", "/api/pdax/ramp/onramp"),
    _keyed("POST", "/api/pdax/ramp/offramp"),
    _keyed("GET", "/api/pdax/ramp"),
    _keyed("GET", "/api/pdax/ramp/{ramp_id}"),
    _keyed("POST", "/api/pdax/ramp/{ramp_id}/reconcile"),
)


def exempt(scope: dict) -> bool:
    """Whether an allowlisted caller is making this request (see EXEMPTIONS)."""
    return any(exemption.admits(scope) for exemption in EXEMPTIONS)


def locked_out(scope: dict) -> bool:
    """Whether the lock refuses this request — in `enforce`; `log` only reports it.

    Pure over the scope and the current settings: it reads no body and makes
    no call, so a request it refuses costs a header lookup and one
    constant-time comparison.
    """
    if scope.get("type") != "http":
        return False
    if not str(scope.get("path", "")).startswith(API_PREFIX):
        return False
    # A preflight carries no credentials by design — the browser sends it on
    # its own, before the real request — so it can never present the token.
    # CORSMiddleware answers it outside this layer anyway; this keeps a bare
    # OPTIONS from being counted as someone locked out.
    if scope.get("method") == "OPTIONS":
        return False
    return not is_frontend(scope) and not exempt(scope)


class LockStats:
    """How many requests the lock has refused — or, in `log`, would have.

    Since boot and over the last hour, for /readiness. The hour is sixty
    one-minute buckets, so a flood costs one integer increment per request
    and the table never holds more than about sixty entries, however long the
    process runs or whatever arrives. In-process, like the rate limiters: a
    restart starts the count again.
    """

    _BUCKET_SECONDS = 60
    _BUCKETS_PER_HOUR = 60

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.total = 0
        self._buckets: dict[int, int] = {}

    def _minute(self) -> int:
        return int(self._clock() // self._BUCKET_SECONDS)

    def record(self) -> None:
        minute = self._minute()
        self.total += 1
        self._buckets[minute] = self._buckets.get(minute, 0) + 1
        if len(self._buckets) > self._BUCKETS_PER_HOUR:
            cutoff = minute - self._BUCKETS_PER_HOUR
            for stale in [m for m in self._buckets if m <= cutoff]:
                del self._buckets[stale]

    def last_hour(self) -> int:
        cutoff = self._minute() - self._BUCKETS_PER_HOUR
        return sum(count for minute, count in self._buckets.items() if minute > cutoff)

    def reset(self) -> None:
        self.total = 0
        self._buckets.clear()


# One per process, read by /readiness.
stats = LockStats()


# What a path that matches no /api route is logged as. One name for all of
# them, so a scan of random paths is one log key, not one per path.
UNROUTED = "<unrouted>"


class RouteTemplates:
    """Which /api route template a path belongs to — what the lock logs instead of the path.

    A template (`/api/tasks/{task_id}`) never carries an id or a capability
    from the URL into the log, and there are only as many of them as routes,
    so the log coalescer keyed on it cannot be grown by a caller inventing
    paths.

    Read off the app's OpenAPI rather than by walking `app.routes`: FastAPI
    0.140 stopped flattening included routers into that list, and the schema
    is the shape that stayed put (tests/route_inventory.py makes the same
    call). Built once, on the first request that needs it, then reused;
    literal routes are tried before templated ones, so
    `/api/agents/bind/endpoint-check` is not mistaken for an agent id.
    """

    def __init__(self) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] | None = None

    def resolve(self, app: Any, path: str) -> str:
        if self._compiled is None:
            self._compiled = self._compile(app)
        for template, pattern in self._compiled:
            if pattern.fullmatch(path):
                return template
        return UNROUTED

    @staticmethod
    def _compile(app: Any) -> list[tuple[str, re.Pattern[str]]]:
        try:
            paths = app.openapi().get("paths", {})
        except Exception:
            # Logging falls back to UNROUTED for everything; the lock itself
            # never depends on a template, so it keeps working regardless.
            logger.exception("origin lock: could not read the route templates; logging every path as %s", UNROUTED)
            paths = {}
        templates = sorted((t for t in paths if t.startswith(API_PREFIX)), key=lambda t: (t.count("{"), t))
        return [(template, template_regex(template)) for template in templates]


class WarningCoalescer:
    """At most one log line per route template per window; the rest are counted into the next.

    A flood of direct calls must not become a flood of log lines — Render's
    log is what an operator reads during that very flood. The first refusal
    on a template in a window is logged; the others in that window are only
    counted, and the next line for the template says how many it stands for.
    Keyed on the template, so the table has at most one entry per route.
    """

    def __init__(self, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window_seconds
        self._clock = clock
        self._last_logged: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def admit(self, key: str) -> int | None:
        """None to stay quiet; otherwise log, saying how many went unlogged since the last line."""
        now = self._clock()
        last = self._last_logged.get(key)
        if last is not None and now - last < self._window:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return None
        self._last_logged[key] = now
        return self._suppressed.pop(key, 0)

    def reset(self) -> None:
        self._last_logged.clear()
        self._suppressed.clear()


# One per process, like `stats`: the window is the process's, whichever
# middleware instance a request passes through.
log_coalescer = WarningCoalescer()


class OriginLockMiddleware:
    """Pure-ASGI origin lock over /api/* (see the module docstring).

    Registered inside the hardening headers and CORS, outside both rate
    limiters and the body cap (app/main.py says why): a refused request
    spends no rate-limit budget and has no body read, while its 403 still
    carries the request id, the hardening headers and, for an allowed
    origin, the CORS header a browser needs to read it.

    The mode is read per request, like the token `is_frontend` checks, so the
    tests and a settings reload see the current value.
    """

    def __init__(
        self,
        app: Any,
        *,
        counter: LockStats | None = None,
        templates: RouteTemplates | None = None,
        coalescer: WarningCoalescer | None = None,
    ) -> None:
        self.app = app
        self.counter = stats if counter is None else counter
        self.templates = RouteTemplates() if templates is None else templates
        self.coalescer = log_coalescer if coalescer is None else coalescer

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        mode = settings.origin_lock_mode
        if mode == "off" or not locked_out(scope):
            await self.app(scope, receive, send)
            return
        self.counter.record()
        self._report(scope, refused=mode == "enforce")
        if mode == "enforce":
            await self._refuse(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(send: Any) -> None:
        """403 in the envelope every other refusal uses (BodyLimitMiddleware's 413, the limiter's 429)."""
        body = json.dumps(
            {
                "detail": REFUSAL_CODE,
                "error": {"code": REFUSAL_CODE, "message": REFUSAL_MESSAGE, "request_id": request_id_var.get()},
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    # The answer depends on a header a shared cache cannot see.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _report(self, scope: dict, *, refused: bool) -> None:
        """One coalesced WARNING: method, route template, who, and the request id.

        Never the path itself, the query string or any header — a URL can carry
        a task id or a `?token=`, and the token being checked is a secret.
        """
        template = self.templates.resolve(scope.get("app"), str(scope.get("path", "")))
        suppressed = self.coalescer.admit(template)
        if suppressed is None:
            return
        # Only token characters reach a method, but cap it anyway: it is the
        # caller's text.
        method = str(scope.get("method", "-"))[:16]
        identity = client_identity(scope) or "-"
        verdict = "refused" if refused else "would refuse"
        logger.warning(
            "origin lock %s %s %s: not from the frontend [%s] client=%s%s",
            verdict,
            method,
            template,
            request_id_var.get(),
            identity,
            f" (+{suppressed} more on this route since the last line)" if suppressed else "",
            extra={
                "http": {
                    "method": method,
                    "route": template,
                    "identity": identity,
                    "origin_lock": "refused" if refused else "would_refuse",
                    "suppressed": suppressed,
                }
            },
        )
