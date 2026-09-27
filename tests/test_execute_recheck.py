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

from app.config import settings
from app.schemas import Agent, Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc, reputation_svc
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


# ── the routing floor, re-applied to a fresh read ───────────────────
#
# The rule (`execution_svc._execute_refusal`):
#   * a read that FAILED cannot overturn what the buyer authorised — the step
#     runs, and the trace says it ran on the plan's own scores;
#   * a read that succeeded and is below the floor refuses the step — unless
#     the buyer authorised it below the floor (a starvation re-admission) and
#     it is no worse than the bound the card showed.


def _onchain(agent_id: str, lower_bound_bps: int) -> reputation_svc.RepInfo:
    return reputation_svc._prior_info(agent_id).model_copy(
        update={"source": "onchain", "lower_bound_bps": lower_bound_bps, "count": 5}
    )


def _reads(monkeypatch, infos: dict[str, reputation_svc.RepInfo]) -> list[list[str]]:
    """The execute-time batch read answers with `infos`; returns each batch asked for."""
    asked: list[list[str]] = []

    async def fake_fetch_reps(agent_ids, timeout_seconds=None):
        asked.append(list(agent_ids))
        return {a: infos.get(a, reputation_svc._prior_info(a)) for a in agent_ids}

    monkeypatch.setattr(reputation_svc, "fetch_reps", fake_fetch_reps)
    return asked


def test_an_agent_that_fell_below_the_floor_since_planning_is_not_paid(monkeypatch):
    """Planned at 5700 (clear of a 5500 floor); a rating has since landed and
    the fresh read says 5499. It is provably below the floor now, so it is not
    dispatched, not charged and not rated, and the buyer is told why."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 5500)
    _register(monkeypatch, "agt_ok")
    _register(monkeypatch, "agt_sunk")
    asked = _reads(monkeypatch, {"agt_sunk": _onchain("agt_sunk", 5499), "agt_ok": _onchain("agt_ok", 6000)})
    resolved = _dispatches(monkeypatch, "agt_ok", "agt_sunk")
    seen = _settles(monkeypatch)

    trace = _run(
        "tsk_rc_sunk",
        _plan("pln_rc_sunk", _step("agt_ok"), _step("agt_sunk", rep_lower_bound_bps=5700)),
    )

    assert asked == [["agt_ok", "agt_sunk"]], "one batch read for the run, covering every agent"
    assert resolved == ["agt_ok"]
    assert seen["totals"] == [pytest.approx(PRICE)]
    assert seen["undispatched"] == frozenset({1})
    assert (
        "step refused: agt_sunk fell below the routing floor after this plan was built (5499 < 5500 bps)"
        " — not dispatched, not charged"
    ) in trace


def test_an_agent_exactly_on_the_floor_is_still_paid(monkeypatch):
    """The floor is inclusive at execute as it is at planning."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 5500)
    _register(monkeypatch, "agt_edge")
    _reads(monkeypatch, {"agt_edge": _onchain("agt_edge", 5500)})
    resolved = _dispatches(monkeypatch, "agt_edge")
    seen = _settles(monkeypatch)

    _run("tsk_rc_edge", _plan("pln_rc_edge", _step("agt_edge")))

    assert resolved == ["agt_edge"]
    assert seen["totals"] == [pytest.approx(PRICE)]


def test_a_degraded_read_at_execute_does_not_strip_an_authorised_step(monkeypatch):
    """The chain was slow: the batch read degraded and served the prior. Under
    a floor ABOVE the prior's bound (the fail-closed configuration) that prior
    fails the floor arithmetically — but it is not evidence about the agent,
    and the buyer authorised this step on a real 6200. It runs, is charged,
    and the trace says which scores it ran on."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 6000)
    degraded = reputation_svc._prior_info("agt_ok", degraded=True)
    assert not reputation_svc.passes_floor(degraded), "precondition: the prior alone would be refused"
    _register(monkeypatch, "agt_ok")
    _reads(monkeypatch, {"agt_ok": degraded})
    resolved = _dispatches(monkeypatch, "agt_ok")
    seen = _settles(monkeypatch)

    trace = _run("tsk_rc_slow", _plan("pln_rc_slow", _step("agt_ok", rep_lower_bound_bps=6200)))

    assert resolved == ["agt_ok"]
    assert seen["totals"] == [pytest.approx(PRICE)]
    assert seen["undispatched"] == frozenset()
    assert (
        "reputation re-check unavailable for [agt_ok] — those steps run on the scores this plan was authorised with"
    ) in trace
    assert not any(m.startswith("step refused") for m in trace)


def test_a_step_the_buyer_authorised_below_the_floor_runs_while_no_worse(monkeypatch):
    """A starvation re-admission: the card flagged it below the floor at 5000
    and the buyer authorised it anyway. A fresh 5000 is exactly what they
    consented to, so it runs."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 5500)
    _register(monkeypatch, "agt_relaxed")
    _reads(monkeypatch, {"agt_relaxed": _onchain("agt_relaxed", 5000)})
    resolved = _dispatches(monkeypatch, "agt_relaxed")
    seen = _settles(monkeypatch)

    _run("tsk_rc_relaxed", _plan("pln_rc_relaxed", _step("agt_relaxed", degraded=True, rep_lower_bound_bps=5000)))

    assert resolved == ["agt_relaxed"]
    assert seen["totals"] == [pytest.approx(PRICE)]


def test_a_step_authorised_below_the_floor_is_refused_once_it_is_worse(monkeypatch):
    """The buyer consented to the evidence they were shown, not to whatever
    arrived after: one point below the shown bound and it is refused."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 5500)
    _register(monkeypatch, "agt_relaxed")
    _reads(monkeypatch, {"agt_relaxed": _onchain("agt_relaxed", 4999)})
    resolved = _dispatches(monkeypatch, "agt_relaxed")
    seen = _settles(monkeypatch)

    trace = _run("tsk_rc_worse", _plan("pln_rc_worse", _step("agt_relaxed", degraded=True, rep_lower_bound_bps=5000)))

    assert resolved == []
    assert seen["totals"] == []
    assert (
        "step refused: agt_relaxed fell further below the routing floor than this plan showed (4999 < 5000 bps)"
        " — not dispatched, not charged"
    ) in trace
