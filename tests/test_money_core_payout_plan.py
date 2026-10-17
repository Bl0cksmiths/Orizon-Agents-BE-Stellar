"""Characterization of `_payout_plan`: what each delivered step is paid, why
the rest are not, and how much the authorized cap cut — pinned field by field."""

from __future__ import annotations

from test_settle_v2 import _plan

from app.services import execution_svc
from app.services.execution_svc import UNPAID_FREE, UNPAID_NO_OWNER, UNPAID_OVER_CAP, _StepPayout


def test_the_cap_cuts_the_step_that_crosses_it_and_leaves_the_next_unpaid() -> None:
    plan = _plan((0.01, 0.02, 0.0, 0.01, 0.01))

    built = execution_svc._payout_plan(
        plan, frozenset(range(5)), {"agt_1": UNPAID_NO_OWNER}, 150_000, {"agt_0": "GOWNER0", "agt_3": "GOWNER3"}
    )

    assert [(p.agent_id, p.amount) for p in built.payouts] == [("agt_0", 100_000), ("agt_3", 50_000)]
    assert built.steps == (
        _StepPayout(0, "agt_0", 100_000, 0, payee="GOWNER0"),
        _StepPayout(1, "agt_1", 0, None, UNPAID_NO_OWNER),
        _StepPayout(2, "agt_2", 0, None, UNPAID_FREE),
        _StepPayout(3, "agt_3", 50_000, 1, payee="GOWNER3"),
        _StepPayout(4, "agt_4", 0, None, UNPAID_OVER_CAP),
    )
    assert (built.clamped_stroops, built.authorized, built.total) == (150_000, 150_000, 150_000)


def test_without_a_cap_every_priced_step_is_paid_in_full() -> None:
    plan = _plan((0.01, 0.0, 0.03))

    built = execution_svc._payout_plan(plan, frozenset({0, 1, 2}), {})

    assert [(p.agent_id, p.amount) for p in built.payouts] == [("agt_0", 100_000), ("agt_2", 300_000)]
    assert [s.unpaid_reason for s in built.steps] == [None, UNPAID_FREE, None]
    assert [s.payee for s in built.steps] == [None, None, None]
    assert (built.clamped_stroops, built.authorized) == (0, None)


def test_a_cap_that_covers_the_run_cuts_nothing() -> None:
    built = execution_svc._payout_plan(_plan((0.01, 0.02)), frozenset({0, 1}), {}, 300_000)

    assert [p.amount for p in built.payouts] == [100_000, 200_000]
    assert built.clamped_stroops == 0


def test_undelivered_steps_are_absent_from_the_plan() -> None:
    built = execution_svc._payout_plan(_plan((0.01, 0.02, 0.03)), frozenset({2}), {})

    assert [s.step_index for s in built.steps] == [2]
    assert [(p.agent_id, p.amount) for p in built.payouts] == [("agt_2", 300_000)]


def test_merged_payouts_keep_each_step_its_own_amount_and_payee() -> None:
    ids = tuple(f"agt_{i % 2}" for i in range(18))
    built = execution_svc._payout_plan(
        _plan(tuple(0.01 for _ in range(18)), ids), frozenset(range(18)), {}, None, {"agt_1": "GONE"}
    )

    assert [(p.agent_id, p.amount) for p in built.payouts] == [("agt_0", 900_000), ("agt_1", 900_000)]
    assert [(s.payout_index, s.payee) for s in built.steps[:2]] == [(0, None), (1, "GONE")]
