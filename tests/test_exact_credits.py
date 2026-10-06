"""Dispute credits in stroops (ADR 0015): never more than the policy's share, never more than moved.

A credit is `fraction` of what a delivered step was charged. It used to be
`round(charged × fraction, 7)` in floats — round-to-nearest, so a share that
fell between two stroops could be paid the stroop ABOVE it: 90% of a 2-stroop
charge (1.8 stroops) credited 2, the whole charge. The share is rounded DOWN
now, on integers, and the credits a settlement can pay are checked over random
settlements and disputes against the two ceilings `creditable_for` enforces.
"""

from __future__ import annotations

import dataclasses
import random

import pytest
from test_dispute_store import a_dispute, a_settlement

from app import money
from app.services import refund_svc
from app.services.dispute_store import SettlementStep


@pytest.mark.parametrize(
    ("charged", "fraction", "credit"),
    [
        (0.0000002, 0.9, 0.0000001),  # 1.8 stroops: was 2, the whole charge
        (0.0000001, 0.7, 0.0),  # 0.7 of a stroop is not a stroop
        (0.054, 0.33, 0.01782),
        (0.25, 1 / 3, 0.0833333),
        (0.054, 1.0, 0.054),
    ],
)
def test_a_credit_is_the_share_rounded_down_to_the_stroop(charged: float, fraction: float, credit: float) -> None:
    assert refund_svc.credited_amount_usdc(charged, fraction) == credit


def test_a_credit_never_exceeds_the_policy_share_over_random_charges() -> None:
    rng = random.Random(15)
    for _ in range(20_000):
        stroops = rng.randint(0, 50_000_000)
        fraction = rng.choice([1.0, 0.5, 0.9, 0.33, 0.25, 0.1, 1 / 3, 2 / 3, rng.random()])
        credit = money.to_stroops(refund_svc.credited_amount_usdc(money.stroops_to_float(stroops), fraction))
        assert credit == money.fraction_of(stroops, fraction)
        assert credit <= stroops * fraction + 1e-9
        assert credit >= stroops * fraction - 1


def test_the_credits_a_settlement_can_pay_stay_within_what_each_step_and_the_run_moved() -> None:
    """Random v2 settlements, disputes on random delivered steps, random policy fractions."""
    rng = random.Random(6)
    for case in range(400):
        n = rng.randint(1, 6)
        steps = []
        for i in range(n):
            planned = rng.randint(1, 3_000_000)
            delivered = rng.random() < 0.8
            paid = planned if delivered and rng.random() < 0.8 else 0
            steps.append(
                SettlementStep(
                    i,
                    f"agt_{i}",
                    None,
                    money.stroops_to_float(paid) if delivered else money.stroops_to_float(planned),
                    delivered,
                    None,
                    paid_usdc=money.stroops_to_float(paid),
                    planned_stroops=planned,
                )
            )
        settled = sum(money.to_stroops(s.paid_usdc or 0.0) for s in steps)
        settlement = a_settlement(steps=tuple(steps), settled_usdc=money.stroops_to_float(settled))
        fraction = rng.choice([1.0, 0.5, 0.9, 1 / 3])
        credited: list[float] = []
        for step in steps:
            if not step.delivered or not step.paid_usdc:
                continue
            dispute = a_dispute(
                step_index=step.step_index,
                charged_usdc=step.price_usdc,
                creditable_usdc=refund_svc.credited_amount_usdc(step.price_usdc, fraction),
            )
            try:
                amount = refund_svc.creditable_for(settlement, dispute, fraction, credited_elsewhere_usdc=credited)
            except refund_svc.RefundRefused:
                continue
            credit = money.to_stroops(amount)
            assert credit == money.fraction_of(money.to_stroops(step.price_usdc), fraction), case
            assert credit <= money.to_stroops(step.paid_usdc), case
            credited.append(amount)
        assert sum(money.to_stroops(c) for c in credited) <= settled, case


def test_an_undelivered_step_is_never_creditable() -> None:
    step = SettlementStep(0, "agt_0", None, 0.05, False, None, paid_usdc=0.0, planned_stroops=500_000)
    settlement = a_settlement(steps=(step,), settled_usdc=0.0)
    with pytest.raises(refund_svc.RefundRefused):
        refund_svc.creditable_for(settlement, a_dispute(creditable_usdc=0.05), 1.0)


def test_the_policy_fraction_shown_is_never_rounded_up() -> None:
    """The receipt states the fraction as `credited_amount_usdc(1.0, f)`: 2/3 is
    0.6666666, not 0.6666667 — the stated policy is no more generous than the
    credits it describes."""
    assert refund_svc.credited_amount_usdc(1.0, 2 / 3) == 0.6666666


def test_a_record_without_planned_stroops_is_still_creditable() -> None:
    step = SettlementStep(0, "agt_0", None, 0.05, True, None)
    settlement = a_settlement(steps=(step,), settled_usdc=0.05)
    dispute = dataclasses.replace(a_dispute(), creditable_usdc=0.05)
    assert refund_svc.creditable_for(settlement, dispute, 1.0) == 0.05


def test_the_credit_line_on_the_workflow_names_the_asset_not_usdc() -> None:
    import asyncio

    from app.schemas import Task
    from app.services import dispute_svc
    from app.state import state

    dispute = a_dispute(task_id="tsk_credit_label")
    state.add_task(Task(id="tsk_credit_label", intent="x", agents=1, spent=0.0, status="complete"))
    try:
        asyncio.run(dispute_svc._note_credit_on_workflow(dispute, 0.0123457, "tx_refund"))
        [line] = [ln.msg for ln in state.traces["tsk_credit_label"] if "upheld" in ln.msg]
    finally:
        state.tasks.pop("tsk_credit_label", None)
        state.traces.pop("tsk_credit_label", None)

    assert f"credited 0.0123457 {money.asset_code()} to the buyer" in line
    assert "USDC" not in line
