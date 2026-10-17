"""Liveness probe, re-served under the frontend proxy's `/api` prefix.

The product frontend reaches this API only through a same-origin rewrite of
`/api/*`, so the root `/health` route — which sits OUTSIDE that prefix — is
unreachable from the browser and from any uptime monitor pointed at the
public domain. A total outage (every `/api/*` call 404ing) can therefore go
unnoticed. This router answers the identical payload at `GET /api/health`.

`health_payload()` is the single source of that payload: both the root route
in main.py and this one call it, so the two bodies can never drift.
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import SERVICE_VERSION
from ..state import state

router = APIRouter(tags=["meta"])


class HealthResponse(BaseModel):
    """Liveness body: process facts only, never dependency status."""

    status: str  # always "ok" — reaching the handler is the signal
    version: str
    uptime_seconds: float


def health_payload() -> HealthResponse:
    """Build the liveness body shared by `/health` and `/api/health`.

    Deliberately cheap and side-effect free: no Soroban, Horizon, OpenAI, or
    PDAX calls and no contract reads, so a monitor can poll it as often as it
    likes. Whether the dependencies are usable is `/readiness`'s job.
    """
    return HealthResponse(
        status="ok",
        version=SERVICE_VERSION,
        uptime_seconds=round(time.time() - state.started_at, 1),
    )


@router.get("/health", summary="Liveness probe (proxy-reachable)")
async def health() -> HealthResponse:
    """Byte-for-byte the root `/health` body, reachable through the frontend's
    `/api/*` proxy so the browser and external monitors can confirm the
    backend is up."""
    return health_payload()
