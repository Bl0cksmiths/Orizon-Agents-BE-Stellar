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
from typing import Any, Literal

from fastapi import FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .agno_logging import install as install_agno_log_hand_back
from .config import SERVICE_VERSION, settings

# Imported by symbol, not as a module: the root `/health` handler defined
# below rebinds the name `health` at module scope, which would shadow a
# `from .routers import health` module import at call time.
from .http_cache import SNAPSHOT_AGE_HEADER, SNAPSHOT_SOURCE_HEADER
from .llm import provider as llm_provider
from .llm.provider import LLMPublicReadiness, LLMReadiness
from .origin_lock import OriginLockMiddleware, OriginLockReadiness
from .origin_lock import readiness as origin_lock_readiness
from .pdax.client import aclose_pdax_client
from .rate_limit import RouteRateLimitMiddleware
from .routers import (
    agents,
    binding,
    disputes,
    ecosystem,
    flow,
    metrics,
    orchestrator,
    payments,
    pdax,
    stellar,
    tasks,
    trace,
)
from .routers.agents import NEXT_CURSOR_HEADER, REGISTRY_COUNT_HEADER, REGISTRY_SYNCED_HEADER, TOTAL_COUNT_HEADER
from .routers.health import HealthResponse, health_payload
from .routers.health import router as health_router

# ErrorEnvelope (and the ErrorBody it nests) live in security.py, not here, so
# a router can name them in its own `responses=` without importing this module
# — which imports the routers. Re-exported by this import, so
# `main.ErrorEnvelope` still resolves for anything that reads it from here.
from .security import (
    BodyLimitMiddleware,
    ErrorEnvelope,
    RateLimitMiddleware,
    RequestContextMiddleware,
    RequestIdLogFilter,
    SecretRedactionLogFilter,
    SecurityHeadersMiddleware,
    header_secret_matches,
    request_id_var,
    security_headers,
    strict_cors_origins,
)
from .seed import seed_registry
from .services import (
    execution_svc,
    platform_treasury,
    rating_writer,
    refund_reconcile,
    registry_sync,
    reputation_svc,
    snapshots,
    task_persistence,
    task_warmup,
)
from .services.binding_registry import refresh_bound_ids, start_refresh_retry, stop_refresh_retry
from .services.binding_store import close_binding_store
from .services.dispute_store import PostgresDisputeStore, close_dispute_store, get_dispute_store
from .services.external_binding import ChallengeBudgetExhausted
from .services.snapshot_store import close_snapshot_store
from .stellar import client as sc


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
        line = {
            "ts": f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')}.{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
            "request_id": getattr(record, "request_id", "-"),
        }
        # Structured fields a record carries (the access log's `http`).
        http = getattr(record, "http", None)
        if isinstance(http, dict):
            line["http"] = http
        return json.dumps(line, ensure_ascii=False, default=str)


# Root logging: everything the app emits leaves as one JSON line carrying the
# current request id. Uvicorn's own loggers keep their handlers (its access
# and error loggers don't propagate to root), so they are unaffected.
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(JsonLogFormatter())
_log_handler.addFilter(RequestIdLogFilter())
# Last, so it masks the record every earlier filter has finished with. See
# SecretRedactionLogFilter for why this cannot be left to call sites.
_log_handler.addFilter(SecretRedactionLogFilter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler], force=True)

# httpx logs "HTTP Request: POST <full URL>" at INFO for every request an
# AsyncClient sends. External dispatch posts to an operator's bound endpoint,
# whose URL can carry a query-string token, so at INFO every dispatch wrote that
# URL into our logs — against ADR 0003's rule to log the host, never the URL.
# Held at WARNING: the app logs what it dispatched itself, without the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

# agno's loggers go through the root handler too (JSON, request id, redaction),
# however late agno itself is first imported — see app/agno_logging.py.
install_agno_log_hand_back()
logger = logging.getLogger(__name__)


def _report_cold_start_routability() -> None:
    """State at boot whether this deployment can route a brand-new agent.

    Registration is open to anyone and routing is gated on reputation, which
    a newcomer does not have. The two only coexist because the floor is
    applied to the prior-smoothed lower bound rather than to the raw on-chain
    mean, which leaves a margin — 177 bps under the shipped config. Close that
    margin (raise REPUTATION_FLOOR_BPS past the prior bound, or lower
    REPUTATION_PRIOR_BPS or REPUTATION_PRIOR_WEIGHT_USDC) and no agent
    registered from then on is ever routed, ever rated, or ever able to climb
    out. Nothing raises and no request errors: registration keeps succeeding
    and nobody new is hired, so the failure mode IS silence.

    Reported from lifespan rather than joining config.py's startup-report
    validators for two reasons. The predicate is reputation_svc's arithmetic
    and reputation_svc imports settings, so config.py cannot import it back
    without a cycle. And only a check in the running process sees the value
    actually in force — this line at boot, and /readiness's `cold_start` on
    demand: the Render dashboard overrides render.yaml, so a floor raised
    there reaches no test and no repo default — CI would keep passing against
    numbers this deployment does not use.

    A warning, never a refusal to boot. config.py raises for configs that
    would expose money routes or sign on the wrong network; a high floor is a
    policy an operator may genuinely intend (a curated network that hires only
    rated agents), and taking a live mainnet service down over a policy choice
    is a worse outcome than a loud line. The healthy case is reported too, at
    INFO, because a check that speaks only when it fails is indistinguishable
    from a check that never ran — which is precisely the ambiguity this
    exists to remove.
    """
    margin = reputation_svc.cold_start_margin()
    if margin.clears:
        logger.info(
            "cold start ok: a newly registered agent scores %d bps against REPUTATION_FLOOR_BPS=%d "
            "(REPUTATION_PRIOR_BPS=%d, REPUTATION_PRIOR_WEIGHT_USDC=%g) — %d bps of margin, so new "
            "agents are routable.",
            margin.lower_bound_bps,
            margin.floor_bps,
            margin.prior_bps,
            margin.prior_weight_usdc,
            margin.margin_bps,
        )
        return
    logger.warning(
        "cold start BROKEN: REPUTATION_FLOOR_BPS=%d is above the prior lower bound of %d bps "
        "(REPUTATION_PRIOR_BPS=%d, REPUTATION_PRIOR_WEIGHT_USDC=%g), a margin of %d bps. A newly "
        "registered agent has no on-chain evidence, so it is scored on that bound, misses the floor "
        "on its first request and is never routed — never routed means never rated, and never rated "
        "means it can never clear the floor. Registration stays open and no new agent is ever hired. "
        "Lower REPUTATION_FLOOR_BPS to %d or below, or raise REPUTATION_PRIOR_BPS / "
        "REPUTATION_PRIOR_WEIGHT_USDC. The value in force is whatever the Render dashboard sets — it "
        "overrides render.yaml.",
        margin.floor_bps,
        margin.lower_bound_bps,
        margin.prior_bps,
        margin.prior_weight_usdc,
        margin.margin_bps,
        margin.lower_bound_bps,
    )


# How long boot holds the first request for the bound-id set (see lifespan).
BOOT_BINDING_LOAD_BUDGET_SECONDS = 3.0

# Boot work that runs on past startup, held so shutdown can stop it.
_boot_tasks: set[asyncio.Task[None]] = set()


async def _load_bindings() -> None:
    """The bound-id load, then its background retry if the load failed."""
    await refresh_bound_ids()
    start_refresh_retry()


async def _warm_reputation() -> None:
    """Wait, bounded, for the registry's first pass, then pre-warm reputation."""
    await registry_sync.wait_first_pass(settings.registry_boot_sync_timeout_seconds)
    # Last, so the reads it queues cannot delay anything above; after the
    # registry wait, so the on-chain agents that pass indexed are read too.
    reputation_svc.start_prewarm()


async def _warm_tasks() -> None:
    """Load the newest tasks from the durable store, then let the overview's
    completion rate pick them up on its next poll."""
    if await task_warmup.warm_recent_tasks():
        metrics.overview_cell.expire()


async def _stop_boot_tasks() -> None:
    tasks = [t for t in _boot_tasks if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=5)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # First thing in the boot sequence: whether this config admits new agents
    # at all is a property no request or error will ever report — only this
    # line and /readiness's `cold_start`, which has to be asked — so it is
    # stated before anything else can bury it.
    _report_cold_start_routability()
    # Resolve the dispute store now, so its line (postgres, or the in-memory
    # fallback at WARNING) is in the boot log rather than inside whichever
    # request first touches a settlement or a dispute — on a quiet instance
    # that request may never come, and the line with it (D-063). Constructing
    # the store dials nothing: a Postgres that is unreachable at boot still
    # fails its first real use loudly, and never falls back to memory.
    get_dispute_store()
    seed_registry()
    # Bound the default executor: asyncio.to_thread otherwise sizes it to
    # min(32, cpu_count + 4) from the HOST's core count, while Render grants
    # this container only a small CPU share — 8 threads comfortably cover the
    # blocking Soroban SDK calls without oversubscribing the worker.
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="soroban")
    asyncio.get_running_loop().set_default_executor(executor)
    # Whether this deployment can write ratings at all — the second silent
    # failure this boot sequence names (services/rating_writer.py). Its answer
    # needs one chain read, so it runs in the background, after the executor
    # bind so that read uses the bounded pool; its line lands a moment after
    # boot rather than holding the waking request behind the RPC.
    rating_writer.start()
    # Mirror on-chain registrations into the marketplace (story 1.02). Started
    # after the executor bind so its to_thread reads use the bounded pool; the
    # loop no-ops while STELLAR_AGENT_REGISTRY is blank, which keeps the
    # hermetic test suite offline.
    registry_sync.start()
    # Settle refund claims parked in `crediting` from the chain (see
    # services/refund_reconcile.py). Off unless REFUND_RECONCILE_ENABLED and
    # DISPUTE_REFUNDS_ENABLED are both on; its chain reads go through the
    # bounded pool bound above, and it signs nothing.
    refund_reconcile.start()
    # Seed the planner's routability set from the binding store. Without this a
    # binding made before this process started would stay unroutable until the
    # operator bound it again — which is precisely the restart AC-5 is about.
    # ...and if that read failed, keep trying in the background. The load
    # swallows its own failure so an unreadable store cannot stop the service,
    # which used to mean a store that was merely SLOW to wake — a cold
    # serverless Postgres, exactly what the min_size=0 pool is built for — left
    # every externally operated agent unroutable for the whole process
    # lifetime, with nothing but a redeploy to fix it. A no-op on the healthy
    # path: the load has already set `_loaded` and no task is created.
    #
    # Boot waits for the load only up to BOOT_BINDING_LOAD_BUDGET_SECONDS. A
    # Neon compute waking from suspend answers in a second or two, and that is
    # worth holding the first request for; a store that takes longer is not —
    # the request that woke the instance would wait on it, the health check
    # with it. Past the budget the load is NOT cancelled: it finishes in the
    # background, and its retry is scheduled when it does.
    bindings = asyncio.get_running_loop().create_task(_load_bindings(), name="boot-bindings")
    _boot_tasks.add(bindings)
    bindings.add_done_callback(_boot_tasks.discard)
    _done, pending_load = await asyncio.wait({bindings}, timeout=BOOT_BINDING_LOAD_BUDGET_SECONDS)
    if pending_load:
        logger.warning(
            "binding store: the bound-id set did not load within %.1f s of boot — serving without it; external "
            "agents are unroutable until the load, still running in the background, lands",
            BOOT_BINDING_LOAD_BUDGET_SECONDS,
        )
    # The registry sync's first pass and the reputation pre-warm that reads it,
    # in the background. The pre-warm waits, bounded, for the pass — started
    # above — so it reads the on-chain agents too; without that the pre-warm
    # read the seeded catalog alone (S13). Boot used to await that wait
    # itself, which on the live registry (~600 agents, a pass of minutes) was
    # REGISTRY_BOOT_SYNC_TIMEOUT_SECONDS added to every wake from idle for
    # nothing: the pass never finished inside it. The mirror says whether it
    # is complete (`X-Registry-Synced`, `registry_synced`), so nothing served
    # in the meantime claims to be the whole registry.
    warmup = asyncio.get_running_loop().create_task(_warm_reputation(), name="boot-reputation-warmup")
    _boot_tasks.add(warmup)
    warmup.add_done_callback(_boot_tasks.discard)
    # The newest tasks from the durable store, so the task list and the
    # overview's completion rate are not empty after a restart. Background
    # and bounded (services/task_warmup.py): boot never waits on it.
    tasks_warmup = asyncio.get_running_loop().create_task(_warm_tasks(), name="boot-task-warmup")
    _boot_tasks.add(tasks_warmup)
    tasks_warmup.add_done_callback(_boot_tasks.discard)
    # The read snapshots behind the dashboard (overview, reputation batch,
    # adoption report): their keep-warm refresher, and the restore of the
    # last adoption report from the database. Background only.
    snapshots.start()
    yield
    # The boot tasks and the snapshot builds first: each may still be reading
    # through the stores and the read pool that the steps below close.
    await _stop_boot_tasks()
    await snapshots.stop()
    # Before anything else in the shutdown: a retry sitting in a 120 s sleep
    # would otherwise still be pending when the loop closes.
    await stop_refresh_retry()
    # Same reason: a scorer read still in flight must not outlive the loop.
    await rating_writer.stop()
    await reputation_svc.stop_prewarm()
    await reputation_svc.stop_refreshes()
    reputation_svc.shutdown_read_pool()
    # Stop the sync loop first — it must not fire a fresh RPC pass while the
    # shutdown below is draining execution tasks.
    await registry_sync.stop()
    # Before the drain below: a pass must not start writing dispute records
    # while the store it writes to is about to be closed.
    await refund_reconcile.stop()
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
    # After the drain, so the runs' final states are among what is flushed:
    # the task journal's queued writes go to the store (bounded), then its
    # pool is released. Before the dispute store closes, like every store.
    await task_persistence.close()
    await aclose_pdax_client()
    # Release the binding store's connection pool. A no-op for the in-memory
    # store, which is what runs whenever DATABASE_URL is unset.
    await close_binding_store()
    # The dispute store's pool, on the same terms: also a no-op in-memory, and
    # also the one place its Postgres connections are handed back — a settlement
    # is written on the execution path, so this store is live on any deployment
    # that has settled a workflow, not only one an operator has bound.
    await close_dispute_store()
    await close_snapshot_store()
    # The model layer: Claude and jev clients and the spend ledger's pool.
    await llm_provider.close()
    executor.shutdown(wait=False)


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
        {
            "name": "disputes",
            "description": (
                "Dispute a settled step inside its 24-hour window, proved by the payer's wallet signature; "
                "and the operator-keyed adjudication that upholds one into a credit or rejects it."
            ),
        },
        {"name": "pdax", "description": "PDAX PHP-to-crypto on/off-ramp: trade, funding, withdrawals, webhooks."},
    ],
)

# Added first → runs innermost: oversized bodies are rejected before the
# router, while the 413 still passes through the header/CORS/request-id
# layers wrapping it.
app.add_middleware(BodyLimitMiddleware)

# Per-route budgets for the write and expensive routes (app/rate_limit.py).
# Inside the global limiter, so a request it refused never spends one, and
# outside the body limiter, which still meters the body this layer replays.
app.add_middleware(RouteRateLimitMiddleware)

# Registered before CORS so CORS wraps it and 429 responses still carry
# the Access-Control-Allow-Origin header the browser needs to read them.
app.add_middleware(RateLimitMiddleware)

# The origin lock (app/origin_lock.py, ADR 0017): /api/* answers our frontend
# only. Starlette runs middleware in the REVERSE of the order added, so being
# added here puts it inside SecurityHeaders, CORS, GZip and RequestContext and
# outside both rate limiters and the body cap. That is the order it needs:
#   * outside the limiters and the body cap, so a refused request costs a
#     header compare — it spends no visitor's rate budget (a flood of direct
#     calls cannot 429 the real frontend's shared buckets) and no body is read;
#   * inside RequestContext, so its log line and its 403 carry the request id;
#   * inside SecurityHeaders and CORS, so the 403 is stamped with the
#     hardening headers and, for an allowed origin, the CORS header a browser
#     needs to read it. CORS also answers preflights before they reach it.
app.add_middleware(OriginLockMiddleware)

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
    return origin in strict_cors_origins(settings.cors_origin_list) or _CORS_ORIGIN_REGEX.fullmatch(origin) is not None


app.add_middleware(
    CORSMiddleware,
    allow_origins=strict_cors_origins(settings.cors_origin_list),
    allow_origin_regex=_CORS_ORIGIN_REGEX.pattern,
    # The API is token/header-based — no cookies — so credentials stay off,
    # and only the methods/headers the frontend actually sends are allowed.
    # x-task-token is the per-task read capability (lib/api.ts): today the
    # frontend proxies same-origin so no preflight happens, but the instant
    # anything calls this API cross-origin with task auth on, its absence
    # here is a hard preflight failure.
    allow_credentials=False,
    # DELETE is here for `DELETE /api/agents/{id}/bind`, an operator revoking
    # their endpoint. It is the one call in this API a compromised operator
    # makes under time pressure, and a missing preflight method fails it with a
    # browser CORS error rather than anything the console could explain. The
    # console proxies same-origin today, so nothing exercises this list — which
    # is precisely why it would have been found the first time it mattered.
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["content-type", "authorization", "x-api-key", "x-task-token", "x-dispute-read-grant"],
    # Response headers a cross-origin caller may read. GET /api/agents says in
    # these whether its list is the whole registry, how long it is and where
    # the next page starts (routers/agents.py); the snapshot reads say how old
    # they are (app/http_cache.py); ETag is what a client revalidates with. A
    # browser hides any header not listed here from cross-origin script.
    expose_headers=[
        REGISTRY_SYNCED_HEADER,
        REGISTRY_COUNT_HEADER,
        TOTAL_COUNT_HEADER,
        NEXT_CURSOR_HEADER,
        SNAPSHOT_AGE_HEADER,
        SNAPSHOT_SOURCE_HEADER,
        "ETag",
        "Retry-After",
    ],
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
        # The derived message — the token with its underscores swapped for
        # spaces — unless the raiser attached one of its own. A
        # `CodedHTTPException` carries a sentence that says what the code
        # cannot (the time a window closed, what to do next), and is raised
        # only where that sentence has been judged safe to disclose to
        # whoever is being refused. The code is unaffected either way, so no
        # client's mapping changes.
        code = detail
        message = getattr(exc, "message", None) or detail.replace("_", " ")
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


@app.exception_handler(ChallengeBudgetExhausted)
async def challenge_budget_handler(request: Request, exc: ChallengeBudgetExhausted) -> JSONResponse:
    """A challenge mint refused for want of room, in the unified envelope.

    Handled here rather than in each of the three mint routes, because it is
    one answer and `routers/binding.py` and `routers/disputes.py` would
    otherwise both need to learn a service exception in order to give it. This
    module already owns every error body; this is one more.

    503, not 429: the caller being refused is usually not the caller who
    filled the budget, and nothing about their own rate is the problem. It is
    a capacity state of the service, it clears on its own as challenges expire
    (five minutes at the outside), and the honest thing to say is "not now".

    The purpose is in the code so an operator reading a log can tell which
    budget is under pressure. It is one of `CHALLENGE_BUDGETS`' own keys and
    never caller text, so no request can shape the token a client branches on.
    """
    return JSONResponse(
        status_code=503,
        content=_error_envelope(
            f"challenge_capacity_{exc.purpose}",
            f"challenge_capacity_{exc.purpose}",
            f"no {exc.purpose} challenge capacity right now — ask again shortly",
        ),
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
    headers = {name.decode(): value.decode() for name, value in security_headers(request.url.path)}
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
# After tasks.router, which owns the other `/tasks/{task_id}/...` reads. Order
# is not load-bearing here — `GET /tasks/{task_id}/disputes` is a literal third
# segment and `tasks.py` declares no catch-all that could swallow it — but
# keeping the two adjacent is what makes that checkable at a glance.
app.include_router(disputes.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(trace.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(metrics.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(flow.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(payments.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(stellar.router, prefix="/api", responses=_ERROR_RESPONSES)
app.include_router(ecosystem.router, prefix="/api", responses=_ERROR_RESPONSES)
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


class ColdStartReadiness(BaseModel):
    """Whether this deployment can route an agent that has never been rated.

    The startup line from `_report_cold_start_routability` says the same
    thing, once per boot — and on the free tier a boot is every wake from idle,
    so by the time anyone asks, the line is buried under a request log or gone
    with the instance that wrote it. This puts the verdict behind a probe that
    answers whenever it is asked, for the configuration actually in force: the
    Render dashboard overrides render.yaml, so no repo file can say what it is.

    Informational only — it never moves `status`, for the startup check's own
    reason. Readiness answers "can this process serve"; a floor above the
    prior's bound serves every request correctly and simply hires nobody new,
    which is a policy an operator may intend (a curated network), not a missing
    dependency. A not-ready answer would also misfire twice over: it would fail
    probes on a deployment that is working as configured, and a monitor keyed on
    the status would page someone about a decision rather than a fault.

    Every field is copied from `reputation_svc.cold_start_margin()` rather than
    recomputed, so this answer and the startup line cannot disagree. None of it
    is secret: the floor and the prior inputs are already public on
    /api/stellar/reputation/params, and the rest is arithmetic over them.
    """

    routable: bool  # a prior-only newcomer clears the routing floor
    lower_bound_bps: int  # what a newcomer is scored on: the prior's lower bound
    floor_bps: int  # REPUTATION_FLOOR_BPS as this process has it
    margin_bps: int  # lower_bound_bps - floor_bps; negative locks newcomers out


class RatingsReadiness(BaseModel):
    """Whether this deployment can write ratings — `rating_writer`'s verdict.

    `signer` says "configured" on a key's mere presence, which cannot tell a
    working deployment from one whose key is not the ReputationLedger's Scorer
    — where every rating reverts with Unauthorized while the service looks
    healthy. `writer` answers the real question for the configuration and the
    chain actually in force; services/rating_writer.py defines its statuses.

    Informational only, like `cold_start` and for the same reason: a
    deployment that cannot rate still serves every request, and read-only
    deployments are legitimate. It adds no live network call to the probe
    either: it reports the last cached chain read and, when that is stale,
    starts a background refresh for the next probe to see.

    Nothing here is secret. `signer` is the public key — the secret never
    leaves the keypair — and `scorer` is public chain data. Both are here
    because the fix for `not_scorer` is `set_scorer(<signer>)`, and after a
    restart has taken the startup line with it, this is where an operator can
    still read the two addresses.
    """

    writer: rating_writer.WriterStatus  # disabled | no_signer | scorer | not_scorer | unchecked
    signer: str | None  # G… ratings are signed with; null unless the chain decides
    scorer: str | None  # the ledger's stored Scorer as last read; null unless a read found one


class RefundReconcileReadiness(BaseModel):
    """The refund reconcile sweep (services/refund_reconcile.py) and its last pass.

    `enabled` is both switches together — REFUND_RECONCILE_ENABLED and the
    DISPUTE_REFUNDS_ENABLED it depends on — and `running` whether the loop is
    alive in this process. The last pass is counts by action and nothing
    else: no dispute ids and no hashes reach this unauthenticated route. A
    nonzero `history_gap`, `no_hash`, `not_this_refund`, `amount_mismatch`,
    `not_crediting`, `missing` or `lost_race` is a claim waiting on a human,
    and the log names it. `last_skipped` says why a pass read nothing, such
    as a deployment that cannot pay credits. Informational, like the rest.
    """

    enabled: bool
    running: bool
    last_run_at: float | None  # epoch seconds, this process's clock
    last_skipped: str | None
    last_outcomes: dict[str, int]


class DisputesReadiness(BaseModel):
    """Which store holds settlements and disputes in this process (D-063).

    `postgres` when DATABASE_URL is set, `memory` otherwise — and `memory`
    loses every settlement and dispute on restart (D-058), which is the check
    an operator runs after a deploy. The boot log names it too; this is for
    whoever cannot read that log, or reads it after a restart took the line.

    The KIND of store only, never the DSN or anything derived from it: the DSN
    carries the database password. It reports the selection, not a live
    connection — the probe dials nothing, and a Postgres that is unreachable
    is answered by the first request that needs it, loudly, never by a quiet
    fall back to memory.

    Informational, like `cold_start`: an in-memory store serves every request.
    """

    store: Literal["postgres", "memory"]
    reconcile: RefundReconcileReadiness


class EscrowReadiness(BaseModel):
    """Which PaymentEscrow this process settles through, and its version (ADR 0010).

    `version` is 2 when runs are settled per delivered step through custody,
    1 when they go through v1's `charge`, and null until this process has read
    it. Never read on the probe's path: the answer is the client's cache, and a
    probe that finds none starts one background read for the next probe to
    see, the way `ratings` refreshes. Informational, like the rest.
    """

    contract: str  # the configured escrow id, public chain data
    version: int | None


class RegistryReadiness(BaseModel):
    """Whether the on-chain registry mirror is complete (services/registry_sync.py).

    After a restart the mirror fills one read at a time, and GET /api/agents
    serves whatever it holds so far, so a count taken then is a prefix of the
    registry. `synced` is false until the first full pass since boot has
    finished, and stays true after it. `agents` is the mirror's size at the
    latest full pass and `last_full_sync_at` its epoch seconds, both null
    before one; `syncing` is true while a pass is running. Informational,
    like the rest: a partial mirror still serves every request.
    """

    synced: bool
    syncing: bool
    agents: int | None
    last_full_sync_at: float | None  # epoch seconds, this process's clock


class TreasuryReadiness(BaseModel):
    """The platform treasury and what the registry says of each built-in agent (ADR 0016).

    `address` is the team register's platform treasury — the owner the built-in
    agents are registered to, and the only account a built-in step is ever
    paid to — or null when the register names none (or names two). `agents`
    is each built-in agent's verdict from the registry mirror's latest pass:
    registered | mismatch | foreign_owner | unregistered | unread. From memory,
    never a read on the probe's path. Informational: a plan prices from the
    catalog whatever it says, and a step is paid only when the settle-time
    `owner_of` is the treasury.
    """

    address: str | None
    agents: dict[str, registry_sync.PlatformVerdict]


def _treasury_readiness() -> TreasuryReadiness:
    try:
        address = platform_treasury.treasury_address()
    except platform_treasury.TreasuryError as e:
        logger.warning("readiness: %s", e)
        address = None
    return TreasuryReadiness(address=address, agents=registry_sync.platform_agents())


_escrow_version_probe: asyncio.Task | None = None


def _escrow_readiness() -> EscrowReadiness:
    """The configured escrow's cached version, kicking one background read if none."""
    global _escrow_version_probe
    escrow = settings.stellar_payment_escrow
    version = sc.cached_escrow_version(escrow) if escrow else None
    if escrow and version is None and (_escrow_version_probe is None or _escrow_version_probe.done()):

        async def _probe() -> None:
            try:
                await asyncio.to_thread(sc.escrow_version, escrow)
            except Exception as e:
                logger.warning("readiness: escrow %s version unreadable: %s", escrow, e)

        _escrow_version_probe = asyncio.create_task(_probe())
    return EscrowReadiness(contract=escrow, version=version)


class ReadinessResponse(BaseModel):
    """Per-dependency readiness report. No live network calls, so the probe
    stays cheap and deterministic: everything is config-derived except
    `ratings`, which reports the rating writer's last cached chain read."""

    status: str  # "ready" | "not_ready"
    llm: str  # "ok" | "missing_key" — the active provider's key (app/llm/provider.py)
    stellar: str  # "configured" | "incomplete"
    signer: str  # "configured" | "absent" — informational, never gates readiness
    pdax: str  # "configured" | "unconfigured" — informational
    cold_start: ColdStartReadiness  # informational, never gates readiness
    ratings: RatingsReadiness  # informational, never gates readiness
    disputes: DisputesReadiness  # informational, never gates readiness
    escrow: EscrowReadiness  # informational, never gates readiness
    registry: RegistryReadiness  # informational, never gates readiness
    treasury: TreasuryReadiness  # informational, never gates readiness
    origin_lock: OriginLockReadiness  # informational, never gates readiness
    # Informational. Anyone: provider and planning active|paused (+ when a
    # pause lifts). With the operator X-API-Key: keys present, models, and
    # today's spend against the cap — numbers an abuser must not see.
    orchestrator: LLMReadiness | LLMPublicReadiness


@app.get(
    "/readiness",
    tags=["meta"],
    summary="Readiness probe with per-dependency status",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A required dependency is not configured."}},
)
async def readiness(
    response: Response,
    x_api_key: str | None = Header(
        default=None,
        alias="X-API-Key",
        description="Optional. The operator's API_KEY adds the AI spend figures to `orchestrator`.",
    ),
) -> ReadinessResponse:
    """503 only when a dependency the API cannot serve without is missing:
    the LLM key or the Stellar contract/RPC config. The signing key is
    deliberately informational — read-only deployments are legitimate — and
    so is `cold_start`: a floor that shuts newcomers out is a policy the
    process serves correctly, not a dependency it lacks. `ratings` is
    informational on the signing key's own grounds: a deployment that cannot
    write ratings still serves every request."""
    # The key of the provider the orchestrator actually runs on: Claude once
    # ANTHROPIC_API_KEY is set (or ORCHESTRATOR_PROVIDER says so), OpenAI until
    # then. jev's key never gates — the guard falls back to Claude without it.
    llm = "ok" if llm_provider.provider_key_present() else "missing_key"

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
    # Read after the verdict and never folded into it — see ColdStartReadiness.
    margin = reputation_svc.cold_start_margin()
    # Likewise, and from cache: never a live read on the probe's path — see
    # RatingsReadiness.
    writer = rating_writer.verdict()
    rating_writer.refresh_if_stale()
    sync = registry_sync.status()
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        llm=llm,
        stellar="configured" if stellar_ok else "incomplete",
        signer="configured" if settings.stellar_signing_key else "absent",
        pdax="configured" if settings.pdax_username and settings.pdax_password else "unconfigured",
        cold_start=ColdStartReadiness(
            routable=margin.clears,
            lower_bound_bps=margin.lower_bound_bps,
            floor_bps=margin.floor_bps,
            margin_bps=margin.margin_bps,
        ),
        ratings=RatingsReadiness(writer=writer.status, signer=writer.signer, scorer=writer.scorer),
        disputes=DisputesReadiness(
            store="postgres" if isinstance(get_dispute_store(), PostgresDisputeStore) else "memory",
            reconcile=RefundReconcileReadiness(**refund_reconcile.status()),
        ),
        escrow=_escrow_readiness(),
        registry=RegistryReadiness(
            synced=sync.synced,
            syncing=sync.syncing,
            agents=sync.agents,
            last_full_sync_at=sync.last_full_sync_at,
        ),
        treasury=_treasury_readiness(),
        # From memory: the mode and this process's refusal counts (ADR 0017).
        origin_lock=origin_lock_readiness(),
        # A wrong or absent key is simply the public view: a probe never 401s.
        orchestrator=llm_provider.readiness(operator=header_secret_matches(x_api_key, settings.api_key)),
    )
