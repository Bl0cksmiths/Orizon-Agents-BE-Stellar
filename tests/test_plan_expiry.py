"""A stored plan is executable for PLAN_TTL_SECONDS after it was built.

It used to be executable until 200 newer plans pushed it out of the store —
hours on a quiet deployment — so a buyer could pay against a card whose
prices, reputation stamps and notices described a marketplace that no longer
existed. The bound is inclusive: a plan exactly TTL old still runs, and one a
millisecond past it is refused with 410 `plan_expired` before any task is
minted, so nothing runs and nothing is charged.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.schemas import Plan, PlanStep, StoredPlan
from app.services import execution_svc
from app.state import state

BUILT_AT = 1_000_000.0


def _plan(plan_id: str, created_at: float = BUILT_AT) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent="ship the launch page",
        plan=Plan(
            steps=[PlanStep(agent_id="agt_x", agent_name="w", rationale="r", est_price_usdc=0.02, est_eta_seconds=1.0)]
        ),
        total_usdc=0.02,
        total_eta=1.0,
        created_at=created_at,
    )


@pytest.fixture()
def clock(monkeypatch):
    """Pin the wall clock execute_plan reads; returns a setter."""
    now = {"t": BUILT_AT}
    monkeypatch.setattr(execution_svc, "_wall_clock", lambda: now["t"])
    return lambda t: now.__setitem__("t", t)


@pytest.fixture()
def runs(monkeypatch):
    """Replace the run loop; records which plans were started."""
    started: list[str] = []

    async def fake_run(plan, task_id, **kwargs):
        started.append(plan.id)

    monkeypatch.setattr(execution_svc, "_run", fake_run)
    return started


def _execute(plan: StoredPlan) -> str:
    async def go() -> str:
        task_id = await execution_svc.execute_plan(plan)
        await asyncio.sleep(0)  # let the (fake) run start
        return task_id

    return asyncio.run(go())


def test_a_new_plan_is_stamped_with_the_time_it_was_built():
    before = time.time()
    plan = StoredPlan(id="pln_stamp", intent="i", plan=Plan(steps=[]), total_usdc=0.0, total_eta=0.0)
    assert before <= plan.created_at <= time.time()


def test_a_plan_exactly_ttl_old_still_executes(clock, runs):
    clock(BUILT_AT + execution_svc.PLAN_TTL_SECONDS)
    task_id = _execute(_plan("pln_exp_edge"))
    assert runs == ["pln_exp_edge"]
    assert state.tasks.pop(task_id).status == "running"
    state.task_tokens.pop(task_id, None)


def test_a_plan_just_past_its_ttl_is_refused_before_any_task_is_minted(clock, runs):
    clock(BUILT_AT + execution_svc.PLAN_TTL_SECONDS + 0.001)
    tasks_before = set(state.tasks)
    with pytest.raises(execution_svc.PlanExpiredError) as refused:
        _execute(_plan("pln_exp_past"))
    assert refused.value.status_code == 410
    assert refused.value.detail == "plan_expired"
    assert runs == []
    assert set(state.tasks) == tasks_before


def test_a_buyer_who_read_the_card_for_fourteen_minutes_can_still_pay(clock, runs):
    """The TTL is sized for the real flow: read the card, sign the 600 s
    authorize the frontend builds, broadcast, execute. A slow, careful read
    must not strand a buyer mid-signature."""
    clock(BUILT_AT + 14 * 60)
    task_id = _execute(_plan("pln_exp_reader"))
    assert runs == ["pln_exp_reader"]
    state.tasks.pop(task_id, None)
    state.task_tokens.pop(task_id, None)


def test_execute_answers_410_plan_expired_in_the_error_envelope(client, clock, runs):
    plan = _plan("pln_exp_http")
    state.add_plan(plan)
    clock(BUILT_AT + execution_svc.PLAN_TTL_SECONDS + 1)
    tasks_before = set(state.tasks)
    try:
        resp = client.post("/api/orchestrator/execute", json={"plan_id": plan.id})
    finally:
        state.plans.pop(plan.id, None)
    assert resp.status_code == 410
    body = resp.json()
    assert body["detail"] == "plan_expired"
    assert body["error"]["code"] == "plan_expired"
    assert "build a fresh plan" in body["error"]["message"]
    assert runs == []
    assert set(state.tasks) == tasks_before
