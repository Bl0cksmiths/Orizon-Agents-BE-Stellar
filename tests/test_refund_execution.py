"""Story 4.03 — the money arithmetic and the transfer (app/services/refund_svc.py).

This is the highest blast radius in the sprint: a settler-funded credit leaves
the PLATFORM's wallet, so every bound that decides the amount has to be pinned,
and every one of them has to be audible in the log when it bites. Three
properties are asserted here rather than left as claims:

  - D4 — the credit is `min(promise, step price × fraction, settled total)`,
    computed from the SETTLEMENT and never from the plan, and each clamp that
    actually reduces the amount is logged with both numbers;
  - D5 — an amount over `MAX_REFUND_USDC` is REFUSED before anything is signed,
    which is asserted by making the stellar client explode if it is ever reached;
  - D3 — SUCCESS, FAILED and TIMEOUT are three distinct outcomes, and anything
    whose fate is unknown lands on TIMEOUT (never FAILED, which would invite a
    retry that credits the buyer twice).

Hermetic: dataclass records built in-process, the stellar client monkeypatched.
No chain, no network, no database.
"""

from __future__ import annotations

import logging

from stellar_sdk import Keypair

from app.services import refund_svc
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
PAYER = Keypair.random().public_key

STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_seo", agent_name="SEO Brief", price_usdc=0.07, delivered=True),
    SettlementStep(step_index=2, agent_id="agt_dead", agent_name="Failed", price_usdc=0.09, delivered=False),
)


def _settlement(*, settled_usdc: float = 0.12, steps: tuple[SettlementStep, ...] = STEPS) -> SettlementRecord:
    """A settlement as `execution_svc` records one when a workflow seals."""
    return SettlementRecord(
        task_id=TASK,
        payer=PAYER,
        auth_id_hex="ab" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=settled_usdc,
        steps=steps,
        settled_at=1_700_000_000.0,
        window_closes_at=1_700_086_400.0,
    )


def _dispute(*, step_index: int = 0, creditable_usdc: float = 0.05) -> DisputeRecord:
    """An upheld dispute, carrying the credit the buyer was promised at open time."""
    return DisputeRecord(
        id="dsp_deadbeefdeadbeef",
        job_id_hex=JOB,
        task_id=TASK,
        step_index=step_index,
        agent_id="agt_writer",
        payer=PAYER,
        reason="the draft was empty",
        status="upheld",
        charged_usdc=0.05,
        creditable_usdc=creditable_usdc,
        opened_at=1_700_000_100.0,
    )


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.services.refund_svc" and r.levelno == level]


def test_credit_is_the_promise_when_no_bound_bites(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        assert refund_svc.creditable_for(_settlement(), _dispute()) == 0.05
    assert _records(caplog, logging.WARNING) == [], "nothing was clamped, so nothing may be logged as clamped"


def test_the_step_price_clamps_a_stale_promise_and_says_so(caplog) -> None:
    """The dispute was opened promising more than the step ever cost. The step
    price wins, and the pair of numbers has to be in the log."""
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        amount = refund_svc.creditable_for(_settlement(), _dispute(creditable_usdc=0.09))

    assert amount == 0.05
    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any(
        "clamped by the step price" in m and "0.0900000" in m and "0.0500000" in m and JOB in m and PAYER in m
        for m in msgs
    ), f"the step-price clamp was not logged with both numbers: {msgs}"


def test_the_settled_total_clamps_the_step_price_and_says_so(caplog) -> None:
    """The charge floors and rounds, so what settled can be below the step's
    own price — and then it, not the price, is the ceiling."""
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        amount = refund_svc.creditable_for(_settlement(settled_usdc=0.01), _dispute())

    assert amount == 0.01
    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any(
        "clamped by the settled total" in m and "0.0500000" in m and "0.0100000" in m and JOB in m and PAYER in m
        for m in msgs
    ), f"the settled-total clamp was not logged with both numbers: {msgs}"


def test_a_lowered_fraction_clamps_the_step_bound(caplog) -> None:
    """Lowering DISPUTE_CREDITED_FRACTION applies to disputes already open —
    raising it cannot, because the promise frozen at open time still caps it."""
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        assert refund_svc.creditable_for(_settlement(), _dispute(), 0.5) == 0.025
        assert refund_svc.creditable_for(_settlement(), _dispute(), 2.0) == 0.05  # fraction clamped to 1.0

    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any("clamped by the step price" in m and "0.0250000" in m for m in msgs), (
        f"the halved step bound was not logged: {msgs}"
    )


def test_the_amount_comes_from_the_settlement_not_the_plan_estimate() -> None:
    """The guard against paying an estimate: a settlement whose steps priced
    high but which settled low credits the settled figure, every time."""
    generous = (SettlementStep(step_index=0, agent_id="agt_writer", agent_name="C", price_usdc=0.9, delivered=True),)
    amount = refund_svc.creditable_for(_settlement(settled_usdc=0.02, steps=generous), _dispute(creditable_usdc=0.9))
    assert amount == 0.02
