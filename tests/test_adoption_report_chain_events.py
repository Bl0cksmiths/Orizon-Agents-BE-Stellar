"""Characterization tests for `events_from_rpc`: both places RPC serves events.

Current RPC serves contract events decoded per operation under
`events.contractEventsXdr`; older RPC only inside `resultMetaXdr` (meta v3's
`soroban_meta.events`, meta v4's per-operation `events`). The events here are
real `ContractEvent`s; the transaction meta around them is a stand-in, since
only its `v3` / `v4` shape is read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from stellar_sdk import StrKey, scval
from stellar_sdk import xdr as stellar_xdr

from scripts.adoption_report import chain
from scripts.adoption_report.chain import ChainEvent, events_from_rpc

CONTRACT = StrKey.encode_contract(bytes(range(32)))


def _event(name: str, *, contract: str | None = CONTRACT) -> stellar_xdr.ContractEvent:
    return stellar_xdr.ContractEvent(
        ext=stellar_xdr.ExtensionPoint(0),
        contract_id=None
        if contract is None
        else stellar_xdr.ContractID(stellar_xdr.Hash(StrKey.decode_contract(contract))),
        type=stellar_xdr.ContractEventType.CONTRACT,
        body=stellar_xdr.ContractEventBody(
            v=0, v0=stellar_xdr.ContractEventV0(topics=[scval.to_symbol(name)], data=scval.to_uint32(7))
        ),
    )


def _plain(name: str) -> ChainEvent:
    return ChainEvent(contract_id=CONTRACT, topics=[name], value=7)


def _meta(monkeypatch: pytest.MonkeyPatch, meta: SimpleNamespace) -> None:
    def from_xdr(raw: str) -> SimpleNamespace:
        assert raw == "meta"
        return meta

    monkeypatch.setattr(chain.stellar_xdr.TransactionMeta, "from_xdr", staticmethod(from_xdr))


def test_grouped_events_are_read_per_operation_skipping_empty_ones():
    grouped = [[_event("a").to_xdr(), _event("x", contract=None).to_xdr()], None, [], [_event("b").to_xdr()]]
    result = {"events": {"contractEventsXdr": grouped}, "resultMetaXdr": "ignored when grouped events exist"}
    assert events_from_rpc(result) == [_plain("a"), _plain("b")]


def test_an_empty_grouped_list_is_no_events_not_a_fallback(monkeypatch):
    _meta(monkeypatch, SimpleNamespace(v3=None, v4=SimpleNamespace(operations=[SimpleNamespace(events=[_event("m")])])))
    assert events_from_rpc({"events": {"contractEventsXdr": []}, "resultMetaXdr": "meta"}) == []


@pytest.mark.parametrize("result", [{}, {"events": None}, {"events": {}, "resultMetaXdr": ""}])
def test_no_events_anywhere_is_an_empty_list(result):
    assert events_from_rpc(result) == []


def test_meta_v3_soroban_events_are_read(monkeypatch):
    soroban = SimpleNamespace(events=[_event("a"), _event("x", contract=None), _event("b")])
    _meta(monkeypatch, SimpleNamespace(v3=SimpleNamespace(soroban_meta=soroban), v4=None))
    assert events_from_rpc({"resultMetaXdr": "meta"}) == [_plain("a"), _plain("b")]


def test_meta_v3_without_soroban_meta_has_no_events(monkeypatch):
    _meta(monkeypatch, SimpleNamespace(v3=SimpleNamespace(soroban_meta=None), v4=None))
    assert events_from_rpc({"events": {"transactionEventsXdr": []}, "resultMetaXdr": "meta"}) == []


def test_meta_v4_events_are_read_per_operation_after_v3(monkeypatch):
    ops = [SimpleNamespace(events=[_event("c")]), SimpleNamespace(events=[]), SimpleNamespace(events=[_event("d")])]
    soroban = SimpleNamespace(events=[_event("a")])
    _meta(monkeypatch, SimpleNamespace(v3=SimpleNamespace(soroban_meta=soroban), v4=SimpleNamespace(operations=ops)))
    assert events_from_rpc({"resultMetaXdr": "meta"}) == [_plain("a"), _plain("c"), _plain("d")]
