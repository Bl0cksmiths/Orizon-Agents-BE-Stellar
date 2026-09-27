"""The amount a transfer was FOR, recorded wherever it is handed to the chain
and its outcome is not known (the reconcile sweep's prerequisite).

A timed-out credit used to leave a hash and no amount, so anything that later
found the transfer on the ledger had nothing on record to check it against.
These drive the real `uphold` down to a fake client — the stub sits below
`execute_refund`, as tests/test_refund_money_path.py's does — and read what the
store kept. Nothing is signed, paid or submitted.
"""

from __future__ import annotations

import asyncio

from test_adjudication import (  # noqa: F401 - autouse fixtures and helpers
    _fresh_state,
    a_dispute,
    rater,
)
from test_refund_money_path import _chain, _flaky, _stored

import app.stellar.client as sc
from app.services import dispute_store, dispute_svc
from app.services.dispute_svc import DisputeError


def _sent_usdc(calls: list[tuple[str, str, list[object]]]) -> float:
    """The amount the fake client was asked to transfer, in USDC."""
    (_contract, fn, args) = calls[-1]
    assert fn == "transfer"
    kind, stroops = args[2]  # type: ignore[misc]
    assert kind == "i128"
    return int(stroops) / 10_000_000  # type: ignore[call-overload]


def test_a_timed_out_credit_records_the_amount_it_was_for_and_never_as_credited(monkeypatch) -> None:
    signed = "5e" * 32
    calls = _chain(monkeypatch, sc.InFlightError("poll failed: rpc dropped", signed))
    dispute = a_dispute()

    try:
        asyncio.run(dispute_svc.uphold(dispute.id))
    except DisputeError as unconfirmed:
        assert unconfirmed.code == "refund_unconfirmed"
    else:  # pragma: no cover - the stub times out
        raise AssertionError("a timed-out credit was answered as settled")

    current, queue = _stored(dispute.id)
    assert (current.status, current.refund_tx, queue) == ("crediting", signed, [dispute.id])
    assert current.inflight_usdc is not None and current.inflight_usdc > 0
    assert current.inflight_usdc == _sent_usdc(calls)
    # The receipt's "what landed" is untouched: nothing is known to have.
    assert current.credited_usdc is None


def test_a_failed_credit_that_could_not_be_handed_back_records_its_amount_too(monkeypatch) -> None:
    calls = _chain(monkeypatch, {"status": "FAILED", "hash": "tx_rejected"})
    dispute = a_dispute()
    real = _flaky(monkeypatch, "release_refund_claim", ConnectionError("database unreachable"))

    try:
        asyncio.run(dispute_svc.uphold(dispute.id))
    except DisputeError as refused:
        assert refused.code == "refund_failed"
    else:  # pragma: no cover
        raise AssertionError("a FAILED credit was answered as settled")

    monkeypatch.setattr(dispute_store, "_store", real)
    current, queue = _stored(dispute.id)
    assert (current.status, current.refund_tx, queue) == ("crediting", "tx_rejected", [dispute.id])
    assert current.inflight_usdc == _sent_usdc(calls)
    assert current.credited_usdc is None
