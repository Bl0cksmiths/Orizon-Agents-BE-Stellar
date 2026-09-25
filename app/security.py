"""
Lightweight, dependency-free hardening primitives.

- `require_api_key` — optional FastAPI dependency. When `settings.api_key`
  is unset (the default for the public demo) it is a no-op; when set, the
  request must carry a matching `X-API-Key` header or it is rejected 401.
- `require_adjudicator` — the same header, but FAILING CLOSED: the refund
  routes it guards spend the platform's own balance, so an unset key is a
  503 rather than an open door. Its docstring is where that divergence from
  the testnet-open posture above is argued.
- `client_key` — resolves who a request belongs to from the X-Forwarded-For
  chain, with the proxy trust boundary set by `TRUSTED_PROXY_HOPS`. Both the
  limiter and the access log key on it, so its docstring is where the
  consequences of getting that boundary wrong are written down.
- `RateLimitMiddleware` — sliding-window rate limiter over that key, as a
  pure ASGI middleware (no external deps). Window/limit come from settings;
  liveness paths and CORS preflights are exempt. Rate-limited responses
  carry `X-RateLimit-Limit` / `X-RateLimit-Remaining` headers.
- `BodyLimitMiddleware` — pure ASGI request-body size cap. Rejects declared
  Content-Length over the limit up front and counts streamed (chunked)
  bodies as the app reads them, answering 413 either way. Per-path
  overrides tighten the budget for routes that buffer the whole body.
- `RequestContextMiddleware` — pure ASGI request-id propagation + one-line
  INFO access log per request (method, path, status, duration, id). Also
  drives `ForwardedChainSampler`, a bounded once-per-process dump of the raw
  proxy chain that exists so `TRUSTED_PROXY_HOPS` can be read off production.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import re
import secrets
import time
import uuid
from collections import deque
from typing import Any

from fastapi import Header, HTTPException

from .config import settings

logger = logging.getLogger(__name__)

# Paths that must never be throttled (probes + root ping), and that stay out
# of the access log. `/api/health` is the SAME liveness probe re-served under
# the frontend proxy's `/api` prefix (routers/health.py): it must be exempt
# identically, or an uptime monitor polling through the proxy would burn the
# shared per-IP budget — every browser behind that egress IP pays for it —
# and would flood the log with probe noise.
EXEMPT_PATHS = frozenset({"/", "/health", "/readiness", "/api/health"})

# Current request's id — set by RequestContextMiddleware, readable from any
# code running in the request's task context (error handlers, log records).
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class CodedHTTPException(HTTPException):
    """An HTTPException that carries its own human message beside its code.

    `app/main.py`'s handler derives `error.message` from the detail: a snake
    token becomes the code and the message is that token with its underscores
    swapped for spaces. That is right for almost everything, and wrong for a
    handful of refusals whose message says something the code cannot — the
    time a dispute window closed, what to do next. Raising one of these keeps
    the stable code AND the written sentence, instead of forcing a choice.

    It is raised only where the message has been read and found safe to
    disclose to whoever is being refused. A message that quotes configured
    limits, or facts about state the caller has not proved they may know, goes
    out as a bare code like everything else — see `routers/disputes._refuse`,
    which is the only place in this service that makes that judgement.

    Lives here, in a module of hardening primitives, for `request_id_var`'s
    reason: both are halves of the error envelope that `main` assembles, and
    `main` imports the routers, so the routers cannot import it back.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=code)
        self.message = message


# Credential-bearing query values (the SSE routes take `?token=`) must not
# reach the access log; only the parameter name survives.
_TOKEN_QUERY_RE = re.compile(r"(^|&)([^&=]*token)=[^&]*")


def _redacted_target(scope: dict) -> str:
    """Path plus query string, with any `*token=` values masked for logging."""
    path = scope.get("path", "-")
    query = (scope.get("query_string") or b"").decode("latin-1")
    if not query:
        return str(path)
    return f"{path}?{_TOKEN_QUERY_RE.sub(r'\1\2=***', query)}"


class RequestIdLogFilter(logging.Filter):
    """Stamp every LogRecord with the current request id ("-" outside a
    request) so formatters can correlate service logs with access lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


# Token shapes that are secret wherever they appear, whatever logged them.
# A shape catches what the configured-value pass cannot: a key that is not
# this deployment's own (a provider echoing back a rejected key, a pasted
# seed in an exception message). Both are anchored to whole tokens so a
# public key, a contract id or a transaction hash is never mistaken for one —
# Stellar public keys start with G and contract ids with C, and only a
# secret seed is an S followed by exactly 55 base32 characters.
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    # OpenAI-style API keys, whole or as a provider echoes them back `*`-masked
    # ("sk-proj-abc*****wxyz") — the unmasked tail is still part of the key.
    re.compile(r"\bsk-[A-Za-z0-9_*\-]{8,}"),
    re.compile(r"\bS[A-Z2-7]{55}\b"),  # Stellar secret seeds (StrKey "S…")
)

# Below this length a configured value is not masked by exact match: a short
# string is more likely to be an ordinary word than a credential, and masking
# every occurrence of it would shred unrelated log text. Every real secret
# this service holds is far longer; a short one is a config mistake the
# validators and the startup lines exist to report, not something to hide.
_MIN_MASKED_SECRET_CHARS = 8


def _configured_secrets() -> tuple[str, ...]:
    """This deployment's own secret values, longest first.

    Read on every call rather than captured at import: tests and hot config
    reloads change `settings`, and a mask built from yesterday's values
    protects nothing. Longest first so a secret that contains another (a
    database URL embedding its password) is masked whole, not in pieces.

    `database_url` is in the list for its password; the URL as a whole is
    masked because a password cannot be cut out of an arbitrary DSN safely.
    `stellar_signing_key` may be a 12/24-word mnemonic, which no token shape
    recognises — only the exact-value pass can catch it.
    """
    values = (
        settings.openai_api_key,
        settings.api_key,
        settings.database_url,
        settings.orizon_dispatch_signing_key,
        settings.stellar_signing_key,
        settings.pdax_password,
        settings.pdax_otp_secret,
        settings.pdax_webhook_secret,
    )
    unique = {v.strip() for v in values if v and len(v.strip()) >= _MIN_MASKED_SECRET_CHARS}
    return tuple(sorted(unique, key=len, reverse=True))


REDACTED = "[redacted]"


def redact_secrets(text: str) -> str:
    """`text` with every configured secret and secret-shaped token masked.

    Exact values first, then shapes: a configured key is masked even when it
    has no recognisable shape, and a foreign key is masked even when it is not
    ours. Pure and cheap enough to run on every log record.
    """
    for secret in _configured_secrets():
        text = text.replace(secret, REDACTED)
    for shape in _SECRET_SHAPES:
        text = shape.sub(REDACTED, text)
    return text


# Only for `formatException`, which is formatter-independent; never formats a
# whole record, so it cannot disagree with the handler's own formatter.
_EXCEPTION_FORMATTER = logging.Formatter()


class SecretRedactionLogFilter(logging.Filter):
    """Mask secrets in every record before any handler formats it.

    Call-site redaction (`orchestrator_svc._loggable`, `_redacted_target`)
    covers the text this codebase writes; it cannot cover a library that logs
    a provider's error verbatim, and agno does exactly that at ERROR. A
    handler filter is the one place every record passes through.

    The message is rendered here (`getMessage`, args interpolated) because a
    secret can arrive in an argument as easily as in the format string, and
    the record is only rewritten when something was masked, so the common
    case leaves `msg`/`args` untouched for any downstream consumer.

    A traceback is where a secret is most likely to hide — an exception built
    from a connection string, a rejected key echoed in a provider error — and
    formatters render it from `exc_info` after filters run. So it is rendered
    and masked here too, and when masking changed it, folded into the message
    with `exc_info` cleared: exactly how `JsonLogFormatter` already presents a
    traceback, so the line is unchanged apart from the mask.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a malformed record is logging's to report, not ours to drop
            return True
        masked = redact_secrets(message)
        if record.exc_info:
            trace = _EXCEPTION_FORMATTER.formatException(record.exc_info)
            masked_trace = redact_secrets(trace)
            if masked_trace != trace:
                masked = f"{masked}\n{masked_trace}"
                record.exc_info = None
                record.exc_text = None
        if masked != message:
            record.msg = masked
            record.args = None
        return True


# Resolved key for a forwarded chain that is too short to contain a client
# entry once the trusted hops are removed. A literal, never an address: it
# cannot collide with a real client, and seeing it as `client=` in the access
# log is itself the signal that TRUSTED_PROXY_HOPS is set higher than the
# number of entries this edge actually appends.
CHAIN_TOO_SHORT_KEY = "forwarded-chain-too-short"


def _trusted_hops() -> int:
    """Configured trailing-hop count, floored at 0 (a negative value would
    index back into the caller-controlled end of the chain)."""
    return max(0, settings.trusted_proxy_hops)


def client_key(scope: dict, hops: int | None = None) -> str:
    """Resolve the client key used for rate limiting and access logs.

    X-Forwarded-For is append-only: each proxy adds the address of the peer it
    received the request from, so the chain reads

        <anything the caller sent>, <caller's address>, <our edge>, <...>

    and only the RIGHTMOST entries were written by infrastructure we control.
    `TRUSTED_PROXY_HOPS` says how many of those trailing entries are ours; they
    are dropped, and the next entry to the left is the client.

    The count cannot be guessed — it is a property of the deployment (Vercel's
    rewrite proxy and Render's edge both rewrite this header, and how many
    entries each contributes is not observable from outside) — and BOTH ways of
    getting it wrong are silent:

    * **Too few hops trusted.** The resolved entry is one our own edge wrote,
      which is the same value for every visitor. `rate_limit_per_minute` then
      behaves as a single budget for the WHOLE service rather than per visitor
      — a handful of open dashboard tabs can 429 everyone — and `client=` is a
      constant in every access line, so abuse cannot be attributed during an
      incident.
    * **Too many hops trusted.** The resolved entry is one the CALLER wrote.
      Anyone can then mint a fresh bucket per request by rotating a header
      value: the limiter stops limiting, and the bucket table grows with
      attacker-chosen keys.

    The default of 0 reproduces the original behaviour exactly — the last entry,
    whatever it is — so deploying this changes nothing until the hop count is
    tuned against a chain actually observed from production (the sampler below
    logs one; see FORWARDED_CHAIN_SAMPLES).

    Two degenerate chains are handled deliberately rather than by clamping:

    * Fewer entries than the configured hop count leaves no client entry at
      all. That resolves to CHAIN_TOO_SHORT_KEY, NOT to the leftmost entry —
      the leftmost is precisely the one the caller controls, so clamping there
      would turn a too-high setting into a limiter bypass instead of a loud,
      visible misconfiguration.
    * Empty entries (a stray `,` or a trailing comma) are dropped before
      indexing. Falling through to `scope["client"]` on that input would be
      unsafe here: uvicorn runs with `--proxy-headers --forwarded-allow-ips='*'`
      (render.yaml), and its ProxyHeadersMiddleware then overwrites
      scope["client"] with the LEFTMOST X-Forwarded-For entry — caller-supplied.
      The scope fallback below is therefore reached only when the header is
      absent entirely (local runs, tests), never on the deployed path.
    """
    headers = dict(scope.get("headers") or [])
    fwd = headers.get(b"x-forwarded-for")
    if fwd:
        chain = [entry.strip() for entry in fwd.decode("latin-1").split(",")]
        chain = [entry for entry in chain if entry]
        if chain:
            skip = _trusted_hops() if hops is None else max(0, hops)
            index = len(chain) - 1 - skip
            return chain[index] if index >= 0 else CHAIN_TOO_SHORT_KEY
    client = scope.get("client")
    return client[0] if client else "unknown"


class ForwardedChainSampler:
    """Log the first N forwarded chains a process sees, then go quiet.

    `TRUSTED_PROXY_HOPS` can only be set correctly by someone who has SEEN a
    real chain from this deployment's edge — Vercel's rewrite proxy and
    Render's edge both rewrite X-Forwarded-For, and how many entries each
    contributes is not observable from outside. This makes exactly that
    visible, in Render's log stream, without becoming a new attack surface:

    * **Not a route.** A diagnostic endpoint returning the chain would be an
      unauthenticated disclosure of visitors' IP addresses on every deployment
      whose API_KEY is empty (the public-demo default), and would stay
      reachable long after the hop count was settled. Logs are already an
      operator-only sink behind Render's dashboard auth, and already carry one
      client address per request in the access line — this adds the rest of
      the chain, for a handful of requests, to a stream that shape of data is
      already in.
    * **Not per request.** The budget is spent within the first few requests
      after a deploy; from then on the cost is one integer comparison. That
      matters on a 512 MB shared-CPU instance, and it keeps client addresses
      from accumulating in the log for traffic that is not being diagnosed.
    * **Not on the probe paths.** EXEMPT_PATHS requests come from Render's own
      health checker and from uptime monitors, whose chains do not look like a
      browser's; sampling them would burn the budget on unrepresentative data
      before a real visitor ever arrives.

    Re-arming needs no code change: every env var edit on Render restarts the
    service, so setting FORWARDED_CHAIN_SAMPLES takes a fresh sample.
    """

    def __init__(self, budget: int | None = None) -> None:
        self.reset(budget)

    def reset(self, budget: int | None = None) -> None:
        """Re-arm the sample. Production re-arms by restarting; tests call it."""
        self.budget = max(0, settings.forwarded_chain_samples if budget is None else budget)
        self.remaining = self.budget

    def sample(self, scope: dict) -> None:
        if self.remaining <= 0:
            return
        self.remaining -= 1
        headers = dict(scope.get("headers") or [])
        raw = (headers.get(b"x-forwarded-for") or b"").decode("latin-1")
        chain = [entry.strip() for entry in raw.split(",") if entry.strip()]
        peer = scope.get("client")
        logger.info(
            "forwarded chain sample %d/%d on %s %s: entries=%d chain=%s peer=%s "
            "TRUSTED_PROXY_HOPS=%d resolves client=%s — set TRUSTED_PROXY_HOPS to the number of "
            "TRAILING entries this edge appends, so the one to their left is the visitor",
            self.budget - self.remaining,
            self.budget,
            scope.get("method", "-"),
            scope.get("path", "-"),
            len(chain),
            chain,
            peer[0] if peer else "-",
            _trusted_hops(),
            client_key(scope),
        )


# Armed at import, i.e. once per worker process.
forwarded_chain_sampler = ForwardedChainSampler()


def header_secret_matches(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison of a HEADER value against a configured secret.

    The comparison is on the bytes that crossed the wire, which is the only
    comparison that can succeed. Starlette decodes header bytes as LATIN-1, so
    the str a dependency receives is the wire bytes one-to-one; re-encoding it
    latin-1 recovers them exactly. The previous `encode("utf-8", "ignore")`
    re-encoded that decoding as UTF-8 instead, which is the identity only while
    every byte is ASCII: a key holding one non-ASCII character was mangled into
    something the configured value could never equal, so a deployment whose
    API_KEY contained an accent answered 401 to its own operator forever —
    indistinguishable, in the log and in the body, from an attacker. `replace`
    rather than `strict` because nothing may raise here: a str outside latin-1
    cannot have come off a header, and a caller who contrives one gets a
    mismatch rather than a 500.

    Both sides are stripped of ASCII whitespace — the OWS an HTTP parser is
    allowed to leave on a header value, which h11 and httptools do not treat
    identically, and the padding an operator's copy-paste leaves on an env var.
    `bytes.strip()` touches only ASCII whitespace, so it cannot eat a
    continuation byte of a multi-byte character the way `str.strip()` can (it
    considers U+00A0 whitespace, and that is a valid UTF-8 continuation byte).
    `config` refuses to boot on a padded key; this is the wire half, and it
    also holds for the tests that set `settings.api_key` past the validators.

    False for an absent header and for an unset secret, so no caller can match
    "no key configured" by sending nothing.
    """
    if supplied is None or not expected:
        return False
    return secrets.compare_digest(
        supplied.encode("latin-1", "replace").strip(),
        expected.encode("utf-8").strip(),
    )


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """No-op unless API_KEY is configured; then enforce the X-API-Key header."""
    expected = settings.api_key
    if not expected:
        return
    if not header_secret_matches(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid_api_key")


async def require_adjudicator(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """The operator key, enforced — the adjudication routes FAIL CLOSED.

    `require_api_key` above is deliberately a no-op while API_KEY is empty,
    because the public demo runs open and everything behind that guard either
    moves play money or spends an allowance the payer already authorised
    on-chain. `config._money_capable_config_requires_api_key` is what keeps
    that trade honest: the process refuses to boot if it holds credentials
    that could move real value anonymously.

    This dependency cannot inherit either half of that posture.

    An upheld dispute is different in KIND, not in degree. It transfers the
    PLATFORM's own settler balance to the payer's address on an adjudicator's
    say-so, with no prior on-chain authorisation to bound it — no allowance,
    no escrow, no signature from the party being debited. Anonymous, that is
    not a demo affordance, it is a drain of the settler wallet, on testnet as
    surely as on mainnet. `settings.max_refund_usdc` caps ONE payout; it does
    not cap how many an open route can be asked for.

    Nor is the boot validator a sufficient backstop here, though it is a
    better one than this paragraph used to claim: since story 4.03
    `_money_capable_config_requires_api_key` fires on `dispute_refunds_enabled`
    ALONE, so a deployment cannot boot with the switch on and API_KEY empty.
    What it cannot do is answer the question this dependency asks. It proves a
    key EXISTS at boot; only a per-request check can say whether the caller
    holds it — and the value it checked is mutable afterwards (tests set it,
    and a reload would reread it), so a route that signs must not authorise
    anyone on the strength of something that was true at import. The second
    refusal below is therefore unreachable on a correctly booted process, and
    it stays: an unset key on a payout route is answered, never waved through,
    whatever is supposed to have stopped it getting here.

    The three refusals, in the order a caller meets them:

    * switch off -> 503 `dispute_refunds_disabled`. Nothing is adjudicable on
      this deployment; it is a configuration state, not the caller's mistake.
    * switch on but no key -> 503 `adjudication_not_configured`. Also the
      operator's, and NEVER a fall-through to "allow". Logged at ERROR: a live
      refund switch with no credential behind it is a misconfiguration someone
      has to see.
    * key missing or wrong -> 401 `invalid_api_key`, the same token
      `require_api_key` answers with, so a client needs one mapping, not two.

    The key itself is never logged, and no refusal names whether a key was
    supplied at all: an adjudication endpoint that distinguishes "no key" from
    "wrong key" in its body is an oracle.
    """
    if not settings.dispute_refunds_enabled:
        raise HTTPException(status_code=503, detail="dispute_refunds_disabled")
    expected = settings.api_key
    if not expected:
        logger.error(
            "adjudication refused: DISPUTE_REFUNDS_ENABLED is on but API_KEY is empty, "
            "so the refund routes have no credential to check and stay closed"
        )
        raise HTTPException(status_code=503, detail="adjudication_not_configured")
    if not header_secret_matches(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid_api_key")


class RequestContextMiddleware:
    """Request-id + access-log middleware (pure ASGI, no external deps).

    Takes the caller's `X-Request-ID` (or generates a short one), echoes it
    on the response so clients can quote it, and logs one INFO line per
    request — method, path, status, duration, id — so any response can be
    correlated with server logs. Probe paths (EXEMPT_PATHS) still get the
    header but are not logged, to keep the log free of health-check noise.

    Being the outermost layer, this is also where the forwarded-chain sample
    is taken (see ForwardedChainSampler): the sample line and the access line
    for the same request carry the same request id, so the raw chain and the
    key it resolved to can be read side by side.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        request_id = (headers.get(b"x-request-id") or b"").decode("latin-1").strip()[:64]
        if not request_id:
            request_id = uuid.uuid4().hex[:16]

        # Deliberately never reset: the 500 handler runs on the outermost
        # ServerErrorMiddleware layer AFTER this frame has unwound, so a
        # finally-reset would erase the id before that handler reads it.
        # Each request runs in its own task context, so nothing leaks across
        # requests.
        request_id_var.set(request_id)
        # Also visible to route handlers as request.state.request_id.
        scope.setdefault("state", {})["request_id"] = request_id

        method = scope.get("method", "-")
        path = scope.get("path", "-")

        # Bounded, once-per-process: the first few real requests after a
        # restart print the raw forwarded chain so TRUSTED_PROXY_HOPS can be
        # read off production instead of guessed. Sampled here, before the
        # request is dispatched, so a 429 or a 500 is sampled too; probe paths
        # are skipped so they cannot spend the budget on chains that do not
        # look like a browser's.
        if path not in EXEMPT_PATHS:
            forwarded_chain_sampler.sample(scope)

        started = time.monotonic()

        async def send_with_context(message: dict) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or []) + [
                    (b"x-request-id", request_id.encode("latin-1")),
                ]
                if path not in EXEMPT_PATHS:
                    duration_ms = (time.monotonic() - started) * 1000.0
                    logger.info(
                        "%s %s -> %s in %.1fms [%s] client=%s",
                        method,
                        # Path + query with token values masked: SSE auth
                        # tokens ride in the query string and must not be
                        # recoverable from logs.
                        _redacted_target(scope),
                        message.get("status"),
                        duration_ms,
                        request_id,
                        # Same key the rate limiter buckets on, so a 429 in
                        # the log can be traced to the client that caused it.
                        client_key(scope),
                    )
            await send(message)

        await self.app(scope, receive, send_with_context)


class _BodyTooLarge(Exception):
    """Internal signal: a streamed request body crossed the size limit."""


class BodyLimitMiddleware:
    """Cap request-body size (pure ASGI, no deps). Nothing else in the stack
    limits bodies — uvicorn and Starlette accept arbitrarily large uploads —
    so without this a single request can exhaust worker memory (the PDAX
    webhook route, for one, buffers the whole body before its HMAC check).

    A declared Content-Length above the limit is refused immediately with
    413; chunked/streamed bodies are counted as the app reads them and
    aborted at the limit. 413 bodies use the app's unified error envelope.
    """

    DEFAULT_LIMIT = 1_048_576  # 1 MiB — comfortably above any legitimate payload
    # Routes that buffer the entire body up front get a tighter budget.
    PATH_LIMITS: dict[str, int] = {"/api/pdax/webhooks/receive": 65_536}

    def __init__(
        self,
        app: Any,
        limit: int | None = None,
        path_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.limit = self.DEFAULT_LIMIT if limit is None else limit
        self.path_limits = dict(self.PATH_LIMITS) if path_limits is None else path_limits

    @staticmethod
    async def _send_413(send: Any) -> None:
        body = json.dumps(
            {
                # Same envelope the app's exception handlers emit.
                "detail": "request_too_large",
                "error": {
                    "code": "request_too_large",
                    "message": "request body exceeds the size limit",
                    "request_id": request_id_var.get(),
                },
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.path_limits.get(scope.get("path", ""), self.limit)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._send_413(send)
                    return
            except ValueError:
                pass  # malformed length — the server rejects it downstream

        # Chunked (or lying) bodies: meter the bytes the app actually reads.
        received = 0
        response_started = False

        async def receive_limited() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge  # unwinds the endpoint mid-read
            return message

        async def send_tracking(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_limited, send_tracking)
        except _BodyTooLarge:
            if response_started:
                raise  # too late for a 413 — let the server drop the connection
            await self._send_413(send)


class RateLimitMiddleware:
    """Sliding-window limiter, keyed by client_key(). In-process (1 worker).

    Timestamps per key live in a dict of deques; old entries are pruned on
    each hit and the whole table is swept periodically so idle keys don't
    accumulate. All mutation happens synchronously between awaits, so it is
    safe under a single asyncio event loop without locks.

    How much this limits *per visitor* rather than *in total* is entirely
    decided by TRUSTED_PROXY_HOPS — see client_key(). At the default of 0 the
    key is a constant this deployment's edge wrote, so `rate_limit_per_minute`
    is one budget for the whole service; the default limit is sized for that
    reading.
    """

    _SWEEP_EVERY = 1024  # requests between full-table sweeps

    def __init__(
        self,
        app: Any,
        limit: int | None = None,
        window_seconds: float = 60.0,
    ) -> None:
        self.app = app
        self.limit = settings.rate_limit_per_minute if limit is None else limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._since_sweep = 0

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        stale = [ip for ip, dq in self._hits.items() if not dq or dq[-1] < cutoff]
        for ip in stale:
            del self._hits[ip]

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or self.limit <= 0
            or scope.get("method") == "OPTIONS"
            or scope.get("path") in EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        key = client_key(scope)
        dq = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()

        self._since_sweep += 1
        if self._since_sweep >= self._SWEEP_EVERY:
            self._since_sweep = 0
            self._sweep(now)

        limit_header = str(self.limit).encode()

        if len(dq) >= self.limit:
            retry_after = max(1, math.ceil(dq[0] + self.window - now))
            body = json.dumps(
                {
                    # Same envelope the app's exception handlers emit: legacy
                    # "detail" plus the structured "error" object.
                    "detail": "rate_limited",
                    "error": {
                        "code": "rate_limited",
                        "message": "too many requests",
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
                        (b"retry-after", str(retry_after).encode()),
                        (b"x-ratelimit-limit", limit_header),
                        (b"x-ratelimit-remaining", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        dq.append(now)
        # Quota headers reflect the window as admitted — the budget left
        # after counting this request.
        remaining = str(max(0, self.limit - len(dq))).encode()

        async def send_with_quota(message: dict) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or []) + [
                    (b"x-ratelimit-limit", limit_header),
                    (b"x-ratelimit-remaining", remaining),
                ]
            await send(message)

        await self.app(scope, receive, send_with_quota)
