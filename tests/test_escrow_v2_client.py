"""The client's escrow v2 helpers: version detection, the authorization read,
the `Vec<Payout>` encoding and the receipt decode. The RPC is a stand-in
SorobanServer, patched where `client._server` hands one out."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from stellar_sdk import StrKey, scval

from app.config import settings
from app.stellar import client as sc

ESCROW = StrKey.encode_contract(b"\x0e" * 32)  # a well-formed id: the envelope is really built

# What the host answered for `version()` on the v1 escrow (read-only testnet
# probe, 2026-09-28), head and cause both.
MISSING_FUNCTION = (
    "HostError: Error(WasmVm, MissingValue)\n\nEvent log (newest first):\n   0: [Diagnostic Event] "
    'topics:[error, Error(WasmVm, MissingValue)], data:["trying to invoke non-existent contract function", version]'
)


class _Rpc:
    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.simulations = 0

    def simulate_transaction(self, tx: Any) -> Any:
        self.simulations += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _ok(value: Any) -> Any:
    return SimpleNamespace(error=None, results=[SimpleNamespace(xdr=value.to_xdr())])


def _err(text: str) -> Any:
    return SimpleNamespace(error=text, results=None)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(sc, "_escrow_versions", {})
    monkeypatch.setattr(settings, "stellar_admin_address", "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV")


def _serve(monkeypatch, answers: list[Any]) -> _Rpc:
    rpc = _Rpc(answers)
    monkeypatch.setattr(sc, "_server", lambda **_kw: rpc)
    return rpc


def test_a_v2_version_is_read_once_and_cached(monkeypatch):
    rpc = _serve(monkeypatch, [_ok(scval.to_uint32(2))])

    assert [sc.escrow_version(ESCROW) for _ in range(3)] == [2, 2, 2]
    assert rpc.simulations == 1
    assert sc.cached_escrow_version(ESCROW) == 2


def test_a_missing_version_function_is_v1_and_cached(monkeypatch):
    rpc = _serve(monkeypatch, [_err(MISSING_FUNCTION)])

    assert sc.escrow_version(ESCROW) == 1
    assert sc.escrow_version(ESCROW) == 1
    assert rpc.simulations == 1


@pytest.mark.parametrize(
    "answer",
    [
        ConnectionError("rpc down"),
        _err("HostError: Error(WasmVm, MissingValue)\nsomething else entirely"),
        _err("HostError: Error(Contract, #1)"),
        _ok(scval.to_uint32(0)),
        _ok(scval.to_symbol("two")),
    ],
    ids=["rpc", "other-missing-value", "contract-error", "zero", "not-an-int"],
)
def test_an_unreadable_version_raises_and_is_not_cached(monkeypatch, answer):
    """ "Could not read" is never remembered as "v1"."""
    rpc = _serve(monkeypatch, [answer, _ok(scval.to_uint32(2))])

    with pytest.raises((RuntimeError, ConnectionError)):
        sc.escrow_version(ESCROW)
    assert sc.cached_escrow_version(ESCROW) is None
    assert sc.escrow_version(ESCROW) == 2
    assert rpc.simulations == 2


def test_the_payouts_encode_as_the_contract_struct():
    value = sc.payouts_vec([sc.Payout("agt_a", 5), sc.Payout("b", 7)])

    first = value.vec.sc_vec[0].map.sc_map
    assert [entry.key.sym.sc_symbol for entry in first] == [b"agent_id", b"amount"]
    assert scval.to_native(value) == [{"agent_id": "agt_a", "amount": 5}, {"agent_id": "b", "amount": 7}]
    assert scval.to_native(sc.payouts_vec([])) == []


def test_receipt_ids_decode_the_vector_or_nothing():
    assert sc.receipt_ids([b"\x01" * 16, "02" * 16]) == [b"\x01" * 16, b"\x02" * 16]
    assert sc.receipt_ids([]) == []
    for bad in (None, "ab" * 16, [b"\x01" * 15], ["zz" * 16], [1]):
        assert sc.receipt_ids(bad) is None


def test_the_authorization_read_needs_the_v2_shape(monkeypatch):
    record = {
        "payer": "G" + "A" * 55,
        "agent_id": "pln_1",
        "max_amount": 5,
        "spent": 0,
        "expires_at": 9,
        "revoked": False,
        "settled": False,
    }
    monkeypatch.setattr(sc, "simulate_read", lambda *a, **k: record)
    assert sc.escrow_authorization(ESCROW, b"\x01" * 16) == sc.EscrowAuthorization(
        "G" + "A" * 55, "pln_1", 5, 0, 9, False, False
    )

    v1 = {k: v for k, v in record.items() if k != "settled"}
    monkeypatch.setattr(sc, "simulate_read", lambda *a, **k: v1)
    with pytest.raises(RuntimeError):
        sc.escrow_authorization(ESCROW, b"\x01" * 16)


def test_a_missing_authorization_is_none_and_other_errors_raise(monkeypatch):
    def not_found(*a, **k):
        raise RuntimeError("simulate failed: HostError: Error(Contract, #2)")

    monkeypatch.setattr(sc, "simulate_read", not_found)
    assert sc.escrow_authorization(ESCROW, b"\x01" * 16) is None

    def unauthorized(*a, **k):
        raise RuntimeError("simulate failed: HostError: Error(Contract, #1)")

    monkeypatch.setattr(sc, "simulate_read", unauthorized)
    with pytest.raises(RuntimeError):
        sc.escrow_authorization(ESCROW, b"\x01" * 16)
