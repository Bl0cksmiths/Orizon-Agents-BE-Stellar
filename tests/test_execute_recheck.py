"""`/execute` re-checks what a stored plan was built on.

The routing floor and the listing filter are applied when a plan is BUILT. An
audit (Epic 3, proof P4) showed they were never applied again: a plan was
decomposed, its code.gen agent was delisted, and executing the stored plan
still matched the agent, paid it and completed. ADR 0006:162 says routing
honours delisting everywhere a candidate is chosen, and dispatching a step is
choosing its agent.

These pin the replacement against the REAL registry — the older run-loop
suites hold the gate open (`_execute_time_recheck_passes`) because they
dispatch ids nobody registered. Only the dispatch and settlement seams are
stubbed; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.schemas import Agent, Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state

INTENT = "ship the launch page"
PRICE = 0.02
AUTH = "ab" * 16
PAYER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
GOOD = {"summary": "done", "artifact": {"title": "t", "files": [{"path": "a.html", "content": "x"}]}}


@pytest.fixture(autouse=True)
def clean():
    yield
    for tid in [t for t in state.tasks if t.startswith("tsk_rc_")]:
        state.tasks.pop(tid, None)
        state.traces.pop(tid, None)


class _Answers:
    real = True

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, intent: str, rationale: str, context: dict | None = None) -> Any:
        return GOOD


def _register(monkeypatch, agent_id: str, status: str = "online") -> None:
    monkeypatch.setitem(
        state.agents,
        agent_id,
        Agent(id=agent_id, name=agent_id, skills=["x"], price=PRICE, rep=0.0, status=status, runs=0),
    )


def _dispatches(monkeypatch, *agent_ids: str) -> list[str]:
    """Every id resolves to a worker that delivers; returns who was resolved."""
    resolved: list[str] = []

    async def _resolve(agent_id: str) -> Any:
        resolved.append(agent_id)
        return _Answers(f"w.{agent_id}") if agent_id in agent_ids else None

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)
    return resolved


def _settles(monkeypatch) -> dict[str, Any]:
    """Capture what the charge was asked for and which steps were rated."""
    seen: dict[str, Any] = {"totals": [], "undispatched": None}

    async def fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        seen["totals"].append(total_usdc)
        return ("chargehash", "sealhash", b"\x02" * 16)

    async def fake_ratings(*a, undispatched=frozenset(), **k):
        seen["undispatched"] = undispatched

    monkeypatch.setattr(execution_svc, "_settle_onchain", fake_settle)
    monkeypatch.setattr(execution_svc, "_submit_ratings", fake_ratings)
    return seen


def _plan(plan_id: str, *steps: PlanStep) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent=INTENT,
        plan=Plan(steps=list(steps)),
        total_usdc=sum(s.est_price_usdc for s in steps),
        total_eta=1.0,
    )


def _step(agent_id: str, **extra: Any) -> PlanStep:
    return PlanStep(
        agent_id=agent_id, agent_name=agent_id, rationale="do it", est_price_usdc=PRICE, est_eta_seconds=1.0, **extra
    )


def _run(task_id: str, plan: StoredPlan) -> list[str]:
    state.add_task(Task(id=task_id, intent=INTENT, agents=len(plan.plan.steps), spent=0.0, status="running"))
    asyncio.run(execution_svc._run(plan, task_id, auth_id_hex=AUTH, payer=PAYER))
    return [ln.msg for ln in state.traces[task_id]]


# ── P4: an agent delisted after its plan was built ──────────────────


def test_p4_an_agent_delisted_after_decompose_is_refused_at_execute_and_not_paid(client, monkeypatch):
    """The audit's proof, end to end through the two routes: decompose a kit
    plan, delist its code.gen agent the way registry sync does
    (`set_active(false)` → status "offline"), execute the stored plan. The
    delisted agent must not be matched or paid, and the buyer's trace must say
    so; the rest of the plan still runs."""
    plan = client.post("/api/orchestrator/decompose", json={"intent": "calculator web app"}).json()
    steps = plan["steps"]
    assert "agt_11c0" in [s["agent_id"] for s in steps]
    monkeypatch.setitem(state.agents, "agt_11c0", state.agents["agt_11c0"].model_copy(update={"status": "offline"}))

    resp = client.post("/api/orchestrator/execute", json={"plan_id": plan["plan_id"]})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    deadline = time.monotonic() + 30
    while state.tasks[task_id].status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    task = state.tasks[task_id]
    trace = [ln.msg for ln in state.traces[task_id]]

    assert (
        "step refused: agt_11c0 was delisted by its operator after this plan was built — not dispatched, not charged"
        in (trace)
    )
    assert not any("(agt_11c0)" in m for m in trace), "the delisted agent was matched"
    assert not any("x402 payment → agt_11c0" in m for m in trace), "the delisted agent was paid"
    # Everything else ran and was billed; the refused step was not.
    others = [s for s in steps if s["agent_id"] != "agt_11c0"]
    assert task.spent == pytest.approx(round(sum(s["est_price_usdc"] for s in others), 4))
    assert f"workflow incomplete — {len(others)}/{len(steps)} agents produced output" in trace
    state.tasks.pop(task_id, None)
    state.traces.pop(task_id, None)


def test_a_delisted_step_is_left_out_of_the_charge_and_the_ratings(monkeypatch):
    """The paid path: the refused step is excluded from the on-chain charge
    exactly as a failed step is (story 2.03), and is not rated — its operator
    was never asked to deliver. Its worker is never even resolved."""
    _register(monkeypatch, "agt_ok")
    _register(monkeypatch, "ext_gone", status="offline")
    resolved = _dispatches(monkeypatch, "agt_ok", "ext_gone")
    seen = _settles(monkeypatch)

    trace = _run("tsk_rc_delisted", _plan("pln_rc_delisted", _step("agt_ok"), _step("ext_gone")))

    assert resolved == ["agt_ok"]
    assert seen["totals"] == [pytest.approx(PRICE)]
    assert seen["undispatched"] == frozenset({1})
    assert (
        "step refused: ext_gone was delisted by its operator after this plan was built — not dispatched, not charged"
        in (trace)
    )
    assert state.tasks["tsk_rc_delisted"].spent == pytest.approx(PRICE)


def test_an_agent_the_registry_dropped_is_refused_too(monkeypatch):
    """Registry sync evicts an on-chain record it stops believing (a reprice
    past the bounds). Its binding may still resolve, so without the registry
    check the stored plan would pay it at the price the card quoted."""
    _register(monkeypatch, "agt_ok")
    monkeypatch.delitem(state.agents, "ext_evicted", raising=False)
    resolved = _dispatches(monkeypatch, "agt_ok", "ext_evicted")
    seen = _settles(monkeypatch)

    trace = _run("tsk_rc_evicted", _plan("pln_rc_evicted", _step("ext_evicted"), _step("agt_ok")))

    assert resolved == ["agt_ok"]
    assert seen["totals"] == [pytest.approx(PRICE)]
    assert seen["undispatched"] == frozenset({0})
    assert "step refused: ext_evicted is no longer in the agent registry — not dispatched, not charged" in trace


def test_a_plan_whose_every_agent_was_delisted_charges_nothing(monkeypatch):
    """Nothing dispatched means nothing delivered: the run fails, spends 0,
    and the charge is never attempted."""
    _register(monkeypatch, "ext_a", status="offline")
    _dispatches(monkeypatch, "ext_a")
    seen = _settles(monkeypatch)

    _run("tsk_rc_all_gone", _plan("pln_rc_all_gone", _step("ext_a")))

    assert seen["totals"] == []
    assert state.tasks["tsk_rc_all_gone"].status == "failed"
    assert state.tasks["tsk_rc_all_gone"].spent == 0.0
