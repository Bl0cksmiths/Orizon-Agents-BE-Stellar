"""Persisted snapshots: a restart serves the last adoption report, not "computing".

Pinned: a live build is saved; the next process restores it, marked persisted
and dated, until its own build lands; a row that is too old, from another
deployment, or no longer decodable is ignored; a store that fails never stops
a snapshot being served; and the Postgres table does all of that for real.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import adoption_svc, registry_sync, snapshot_store, snapshots
from app.services.snapshot_store import InMemorySnapshotStore, PostgresSnapshotStore, StoredSnapshot
from app.services.snapshots import SnapshotCell
from app.state import state


@dataclass
class _Doc:
    n: int
    generated_at: float


def _decode(body: bytes) -> _Doc:
    raw = json.loads(body)
    return _Doc(n=int(raw["n"]), generated_at=float(raw["generated_at"]))


def _encode(doc: _Doc) -> bytes:
    return json.dumps({"n": doc.n, "generated_at": doc.generated_at}).encode()


class _Builds:
    def __init__(self) -> None:
        self.n = 0

    async def __call__(self) -> _Doc:
        self.n += 1
        return _Doc(n=self.n, generated_at=time.time())


def _cell(builds: _Builds, name: str = "doc") -> SnapshotCell[_Doc]:
    return SnapshotCell(
        name,
        lambda: builds(),
        _encode,
        lambda d: d.generated_at,
        fresh_seconds=60.0,
        build_timeout_seconds=5.0,
        retry_after_failure_seconds=30.0,
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySnapshotStore]:
    s = InMemorySnapshotStore()
    monkeypatch.setattr(snapshot_store, "_store", s)
    monkeypatch.setattr(snapshots, "_boot_hooks", [])
    yield s


async def _drain_saves() -> None:
    while snapshot_store._saves:
        await asyncio.gather(*list(snapshot_store._saves))


async def _boot() -> None:
    for hook in snapshots._boot_hooks:
        await hook()


# ── the in-memory store ─────────────────────────────────────────────────────
def test_the_in_memory_store_round_trips_and_never_moves_backwards() -> None:
    s = InMemorySnapshotStore()

    async def go() -> list[StoredSnapshot | None]:
        await s.save("a", "scope", 10.0, b"new")
        await s.save("a", "scope", 5.0, b"older, landing late")
        return [await s.load("a", "scope"), await s.load("a", "other"), await s.load("b", "scope")]

    assert asyncio.run(go()) == [StoredSnapshot(10.0, b"new"), None, None]


def test_the_store_is_chosen_from_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot_store, "_store", None)
    monkeypatch.setattr(settings, "database_url", "")
    assert isinstance(snapshot_store.get_snapshot_store(), InMemorySnapshotStore)
    monkeypatch.setattr(snapshot_store, "_store", None)
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@127.0.0.1:1/db")
    assert isinstance(snapshot_store.get_snapshot_store(), PostgresSnapshotStore)
    asyncio.run(snapshot_store.close_snapshot_store())
    assert snapshot_store._store is None


def test_the_scope_names_the_network_and_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    before = snapshot_store.deployment_scope()
    assert before == snapshot_store.deployment_scope()
    monkeypatch.setattr(settings, "stellar_agent_registry", "CNEWREGISTRY")
    assert snapshot_store.deployment_scope() != before


# ── persist and restore ─────────────────────────────────────────────────────
def test_a_live_build_is_saved_and_restored_into_the_next_process(store: InMemorySnapshotStore) -> None:
    builds = _Builds()
    cell = _cell(builds)
    snapshot_store.persist(cell, _decode, max_restore_age_seconds=3600)

    async def first_process() -> None:
        await cell.get(wait_seconds=None)
        await _drain_saves()

    asyncio.run(first_process())
    cell.reset()  # a new process: nothing in memory

    async def second_process() -> snapshots.Snapshot[_Doc] | None:
        await _boot()
        return cell.current()

    restored = asyncio.run(second_process())
    assert restored is not None
    assert restored.source == "persisted"
    assert restored.value.n == 1
    assert builds.n == 1  # restored, not rebuilt


def test_a_restored_snapshot_never_replaces_a_live_one(store: InMemorySnapshotStore) -> None:
    builds = _Builds()
    cell = _cell(builds)
    snapshot_store.persist(cell, _decode, max_restore_age_seconds=3600)
    asyncio.run(store.save("doc", snapshot_store.deployment_scope(), time.time(), _encode(_Doc(99, time.time()))))

    async def go() -> int:
        await cell.get(wait_seconds=None)
        await _boot()
        snap = cell.current()
        assert snap is not None
        return snap.value.n

    assert asyncio.run(go()) == 1


@pytest.mark.parametrize(
    ("generated_ago", "body", "scope_changed", "why"),
    [
        (7200.0, None, False, "too old"),
        (10.0, b"{not json", False, "undecodable"),
        (10.0, None, True, "another deployment"),
    ],
)
def test_a_row_that_cannot_be_trusted_is_not_restored(
    store: InMemorySnapshotStore,
    monkeypatch: pytest.MonkeyPatch,
    generated_ago: float,
    body: bytes | None,
    scope_changed: bool,
    why: str,
) -> None:
    cell = _cell(_Builds())
    snapshot_store.persist(cell, _decode, max_restore_age_seconds=3600)
    at = time.time() - generated_ago
    asyncio.run(store.save("doc", snapshot_store.deployment_scope(), at, body or _encode(_Doc(7, at))))
    if scope_changed:
        monkeypatch.setattr(settings, "stellar_payment_escrow", "CANOTHERESCROW")
    asyncio.run(_boot())
    assert cell.current() is None, why


class _BrokenStore:
    async def load(self, name: str, scope: str) -> StoredSnapshot | None:
        raise ConnectionError("neon asleep")

    async def save(self, name: str, scope: str, generated_at: float, body: bytes) -> None:
        raise ConnectionError("neon asleep")

    async def close(self) -> None:
        raise ConnectionError("neon asleep")


def test_a_failing_store_never_stops_a_snapshot_being_built_or_served(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(snapshot_store, "_store", _BrokenStore())
    monkeypatch.setattr(snapshots, "_boot_hooks", [])
    builds = _Builds()
    cell = _cell(builds)
    snapshot_store.persist(cell, _decode, max_restore_age_seconds=3600)

    async def go() -> snapshots.Snapshot[_Doc] | None:
        await _boot()
        snap = await cell.get(wait_seconds=None)
        await _drain_saves()
        await snapshot_store.close_snapshot_store()
        return snap

    with caplog.at_level("WARNING"):
        snap = asyncio.run(go())
    assert snap is not None and snap.value.n == 1
    assert "snapshot doc not restored: ConnectionError: neon asleep" in caplog.text
    assert "snapshot doc not persisted: ConnectionError: neon asleep" in caplog.text
    assert "snapshot store did not close cleanly" in caplog.text


# ── the adoption report, end to end ─────────────────────────────────────────
def test_after_a_restart_the_last_adoption_report_is_served_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySnapshotStore()
    monkeypatch.setattr(snapshot_store, "_store", store)
    report = adoption_svc.AdoptionReport(
        network="testnet",
        generated_at=int(time.time()) - 120,
        window_days=7.0,
        targets=adoption_svc.TARGETS,
        totals=adoption_svc.AdoptionCounts(external_agents=2, unique_operator_wallets=2, settled_external_workflows=3),
        met=adoption_svc.AdoptionMet(
            external_agents=True, unique_operator_wallets=True, settled_external_workflows=True
        ),
        operators=[],
        excluded=[],
        degraded=False,
        unreadable_agents=[],
    )
    asyncio.run(
        store.save(
            "adoption", snapshot_store.deployment_scope(), report.generated_at, report.model_dump_json().encode()
        )
    )
    # A process still filling its registry mirror: it may not build yet.
    monkeypatch.setattr(registry_sync, "status", lambda: registry_sync.SyncStatus(synced=False))
    monkeypatch.setattr(state, "started_at", time.time())
    restore = next(h for h in snapshots._boot_hooks if h.__name__ == "restore_adoption_snapshot")
    asyncio.run(restore())

    response = TestClient(app).get("/api/ecosystem/adoption")

    assert response.status_code == 200
    assert response.headers["x-snapshot-source"] == "persisted"
    assert int(response.headers["x-snapshot-age"]) >= 119
    assert response.json() == json.loads(report.model_dump_json())
    assert adoption_svc.report_cell.building() is False


# ── Postgres, for real ──────────────────────────────────────────────────────
def test_the_postgres_store_round_trips_upserts_forward_only_and_reopens(pg_dsn: str) -> None:
    async def go() -> list[Any]:
        first = PostgresSnapshotStore(pg_dsn)
        await first.save("adoption", "scope-a", 100.0, b'{"n": 1}')
        await first.save("adoption", "scope-a", 200.0, b'{"n": 2}')
        await first.save("adoption", "scope-a", 150.0, b'{"n": "late"}')
        await first.save("adoption", "scope-b", 50.0, b'{"n": 3}')
        await first.close()
        second = PostgresSnapshotStore(pg_dsn)  # CREATE TABLE IF NOT EXISTS again: idempotent
        try:
            return [
                await second.load("adoption", "scope-a"),
                await second.load("adoption", "scope-b"),
                await second.load("adoption", "scope-c"),
                await second.load("overview", "scope-a"),
            ]
        finally:
            await second.close()

    assert asyncio.run(go()) == [
        StoredSnapshot(200.0, b'{"n": 2}'),
        StoredSnapshot(50.0, b'{"n": 3}'),
        None,
        None,
    ]


# ── concurrent first use ──────────────────────────────────────────────────


def test_concurrent_first_uses_create_the_schema_without_a_race(pg_dsn: str) -> None:
    """Several processes creating the schema at once — an old and a new instance
    across a deploy — must queue on the DDL lock, not fail on the catalog's
    unique index (app/services/pg_schema.py)."""

    async def first_uses() -> None:
        stores = [PostgresSnapshotStore(pg_dsn) for _ in range(8)]
        try:
            await asyncio.gather(*(store._ready_pool() for store in stores))
        finally:
            for store in stores:
                await store.close()

    asyncio.run(first_uses())
