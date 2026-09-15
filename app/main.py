from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import SERVICE_VERSION, settings
from .pdax.client import aclose_pdax_client
from .routers import agents, binding, flow, metrics, orchestrator, payments, pdax, stellar, tasks, trace

# Imported by symbol, not as a module: the root `/health` handler defined
# below rebinds the name `health` at module scope, which would shadow a
# `from .routers import health` module import at call time.
from .routers.health import HealthResponse, health_payload
from .routers.health import router as health_router
from .security import (
    BodyLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    RequestIdLogFilter,
    request_id_var,
)
from .seed import seed_registry
from .services import execution_svc, registry_sync
from .services.binding_registry import refresh_bound_ids
from .services.binding_store import close_binding_store


class JsonLogFormatter(logging.Formatter):
    """Compact single-line JSON records: ts, level, logger, msg, request_id.

    Dependency-free. Tracebacks are folded into msg so a crash stays one
    parseable line for Render's log viewer; timestamps are UTC.
    """

    @staticmethod
    def converter(secs: float | None) -> time.struct_time:
        return time.gmtime(secs)

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"
        return json.dumps(
            {
                "ts": f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')}.{int(record.msecs):03d}Z",
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
                "request_id": getattr(record, "request_id", "-"),
            },
            ensure_ascii=False,
        )


# Root logging: everything the app emits leaves as one JSON line carrying the
# current request id. Uvicorn's own loggers keep their handlers (its access
# and error loggers don't propagate to root), so they are unaffected.
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(JsonLogFormatter())
_log_handler.addFilter(RequestIdLogFilter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler], force=True)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    seed_registry()
    # Bound the default executor: asyncio.to_thread otherwise sizes it to
    # min(32, cpu_count + 4) from the HOST's core count, while Render grants
    # this container only a small CPU share — 8 threads comfortably cover the
    # blocking Soroban SDK calls without oversubscribing the worker.
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="soroban")
    asyncio.get_running_loop().set_default_executor(executor)
    # Mirror on-chain registrations into the marketplace (story 1.02). Started
    # after the executor bind so its to_thread reads use the bounded pool; the
    # loop no-ops while STELLAR_AGENT_REGISTRY is blank, which keeps the
    # hermetic test suite offline.
    registry_sync.start()
    # Seed the planner's routability set from the binding store. Without this a
    # binding made before this process started would stay unroutable until the
    # operator bound it again — which is precisely the restart AC-5 is about.
    await refresh_bound_ids()
    yield
    # Stop the sync loop first — it must not fire a fresh RPC pass while the
    # shutdown below is draining execution tasks.
    await registry_sync.stop()
    # Drain in-flight background executions: a bounded grace window to let
    # them finish, then cancel stragglers and reap the cancellations so the
    # process exits without "task was destroyed but it is pending" noise.
    pending = {t for t in execution_svc._background_tasks if not t.done()}
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=15)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=5)
    await aclose_pdax_client()
    # Release the binding store's connection pool. A no-op for the in-memory
    # store, which is what runs whenever DATABASE_URL is unset.
    await close_binding_store()
    executor.shutdown(wait=False)


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware stamping baseline hardening headers on responses.

    Deliberately minimal for a JSON API: no CSP (nothing is rendered) and no
    HSTS (TLS terminates at Render's edge, which sets it).
    """

    _HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
    ]

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or []) + self._HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


# DOCS_ENABLED=false hides /docs, /redoc, and the schema on locked-down
# deployments. Read via getattr — config.py declares the field separately —
# so the public-demo default (docs on) holds either way.
_docs_enabled = bool(getattr(settings, "docs_enabled", True))

app = FastAPI(
    title="Orizon Agents API",
    version=SERVICE_VERSION,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    description=(
        "The orchestration layer for autonomous digital labor: goal decomposition "
        "across a priced agent network, Soroban-settled payments and reputation on "
        "Stellar, and PDAX fiat on/off-ramping. Errors use a unified envelope — the "
        'legacy "detail" key plus an "error" object with a stable code, message, '
        "and request id."
    ),
    contact={"name": "Orizon Agents", "url": "https://orizons.xyz"},
    servers=[{"url": "https://orizon-agents-be-stellar.onrender.com", "description": "production"}],
    lifespan=lifespan,
    openapi_tags=[
        {"name": "meta", "description": "Service identity and health/readiness probes."},
        {"name": "agents", "description": "Registered agents and their skills, pricing, and reputation."},
        {"name": "orchestrator", "description": "Decompose a goal into a plan and execute it across agents."},
        {"name": "tasks", "description": "Task history, status, and produced artifacts."},
        {"name": "trace", "description": "Per-task execution traces, polled or streamed."},
        {"name": "metrics", "description": "Aggregate network metrics for the dashboard."},
        {"name": "flow", "description": "Agent-graph flow layout consumed by the frontend visualizer."},
        {"name": "payments", "description": "x402 payment challenges and settlement."},
        {"name": "stellar", "description": "Soroban contract reads, unsigned-XDR builds, and signed-XDR submits."},
        {
            "name": "binding",
            "description": "Bind an operator HTTPS endpoint to an on-chain agent id, proved by a wallet signature.",
        },
        {"name": "pdax", "description": "PDAX PHP-to-crypto on/off-ramp: trade, funding, withdrawals, webhooks."},
    ],
)

# Added first → runs innermost: oversized bodies are rejected before the
# router, while the 413 still passes through the header/CORS/request-id
# layers wrapping it.
app.add_middleware(BodyLimitMiddleware)

# Registered before CORS so CORS wraps it and 429 responses still carry
# the Access-Control-Allow-Origin header the browser needs to read them.
app.add_middleware(RateLimitMiddleware)

# Wraps the rate limiter (and everything inside it), so the limiter's 429
# short-circuits — and the body limiter's 413s — carry the hardening
# headers too, not just responses that reached the router.
app.add_middleware(SecurityHeadersMiddleware)

# This project's Vercel production/preview origins only — a broad
# `.*\.vercel\.app` would let ANY hosted Vercel page drive the
# unauthenticated LLM routes from visitors' browsers.
_CORS_ORIGIN_REGEX = re.compile(r"^https://orizon-agents-fe-stellar(-[a-z0-9-]+)?\.vercel\.app$")


def _cors_allows(origin: str) -> bool:
    """Mirror the CORS middleware's decision — the exact allow-list OR the
    compiled origin regex — for handlers that must stamp CORS headers by
    hand (the 500 handler runs outside the middleware stack)."""
    return origin in settings.cors_origin_list or _CORS_ORIGIN_REGEX.fullmatch(origin) is not None


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=_CORS_ORIGIN_REGEX.pattern,
    # The API is token/header-based — no cookies — so credentials stay off,
    # and only the methods/headers the frontend actually sends are allowed.
    # x-task-token is the per-task read capability (lib/api.ts): today the
    # frontend proxies same-origin so no preflight happens, but the instant
    # anything calls this API cross-origin with task auth on, its absence
    # here is a hard preflight failure.
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "authorization", "x-api-key", "x-task-token"],
)

# Added last → runs outermost, so artifact/trace payloads (30–76 kB) leave the
# stack compressed while the rate limiter still sees the raw request.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Outermost of all: every response — including 429s from the limiter and
# CORS rejections — carries an X-Request-ID, and the logged duration covers
# the full middleware stack.
app.add_middleware(RequestContextMiddleware)

# Details that are already machine-readable tokens ("invalid_api_key",
# "build_failed") are promoted to the envelope's error code as-is.
_SNAKE_TOKEN = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")


class ErrorBody(BaseModel):
    """Structured half of the unified error envelope."""

    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    """Every error response body: the legacy FastAPI "detail" (string or
    validation-error list) plus the structured "error" object."""

    detail: Any
    error: ErrorBody


# Merged into every routed operation via include_router below, so the docs
# show the envelope on the error statuses any endpoint can produce.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorEnvelope, "description": "Request validation failed."},
    429: {"model": ErrorEnvelope, "description": "Rate limited — retry after the indicated delay."},
    500: {"model": ErrorEnvelope, "description": "Unhandled server error."},
}


def _error_envelope(detail: Any, code: str, message: str) -> dict[str, Any]:
    """Unified error body: the legacy FastAPI "detail" key (unchanged, so
    existing clients keep working) plus an "error" object carrying a stable
    snake_case code, a human message, and the request id for support."""
    return {
        "detail": detail,
        "error": {"code": code, "message": message, "request_id": request_id_var.get()},
    }


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Emit HTTPExceptions in the unified envelope, preserving FastAPI's
    default semantics: same status, same headers, same "detail" value, and
    no body at all for statuses that must not carry one (204/304)."""
    headers = getattr(exc, "headers", None)
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    detail = exc.detail
    if isinstance(detail, str) and _SNAKE_TOKEN.fullmatch(detail):
        code, message = detail, detail.replace("_", " ")
    else:
        try:
            code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        except ValueError:
            code = "http_error"
        message = detail if isinstance(detail, str) else "request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_envelope(jsonable_encoder(detail), code, message),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Same 422 body FastAPI emits by default, plus the "error" object."""
    return JSONResponse(
        status_code=422,
        content=_error_envelope(jsonable_encoder(exc.errors()), "validation_error", "request validation failed"),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the full traceback server-side; never leak exception text to clients."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    # This handler runs on the OUTERMOST layer (ServerErrorMiddleware), so the
    # security-header, rate-limit, and CORS middleware never see the response.
    # Stamp the hardening headers — and the CORS header for known origins —
    # by hand, or browsers report an opaque CORS failure instead of letting
    # the frontend read this JSON envelope.
    headers = {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
    }
    # RequestContextMiddleware's send wrapper never sees this response (the
    # exception unwound past it), so echo the id header here as well.
    request_id = request_id_var.get()
    if request_id != "-":
        headers["x-request-id"] = request_id
    origin = request.headers.get("origin")
    if origin and _cors_allows(origin):
        headers["access-control-allow-origin"] = origin
        headers["vary"] = "Origin"
    return JSONResponse(
        status_code=500,
        content=_error_envelope("internal server error", "internal_error", "internal server error"),
        headers=headers,
    )


app.include_router(agents.router, prefix="/api", responses=_ERROR_RESPONSES)
# Before agents.router would also work, but the binding paths are deliberately
# shaped so they cannot collide with GET /agents/{agent_id} at any position.
app.include_router(binding.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(orchestrator.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(tasks.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(trace.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(metrics.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(flow.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(payments.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(stellar.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(pdax.router, prefix="/api", responses=_ERROR_RESPONSES)
# No _ERROR_RESPONSES: the probe takes no input and is exempt from the rate
# limiter, so the 422/429 rows documented on the other routers cannot occur.
app.include_router(health_router, prefix="/api")


@app.get("/", tags=["meta"], summary="Service identity ping")
async def root() -> dict[str, str]:
    return {"service": "orizon-agents", "status": "online"}


@app.get("/health", tags=["meta"], summary="Liveness probe", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe — process is up and serving. The body comes from the
    shared `health_payload()`, so this route and the proxy-reachable
    `/api/health` (routers/health.py) always answer identically."""
    return health_payload()


class ReadinessResponse(BaseModel):
    """Per-dependency readiness report. Purely config-derived — no live
    network calls, so the probe stays cheap and deterministic."""

    status: str  # "ready" | "not_ready"
    llm: str  # "ok" | "missing_key"
    stellar: str  # "configured" | "incomplete"
    signer: str  # "configured" | "absent" — informational, never gates readiness
    pdax: str  # "configured" | "unconfigured" — informational


@app.get(
    "/readiness",
    tags=["meta"],
    summary="Readiness probe with per-dependency status",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A required dependency is not configured."}},
)
async def readiness(response: Response) -> ReadinessResponse:
    """503 only when a dependency the API cannot serve without is missing:
    the LLM key or the Stellar contract/RPC config. The signing key is
    deliberately informational — read-only deployments are legitimate."""
    llm = "ok" if settings.openai_api_key else "missing_key"

    contract_ids = (
        settings.stellar_agent_registry,
        settings.stellar_payment_escrow,
        settings.stellar_attestation_registry,
        settings.stellar_asset_sac,
    )
    # The admin address is as load-bearing as the contract ids: every
    # simulate_read needs a source account and raises outright without one
    # (app/stellar/client.py), so a deploy missing STELLAR_ADMIN_ADDRESS has
    # 100% of its contract reads failing — it must not report "ready".
    stellar_ok = bool(settings.stellar_rpc_url) and bool(settings.stellar_admin_address) and all(contract_ids)
    # The reputation ledger only matters while reputation-gated routing is on.
    if settings.reputation_enabled and not settings.stellar_reputation_ledger:
        stellar_ok = False

    ready = llm == "ok" and stellar_ok
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        llm=llm,
        stellar="configured" if stellar_ok else "incomplete",
        signer="configured" if settings.stellar_signing_key else "absent",
        pdax="configured" if settings.pdax_username and settings.pdax_password else "unconfigured",
    )
