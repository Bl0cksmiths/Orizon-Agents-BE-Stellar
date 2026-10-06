"""Settled + returned = authorized, to the stroop (ADR 0015).

The run half of the pricing contract, through the real run loop, payout
builder, settlement record and receipt view over the v2 harness of
`test_settle_v2` (the chain faked at the client seam, nothing on the network):

  * each delivered step whose operator can be paid settles exactly its plan
    `price_stroops`; every other step's price is returned, and so is anything
    authorized above the plan's total;
  * the record keeps what was planned and what was authorized, so the receipt
    can show planned / charged / returned per step and in total;
  * the task says what was charged, in stroops;
  * prices are the plan's, whatever the registry says by the time it runs;
  * nothing labels the native asset "USDC".
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest
from test_settle_v2 import (
    AUTH_ID_HEX,
    PAYER,
    _auth,
    _Boom,
    _Chain,
    _install,
    _Ok,
    _payouts,
    _plan,
    _run,
    _Store,
    _workers,
)

from app import money
from app.config import settings
from app.routers.disputes import SettlementView
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import dispute_store, execution_svc, platform_treasury
from app.services.dispute_store import SettlementRecord, SettlementStep
from app.state import state

TESTNET = "Test SDF Network ; September 2015"
TESTNET_NATIVE_SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"


@pytest.fixture(autouse=True)
def _recheck_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _testnet_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_network_passphrase", TESTNET)
    monkeypatch.setattr(settings, "stellar_asset_sac", TESTNET_NATIVE_SAC)


@pytest.fixture(autouse=True)
def _forget_tasks() -> Any:
    yield
    for key in [t for t in state.tasks if t.startswith("tsk_px_")]:
        state.tasks.pop(key, None)
        state.traces.pop(key, None)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fresh = _Store()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


def _messages(task_id: str) -> list[str]:
    return [line.msg for line in state.traces[task_id]]


# ── a paid run ─────────────────────────────────────────────────────────
def test_each_delivered_step_settles_its_price_and_the_rest_is_returned(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    plan = _plan((0.012, 0.0250002, 0.054))
    total = plan.plan.total_stroops
    assert total == 120_000 + 250_002 + 540_000
    chain = _install(monkeypatch, _Chain(auth=_auth(max_amount=total)))
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Boom(), "agt_2": _Ok()})

    _run(plan, "tsk_px_partial")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "agt_0", "amount": 120_000}, {"agent_id": "agt_2", "amount": 540_000}]
    [record] = store.recorded[:1]
    assert record.authorized_stroops == total
    assert [s.planned_stroops for s in record.steps] == [120_000, 250_002, 540_000]
    view = SettlementView.of(record)
    assert [(s.planned.stroops, s.charged.stroops, s.returned.stroops) for s in view.steps] == [
        (120_000, 120_000, 0),
        (250_002, 0, 250_002),
        (540_000, 540_000, 0),
    ]
    totals = view.totals
    assert totals.authorized.stroops == total
    assert totals.planned.stroops == total
    assert totals.charged.stroops == 660_000
    assert totals.returned.stroops == 250_002
    assert totals.surplus.stroops == 0
    assert totals.charged.stroops + totals.returned.stroops == totals.authorized.stroops


def test_a_delivered_step_nobody_can_be_paid_for_is_returned_whole(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    """The seeded catalogue has no on-chain owner, so its delivered steps are
    returned — and the receipt says planned, charged 0, returned all of it."""
    plan = _plan((0.012, 0.02), ("agt_01h8", "ext_op"))
    _install(monkeypatch, _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_01h8": None}))
    _workers(monkeypatch, {"agt_01h8": _Ok(), "ext_op": _Ok()})

    _run(plan, "tsk_px_unowned")

    view = SettlementView.of(store.recorded[0])
    seeded, paid = view.steps
    assert (seeded.planned.stroops, seeded.charged.stroops, seeded.returned.stroops) == (120_000, 0, 120_000)
    assert seeded.unpaid_reason == "no_onchain_owner"
    assert (paid.planned.stroops, paid.charged.stroops, paid.returned.stroops) == (200_000, 200_000, 0)
    assert view.totals.charged.stroops + view.totals.returned.stroops == 320_000


def test_an_over_authorization_is_returned_and_reported_as_surplus(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    plan = _plan((0.012,))
    _install(monkeypatch, _Chain(auth=_auth(max_amount=130_000)))
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(plan, "tsk_px_surplus")

    totals = SettlementView.of(store.recorded[0]).totals
    assert (totals.authorized.stroops, totals.charged.stroops) == (130_000, 120_000)
    assert (totals.surplus.stroops, totals.returned.stroops) == (10_000, 10_000)


def test_the_task_says_what_was_charged_not_what_was_delivered(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """`spent` used to be the delivered steps' estimates rounded to 4 places,
    on a paid run too: 0.032 'spent' on a run that paid one operator 0.02."""
    plan = _plan((0.012, 0.02), ("agt_01h8", "ext_op"))
    _install(monkeypatch, _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_01h8": None}))
    _workers(monkeypatch, {"agt_01h8": _Ok(), "ext_op": _Ok()})

    _run(plan, "tsk_px_spent")

    task = state.tasks["tsk_px_spent"]
    assert (task.spent_stroops, task.spent) == (200_000, 0.02)


def test_a_simulated_run_spends_the_delivered_prices_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Boom(), "agt_2": _Ok()})
    plan = _plan((0.0123457, 0.05, 0.00001))
    state.add_task(Task(id="tsk_px_sim", intent="x", agents=3, spent=0.0, status="running"))

    asyncio.run(execution_svc._run(plan, "tsk_px_sim"))

    task = state.tasks["tsk_px_sim"]
    # 0.0123457 + 0.00001 is 0.0123557 — `round(spent, 4)` reported 0.0124.
    assert (task.spent_stroops, task.spent) == (123_557, 0.0123557)


# ── prices are the plan's ────────────────────────────────────────────────
def test_a_registry_price_change_after_planning_changes_nothing(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """The buyer authorized the plan's prices. An operator who reprices
    between plan and execute is paid what the plan said, not the new price."""
    saved = dict(state.agents)
    try:
        from app.seed import seed_registry

        seed_registry()
        plan = StoredPlan(
            id="pln_v2",
            intent="x",
            plan=Plan(
                steps=[
                    PlanStep(
                        agent_id="agt_11c0",
                        rationale="r",
                        price_stroops=money.to_stroops(state.agents["agt_11c0"].price),
                        est_eta_seconds=1.0,
                    )
                ]
            ),
            total_eta=1.0,
        )
        # Cheaper, so the authorization's cap cannot mask a re-read: a payout
        # at the new price would be 300_000, under the 540_000 authorized.
        state.add_agent(state.agents["agt_11c0"].model_copy(update={"price": 0.03}))
        # A built-in agent is paid only to the platform treasury (ADR 0016).
        treasury = platform_treasury.treasury_address()
        chain = _install(
            monkeypatch, _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_11c0": treasury})
        )
        _workers(monkeypatch, {"agt_11c0": _Ok()})

        _run(plan, "tsk_px_frozen")
    finally:
        state.agents.clear()
        state.agents.update(saved)

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "agt_11c0", "amount": 540_000}]


# ── labels ─────────────────────────────────────────────────────────────
def test_a_paid_runs_trace_names_the_asset_and_the_exact_amount(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    plan = _plan((0.0125, 0.0250002))
    _install(monkeypatch, _Chain(auth=_auth(max_amount=plan.plan.total_stroops)))
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Ok()})

    _run(plan, "tsk_px_label_paid")

    messages = _messages("tsk_px_label_paid")
    assert any(m.startswith("x402 settle → 0.0375002 XLM paid to 2 operator payout(s)") for m in messages), messages
    assert any(m.startswith("workflow sealed — 2 agents · 0.0375002 XLM") for m in messages), messages
    assert not any("USDC" in m for m in messages)


def test_a_simulated_runs_trace_names_the_asset_and_the_exact_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    _workers(monkeypatch, {"agt_0": _Ok()})
    state.add_task(Task(id="tsk_px_label_sim", intent="x", agents=1, spent=0.0, status="running"))

    asyncio.run(execution_svc._run(_plan((0.0125,)), "tsk_px_label_sim"))

    messages = _messages("tsk_px_label_sim")
    assert "x402 payment → agt_0 :: 0.0125 XLM (simulated)" in messages
    assert any(m.startswith("workflow sealed — 1 agents · 0.0125 XLM") for m in messages), messages
    assert not any("USDC" in m for m in messages)


# ── the receipt ─────────────────────────────────────────────────────────
def test_the_receipt_names_the_asset_and_shows_display_amounts() -> None:
    record = SettlementRecord(
        task_id="tsk_px_view",
        payer=PAYER,
        auth_id_hex=AUTH_ID_HEX,
        job_id_hex="cd" * 16,
        charge_tx="tx",
        proof_tx=None,
        settled_usdc=0.0125,
        steps=(
            SettlementStep(0, "ext_a", None, 0.0125, True, "ok", paid_usdc=0.0125, planned_stroops=125_000),
            SettlementStep(1, "ext_b", None, 0.0250002, False, None, paid_usdc=0.0, planned_stroops=250_002),
        ),
        settled_at=1.0,
        window_closes_at=2.0,
        authorized_stroops=375_002,
    )

    view = SettlementView.of(record)

    assert view.asset == money.AssetInfo(code="XLM", issuer=None, decimals=7)
    assert [(s.planned.display, s.charged.display, s.returned.display) for s in view.steps] == [
        ("0.0125", "0.0125", "0.000"),
        ("0.0250002", "0.000", "0.0250002"),
    ]
    assert view.totals.returned.display == "0.0250002"


def test_a_v1_receipt_has_no_per_step_charge_and_no_invented_return() -> None:
    """v1 moved one total for the run: nothing per step is known, so nothing per step is claimed."""
    record = SettlementRecord(
        task_id="tsk_px_v1",
        payer=PAYER,
        auth_id_hex=AUTH_ID_HEX,
        job_id_hex="ef" * 16,
        charge_tx="tx",
        proof_tx=None,
        settled_usdc=0.05,
        steps=(SettlementStep(0, "agt_x", None, 0.05, True, None),),
        settled_at=1.0,
        window_closes_at=2.0,
    )

    view = SettlementView.of(record)

    [step] = view.steps
    assert (step.planned.stroops, step.charged, step.returned) == (500_000, None, None)
    assert view.totals.charged.stroops == 500_000
    assert (view.totals.authorized, view.totals.returned, view.totals.surplus) == (None, None, None)


# ── the invariant, over random plans ─────────────────────────────────────
_PRICE_POOL = (0.012, 0.018, 0.009, 0.011, 0.024, 0.054, 0.052, 0.18, 0.0250002, 0.0000001, 0.0123457)
_OUTCOMES = ("ok", "boom", "unowned", "unreadable", "refused")


class _Unusable:
    name = "w.unusable"

    async def run(self, intent: str, rationale: str, context: Any = None) -> Any:
        return "not a dict"


def _random_case(rng: random.Random, index: int) -> tuple[StoredPlan, list[str], int]:
    n = rng.randint(1, 6)
    ids = tuple(f"ext_{index}_{i}" for i in range(n))
    prices = tuple(rng.choice(_PRICE_POOL) if rng.random() < 0.7 else rng.randint(1, 3_000_000) / 1e7 for _ in ids)
    outcomes = [rng.choice(_OUTCOMES) for _ in ids]
    plan = _plan(prices, ids)
    surplus = 0 if rng.random() < 0.7 else rng.randint(1, 500_000)
    return plan, outcomes, plan.plan.total_stroops + surplus


def _refusing(refused: set[int]) -> Any:
    """An execute-time re-check that refuses the steps at `refused`, in plan order."""
    calls = iter(range(10**6))

    def _refusal(*_a: Any, **_k: Any) -> str | None:
        return "refused at execute" if next(calls) in refused else None

    return _refusal


def test_settled_plus_returned_is_authorized_across_random_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(20261006)

    async def _no_drain(_task_id: str) -> None:
        return None

    monkeypatch.setattr(execution_svc, "_finish_stream", _no_drain)
    for index in range(120):
        plan, outcomes, authorized = _random_case(rng, index)
        ids = [s.agent_id for s in plan.plan.steps]
        owners: dict[str, Any] = {}
        workers: dict[str, Any] = {}
        refused: set[int] = set()
        for i, (agent_id, outcome) in enumerate(zip(ids, outcomes, strict=True)):
            workers[agent_id] = _Boom() if outcome == "boom" else (_Unusable() if i % 5 == 4 else _Ok())
            if outcome == "unowned":
                owners[agent_id] = None
            elif outcome == "unreadable":
                owners[agent_id] = RuntimeError("rpc")
            elif outcome == "refused":
                refused.add(i)
        monkeypatch.setattr(execution_svc, "_execute_refusal", _refusing(refused))
        store = _Store()
        monkeypatch.setattr(dispute_store, "_store", store)
        chain = _install(monkeypatch, _Chain(auth=_auth(max_amount=authorized), owners=owners))
        _workers(monkeypatch, workers)
        task_id = f"tsk_px_prop_{index}"

        _run(plan, task_id)

        delivered = {
            i
            for i, outcome in enumerate(outcomes)
            if outcome != "boom" and i not in refused and not isinstance(workers[ids[i]], _Unusable)
        }
        paid = {i for i in delivered if outcomes[i] == "ok"}
        prices = [s.price_stroops for s in plan.plan.steps]
        [settle] = chain.named("settle")
        payouts = _payouts(settle)
        # Each paid step settles exactly its plan price, in plan order, and nothing else is paid.
        assert [(p["agent_id"], p["amount"]) for p in payouts] == [(ids[i], prices[i]) for i in sorted(paid)], index
        charged = sum(p["amount"] for p in payouts)
        returned = authorized - charged  # what the contract hands back in the same settle
        assert charged + returned == authorized
        assert returned == sum(prices[i] for i in range(len(ids)) if i not in paid) + (authorized - sum(prices))
        assert state.tasks[task_id].spent_stroops == charged, index
        if not delivered:
            assert store.recorded == [], index
            continue
        view = SettlementView.of(store.recorded[0])
        totals = view.totals
        assert totals.authorized.stroops == authorized, index
        assert totals.charged.stroops == charged, index
        assert totals.returned.stroops == returned, index
        assert totals.surplus.stroops == authorized - sum(prices), index
        for i, step in enumerate(view.steps):
            assert step.planned.stroops == prices[i]
            assert step.charged.stroops == (prices[i] if i in paid else 0)
            assert step.planned.stroops == step.charged.stroops + step.returned.stroops
        assert sum(s.returned.stroops for s in view.steps) + totals.surplus.stroops == totals.returned.stroops
