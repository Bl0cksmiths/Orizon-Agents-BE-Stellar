"""The lifecycle harness's settlement and seal checks (story 5.01), one rule at a time."""

from __future__ import annotations

from typing import Any

from scripts.lifecycle.chain import ChainEvent, Observation
from scripts.lifecycle.verify import (
    check_authorization_view,
    check_buyer_delta,
    check_charged_events,
    check_operator_deltas,
    check_seal,
    check_settled_event,
    check_tx,
    check_v1_charge,
    expected_payouts,
    passed,
)

JOB = "0a" * 16
AUTH = "0b" * 16
OWNER = "GOWNER"
BUYER = "GBUYER"


def _settlement(**over: Any) -> dict[str, Any]:
    base = {
        "job_id_hex": JOB,
        "payer": BUYER,
        "settled_usdc": 0.05,
        "steps": [
            {"step_index": 0, "agent_id": "ext", "price_usdc": 0.05, "delivered": True},
            {"step_index": 1, "agent_id": "agt_seed", "price_usdc": 0.02, "delivered": True},
            {"step_index": 2, "agent_id": "ext2", "price_usdc": 0.03, "delivered": False},
        ],
    }
    return {**base, **over}


def _charged(agent: str, amount: int, job: str = JOB, receipt: str = "0c" * 16) -> ChainEvent:
    return ChainEvent(ESCROW, "tx", 5, ["charged", agent], [receipt, AUTH, amount, job])


def _settled(spent: int, returned: int, job: str = JOB) -> ChainEvent:
    return ChainEvent(ESCROW, "tx", 5, ["settled"], [AUTH, job, spent, returned])


ESCROW = "CESCROW"
OWNERS = {"ext": OWNER, "ext2": "GOWNER2"}


def test_payouts_are_expected_only_for_delivered_steps_with_an_owner() -> None:
    assert dict(expected_payouts(_settlement(), OWNERS)) == {("ext", 500_000): 1}


def test_a_stated_paid_amount_is_what_the_events_are_held_to() -> None:
    steps = [
        {"agent_id": "ext", "price_usdc": 0.05, "delivered": True, "paid_usdc": 0.04},
        {"agent_id": "ext2", "price_usdc": 0.03, "delivered": True, "paid_usdc": None},
    ]
    assert dict(expected_payouts(_settlement(steps=steps), {})) == {("ext", 400_000): 1}


def test_one_charged_event_per_paid_step_passes() -> None:
    checks, paid = check_charged_events([_charged("ext", 500_000)], _settlement(), OWNERS)
    assert paid == 500_000 and passed(checks)


def test_a_missing_charged_event_fails() -> None:
    checks, _ = check_charged_events([], _settlement(), OWNERS)
    assert not passed(checks)


def test_a_charged_event_naming_the_wrong_agent_fails() -> None:
    checks, _ = check_charged_events([_charged("orizon_batch", 500_000)], _settlement(), OWNERS)
    assert checks[0].ok is False


def test_a_charged_event_for_the_wrong_amount_fails() -> None:
    checks, _ = check_charged_events([_charged("ext", 499_999)], _settlement(), OWNERS)
    assert checks[0].ok is False


def test_a_charged_event_for_another_job_fails() -> None:
    checks, _ = check_charged_events([_charged("ext", 500_000, job="ff" * 16)], _settlement(), OWNERS)
    assert checks[0].ok is False and "other than" in checks[0].detail


def test_paying_an_undelivered_step_fails() -> None:
    events = [_charged("ext", 500_000), _charged("ext2", 300_000)]
    checks, _ = check_charged_events(events, _settlement(), OWNERS)
    assert checks[0].ok is False


def test_a_charged_sum_the_settlement_does_not_report_fails() -> None:
    checks, _ = check_charged_events([_charged("ext", 500_000)], _settlement(settled_usdc=0.07), OWNERS)
    assert [c.ok for c in checks] == [True, False]


def test_settled_event_spent_and_returned() -> None:
    assert check_settled_event([_settled(500_000, 200_000)], _settlement(), 500_000, 700_000).ok
    assert not check_settled_event([_settled(500_000, 199_999)], _settlement(), 500_000, 700_000).ok
    assert not check_settled_event([_settled(400_000, 300_000)], _settlement(), 500_000, 700_000).ok
    assert not check_settled_event([], _settlement(), 500_000, 700_000).ok
    assert not check_settled_event([_settled(1, 1), _settled(1, 1)], _settlement(), 1, None).ok
    unknown_max = check_settled_event([_settled(500_000, 9)], _settlement(), 500_000, None)
    assert unknown_max.ok and "not cross-checked" in unknown_max.detail


def test_authorization_view() -> None:
    good = {"settled": True, "spent": 500_000, "payer": BUYER}
    assert check_authorization_view(good, 500_000, BUYER).ok
    assert not check_authorization_view({**good, "settled": False}, 500_000, BUYER).ok
    assert not check_authorization_view({**good, "spent": 1}, 500_000, BUYER).ok
    assert not check_authorization_view({**good, "payer": "GOTHER"}, 500_000, BUYER).ok
    assert not check_authorization_view(None, 500_000, BUYER).ok


def test_buyer_delta_is_the_paid_sum_plus_the_fee() -> None:
    assert check_buyer_delta(1_000_000, 499_900, 500_000, 100, True).ok
    assert not check_buyer_delta(1_000_000, 499_901, 500_000, 100, True).ok
    assert check_buyer_delta(1_000_000, 500_000, 500_000, 100, False).ok  # fee not in the asset
    assert check_buyer_delta(None, 1, 1, 1, True).ok is None
    assert check_buyer_delta(1, 1, 1, None, True).ok is None


def test_operator_delta() -> None:
    events = [_charged("ext", 500_000)]
    ok = check_operator_deltas({OWNER: 10}, {OWNER: 500_010}, OWNERS, events, set())
    assert [c.ok for c in ok] == [True]
    short = check_operator_deltas({OWNER: 10}, {OWNER: 500_009}, OWNERS, events, set())
    assert [c.ok for c in short] == [False]
    shared = check_operator_deltas({OWNER: 10}, {OWNER: 1}, OWNERS, events, {OWNER})
    assert [c.ok for c in shared] == [None] and "not isolatable" in shared[0].detail
    unmeasured = check_operator_deltas({}, {}, OWNERS, events, set())
    assert [c.ok for c in unmeasured] == [None]


def test_v1_charge() -> None:
    ok = Observation("h", "SUCCESS", 5, "rpc")
    assert passed(check_v1_charge(ok, [_charged("orizon_batch", 1)]))
    assert not passed(check_v1_charge(ok, []))
    assert not passed(check_v1_charge(Observation("h", "FAILED", 5, "rpc"), []))
    assert not passed(check_v1_charge(None, []))


def test_tx_check_reads_the_ledgers_word() -> None:
    assert check_tx("t", Observation("h", "SUCCESS", 1, "horizon")).ok
    assert not check_tx("t", Observation("h", "NOT_FOUND", None, "none")).ok


def test_seal() -> None:
    att = {"agents": ["ext", "agt_seed"], "total_spent": 500_000, "receipts": ["0c" * 16], "sealed_at": 1}
    assert passed(check_seal(att, _settlement(), "ext", ["0c" * 16]))
    assert not passed(check_seal(att, _settlement(), "someone", ["0c" * 16]))
    assert not passed(check_seal({**att, "total_spent": 1}, _settlement(), "ext", None))
    assert not passed(check_seal(att, _settlement(), "ext", ["0d" * 16]))
    assert not passed(check_seal(None, _settlement(), "ext", None))
