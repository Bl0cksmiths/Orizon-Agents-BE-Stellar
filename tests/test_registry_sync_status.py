"""The registry mirror must say when it is complete, and never before.

After a restart the mirror fills one sequential `get` at a time, so for minutes
`state.agents` holds a growing prefix of the registry. Seen live on 2026-10-02:
GET /api/agents answered 226, then 266, then 278 over 45 s, and the site's hero
cached "31 registered agents" from a half-filled mirror. What is pinned here:

  - `synced` stays false through a pass that is still running, flips only when
    a pass has walked the whole of `list_ids` and every listed id has answered;
  - a pass that leaves an id unanswered, that raises, or that is cancelled
    midway, leaves it false;
  - an id that answered once stays answered (the mirror still holds it), so a
    transient failure on a later pass cannot hold the latch open forever.

Hermetic: `simulate_read` is an in-memory registry.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from stellar_sdk import scval

from app.config import settings
from app.schemas import Agent
from app.services import binding_registry, registry_sync
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


@pytest.fixture(autouse=True)
def fresh_status(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A process that has just booted: no pass has run, no id has answered."""
    agents_before = dict(state.agents)
    monkeypatch.setattr(registry_sync, "_status", registry_sync.SyncStatus())
    monkeypatch.setattr(registry_sync, "_answered_ids", set())
    monkeypatch.setattr(registry_sync, "_skipped_agt_ids", set())
    monkeypatch.setattr(registry_sync, "_refused_price_ids", set())
    yield
    state.agents.clear()
    state.agents.update(agents_before)


@pytest.fixture()
def registry_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)


def _fake_registry(
    monkeypatch: pytest.MonkeyPatch,
    records: dict[str, dict[str, Any]],
    failing_ids: set[str] | None = None,
    on_get: Any = None,
) -> None:
    def fake_simulate_read(contract_id: str, fn: str, args: list | None = None, source: str | None = None) -> Any:
        if fn == "list_ids":
            return list(records)
        agent_id = scval.from_symbol(args[0])  # type: ignore[index]
        if on_get is not None:
            on_get(agent_id)
        if failing_ids and agent_id in failing_ids:
            raise RuntimeError(f"simulate failed: {agent_id}")
        return records[agent_id]

    monkeypatch.setattr(registry_sync.sc, "simulate_read", fake_simulate_read)


# ── the latch ─────────────────────────────────────────────────────────────
def test_a_fresh_process_is_not_synced() -> None:
    assert registry_sync.status() == registry_sync.SyncStatus(
        synced=False, syncing=False, agents=None, last_full_sync_at=None
    )


def test_synced_stays_false_through_a_running_pass_and_flips_after_a_full_one(
    registry_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-pass the mirror already holds a prefix; that prefix is not the registry."""
    seen: list[tuple[registry_sync.SyncStatus, int]] = []

    def watch(agent_id: str) -> None:
        if agent_id == "ext_c":
            seen.append((registry_sync.status(), sum(1 for a in state.agents.values() if a.source == "onchain")))

    _fake_registry(monkeypatch, {i: _raw(i) for i in ("ext_a", "ext_b", "ext_c")}, on_get=watch)

    assert asyncio.run(registry_sync.sync_once()) == 3

    ((mid, mirrored),) = seen
    assert mirrored == 2  # two agents already served...
    assert mid.syncing is True
    assert mid.synced is False  # ...and not called complete
    done = registry_sync.status()
    assert done.synced is True
    assert done.syncing is False
    assert done.agents == len(state.agents)
    assert done.last_full_sync_at is not None


def test_a_pass_that_leaves_an_id_unanswered_is_not_full(
    registry_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch, {i: _raw(i) for i in ("ext_a", "ext_b")}, failing_ids={"ext_b"})

    assert asyncio.run(registry_sync.sync_once()) == 1  # the pass itself survives

    assert "ext_a" in state.agents
    status = registry_sync.status()
    assert status.synced is False
    assert status.agents is None
    assert status.last_full_sync_at is None


def test_a_pass_cancelled_midway_leaves_synced_false(
    registry_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one way a pass stops part-way through: its task is cancelled
    (shutdown, a timeout around an on-demand call) while a read is in flight."""
    entered, release = threading.Event(), threading.Event()

    def block_on_last(agent_id: str) -> None:
        if agent_id == "ext_c":
            entered.set()
            release.wait(timeout=5)

    _fake_registry(monkeypatch, {i: _raw(i) for i in ("ext_a", "ext_b", "ext_c")}, on_get=block_on_last)

    async def go() -> None:
        task = asyncio.create_task(registry_sync.sync_once())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(go())

    assert {"ext_a", "ext_b"} <= set(state.agents)  # a prefix landed
    status = registry_sync.status()
    assert status.synced is False
    assert status.syncing is False  # and the flag was not left stuck on


def test_a_failed_list_ids_leaves_synced_false(registry_configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(registry_sync.sc, "simulate_read", down)

    with pytest.raises(RuntimeError):
        asyncio.run(registry_sync.sync_once())

    assert registry_sync.status().synced is False
    assert registry_sync.status().syncing is False


def test_an_id_that_answered_once_does_not_hold_the_latch_open(
    registry_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 1 misses ext_b; pass 2 reads ext_b but misses ext_a, which pass 1
    already mirrored. Between them every id has answered: the mirror is full."""
    records = {i: _raw(i) for i in ("ext_a", "ext_b")}
    _fake_registry(monkeypatch, records, failing_ids={"ext_b"})
    asyncio.run(registry_sync.sync_once())
    assert registry_sync.status().synced is False

    _fake_registry(monkeypatch, records, failing_ids={"ext_a"})
    asyncio.run(registry_sync.sync_once())

    assert {"ext_a", "ext_b"} <= set(state.agents)
    assert registry_sync.status().synced is True


def test_a_refused_record_has_answered(registry_configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A price we refuse is a decision about a record the chain DID return:
    it is not missing from the mirror, it is excluded from it."""
    _fake_registry(monkeypatch, {"ext_a": _raw("ext_a"), "ext_dust": _raw("ext_dust", price=1)})

    asyncio.run(registry_sync.sync_once())

    assert "ext_dust" not in state.agents
    assert registry_sync.status().synced is True


def test_the_seeded_namespace_never_holds_the_latch_open(
    registry_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agt_` ids are never read, so they can never answer; they must not count."""
    _fake_registry(monkeypatch, {"agt_squat": _raw("agt_squat"), "ext_a": _raw("ext_a")})

    asyncio.run(registry_sync.sync_once())

    assert registry_sync.status().synced is True


def test_a_blank_registry_is_full_by_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_agent_registry", "")

    asyncio.run(registry_sync.sync_once())

    status = registry_sync.status()
    assert status.synced is True
    assert status.agents == len(state.agents)


def test_a_later_failing_pass_keeps_the_latch_and_its_count(
    registry_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch, {"ext_a": _raw("ext_a")})
    asyncio.run(registry_sync.sync_once())
    full = registry_sync.status()

    monkeypatch.setattr(registry_sync.sc, "simulate_read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        asyncio.run(registry_sync.sync_once())

    assert registry_sync.status() == full


def test_status_is_a_copy() -> None:
    registry_sync.status().synced = True
    assert registry_sync.status().synced is False


# ── /readiness ────────────────────────────────────────────────────────────
def test_readiness_reports_the_registry_block(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan's first pass over a blank registry is full by definition,
    so a booted test app reports a synced mirror of the seeded catalog."""
    body = client.get("/readiness").json()
    registry = body["registry"]
    assert set(registry) == {"synced", "syncing", "agents", "last_full_sync_at"}
    assert registry["synced"] is True
    assert registry["syncing"] is False
    assert registry["agents"] == len(state.agents)
    assert isinstance(registry["last_full_sync_at"], float)


def test_readiness_reports_a_partial_mirror_as_unsynced(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_sync, "_status", registry_sync.SyncStatus(syncing=True))
    r = client.get("/readiness")
    assert r.json()["registry"] == {"synced": False, "syncing": True, "agents": None, "last_full_sync_at": None}
    # Informational: a partial mirror never moves the verdict.
    monkeypatch.setattr(registry_sync, "_status", registry_sync.SyncStatus(synced=True, agents=1))
    assert client.get("/readiness").status_code == r.status_code


# ── GET /api/agents ───────────────────────────────────────────────────────
# The body contract is frozen: a bare list of exactly these objects.
_AGENTS_SNAPSHOT = [
    {
        "id": "agt_one",
        "name": "copy.v1",
        "skills": ["copy"],
        "price": 0.012,
        "rep": 4.5,
        "status": "online",
        "runs": 7,
        "real": False,
        "owner": None,
        "source": "seeded",
        "bound": None,
    },
    {
        "id": "ext_a",
        "name": "ext_a.worker",
        "skills": ["translate"],
        "price": 0.05,
        "rep": 3.5,
        "status": "online",
        "runs": 0,
        "real": False,
        "owner": OWNER,
        "source": "onchain",
        "bound": True,
    },
]


@pytest.fixture()
def two_agents(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """One seeded and one bound on-chain agent, after the lifespan has seeded."""
    monkeypatch.setattr(binding_registry, "_bound_ids", {"ext_a"})
    monkeypatch.setattr(binding_registry, "_loaded", True)
    state.agents.clear()
    state.add_agent(Agent(id="agt_one", name="copy.v1", skills=["copy"], price=0.012, rep=4.5, status="online", runs=7))
    state.add_agent(registry_sync._to_agent(_raw("ext_a")))


@pytest.mark.parametrize("synced", [True, False])
def test_the_agent_list_says_whether_it_is_the_whole_registry(
    client, two_agents: None, monkeypatch: pytest.MonkeyPatch, synced: bool
) -> None:
    monkeypatch.setattr(registry_sync, "_status", registry_sync.SyncStatus(synced=synced))

    r = client.get("/api/agents")

    assert r.status_code == 200
    assert r.headers["X-Registry-Synced"] == ("true" if synced else "false")
    assert r.headers["X-Registry-Count"] == "2"
    assert r.json() == _AGENTS_SNAPSHOT  # the body is the same list either way


def test_the_count_header_is_the_length_of_the_list(client, monkeypatch: pytest.MonkeyPatch) -> None:
    r = client.get("/api/agents")
    assert int(r.headers["X-Registry-Count"]) == len(r.json()) == len(state.agents)


def test_a_cross_origin_caller_may_read_the_registry_headers(client, two_agents: None) -> None:
    r = client.get("/api/agents", headers={"Origin": "https://orizon-agents-fe-stellar.vercel.app"})
    assert r.headers["access-control-allow-origin"] == "https://orizon-agents-fe-stellar.vercel.app"
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"x-registry-synced", "x-registry-count"} <= exposed
