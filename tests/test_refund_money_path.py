"""The money path under failure: a credit that was never sent, a store that
goes away mid-uphold, a transfer lost after it was signed (Epic 4 hardening).

`tests/test_adjudication.py` pins the ORDER of `uphold` with the settler stubbed
at `execute_refund`. Most of what is asserted here needs the stub one layer
lower — at `sc.invoke_with_server_key_async` — because the question is what the
REAL `execute_refund` and `credit_refund` make of what the client raises, and a
stub above them would answer it for them.

Hermetic, like the rest of the suite: the in-memory dispute store and fake
chain answers. Nothing is signed, paid or submitted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from test_adjudication import (  # noqa: F401 - autouse fixtures and helpers
    LANDED,
    _fresh_state,
    a_dispute,
    invalidated,
    rater,
)
from test_refund_execution import _dispute as _upheld_dispute

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
from app.services.dispute_svc import DisputeError


def _chain(monkeypatch, *answers: dict[str, Any] | BaseException) -> list[tuple[str, str, list[Any]]]:
    """Stand in for the stellar client BELOW `execute_refund`.

    Each call pops the next answer: a dict is returned, an exception raised.
    Once the script runs out every call lands, so "the retry pays" can be
    asserted without scripting it.
    """
    calls: list[tuple[str, str, list[Any]]] = []
    script = list(answers)

    async def _invoke(contract_id: str, fn: str, args: list[Any]) -> dict[str, Any]:
        calls.append((contract_id, fn, args))
        answer = script.pop(0) if script else LANDED
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(sc, "signer_public_key", lambda: "GSETTLER")
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(asset_sac="CSAC_ASSET", payment_escrow="CESCROW"))
    monkeypatch.setattr(sc, "addr", lambda a: ("addr", a))
    monkeypatch.setattr(sc, "i128", lambda v: ("i128", v))
    monkeypatch.setattr(sc, "invoke_with_server_key_async", _invoke)
    return calls


def _stored(dispute_id: str) -> tuple[dispute_store.DisputeRecord, list[str]]:
    store = dispute_store.get_dispute_store()
    current = asyncio.run(store.get_dispute(dispute_id))
    assert current is not None
    return current, [c.dispute_id for c in asyncio.run(store.list_refund_claims())]


# ── B1: a refusal before anything was sent is FAILED, not TIMEOUT ──


@pytest.mark.parametrize(
    "why",
    [
        # SAC: the settler holds too little USDC, and simulation refuses it.
        sc.ContractError("prepare failed: HostError: Error(Contract, #10)", 10),
        sc.NotSubmittedError("load_account failed: rpc unreachable"),
        sc.NotSubmittedError("sign failed: no secret"),
        # The RPC answered the send with ERROR / TRY_AGAIN_LATER and holds nothing.
        sc.NotSubmittedError("submit failed: AAAA"),
    ],
    ids=["simulation", "load_account", "sign", "send_refused"],
)
def test_a_transfer_refused_before_the_send_hands_the_claim_back(monkeypatch, why) -> None:
    calls = _chain(monkeypatch, why)
    dispute = a_dispute()

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert (refused.value.code, refused.value.status_code) == ("refund_failed", 502)
    assert len(calls) == 1
    current, queue = _stored(dispute.id)
    assert (current.status, current.refund_tx, queue) == ("upheld", None, [])
    # The buyer can still be paid: the next uphold claims it and the transfer lands.
    assert asyncio.run(dispute_svc.uphold(dispute.id)).status == "credited"
    assert len(calls) == 2


def test_a_signing_key_that_will_not_parse_is_failed_and_releases(monkeypatch) -> None:
    """`config_gap` asks presence only, so a key that is set but is not a key
    passes it and fails inside `execute_refund` — before anything is sent."""
    sent: list[Any] = []

    async def _never(*args: Any) -> dict[str, Any]:  # pragma: no cover - must never run
        sent.append(args)
        return LANDED

    monkeypatch.setattr(sc, "invoke_with_server_key_async", _never)
    dispute = a_dispute()
    monkeypatch.setattr(settings, "stellar_signing_key", "SNOT-A-REAL-SECRET")
    sc._signer_keypair.cache_clear()
    try:
        assert refund_svc.config_gap() is None
        with pytest.raises(DisputeError) as refused:
            asyncio.run(dispute_svc.uphold(dispute.id))
    finally:
        sc._signer_keypair.cache_clear()

    assert refused.value.code == "refund_failed"
    assert sent == []
    current, queue = _stored(dispute.id)
    assert (current.status, queue) == ("upheld", [])


def test_not_submitted_is_the_only_raise_read_as_failed(monkeypatch) -> None:
    _chain(monkeypatch, sc.NotSubmittedError("prepare failed: x"), RuntimeError("the socket dropped"))

    first = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))
    second = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))

    assert (first.status, first.tx_hash) == ("FAILED", None)
    assert (second.status, second.tx_hash) == ("TIMEOUT", None)


# ── B4: a failure after the send keeps the hash of what was signed ──


def test_a_transfer_lost_after_the_send_records_its_signed_hash(monkeypatch) -> None:
    signed = "5e" * 32
    calls = _chain(monkeypatch, sc.InFlightError("send failed: connection reset", signed))
    dispute = a_dispute()

    with pytest.raises(DisputeError) as unconfirmed:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert unconfirmed.value.code == "refund_unconfirmed"
    current, queue = _stored(dispute.id)
    # Still held, never released — and now the record names the transaction.
    assert (current.status, current.refund_tx, queue) == ("crediting", signed, [dispute.id])
    assert len(calls) == 1


def test_a_raise_that_is_not_in_flight_still_has_no_hash(monkeypatch) -> None:
    """Only the client's own type vouches for a hash; anything else has none."""
    _chain(monkeypatch, sc.InFlightError("poll failed: x", "7a" * 32), ConnectionError("dropped"))

    first = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))
    second = asyncio.run(refund_svc.credit_refund(_upheld_dispute(), 0.05))

    assert (first.status, first.tx_hash) == ("TIMEOUT", "7a" * 32)
    assert (second.status, second.tx_hash) == ("TIMEOUT", None)


# ── B3: the store failing mid-uphold never strands a dispute nothing paid ──


class _Flaky:
    """The real store, with `method` failing once the claim has been taken."""

    def __init__(self, inner: Any, method: str, error: BaseException) -> None:
        self._inner, self._method, self._error, self._armed = inner, method, error, False

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name == "claim_refund":

            async def _claim(*args: Any, **kwargs: Any) -> Any:
                claimed = await attr(*args, **kwargs)
                self._armed = True
                return claimed

            return _claim
        if name == self._method:

            async def _maybe_fail(*args: Any, **kwargs: Any) -> Any:
                if self._armed:
                    raise self._error
                return await attr(*args, **kwargs)

            return _maybe_fail
        return attr


def _flaky(monkeypatch, method: str, error: BaseException) -> Any:
    real = dispute_store.get_dispute_store()
    monkeypatch.setattr(dispute_store, "_store", _Flaky(real, method, error))
    return real


@pytest.mark.parametrize(
    "error",
    [ConnectionError("database unreachable"), asyncio.CancelledError()],
    ids=["store_down", "cancelled"],
)
def test_a_failure_between_the_claim_and_the_transfer_hands_the_claim_back(monkeypatch, error) -> None:
    calls = _chain(monkeypatch)
    dispute = a_dispute()
    real = _flaky(monkeypatch, "get_settlement", error)

    with pytest.raises(type(error)):
        asyncio.run(dispute_svc.uphold(dispute.id))

    monkeypatch.setattr(dispute_store, "_store", real)
    assert calls == []  # nothing was signed
    current, queue = _stored(dispute.id)
    assert (current.status, current.refund_tx, queue) == ("upheld", None, [])
    assert asyncio.run(dispute_svc.uphold(dispute.id)).status == "credited"


def test_a_bug_while_pricing_the_credit_hands_the_claim_back(monkeypatch) -> None:
    calls = _chain(monkeypatch)
    dispute = a_dispute()

    def _broken(*args: Any, **kwargs: Any) -> float:
        raise TypeError("a bug in the arithmetic")

    monkeypatch.setattr(refund_svc, "creditable_for", _broken)
    with pytest.raises(TypeError):
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert calls == []
    current, queue = _stored(dispute.id)
    assert (current.status, queue) == ("upheld", [])


def test_a_release_that_fails_after_failed_records_the_failed_hash(monkeypatch) -> None:
    calls = _chain(monkeypatch, {"status": "FAILED", "hash": "tx_rejected"})
    dispute = a_dispute()
    real = _flaky(monkeypatch, "release_refund_claim", ConnectionError("database unreachable"))

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    monkeypatch.setattr(dispute_store, "_store", real)
    assert refused.value.code == "refund_failed"
    assert "released by hand" in str(refused.value)
    assert len(calls) == 1
    current, queue = _stored(dispute.id)
    # Held (the release failed), but no longer indistinguishable from a
    # transfer in flight: the record names the one the ledger rejected.
    assert (current.status, current.refund_tx, queue) == ("crediting", "tx_rejected", [dispute.id])


def test_a_release_that_fails_on_a_refusal_still_answers_the_refusal(monkeypatch) -> None:
    calls = _chain(monkeypatch)
    dispute = a_dispute()
    monkeypatch.setattr(settings, "max_refund_usdc", 0.01)
    _flaky(monkeypatch, "release_refund_claim", ConnectionError("database unreachable"))

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refund_above_cap"
    assert calls == []


def test_a_cancel_after_the_transfer_was_handed_over_holds_the_claim(monkeypatch) -> None:
    """The guard ends where the transfer begins: a cancellation there may have
    come after the send, so the claim is kept whatever else happens."""
    calls = _chain(monkeypatch, asyncio.CancelledError())
    dispute = a_dispute()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert len(calls) == 1
    current, queue = _stored(dispute.id)
    assert (current.status, queue) == ("crediting", [dispute.id])
