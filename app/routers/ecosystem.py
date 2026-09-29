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

from fastapi import APIRouter, HTTPException

from ..services import adoption_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ecosystem", tags=["ecosystem"])


@router.get(
    "/adoption",
    response_model=adoption_svc.AdoptionReport,
    summary="External operator adoption against SOW §6.3, from on-chain facts",
)
async def adoption() -> adoption_svc.AdoptionReport:
    """Externally operated agents, their owners, and their verified settlements.

    Every owner and every charge links to Stellar Expert. Agents owned by a
    team wallet or a key this deployment holds are listed under `excluded`
    with the reason, never counted. A read that failed sets `degraded` and
    names the agent in `unreadable_agents`: the totals are then a floor, not
    a zero. Settlements are read from Soroban RPC events, which the node keeps
    for about seven days: `window_days` is the span the scans actually covered
    (the smallest, when agents' scans differ; 0 when none ran), and a
    settlement older than that is not counted. Cached for about 30 s.

    503 only when no report could be produced at all — the per-read failures
    above are answered in the report itself.
    """
    try:
        return await adoption_svc.fetch_report()
    except Exception as e:
        logger.error("[adoption] report could not be produced: %s: %s", type(e).__name__, e)
        raise HTTPException(503, "adoption_unavailable") from e
