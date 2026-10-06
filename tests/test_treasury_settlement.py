"""A delivered built-in step is paid to the platform treasury, and to nobody else (ADR 0016).

Escrow v2 pays `owner_of(agent_id)`, and `AgentRegistry.register` is
permissionless and write-once: whoever registers an `agt_` id first owns it.
So the settle names a built-in agent only when its on-chain owner IS the
declared treasury; any other owner's step is returned to the buyer with its
own reason, while an operator's agent is paid to its operator as before. The
record keeps who each payout went to, and the receipt shows it.

Through the real run loop, payout builder, record and receipt view, over the
v2 harness of `test_settle_v2` (the chain faked at the client seam).
"""

from __future__ import annotations

from typing import Any

import pytest
from stellar_sdk import Keypair
from test_settle_v2 import OWNER, _auth, _Chain, _install, _Ok, _payouts, _plan, _run, _Store, _workers

from app.routers.disputes import SettlementStepView, SettlementView
from app.services import dispute_store, execution_svc, platform_treasury
from app.services.dispute_store import SettlementStep, steps_from_json, steps_to_json
from app.state import state

TREASURY = platform_treasury.treasury_address()
STRANGER = Keypair.random().public_key


@pytest.fixture(autouse=True)
def _recheck_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _forget_tasks() -> Any:
    yield
    for key in [t for t in state.tasks if t.startswith("tsk_tr_")]:
        state.tasks.pop(key, None)
        state.traces.pop(key, None)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fresh = _Store()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


def test_the_register_names_a_treasury() -> None:
    assert TREASURY is not None


def test_a_delivered_built_in_step_is_paid_to_the_treasury(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    plan = _plan((0.012, 0.054), ("agt_01h8", "agt_11c0"))
    chain = _install(
        monkeypatch,
        _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_01h8": TREASURY, "agt_11c0": TREASURY}),
    )
    _workers(monkeypatch, {"agt_01h8": _Ok(), "agt_11c0": _Ok()})

    _run(plan, "tsk_tr_paid")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [
        {"agent_id": "agt_01h8", "amount": 120_000},
        {"agent_id": "agt_11c0", "amount": 540_000},
    ]
    record = store.recorded[0]
    assert [(s.payee, s.unpaid_reason) for s in record.steps] == [(TREASURY, None), (TREASURY, None)]
    view = SettlementView.of(record)
    assert [(s.payee, s.payee_role) for s in view.steps] == [
        (TREASURY, "platform_treasury"),
        (TREASURY, "platform_treasury"),
    ]
    assert view.totals.charged.stroops == 660_000
    assert view.totals.returned.stroops == 0


def test_a_built_in_id_anyone_else_owns_is_not_paid(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """A squatted `agt_` id: its step is returned, and the operator beside it is still paid."""
    plan = _plan((0.012, 0.02), ("agt_01h8", "ext_op"))
    chain = _install(
        monkeypatch,
        _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_01h8": STRANGER, "ext_op": OWNER}),
    )
    _workers(monkeypatch, {"agt_01h8": _Ok(), "ext_op": _Ok()})

    _run(plan, "tsk_tr_squat")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "ext_op", "amount": 200_000}]
    record = store.recorded[0]
    squatted, operator = record.steps
    assert (squatted.payee, squatted.unpaid_reason, squatted.paid_usdc) == (None, "owner_not_platform_treasury", 0.0)
    assert (operator.payee, operator.unpaid_reason) == (OWNER, None)
    view = SettlementView.of(record)
    assert [(s.payee, s.payee_role) for s in view.steps] == [(None, None), (OWNER, "operator")]
    assert (view.steps[0].charged.stroops, view.steps[0].returned.stroops) == (0, 120_000)
    errors = [line.msg for line in state.traces["tsk_tr_squat"] if line.level == "error"]
    assert "agt_01h8 is registered on-chain to an account that is not the platform treasury — its step is not paid" in (
        errors
    )


def test_without_a_declared_treasury_no_built_in_step_is_paid(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    monkeypatch.setattr(platform_treasury, "treasury_address", lambda: None)
    plan = _plan((0.012,), ("agt_01h8",))
    chain = _install(monkeypatch, _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_01h8": TREASURY}))
    _workers(monkeypatch, {"agt_01h8": _Ok()})

    _run(plan, "tsk_tr_undeclared")

    [settle] = chain.named("settle")
    assert _payouts(settle) == []  # the whole custody released
    assert state.tasks["tsk_tr_undeclared"].settlement == "released"


def test_an_unreadable_treasury_pays_no_built_in_step(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """Two treasuries in the register: the backend cannot say which one is
    ours, so it names no built-in agent rather than guess."""

    def _two() -> str:
        raise platform_treasury.TreasuryError("the team register has 2 entries")

    monkeypatch.setattr(platform_treasury, "treasury_address", _two)
    plan = _plan((0.012, 0.02), ("agt_01h8", "ext_op"))
    chain = _install(
        monkeypatch,
        _Chain(auth=_auth(max_amount=plan.plan.total_stroops), owners={"agt_01h8": TREASURY, "ext_op": OWNER}),
    )
    _workers(monkeypatch, {"agt_01h8": _Ok(), "ext_op": _Ok()})

    _run(plan, "tsk_tr_two")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "ext_op", "amount": 200_000}]
    assert store.recorded[0].steps[0].unpaid_reason == "owner_not_platform_treasury"


# ── the record keeps the payee ─────────────────────────────────────────
def test_the_payee_survives_the_store() -> None:
    step = SettlementStep(0, "agt_01h8", "copywrite.v3", 0.012, True, paid_usdc=0.012, payee=TREASURY)

    [back] = steps_from_json(steps_to_json((step,)))

    assert back.payee == TREASURY


def test_a_row_from_before_the_payee_reads_none() -> None:
    raw = '[{"step_index":0,"agent_id":"ext_op","agent_name":null,"price_usdc":0.02,"delivered":true}]'

    [back] = steps_from_json(raw)

    assert back.payee is None
    view = SettlementStepView.of(back, 1.0)
    assert (view.payee, view.payee_role) == (None, None)
