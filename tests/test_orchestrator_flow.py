"""End-to-end offline flow: kit decompose → execute → artifact."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.services import execution_svc, orchestrator_svc


async def _no_thinking() -> None:
    return None


@pytest.fixture(autouse=True)
def no_kit_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the kit path's 1.4–2.4 s of cosmetic "thinking" — it changes timing only."""
    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_thinking)


def _plan_content(plan: dict) -> dict:
    """Everything in a plan response except its random id."""
    return {k: v for k, v in plan.items() if k != "plan_id"}


def test_kit_decompose_returns_deterministic_plan(client: TestClient) -> None:
    first = client.post("/api/orchestrator/decompose", json={"intent": "tetris game in html"})
    second = client.post("/api/orchestrator/decompose", json={"intent": "tetris game in html"})
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()

    # Deterministic: the same intent over the same registry and reputation is
    # the same plan — steps, prices, ETAs, notices and totals — under a new id.
    assert a["plan_id"] and b["plan_id"] and a["plan_id"] != b["plan_id"]
    assert _plan_content(a) == _plan_content(b)
    assert [s["agent_id"] for s in a["steps"]] == [aid for aid, _ in orchestrator_svc._KIT_PIPELINE]
    assert a["total_usdc"] == pytest.approx(sum(s["est_price_usdc"] for s in a["steps"]), abs=1e-4)
    # The kit path never asks the planner, so it can never be serving the
    # planner's fallback — the flag is present and down.
    assert a["planner_fallback"] is False


async def _drain_workflows() -> None:
    """Wait for every workflow /execute started, on the app's own loop."""
    while pending := [t for t in execution_svc._background_tasks if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


def test_kit_execute_produces_baked_artifact(client: TestClient) -> None:
    plan = client.post("/api/orchestrator/decompose", json={"intent": "calculator web app"}).json()
    task = client.post("/api/orchestrator/execute", json={"plan_id": plan["plan_id"]}).json()
    assert task["task_id"]

    # Waited on, not polled: the run finishes when its task does, with no
    # wall-clock guess about how long a real kit worker run takes.
    assert client.portal is not None
    client.portal.call(_drain_workflows)

    artifact = client.get(f"/api/tasks/{task['task_id']}/artifact").json().get("artifact")
    assert artifact is not None, "the run finished without an artifact"
    assert artifact["preview_html"]
    assert len(artifact["preview_html"]) > 10_000
    # Every artifact the API hands back carries the containment policy, so the
    # iframe cannot be talked into egress even if generation was steered.
    assert "connect-src 'none'" in artifact["preview_html"]
    assert "Content-Security-Policy" in artifact["files"][0]["content"]


@pytest.mark.parametrize("intent", ["hi", "   ", "a" * 501])
def test_decompose_rejects_an_intent_outside_its_bounds(client: TestClient, intent: str) -> None:
    r = client.post("/api/orchestrator/decompose", json={"intent": intent})
    assert r.status_code == 422
