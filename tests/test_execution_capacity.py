"""The execute fan-out is bounded: once orchestrator_max_concurrent workflows
are in flight, execute_plan raises CapacityExhaustedError before minting a
task, and the API maps it to 503 "capacity_exhausted"."""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan
from app.services import execution_svc
from app.state import state


def _plan(plan_id: str) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent="exercise the capacity bound",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id="agt_x",
                    agent_name="w.x",
                    rationale="r",
                    est_price_usdc=0.01,
                    est_eta_seconds=1.0,
                )
            ]
        ),
        total_usdc=0.01,
        total_eta=1.0,
    )


class _StuckTask:
    """Stands in for an in-flight workflow in _background_tasks."""

    def done(self) -> bool:
        return False


def test_execute_plan_rejects_at_capacity(monkeypatch):
    monkeypatch.setattr(settings, "orchestrator_max_concurrent", 1)

    async def scenario() -> None:
        blocker = asyncio.create_task(asyncio.sleep(30))
        execution_svc._track_background_task(blocker)
        tasks_before = dict(state.tasks)
        try:
            plan = _plan("pln_cap_svc")
            with pytest.raises(execution_svc.CapacityExhaustedError):
                await execution_svc.execute_plan(plan)
        finally:
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
        # Refused before minting: no task or token appeared.
        assert state.tasks == tasks_before

    asyncio.run(scenario())


def test_execute_api_returns_503_at_capacity(client, monkeypatch):
    monkeypatch.setattr(settings, "orchestrator_max_concurrent", 1)
    plan = client.post("/api/orchestrator/decompose", json={"intent": "pomodoro timer app"}).json()

    stuck = _StuckTask()
    execution_svc._background_tasks.add(stuck)  # type: ignore[arg-type]
    try:
        r = client.post("/api/orchestrator/execute", json={"plan_id": plan["plan_id"]})
    finally:
        execution_svc._background_tasks.discard(stuck)  # type: ignore[arg-type]

    assert r.status_code == 503
    body = r.json()
    assert body["detail"] == "capacity_exhausted"
    assert body["error"]["code"] == "capacity_exhausted"
