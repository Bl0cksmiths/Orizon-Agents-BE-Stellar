"""Ecosystem evidence — public, read-only answers a reviewer can verify on-chain.

`GET /ecosystem/adoption` is SOW §6.3's three numbers: externally operated
agents, unique operator wallets, and workflows settled to external agents. See
app/services/adoption_svc.py and docs/decisions/0012-adoption-evidence.md.

Public on purpose: the point is that anyone can check the claim without an
account, a key, or our word for it. It spends no key and moves nothing, and it
sits under the service-wide RateLimitMiddleware like every other public read.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import http_cache
from ..services import adoption_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ecosystem", tags=["ecosystem"])

# How long a shared cache may hold the report past its freshness while it
# fetches the next one. The report is rebuilt every 5 minutes; a CDN copy a
# minute old says nothing a fresher one would not.
ADOPTION_SHARED_MAX_AGE_SECONDS = 60.0
ADOPTION_STALE_WHILE_REVALIDATE_SECONDS = 600


class AdoptionPending(BaseModel):
    """The 202 answer while the first report since boot is being computed."""

    status: Literal["computing"]
    message: str
    retry_after_seconds: int


@router.get(
    "/adoption",
    response_model=adoption_svc.AdoptionReport,
    summary="External operator adoption against SOW §6.3, from on-chain facts",
    responses={
        202: {"model": AdoptionPending, "description": "The first report since boot is still being computed."},
        304: {"description": "Not modified: the `If-None-Match` ETag is current."},
        503: {"description": "No report could be produced; the next attempt is scheduled."},
    },
)
async def adoption(request: Request) -> Response:
    """Externally operated agents, their owners, and their verified settlements.

    Every owner and every charge links to Stellar Expert. Agents owned by a
    team wallet or a key this deployment holds are listed under `excluded`
    with the reason, never counted. A read that failed sets `degraded` and
    names the agent in `unreadable_agents`: the totals are then a floor, not
    a zero. Settlements are read from Soroban RPC events, which the node keeps
    for about seven days: `window_days` is the span the scans actually covered
    (the smallest, when agents' scans differ; 0 when none ran), and a
    settlement older than that is not counted.

    Computed in the background from one scan of the escrow's settlement events
    for every external agent, kept between builds so each reads only the
    ledgers closed since; rebuilt every 5 minutes and when the registry's
    agents change. The last report is served at once: `generated_at` dates
    it, and so do `Last-Modified` and `X-Snapshot-Age`;
    `X-Snapshot-Source: persisted` marks one restored from the database after
    a restart, served whatever its age until the new process's first build
    lands. A build that runs out of time publishes what it read as a PARTIAL
    report — `complete: false`, `degraded: true`, every number a floor, and
    `coverage` saying how much was read — unless a complete report under an
    hour old is in service, which is kept instead. With no report yet the
    answer is 202 with `Retry-After`; 503 only when the last attempt failed
    and there is nothing to serve.
    """
    snap = await adoption_svc.report_snapshot()
    if snap is not None:
        return http_cache.snapshot_response(
            request,
            snap,
            fresh_seconds=ADOPTION_SHARED_MAX_AGE_SECONDS,
            stale_while_revalidate=ADOPTION_STALE_WHILE_REVALIDATE_SECONDS,
        )
    status = adoption_svc.report_cell.status()
    if status.building or status.last_error is None:
        # Building, or about to: the gate holds the first build until the
        # registry mirror is complete, and the schedule starts it then.
        retry = adoption_svc.REPORT_PENDING_RETRY_AFTER_SECONDS
        pending = AdoptionPending(
            status="computing",
            message=(
                "The adoption report is being computed from on-chain data "
                "(one scan of settlement events for every external agent). Ask again shortly."
            ),
            retry_after_seconds=retry,
        )
        return Response(
            content=pending.model_dump_json(),
            status_code=202,
            media_type=http_cache.JSON,
            headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
        )
    logger.error("[adoption] no report to serve: %s", status.last_error)
    raise HTTPException(
        503,
        "adoption_unavailable",
        headers={"Retry-After": str(int(adoption_svc.REPORT_RETRY_AFTER_FAILURE_SECONDS))},
    )
