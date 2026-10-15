"""One registry sync pass, pinned as a sequence: what it reads, in what order, and what it decides per id.

`list_ids`, then one batched record read of every non-`agt_` id plus the
built-in ones, then the platform check over that batch, then a `get` for each
id the batch left out, in listing order. Per id: a built-in is checked only, a
seeded `agt_` id is skipped and warned about once per process, a refused price
is not counted, a failed read is warned about, and a believable price re-arms
a refusal. The pass is full only when every non-`agt_` id has answered.
Faked at the stellar client; nothing reaches testnet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from stellar_sdk import scval

from app.config import settings
from app.services import platform_treasury, registry_sync
from app.state import state

REGISTRY_ID = "CFAKEREGISTRY"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
LOGGER = "app.services.registry_sync"
BUILT_IN = platform_treasury.built_in_ids()[0]
SQUATTER = "agt_squatter"


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


class Chain:
    """`list_ids`, the batch read and `get` over one dict, every call logged in order."""

    def __init__(self, records: dict[str, dict[str, Any]], *, batch_skips: set[str], failing: set[str]) -> None:
        self.records = records
        self.batch_skips = batch_skips
        self.failing = failing
        self.calls: list[tuple[Any, ...]] = []

    def simulate_read(self, contract_id: str, fn: str, args: list | None = None, source: str | None = None) -> Any:
        assert contract_id == REGISTRY_ID
        if fn == "list_ids":
            self.calls.append(("list_ids",))
            return list(self.records)
        assert fn == "get"
        assert args is not None
        agent_id = scval.from_symbol(args[0])
        self.calls.append(("get", agent_id))
        if agent_id in self.failing:
            raise RuntimeError(f"simulate failed: {agent_id}")
        return self.records[agent_id]

    def read_agent_records(self, contract_id: str, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        assert contract_id == REGISTRY_ID
        self.calls.append(("batch", list(agent_ids)))
        return {i: self.records[i] for i in agent_ids if i not in self.batch_skips}


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> Any:
    agents_before = dict(state.agents)
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)
    monkeypatch.setattr(registry_sync, "_answered_ids", set())
    monkeypatch.setattr(registry_sync, "_skipped_agt_ids", set())
    monkeypatch.setattr(registry_sync, "_refused_price_ids", set())
    monkeypatch.setattr(registry_sync, "_status", registry_sync.SyncStatus())

    def install(
        records: dict[str, dict[str, Any]], *, batch_skips: set[str] | None = None, failing: set[str] | None = None
    ) -> Chain:
        fake = Chain(records, batch_skips=batch_skips or set(), failing=failing or set())
        monkeypatch.setattr(registry_sync.sc, "simulate_read", fake.simulate_read)
        monkeypatch.setattr(registry_sync.sc, "read_agent_records", fake.read_agent_records)
        monkeypatch.setattr(
            registry_sync,
            "_check_platform",
            lambda ids, batched: fake.calls.append(("check_platform", list(ids), sorted(batched))),
        )
        return fake

    yield install
    state.agents.clear()
    state.agents.update(agents_before)


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == level]


def test_a_pass_reads_in_order_and_decides_each_id(chain: Any, caplog: pytest.LogCaptureFixture) -> None:
    fake = chain(
        {
            "ext_a": _raw("ext_a"),
            BUILT_IN: _raw(BUILT_IN),
            SQUATTER: _raw(SQUATTER, name="CLOBBERED"),
            "ext_b": _raw("ext_b"),
            "ext_cheap": _raw("ext_cheap", price=1),
            "ext_bad": _raw("ext_bad"),
        },
        batch_skips={"ext_b", "ext_bad"},
        failing={"ext_bad"},
    )
    built_in_before = state.agents.get(BUILT_IN)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert asyncio.run(registry_sync.sync_once()) == 2

    ids = ["ext_a", BUILT_IN, SQUATTER, "ext_b", "ext_cheap", "ext_bad"]
    assert fake.calls == [
        ("list_ids",),
        ("batch", ["ext_a", "ext_b", "ext_cheap", "ext_bad", BUILT_IN]),
        ("check_platform", ids, sorted(["ext_a", "ext_cheap", BUILT_IN])),
        ("get", "ext_b"),
        ("get", "ext_bad"),
    ]
    assert state.agents["ext_a"].source == "onchain"
    assert state.agents["ext_b"].source == "onchain"
    assert "ext_cheap" not in state.agents
    assert "ext_bad" not in state.agents
    assert state.agents.get(BUILT_IN) == built_in_before
    assert state.agents.get(SQUATTER) is None
    assert registry_sync._answered_ids == {"ext_a", "ext_b", "ext_cheap"}
    assert registry_sync._refused_price_ids == {"ext_cheap"}
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 3
    assert warnings[0].startswith(f"registry sync: skipping on-chain id {SQUATTER!r}")
    assert warnings[1].startswith("registry sync: refusing on-chain agent 'ext_cheap'")
    assert warnings[2] == "registry sync: failed to index 'ext_bad': RuntimeError: simulate failed: ext_bad"
    assert _messages(caplog, logging.DEBUG)[-1] == (
        "registry sync: pass not full — 1 listed id(s) have never answered a read: ext_bad"
    )
    assert registry_sync.status().synced is False


def test_a_seeded_id_is_warned_about_once_and_never_holds_the_pass(
    chain: Any, caplog: pytest.LogCaptureFixture
) -> None:
    chain({SQUATTER: _raw(SQUATTER), "ext_a": _raw("ext_a")})

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert asyncio.run(registry_sync.sync_once()) == 1
        assert asyncio.run(registry_sync.sync_once()) == 1

    assert [m for m in _messages(caplog, logging.WARNING) if SQUATTER in m] == [
        f"registry sync: skipping on-chain id {SQUATTER!r} — the agt_ namespace is the seeded catalog, and "
        "upserting it would clobber a worker-backed agent"
    ]
    assert registry_sync._skipped_agt_ids == {SQUATTER}
    assert registry_sync.status().synced is True


def test_a_believable_price_re_arms_the_refusal(chain: Any) -> None:
    records = {"ext_a": _raw("ext_a", price=1)}
    chain(records)
    assert asyncio.run(registry_sync.sync_once()) == 0
    assert registry_sync._refused_price_ids == {"ext_a"}
    assert registry_sync.status().synced is True  # refused is still answered

    records["ext_a"] = _raw("ext_a")
    assert asyncio.run(registry_sync.sync_once()) == 1
    assert registry_sync._refused_price_ids == set()
    assert state.agents["ext_a"].price == pytest.approx(0.05)


def test_only_the_first_five_unanswered_ids_are_named(chain: Any, caplog: pytest.LogCaptureFixture) -> None:
    ids = [f"ext_{i}" for i in range(7)]
    chain({i: _raw(i) for i in ids}, batch_skips=set(ids), failing=set(ids))

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert asyncio.run(registry_sync.sync_once()) == 0

    assert _messages(caplog, logging.DEBUG)[-1] == (
        "registry sync: pass not full — 7 listed id(s) have never answered a read: ext_0, ext_1, ext_2, ext_3, ext_4"
    )
