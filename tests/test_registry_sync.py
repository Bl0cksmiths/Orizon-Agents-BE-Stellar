"""The registry sync must mirror the chain without harming what it mirrors into.

The policies pinned here, each load-bearing for story 1.02:

  - the mapper translates a raw registry record faithfully (stroops → USDC,
    active → status) and stamps the reputation PRIOR and `source="onchain"` —
    provenance is contracted evidence (SOW §6.3), not cosmetics;
  - a pass re-reads KNOWN ids, so an on-chain reprice/delist propagates;
  - the seeded `agt_` namespace is never upserted — add_agent is an upsert,
    and an on-chain squatter must not clobber a worker-backed agent;
  - a blank STELLAR_AGENT_REGISTRY disables the sync without touching the
    RPC (the gate reads the setting live, never the lru-cached contract ids);
  - one bad record never kills the rest of a pass;
  - the loop survives failing passes and coalesces the logging: one WARNING
    per outage, DEBUG for the streak, INFO on recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest
from stellar_sdk import scval

from app.config import settings
from app.schemas import Agent
from app.services import registry_sync
from app.state import state

LOGGER_NAME = "app.services.registry_sync"

REGISTRY_ID = "CFAKEREGISTRY"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


def _raw(agent_id: str, **overrides: Any) -> dict[str, Any]:
    """A full registry `get` record as simulate_read decodes it."""
    record: dict[str, Any] = {
        "active": True,
        "id": agent_id,
        "name": f"{agent_id}.worker",
        "owner": OWNER,
        "price": 500_000,  # 0.05 USDC in stroops
        "registered_at": 1_757_000_000,
        "skills": ["translate", "en"],
    }
    record.update(overrides)
    return record


@pytest.fixture(autouse=True)
def clean_registry_sync():
    """Restore state.agents and the service's module-level once-flags, so no
    test observes another's injected agents or spent log guards."""
    agents_before = dict(state.agents)
    registry_sync._platform_logged.clear()
    yield
    state.agents.clear()
    state.agents.update(agents_before)
    registry_sync._disabled_logged = False
    registry_sync._skipped_agt_ids.clear()
    registry_sync._platform.clear()
    registry_sync._platform_logged.clear()
    registry_sync._failing = False
    registry_sync._task = None


@pytest.fixture()
def registry_configured(monkeypatch):
    """A registry contract id, so passes take the on-chain path."""
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)


def _fake_registry(monkeypatch, records: dict[str, dict[str, Any]], failing_ids: set[str] | None = None) -> list:
    """Patch simulate_read with an in-memory registry; returns the call log."""
    calls: list[tuple[str, str, str | None]] = []

    def fake_simulate_read(contract_id: str, fn: str, args: list | None = None, source: str | None = None) -> Any:
        if fn == "list_ids":
            calls.append((contract_id, fn, None))
            return list(records)
        assert fn == "get"
        agent_id = scval.from_symbol(args[0])  # asserts args arrive as real syms
        calls.append((contract_id, fn, agent_id))
        if failing_ids and agent_id in failing_ids:
            raise RuntimeError(f"simulate failed: {agent_id}")
        return records[agent_id]

    monkeypatch.setattr(registry_sync.sc, "simulate_read", fake_simulate_read)
    return calls


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == level]


# ── mapper ──────────────────────────────────────────────────────


def test_to_agent_maps_every_field():
    raw = _raw("ext_a", active=False, price=10_000, skills=["copy", "seo"])
    agent = registry_sync._to_agent(raw)
    assert agent.id == "ext_a"
    assert agent.name == "ext_a.worker"
    assert agent.owner == OWNER
    assert agent.skills == ["copy", "seo"]
    assert agent.price == pytest.approx(0.001)  # 10_000 stroops → USDC
    assert agent.status == "offline"  # delisted on-chain
    assert agent.rep == pytest.approx(3.5)  # the reputation prior, never 0
    assert agent.runs == 0
    assert agent.real is False  # no in-process worker
    assert agent.source == "onchain"  # SOW §6.3 provenance evidence


def test_to_agent_active_record_is_online():
    assert registry_sync._to_agent(_raw("ext_a")).status == "online"


# ── sync_once ───────────────────────────────────────────────────


def test_sync_once_upserts_then_refreshes(registry_configured, monkeypatch):
    records = {"ext_a": _raw("ext_a")}
    calls = _fake_registry(monkeypatch, records)

    assert asyncio.run(registry_sync.sync_once()) == 1
    agent = state.agents["ext_a"]
    assert agent.price == pytest.approx(0.05)
    assert agent.status == "online"
    assert calls[0] == (REGISTRY_ID, "list_ids", None)  # the LIVE setting, not contract_ids()

    # The operator reprices and delists on-chain — the next pass must
    # propagate both, or 1.08's delist is cosmetic.
    records["ext_a"] = _raw("ext_a", price=1_500_000, active=False)
    assert asyncio.run(registry_sync.sync_once()) == 1
    agent = state.agents["ext_a"]
    assert agent.price == pytest.approx(0.15)
    assert agent.status == "offline"


def test_seeded_namespace_is_never_clobbered(registry_configured, monkeypatch, caplog):
    seeded = Agent(
        id="agt_01h8", name="copywrite.v3", skills=["copy"], price=0.012, rep=4.92, status="online", runs=18420
    )
    state.add_agent(seeded)
    # Poisoned payload: if the pass DID index the squatted id, this record
    # would visibly replace the seeded agent.
    records = {"agt_01h8": _raw("agt_01h8", name="CLOBBERED"), "ext_b": _raw("ext_b")}
    calls = _fake_registry(monkeypatch, records)
    # The batch read vouches for both records; the squatted built-in one is
    # only checked against the catalog (ADR 0016), never upserted.
    monkeypatch.setattr(registry_sync.sc, "read_agent_records", lambda _c, ids: {i: records[i] for i in ids})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert asyncio.run(registry_sync.sync_once()) == 1
        assert asyncio.run(registry_sync.sync_once()) == 1

    assert state.agents["agt_01h8"] == seeded  # untouched
    assert state.agents["ext_b"].source == "onchain"
    assert (REGISTRY_ID, "get", "agt_01h8") not in calls  # never even read
    warnings = [r for r in _records(caplog, logging.WARNING) if "agt_01h8" in r.getMessage()]
    assert len(warnings) == 1  # once per id per process, not once per pass


def test_blank_setting_disables_without_touching_rpc(monkeypatch, caplog):
    monkeypatch.setattr(settings, "stellar_agent_registry", "")

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("simulate_read must not be called while the registry is unset")

    monkeypatch.setattr(registry_sync.sc, "simulate_read", never)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert asyncio.run(registry_sync.sync_once()) == 0
        assert asyncio.run(registry_sync.sync_once()) == 0

    infos = [r for r in _records(caplog, logging.INFO) if "disabled" in r.getMessage()]
    assert len(infos) == 1  # once per process, not once per tick


def test_one_bad_record_never_kills_the_pass(registry_configured, monkeypatch, caplog):
    records = {"bad": _raw("bad"), "ext_c": _raw("ext_c")}
    _fake_registry(monkeypatch, records, failing_ids={"bad"})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert asyncio.run(registry_sync.sync_once()) == 1

    assert "bad" not in state.agents
    assert state.agents["ext_c"].id == "ext_c"
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "'bad'" in warnings[0].getMessage()


# ── the background loop ─────────────────────────────────────────


def test_sync_loop_survives_failures_and_coalesces_the_logging(monkeypatch, caplog):
    calls = {"n": 0}

    async def flaky_sync_once() -> int:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("rpc down")
        return 0

    real_sleep = asyncio.sleep

    async def instant_sleep(seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(registry_sync, "sync_once", flaky_sync_once)
    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    async def drive() -> None:
        task = asyncio.create_task(registry_sync._sync_loop())
        while calls["n"] < 4:  # two failures, the recovery, one steady pass
            await real_sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(asyncio.wait_for(drive(), timeout=5.0))

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1  # one per outage, not one per pass
    assert "rpc down" in warnings[0].getMessage()
    assert len(_records(caplog, logging.DEBUG)) >= 1  # the streak coalesced
    infos = [r for r in _records(caplog, logging.INFO) if "recovered" in r.getMessage()]
    assert len(infos) == 1  # armed by the streak, fired once


def test_start_is_idempotent_and_stop_clears_the_task(monkeypatch):
    monkeypatch.setattr(settings, "stellar_agent_registry", "")

    async def scenario() -> None:
        registry_sync.start()
        first = registry_sync._task
        registry_sync.start()
        assert registry_sync._task is first  # no second loop
        await registry_sync.stop()
        assert registry_sync._task is None
        assert first is not None and first.cancelled()

    asyncio.run(scenario())


def test_lifespan_starts_and_stops_the_sync_task():
    """Every TestClient runs lifespan — the loop must exist inside the app
    context and be fully reaped on exit, or every test would leak a task."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        assert registry_sync._task is not None
        assert not registry_sync._task.done()
    assert registry_sync._task is None


def test_successful_submit_kicks_a_sync_pass(client, monkeypatch):
    """The BLO-12 fast path: a SUCCESS submit fires one fire-and-forget sync
    so a fresh registration is listed within seconds; a failed submit or a
    submit error must not."""
    from app.routers import stellar as stellar_router

    kicks: list[bool] = []
    monkeypatch.setattr(stellar_router.registry_sync, "kick", lambda: kicks.append(True))

    monkeypatch.setattr(stellar_router.sc, "envelope_identity", lambda _xdr: ("deadbeef", "GSOURCE"))

    async def _ok(_xdr):
        return {"status": "SUCCESS", "hash": "deadbeef"}

    monkeypatch.setattr(stellar_router.sc, "submit_signed_xdr_async", _ok)
    r = client.post("/api/stellar/submit", json={"signed_xdr": "AAAA"})
    assert r.status_code == 200
    assert kicks == [True]

    async def _failed(_xdr):
        return {"status": "FAILED", "hash": "deadbeef"}

    monkeypatch.setattr(stellar_router.sc, "submit_signed_xdr_async", _failed)
    r = client.post("/api/stellar/submit", json={"signed_xdr": "AAAA"})
    assert r.status_code == 200
    assert kicks == [True]  # unchanged — no kick for a failed tx


def test_sync_trigger_endpoint_reports_count_and_failure(client, monkeypatch):
    from app.routers import stellar as stellar_router

    async def _three():
        return 3

    monkeypatch.setattr(stellar_router.registry_sync, "sync_once", _three)
    r = client.post("/api/stellar/agents/sync")
    assert r.status_code == 200
    assert r.json() == {"synced": 3}

    async def _boom():
        raise RuntimeError("rpc unreachable")

    monkeypatch.setattr(stellar_router.registry_sync, "sync_once", _boom)
    r = client.post("/api/stellar/agents/sync")
    assert r.status_code == 503
    assert "registry_sync_failed" in r.text
