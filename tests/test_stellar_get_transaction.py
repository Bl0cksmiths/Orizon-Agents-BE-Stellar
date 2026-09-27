"""`sc.get_transaction`: the reconcile sweep's one question to the chain.

Fed the RPC's real answers (tests/refund_ledger_fixtures.py) through the SDK's
own response model, so what is asserted is what the helper makes of the wire
format rather than of a hand-built object. Nothing touches a network here.
"""

from __future__ import annotations

from typing import Any

import pytest
from refund_ledger_fixtures import REAL_LEDGER, REAL_NOT_FOUND_ANSWER, REAL_REFUND_ENVELOPE_XDR, REAL_REFUND_HASH
from stellar_sdk.soroban_rpc import GetTransactionResponse

import app.stellar.client as sc


class _Rpc:
    def __init__(self, answer: dict[str, Any] | BaseException) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def get_transaction(self, tx_hash: str) -> GetTransactionResponse:
        self.asked.append(tx_hash)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return GetTransactionResponse.model_validate(self.answer)


def _served(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any] | BaseException) -> tuple[_Rpc, list[bool]]:
    rpc = _Rpc(answer)
    profiles: list[bool] = []

    def _server(*, submit: bool = False) -> _Rpc:
        profiles.append(submit)
        return rpc

    monkeypatch.setattr(sc, "_server", _server)
    return rpc, profiles


def test_a_not_found_answer_carries_the_window_the_rpc_can_answer_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real answer for a refund that DID land, after the RPC forgot it: the
    status alone says NOT_FOUND, and only the window says why. Both ends come
    back as the ledger's own close times, as integers."""
    rpc, profiles = _served(monkeypatch, REAL_NOT_FOUND_ANSWER)

    found = sc.get_transaction(REAL_REFUND_HASH)

    assert rpc.asked == [REAL_REFUND_HASH]
    assert found == sc.LedgerTransaction(
        tx_hash=REAL_REFUND_HASH,
        status="NOT_FOUND",
        latest_ledger=4901066,
        latest_ledger_close_time=1790528917,
        oldest_ledger=4780107,
        oldest_ledger_close_time=1789924122,
        ledger=None,
        envelope_xdr=None,
    )
    # The landed ledger is older than anything this RPC still holds.
    assert REAL_LEDGER < found.oldest_ledger
    # A read: the 5 s, no-retry profile, never the submit one.
    assert profiles == [False]


def test_a_found_answer_carries_its_ledger_and_the_envelope_as_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = REAL_NOT_FOUND_ANSWER | {
        "status": "SUCCESS",
        "ledger": REAL_LEDGER,
        "createdAt": "1789199247",
        "envelopeXdr": REAL_REFUND_ENVELOPE_XDR,
        "resultXdr": "AAAAAAAAAGQAAAAAAAAAAQAAAAAAAAAYAAAAAAAAAAA=",
        "resultMetaXdr": "AAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
    }
    _served(monkeypatch, answer)

    found = sc.get_transaction(REAL_REFUND_HASH)

    assert (found.status, found.ledger, found.envelope_xdr) == ("SUCCESS", REAL_LEDGER, REAL_REFUND_ENVELOPE_XDR)


def test_a_failed_lookup_raises_rather_than_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    """An RPC that could not be asked has said nothing, and the sweep must not
    be handed anything it could mistake for NOT_FOUND."""
    _served(monkeypatch, ConnectionError("rpc unreachable"))

    with pytest.raises(ConnectionError):
        sc.get_transaction(REAL_REFUND_HASH)
