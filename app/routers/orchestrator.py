import hashlib
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..demo_kits import detect_kit
from ..llm import provider
from ..schemas import DecomposeRequest, DecomposeResponse, ExecuteRequest, ExecuteResponse, StoredPlan
from ..security import CodedHTTPException, ErrorEnvelope, KeyedRateLimiter, client_identity, request_id_var
from ..services import authorization_guard as guard
from ..services import task_persistence
from ..services.execution_svc import CapacityExhaustedError, PlanExpiredError, execute_plan, plan_expired
from ..services.intent_screening import IntentRefused
from ..services.orchestrator_svc import NoRoutableAgentsError, PlannerBusyError, decompose
from ..state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


def _intent_ref(intent: str) -> str:
    """How a log line names a buyer's intent: its length and a short hash.

    Never the text. An intent is whatever the buyer typed about their own
    business, up to 500 characters of it, and the redaction filter only
    removes secret-shaped tokens. The hash still lets an operator tie
    together every refusal of the same intent without reading it.
    """
    digest = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:12]
    return f"len={len(intent)} sha256={digest}"


# Per-client budget for the planner. The global limiter is sized for polling,
# and every decompose that reaches a model is a paid call: every free-form
# one, and on Claude every kit one too (`_spends_model_calls`).
_planner_limiter = KeyedRateLimiter(lambda: settings.decompose_rate_limit_per_minute)


def _spends_model_calls(intent: str) -> bool:
    """Whether this decompose costs a paid model call, and so spends the planner budget.

    A free-form intent always does. A curated kit does on Claude, where
    `screen_kit` puts every kit intent through the jev guard (and the Claude
    fallback guard when jev is down) — calls the daily spend cap does not
    stop, so without this budget a script could drive them unthrottled. Only
    on the legacy provider is a kit free: its plan reads no model at all.
    `decompose` makes the same two decisions.
    """
    return detect_kit(intent) is None or provider.active_provider() == "anthropic"


# What each refusal adds to the error envelope, beside `error.message`: the
# blocked request's reason, or the question to answer before planning. Named
# fields, so a client renders them without parsing a sentence.
_REFUSAL_FIELD: dict[str, str] = {"intent_blocked": "reason", "intent_needs_detail": "question"}


def _refused(err: IntentRefused) -> JSONResponse:
    """A pipeline refusal in the app's error envelope, plus its named field.

    Every message here is written for the buyer (`intent_guard_prompts`,
    `intent_screening`) — plain words, no scores and no model output — which
    is what makes it safe to return as is.
    """
    content: dict[str, Any] = {
        "detail": err.code,
        "error": {"code": err.code, "message": err.message, "request_id": request_id_var.get()},
    }
    field = _REFUSAL_FIELD.get(err.code)
    if field is not None:
        content[field] = err.message
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after is not None else None
    return JSONResponse(status_code=err.status, content=content, headers=headers)


@router.post(
    "/decompose",
    response_model=DecomposeResponse,
    summary="Decompose an intent into a plan",
    responses={
        422: {
            "model": ErrorEnvelope,
            "description": "`intent_blocked` (+ `reason`), `intent_needs_detail` (+ `question`), or invalid input.",
        },
        503: {
            "model": ErrorEnvelope,
            "description": "`intent_unavailable` or `planning_paused` (+ Retry-After), `no_routable_agents`",
        },
    },
)
async def orchestrator_decompose(req: DecomposeRequest, request: Request) -> DecomposeResponse | JSONResponse:
    if _spends_model_calls(req.intent):
        retry_after = _planner_limiter.hit(client_identity(dict(request.scope)))
        if retry_after is not None:
            raise HTTPException(429, "decompose_rate_limited", headers={"Retry-After": str(retry_after)})
    try:
        # `spec` only when sent, so the call is the one it always was otherwise.
        plan = await (decompose(req.intent, spec=req.spec) if req.spec is not None else decompose(req.intent))
    except IntentRefused as e:
        # The request check or the planner declined, or AI planning cannot run
        # now. An answer for the buyer in plain words, not a fault: logged
        # without a traceback, and never with the intent's text.
        logger.info("decompose refused for intent %s: %s", _intent_ref(req.intent), e.code)
        return _refused(e)
    except TimeoutError as e:
        # asyncio.wait_for tripped decompose_timeout_seconds — the LLM hung,
        # nothing else failed. Distinct from the blanket 502 below.
        logger.warning("decompose timed out for intent %s", _intent_ref(req.intent))
        raise HTTPException(504, "decompose_timeout") from e
    except NoRoutableAgentsError as e:
        # Nothing listed and dispatchable was left to offer the planner. The
        # request was fine and the condition clears when an operator binds or
        # relists an agent, so this is a retryable 503 — not the 502 below,
        # which is a fault, and not worth a traceback.
        logger.warning("decompose refused for intent %s: %s", _intent_ref(req.intent), e)
        raise HTTPException(503, "no_routable_agents") from e
    except PlannerBusyError as e:
        # Every planning slot is busy and the wait queue is full. Refused at
        # once rather than queued: retryable, and never a planner call.
        logger.warning("decompose refused, planner busy: %s", e)
        raise HTTPException(503, "planner_busy") from e
    except Exception as e:
        # A planner that failed never lands here: `decompose` serves the
        # fallback plan for it and flags it `planner_fallback` (BLO-121). What
        # is left is a fault nothing anticipated, so it keeps its traceback.
        logger.exception("decompose failed for intent %s", _intent_ref(req.intent))
        raise HTTPException(502, "decompose_failed") from e
    # Write-through for the plan id this hands out: the buyer reads the card
    # and signs against it, and a restart in that minute must not turn their
    # authorisation into a `plan_unknown` release (D-090). Bounded; the write
    # keeps retrying in the background if this gives up on it.
    if not await task_persistence.flush(task_persistence.RESPONSE_FLUSH_SECONDS):
        logger.warning("plan %s: not yet durable when /decompose answered; its write is still queued", plan.plan_id)
    return plan


def _refused_after_release(status: int, detail: str, code: str, message: str, released: guard.Release) -> JSONResponse:
    """A refusal in the app's error envelope, plus what became of the buyer's custody.

    `release_tx_hash` is the extra field: the full-release settle's hash when
    it confirmed — the frontend's "your funds were returned" — and null when
    it was attempted and did not, in which case the custody stays reclaimable
    after `expires_at`. It is present ONLY when a release was attempted, so a
    refusal that never touched the chain keeps the envelope it always had.
    """
    if released.tx_hash:
        message = f"{message} — your authorized funds were returned to your wallet"
    else:
        message = f"{message} — your authorized funds could not be returned now; reclaim them once it expires"
    return JSONResponse(
        status_code=status,
        content={
            "detail": detail,
            "error": {"code": code, "message": message, "request_id": request_id_var.get()},
            "release_tx_hash": released.tx_hash,
        },
    )


async def _response(task_id: str) -> ExecuteResponse:
    # Write-through for the receipt: the task id and read token this hands out
    # must outlive a restart that lands a moment later (D-090). Here, after the
    # authorization is claimed for the task, so a cancelled wait cannot unclaim
    # an authorization the run is spending. Bounded — a slow database delays
    # durability, not the buyer's answer — and the write keeps retrying in the
    # background if this gives up on it.
    if not await task_persistence.flush(task_persistence.RESPONSE_FLUSH_SECONDS):
        logger.warning("task %s: not yet durable when /execute answered; its write is still queued", task_id)
    return ExecuteResponse(task_id=task_id, read_token=state.task_tokens.get(task_id))


@router.post("/execute", response_model=ExecuteResponse, summary="Execute a stored plan")
async def orchestrator_execute(req: ExecuteRequest) -> ExecuteResponse | JSONResponse:
    if (req.auth_id_hex is None) != (req.payer is None):
        # Half a pair used to run SIMULATED without a word, so a buyer who
        # meant to pay got a demo run. Say so instead.
        raise CodedHTTPException(
            422,
            "authorization_incomplete",
            "send both auth_id_hex and payer for a paid run, or neither for a simulated one",
        )
    try:
        # From memory, or read back from the durable store after a restart
        # (D-090) — a plan the buyer authorised moments before one must still
        # execute. A store that cannot answer is a retryable 503, and nothing
        # below runs: on the paid path a missing plan RELEASES the buyer's
        # custody, which a plan that merely could not be read must never do.
        plan = await task_persistence.load_plan(req.plan_id)
    except task_persistence.TaskStoreUnavailable as e:
        raise HTTPException(503, "plan_store_unavailable", headers={"Retry-After": "5"}) from e
    if req.auth_id_hex is None or req.payer is None:
        # The simulated run: no authorization, no charge, no seal — and no
        # on-chain rating, which `execute_plan` writes only on the paid path
        # (ADR 0011, tests/test_execute_guard_route.py pins it).
        if plan is None:
            raise HTTPException(404, f"unknown plan_id: {req.plan_id}")
        try:
            task_id = await execute_plan(plan)
        except CapacityExhaustedError as e:
            # No task was minted; the client should retry once a slot frees up.
            logger.warning("execute rejected for plan %s: %s", req.plan_id, e)
            raise HTTPException(503, "capacity_exhausted") from e
        return await _response(task_id)
    return await _execute_paid(req, plan, req.auth_id_hex, req.payer)


async def _execute_paid(
    req: ExecuteRequest, plan: StoredPlan | None, auth_id_hex: str, payer: str
) -> ExecuteResponse | JSONResponse:
    """A paid run: one task per authorization, and custody back on a run that can never happen.

    Whether the authorization may pay for this run at all — this payer's, for
    this plan, unspent, large enough, long enough — is `execute_plan`'s check
    (`authorization_*` codes, S2), and it is not repeated here. Everything
    below happens under the authorization's lock, so of two concurrent
    executes against it exactly one can start a task (ADR 0011).
    """
    async with guard.exclusive(auth_id_hex):
        # First, and before any release below: a claimed authorization is
        # funding a run, and must never be released out from under it. Only a
        # v2 execute ever claims, so on v1 this never refuses.
        guard.refuse_if_claimed(auth_id_hex)
        if plan is None:
            # Gone — a restart, or evicted by newer plans. The authorization's
            # label is this plan id, so it can never pay for anything else:
            # hand the custody back rather than leave it locked until expiry.
            released = await guard.release_if_owned(auth_id_hex, payer, req.plan_id, reason="plan_unknown")
            detail = f"unknown plan_id: {req.plan_id}"
            if released is None:
                raise HTTPException(404, detail)
            return _refused_after_release(404, detail, "not_found", "this plan is no longer held", released)
        if plan_expired(plan):
            # `execute_plan` refuses this too, first thing; answering here keeps
            # the 410 when the chain cannot be read, as it always was.
            return await _refuse_expired(plan, auth_id_hex, payer)
        # Read, never guessed, before any paid run: an unreadable version is a
        # 503 here, where `execute_plan` would read it as v1 and skip its check.
        enforced = await guard.escrow_version() >= 2
        if enforced:
            guard.claim(auth_id_hex)
        try:
            task_id = await execute_plan(plan, auth_id_hex=auth_id_hex, payer=payer)
        except PlanExpiredError:
            # The plan crossed its TTL between the check above and here.
            guard.unclaim(auth_id_hex)
            return await _refuse_expired(plan, auth_id_hex, payer)
        except CapacityExhaustedError as e:
            guard.unclaim(auth_id_hex)
            logger.warning("execute rejected for plan %s: %s", req.plan_id, e)
            released = await guard.release_if_owned(auth_id_hex, payer, plan.id, reason="capacity_exhausted")
            if released is None:
                raise HTTPException(503, "capacity_exhausted") from e
            return _refused_after_release(
                503, "capacity_exhausted", "capacity_exhausted", "the service is at capacity", released
            )
        except BaseException:
            # Refused before a task was minted — `authorization_*` from
            # `execute_plan` among it. Nothing is released: that authorization
            # may be someone else's, or another plan's.
            guard.unclaim(auth_id_hex)
            raise
        if enforced:
            guard.claim(auth_id_hex, task_id)
    return await _response(task_id)


async def _refuse_expired(plan: StoredPlan, auth_id_hex: str, payer: str) -> JSONResponse:
    released = await guard.release_if_owned(auth_id_hex, payer, plan.id, reason="plan_expired")
    if released is None:
        raise PlanExpiredError(plan.id)
    return _plan_expired(plan, released)


def _plan_expired(plan: StoredPlan, released: guard.Release) -> JSONResponse:
    expired = PlanExpiredError(plan.id)
    return _refused_after_release(410, "plan_expired", "plan_expired", expired.message, released)
