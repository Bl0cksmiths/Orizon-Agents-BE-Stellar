"""Characterization tests for `_charged_proof`: every way a payment is shown or not.

With RPC events, the escrow's `charged` event must name the agent, the amount
and the job. Past the RPC's event window (Horizon: no events), the SUCCESSFUL
`settle` invocation's own payouts must. Each case pins the whole result: the
check, the authorization id it names and which proof was used.
"""

from __future__ import annotations

from typing import Any

from scripts.adoption_report.api import WorkflowClaim
from scripts.adoption_report.chain import ChainEvent, Invocation, TxRecord
from scripts.adoption_report.verify import Check, _charged_proof

ESCROW = "CESCROW0000000000000000000000000000000000000000000000000"
OTHER = "COTHER00000000000000000000000000000000000000000000000000"
JOB = "AB" * 16
CLAIM = WorkflowClaim(job_id_hex=JOB, tx_hash="t" * 64, explorer="", amount_usdc=0.01, payer="G", settled_at=0)
SUBJECT = "workflow tttttt…tttt (alpha)"


def _tx(events: list[ChainEvent] | None = None, invocations: list[Invocation] | None = None) -> TxRecord:
    return TxRecord("t" * 64, "SUCCESS", 7, "rpc", invocations or [], events)


def _charged(agent: str, value: Any, contract: str = ESCROW) -> ChainEvent:
    return ChainEvent(contract, ["charged", agent], value)


def _check(ok: bool, detail: str) -> Check:
    return Check(SUBJECT, "charged_names_agent", ok, detail)


def test_a_charged_event_naming_the_agent_amount_and_job_is_the_proof():
    events = [_charged("beta", ["x", "a2", 100000, JOB.lower()]), _charged("alpha", ["x", "a1", "100000", JOB.lower()])]
    assert _charged_proof(_tx(events), ESCROW, "alpha", CLAIM, SUBJECT) == (
        _check(True, f"charged(alpha) for 100000 stroops, job {JOB.lower()} (RPC contract events)"),
        "a1",
        "charged event",
    )


def test_no_charged_event_naming_the_agent_lists_who_was_named():
    events = [
        _charged("gamma", []),
        _charged("beta", []),
        _charged("alpha", [], contract=OTHER),
        ChainEvent(ESCROW, ["refunded", "alpha"], []),
        ChainEvent(ESCROW, [], []),
        ChainEvent(ESCROW, ["charged"], []),
    ]
    assert _charged_proof(_tx(events), ESCROW, "alpha", CLAIM, SUBJECT) == (
        _check(
            False,
            f"tx {'t' * 64} emitted 3 charged event(s) from the escrow, naming ['beta', 'gamma']; "
            "none names agent alpha",
        ),
        None,
        "",
    )


def test_no_events_at_all_names_nobody():
    assert _charged_proof(_tx([]), ESCROW, "alpha", CLAIM, SUBJECT)[0] == _check(
        False, f"tx {'t' * 64} emitted 0 charged event(s) from the escrow, naming []; none names agent alpha"
    )


def test_charged_events_for_the_agent_with_the_wrong_amount_or_job_say_what_they_carry():
    events = [
        _charged("alpha", ["x", "a1", 99999, JOB]),
        _charged("alpha", ["x", "a1", 100000, "cd" * 16]),
        _charged("alpha", "not a list"),
        _charged("alpha", ["x", "a1", 100000]),
    ]
    assert _charged_proof(_tx(events), ESCROW, "alpha", CLAIM, SUBJECT) == (
        _check(
            False,
            f"charged event(s) for alpha in tx {'t' * 64} carry (stroops, job) "
            f"[(99999, '{JOB}'), (100000, '{'cd' * 16}')]; "
            f"the API claims (100000, {JOB.lower()})",
        ),
        None,
        "",
    )


def _settle(payouts: Any, job: str = JOB, contract: str = ESCROW, function: str = "settle") -> Invocation:
    return Invocation(contract, function, ["payer", "auth-1", job, payouts])


def test_past_the_event_window_a_settle_payout_is_the_proof():
    invocations = [
        Invocation(OTHER, "settle", ["payer", "auth-0", JOB, [{"agent_id": "alpha", "amount": 100000}]]),
        Invocation(ESCROW, "settle", ["payer", "auth-0", JOB]),
        _settle([{"agent_id": "beta", "amount": 100000}, {"agent_id": "alpha", "amount": "100000"}]),
    ]
    assert _charged_proof(_tx(None, invocations), ESCROW, "alpha", CLAIM, SUBJECT) == (
        _check(
            True,
            "tx is past the RPC's event window; the SUCCESSFUL settle's payouts name alpha "
            f"for 100000 stroops, job {JOB.lower()} (Horizon envelope)",
        ),
        "auth-1",
        "settle payout (events past RPC retention)",
    )


def test_past_the_event_window_without_a_matching_payout_the_payment_cannot_be_shown():
    invocations = [
        _settle("not a list"),
        _settle(["not a dict", {"agent_id": "alpha", "amount": None}, {"agent_id": "alpha", "amount": 100001}]),
        _settle([{"agent_id": "alpha", "amount": 100000}], job="cd" * 16),
        _settle([{"agent_id": "alpha", "amount": 100000}], function="charge"),
    ]
    assert _charged_proof(_tx(None, invocations), ESCROW, "alpha", CLAIM, SUBJECT) == (
        _check(
            False,
            f"tx {'t' * 64} is past the RPC's event window and its invocation ['CESCRO…0000.charge', "
            "'CESCRO…0000.settle'] has no settle payout naming alpha for 100000 stroops, "
            f"job {JOB.lower()}; the payment to alpha cannot be shown",
        ),
        None,
        "",
    )
