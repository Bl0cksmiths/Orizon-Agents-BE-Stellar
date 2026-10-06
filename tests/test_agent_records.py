"""AgentRegistry records read straight from contract storage, in batches.

A simulated `get` is two RPC round trips per agent (load the source account,
then simulate), so a registry of 1,000 agents cost the mirror's first pass
~2,000 sequential reads — the ~29 minutes after every boot during which the
adoption report could not be built. `sc.read_agent_records` reads the same
records off the ledger, 200 keys per getLedgerEntries call.

What is pinned here is that the shortcut says exactly what `get` says, and
never more: the same decoded record shape the mirror maps, one request per 200
ids, and an id the batch could not answer (no entry, an entry that does not
decode, an entry under another id) left out for the caller to read the
ordinary way — never guessed at.
"""

from __future__ import annotations

from typing import Any

import pytest
from stellar_sdk import Address, scval
from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.soroban_rpc import GetLedgerEntriesResponse, LedgerEntryResult

from app.stellar import client as sc

REGISTRY_ID = "CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


def _record(agent_id: str, *, owner: str = OWNER, price: int = 10_000) -> stellar_xdr.SCVal:
    """The `Agent` struct as the registry stores it: a map keyed by field symbol."""
    return scval.to_map(
        {
            scval.to_symbol("active"): scval.to_bool(True),
            scval.to_symbol("id"): scval.to_symbol(agent_id),
            scval.to_symbol("name"): scval.to_string(f"{agent_id} name"),
            scval.to_symbol("owner"): scval.to_address(owner),
            scval.to_symbol("price"): scval.to_int128(price),
            scval.to_symbol("registered_at"): scval.to_uint64(1_788_768_432),
            scval.to_symbol("skills"): scval.to_vec([scval.to_symbol("audit")]),
        }
    )


def _entry(key: stellar_xdr.LedgerKey, val: stellar_xdr.SCVal) -> LedgerEntryResult:
    data = key.contract_data
    entry = stellar_xdr.LedgerEntryData(
        type=stellar_xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=stellar_xdr.ContractDataEntry(
            ext=stellar_xdr.ExtensionPoint(0),
            contract=data.contract,
            key=data.key,
            durability=data.durability,
            val=val,
        ),
    )
    return LedgerEntryResult(key=key.to_xdr(), xdr=entry.to_xdr(), lastModifiedLedgerSeq=1, liveUntilLedgerSeq=9)


class _Ledger:
    """A getLedgerEntries stand-in over a dict of stored records."""

    def __init__(self, stored: dict[str, stellar_xdr.SCVal]) -> None:
        self.stored = stored
        self.requests: list[list[stellar_xdr.LedgerKey]] = []

    def get_ledger_entries(self, keys: list[stellar_xdr.LedgerKey]) -> GetLedgerEntriesResponse:
        self.requests.append(keys)
        entries = []
        for key in keys:
            data = key.contract_data
            assert Address.from_xdr_sc_address(data.contract).address == REGISTRY_ID
            assert data.durability == stellar_xdr.ContractDataDurability.PERSISTENT
            tag, agent_id = scval.to_native(data.key)
            assert tag == "Agent"
            if agent_id in self.stored:
                entries.append(_entry(key, self.stored[agent_id]))
        # The node answers in its own order, never necessarily the request's.
        return GetLedgerEntriesResponse(entries=list(reversed(entries)), latestLedger=1)


@pytest.fixture()
def ledger(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(stored: dict[str, stellar_xdr.SCVal]) -> _Ledger:
        fake = _Ledger(stored)
        monkeypatch.setattr(sc, "_server", lambda **_kw: fake)
        return fake

    return install


def test_a_record_reads_back_exactly_as_a_simulated_get_decodes_it(ledger: Any) -> None:
    ledger({"w1_audit_a7x": _record("w1_audit_a7x")})

    records = sc.read_agent_records(REGISTRY_ID, ["w1_audit_a7x"])

    # The shape `registry_sync._to_agent` maps, which is `simulate_read`'s:
    # addresses as G-strings, the i128 as an int, symbols as str.
    expected = sc._to_jsonable(scval.to_native(_record("w1_audit_a7x")))
    assert records == {"w1_audit_a7x": expected}
    assert records["w1_audit_a7x"]["owner"] == OWNER
    assert records["w1_audit_a7x"]["name"] == "w1_audit_a7x name"


def test_ids_are_read_two_hundred_to_a_request(ledger: Any) -> None:
    ids = [f"op_{i:04d}" for i in range(450)]
    fake = ledger({i: _record(i) for i in ids})

    records = sc.read_agent_records(REGISTRY_ID, ids)

    assert sorted(records) == ids
    assert [len(keys) for keys in fake.requests] == [200, 200, 50]


def test_no_ids_is_no_request(ledger: Any) -> None:
    fake = ledger({})

    assert sc.read_agent_records(REGISTRY_ID, []) == {}
    assert fake.requests == []


def test_an_id_with_no_entry_is_left_out_not_guessed(ledger: Any) -> None:
    ledger({"present": _record("present")})

    assert sorted(sc.read_agent_records(REGISTRY_ID, ["present", "absent"])) == ["present"]


@pytest.mark.parametrize(
    "stored",
    [
        scval.to_string("not a record"),
        _record("someone_else"),  # stored under this key, but naming another id
    ],
    ids=["not_a_map", "wrong_id"],
)
def test_an_entry_that_is_not_this_agents_record_is_left_out(ledger: Any, stored: stellar_xdr.SCVal) -> None:
    ledger({"odd": stored, "fine": _record("fine")})

    assert sorted(sc.read_agent_records(REGISTRY_ID, ["odd", "fine"])) == ["fine"]


def test_a_failed_request_leaves_only_its_own_ids_for_the_ordinary_read(ledger: Any) -> None:
    """One batch failing costs that batch, not the ones that answered — and its
    ids are absent, which every caller already reads as "ask `get`"."""
    ids = [f"op_{i:04d}" for i in range(250)]
    fake = ledger({i: _record(i) for i in ids})
    real = fake.get_ledger_entries

    def flaky(keys: list[stellar_xdr.LedgerKey]) -> GetLedgerEntriesResponse:
        if len(fake.requests) == 0:
            fake.requests.append(keys)
            raise ConnectionError("rpc down")
        return real(keys)

    fake.get_ledger_entries = flaky

    records = sc.read_agent_records(REGISTRY_ID, ids)

    assert sorted(records) == ids[200:]


def test_an_unreachable_rpc_reads_as_nothing_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(**_kw: Any) -> Any:
        raise ConnectionError("rpc down")

    monkeypatch.setattr(sc, "_server", down)

    assert sc.read_agent_records(REGISTRY_ID, ["a"]) == {}
