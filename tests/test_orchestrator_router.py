"""The /decompose router: how each planning failure reaches the caller.

`decompose` itself is replaced here — these tests are about the router's
mapping from a failure to a status and code, not about planning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient

from app.routers import orchestrator as router
from app.services import orchestrator_svc

INTENT = "write a haiku about databases"


def _raising(exc: BaseException) -> Callable[[str], Awaitable[object]]:
    async def _decompose(_intent: str) -> object:
        raise exc

    return _decompose


def _post(client: TestClient, intent: str = INTENT) -> tuple[int, str]:
    r = client.post("/api/orchestrator/decompose", json={"intent": intent})
    return r.status_code, r.json().get("detail")


def test_a_full_planner_queue_is_a_retryable_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "decompose", _raising(orchestrator_svc.PlannerBusyError("1 running, 1 waiting")))

    assert _post(client) == (503, "planner_busy")
