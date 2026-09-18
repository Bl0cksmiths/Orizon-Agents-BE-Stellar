import logging

from fastapi import APIRouter, HTTPException

from ..schemas import DecomposeRequest, DecomposeResponse, ExecuteRequest, ExecuteResponse
from ..services.execution_svc import CapacityExhaustedError, execute_plan
from ..services.orchestrator_svc import NoRoutableAgentsError, decompose
from ..state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


@router.post("/decompose", response_model=DecomposeResponse, summary="Decompose an intent into a plan")
async def orchestrator_decompose(req: DecomposeRequest) -> DecomposeResponse:
    try:
        return await decompose(req.intent)
    except TimeoutError as e:
        # asyncio.wait_for tripped decompose_timeout_seconds — the LLM hung,
        # nothing else failed. Distinct from the blanket 502 below.
        logger.warning("decompose timed out for intent %r", req.intent)
        raise HTTPException(504, "decompose_timeout") from e
    except Exception as e:
        logger.exception("decompose failed for intent %r", req.intent)
        raise HTTPException(502, "decompose_failed") from e


@router.post("/execute", response_model=ExecuteResponse, summary="Execute a stored plan")
async def orchestrator_execute(req: ExecuteRequest) -> ExecuteResponse:
    plan = state.plans.get(req.plan_id)
    if plan is None:
        raise HTTPException(404, f"unknown plan_id: {req.plan_id}")
    try:
        task_id = await execute_plan(plan, auth_id_hex=req.auth_id_hex, payer=req.payer)
    except CapacityExhaustedError as e:
        # No task was minted; the client should retry once a slot frees up.
        logger.warning("execute rejected for plan %s: %s", req.plan_id, e)
        raise HTTPException(503, "capacity_exhausted") from e
    return ExecuteResponse(task_id=task_id, read_token=state.task_tokens.get(task_id))
