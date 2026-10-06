"""The registry mirror reads records in batches, and falls back per id.

A pass used to be one simulated `get` per agent, each two sequential RPC round
trips: ~1,000 agents took the first full pass ~29 minutes after a boot, and a
single read that timed out left the pass "not full", so `synced` waited for the
NEXT one. Every pass re-reads every id, so the mirror was also that stale in
steady state. A pass is now `list_ids` plus one getLedgerEntries per 200 ids;
an id the batch did not vouch for is read with `get` as before, so nothing the
old pass guaranteed is given up.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from stellar_sdk import scval

from app.config import settings
from app.services import registry_sync
from app.state import state

REGISTRY_ID = "CFAKEREGISTRY"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


def _raw(agent_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "active": True,
        "id": agent_id,
        "name": f"{agent_id}.worker",
        "owner": OWNER,
        "price": 500_000,
        "registered_at": 1_757_000_000,
        "skills": ["translate"],
    }
    record.update(overrides)
    return record


class _Registry:
    """`list_ids`, the batched record read, and `get`, over one dict."""

    def __init__(self, records: dict[str, dict[str, Any]], *, batch_skips: set[str] | None = None) -> None:
        self.records = records
        self.batch_skips = batch_skips or set()
        self.batches: list[list[str]] = []
        self.gets: list[str] = []
        self.failing_gets: set[str] = set()

    def simulate_read(self, contract_id: str, fn: str, args: list | None = None, source: str | None = None) -> Any:
        assert contract_id == REGISTRY_ID
        if fn == "list_ids":
            return list(self.records)
        assert fn == "get"
        agent_id = scval.from_symbol(args[0])  # type: ignore[index]
        self.gets.append(agent_id)
        if agent_id in self.failing_gets:
            raise RuntimeError(f"simulate failed: {agent_id}")
        return self.records[agent_id]

    def read_agent_records(self, contract_id: str, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        assert contract_id == REGISTRY_ID
        self.batches.append(list(agent_ids))
        return {i: self.records[i] for i in agent_ids if i in self.records and i not in self.batch_skips}


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> Any:
    agents_before = dict(state.agents)
    answered_before = set(registry_sync._answered_ids)
    status_before = registry_sync._status
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)
    registry_sync._answered_ids.clear()
    registry_sync._status = registry_sync.SyncStatus()

    def install(records: dict[str, dict[str, Any]], **kw: Any) -> _Registry:
        fake = _Registry(records, **kw)
        monkeypatch.setattr(registry_sync.sc, "simulate_read", fake.simulate_read)
        monkeypatch.setattr(registry_sync.sc, "read_agent_records", fake.read_agent_records)
        return fake

    yield install
    state.agents.clear()
    state.agents.update(agents_before)
    registry_sync._answered_ids.clear()
    registry_sync._answered_ids.update(answered_before)
    registry_sync._status = status_before
    registry_sync._skipped_agt_ids.clear()
    registry_sync._refused_price_ids.clear()
    registry_sync._platform.clear()
    registry_sync._platform_logged.clear()


def _onchain() -> dict[str, Any]:
    return {a.id: a for a in state.agents.values() if a.source == "onchain"}


def test_a_pass_reads_every_record_in_one_batch_and_simulates_no_get(registry: Any) -> None:
    fake = registry({f"op_{i:04d}": _raw(f"op_{i:04d}") for i in range(450)})

    assert asyncio.run(registry_sync.sync_once()) == 450

    assert len(fake.batches) == 1 and len(fake.batches[0]) == 450
    assert fake.gets == []
    assert len(_onchain()) == 450
    assert registry_sync.status().synced is True


def test_an_id_the_batch_left_out_is_read_with_get(registry: Any) -> None:
    fake = registry({i: _raw(i) for i in ("ext_a", "ext_b", "ext_c")}, batch_skips={"ext_b"})

    asyncio.run(registry_sync.sync_once())

    assert fake.gets == ["ext_b"]
    assert sorted(_onchain()) == ["ext_a", "ext_b", "ext_c"]
    assert registry_sync.status().synced is True


def test_an_id_neither_read_answers_keeps_the_pass_from_being_full(registry: Any) -> None:
    fake = registry({i: _raw(i) for i in ("ext_a", "ext_b")}, batch_skips={"ext_b"})
    fake.failing_gets.add("ext_b")

    asyncio.run(registry_sync.sync_once())

    assert sorted(_onchain()) == ["ext_a"]
    assert registry_sync.status().synced is False


def test_a_batch_that_answers_nothing_is_the_old_pass_exactly(registry: Any) -> None:
    ids = ("ext_a", "ext_b", "ext_c")
    fake = registry({i: _raw(i) for i in ids}, batch_skips=set(ids))

    asyncio.run(registry_sync.sync_once())

    assert fake.gets == list(ids)
    assert sorted(_onchain()) == list(ids)


def test_a_batched_record_meets_the_same_trust_boundary(registry: Any) -> None:
    """The mapper is the enforcement point whichever read produced the record:
    an unbelievable price is refused and an earlier copy delisted."""
    records = {"ext_a": _raw("ext_a"), "pricey": _raw("pricey")}
    fake = registry(records)
    asyncio.run(registry_sync.sync_once())
    assert "pricey" in _onchain()

    records["pricey"] = _raw("pricey", price=10**15, name="x" * 500)
    asyncio.run(registry_sync.sync_once())

    assert "pricey" not in _onchain()
    assert fake.gets == []


def test_the_seeded_namespace_is_read_only_to_be_checked(registry: Any) -> None:
    """A built-in id rides in the batch so its record can be checked against the
    catalog (ADR 0016); it is never read with `get` and never mirrored."""
    fake = registry({"agt_01h8": _raw("agt_01h8"), "ext_a": _raw("ext_a")})

    asyncio.run(registry_sync.sync_once())

    assert fake.batches == [["ext_a", "agt_01h8"]]
    assert fake.gets == []
    assert sorted(_onchain()) == ["ext_a"]
