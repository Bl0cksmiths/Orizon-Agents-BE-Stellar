"""S13: the registry sync's first pass, raced against the planner at boot.

The loop's first pass ran in the background while the reputation pre-warm read
`state.list_agents()` — the seeded catalog alone on a fresh process — so after
every restart the first plans were built without any on-chain agent, and each
one's first reputation read was cold: the prior, failing open. Boot now waits,
bounded, for that first pass before the pre-warm, and pre-warms what it found.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time

import pytest

from app.services import registry_sync

LOGGER_NAME = "app.services.registry_sync"


@pytest.fixture(autouse=True)
def clean_loop():
    yield
    registry_sync._task = None
    registry_sync._first_pass = None


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


def test_the_wait_returns_once_the_first_pass_finishes(monkeypatch):
    async def scenario() -> tuple[bool, bool]:
        gate = asyncio.Event()

        async def sync_once() -> int:
            await gate.wait()
            return 1

        monkeypatch.setattr(registry_sync, "sync_once", sync_once)
        registry_sync.start()
        waiting = asyncio.ensure_future(registry_sync.wait_first_pass(5.0))
        await asyncio.sleep(0.05)
        early = waiting.done()
        gate.set()
        finished = await waiting
        await registry_sync.stop()
        return early, finished

    early, finished = asyncio.run(scenario())

    assert early is False  # it really waited for the pass
    assert finished is True


def test_a_hung_first_pass_times_out_and_the_loop_carries_on(monkeypatch, caplog):
    """A hung RPC holds a worker thread, not boot. The wait gives up at its
    bound with one WARNING; the pass is NOT cancelled, and lands afterwards."""
    rpc = threading.Event()
    landed: list[int] = []

    async def sync_once() -> int:
        await asyncio.to_thread(rpc.wait, 10)
        landed.append(1)
        return 1

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)

    async def scenario() -> tuple[bool, float, bool]:
        registry_sync.start()
        started = time.monotonic()
        finished = await registry_sync.wait_first_pass(0.2)
        elapsed = time.monotonic() - started
        loop_alive = registry_sync._task is not None and not registry_sync._task.done()
        rpc.set()
        first = registry_sync._first_pass
        assert first is not None
        await asyncio.wait_for(first.wait(), timeout=5.0)
        await registry_sync.stop()
        return finished, elapsed, loop_alive

    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            finished, elapsed, loop_alive = asyncio.run(scenario())
    finally:
        rpc.set()

    assert finished is False
    assert elapsed < 1.0
    assert loop_alive is True
    assert landed == [1]  # the pass boot stopped waiting for still finished
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "did not finish within 0.2 s" in warnings[0]


def test_a_failed_first_pass_still_releases_boot(monkeypatch):
    async def sync_once() -> int:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)

    async def scenario() -> bool:
        registry_sync.start()
        finished = await registry_sync.wait_first_pass(5.0)
        await registry_sync.stop()
        return finished

    assert asyncio.run(scenario()) is True


def test_a_zero_bound_does_not_wait_and_does_not_warn(monkeypatch, caplog):
    async def sync_once() -> int:
        await asyncio.sleep(10)
        return 0

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)

    async def scenario() -> bool:
        registry_sync.start()
        finished = await registry_sync.wait_first_pass(0.0)
        await registry_sync.stop()
        return finished

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert asyncio.run(scenario()) is False
    assert _warnings(caplog) == []


def test_without_a_loop_there_is_nothing_to_wait_for():
    assert asyncio.run(registry_sync.wait_first_pass(5.0)) is True


# ── the lifespan ────────────────────────────────────────────────


REGISTRY = "CFAKEREGISTRY"
LEDGER = "CFAKELEDGER"
ONCHAIN_ID = "ext_onchain1"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


class Chain:
    """One fake RPC for both contracts boot reads: the AgentRegistry the sync
    mirrors and the ReputationLedger the pre-warm reads. `hold_registry()`
    parks `list_ids` — a hung RPC — until `release()`."""

    def __init__(self) -> None:
        self.rep_reads: list[str] = []
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def hold_registry(self) -> None:
        self.gate = threading.Event()

    def release(self) -> None:
        if self.gate is not None:
            self.gate.set()

    def simulate_read(self, contract_id: str, method: str, args: list | None = None, **_kw: object) -> object:
        if (contract_id, method) == (REGISTRY, "list_ids"):
            if self.gate is not None:
                assert self.gate.wait(10), "the registry read was never released"
            return [ONCHAIN_ID]
        if (contract_id, method) == (REGISTRY, "get"):
            assert args is not None
            return {
                "active": True,
                "id": args[0],
                "name": f"{args[0]}.worker",
                "owner": OWNER,
                "price": 500_000,
                "registered_at": 1_757_000_000,
                "skills": ["translate"],
            }
        if (contract_id, method) == (LEDGER, "rep_state"):
            assert args is not None
            with self._lock:
                self.rep_reads.append(args[0])
            return {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0}
        raise RuntimeError(f"no fake for {contract_id}.{method}")


@pytest.fixture()
def chain(monkeypatch):
    from types import SimpleNamespace

    from app.config import settings
    from app.services import reputation_svc
    from app.state import state
    from app.stellar import cache as rcache
    from app.stellar import client as sc

    fake = Chain()
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY)
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger=LEDGER))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    monkeypatch.setattr(sc, "simulate_read", fake.simulate_read)
    agents_before = dict(state.agents)
    rcache.clear()
    yield fake
    fake.release()
    rcache.clear()
    state.agents.clear()
    state.agents.update(agents_before)
    reputation_svc._prewarm_task = None


def _join_warmup(client) -> None:
    """Wait for lifespan's background warm-up: the registry wait, then the
    pre-warm's start. Boot itself no longer waits for either."""
    import app.main as main

    tasks = {t for t in main._boot_tasks if t.get_name() == "boot-reputation-warmup"}
    if not tasks:
        return

    async def join() -> None:
        await asyncio.wait(tasks)

    client.portal.call(join)


def _join_prewarm(client) -> None:
    from app.services import reputation_svc

    _join_warmup(client)

    task = reputation_svc._prewarm_task
    if task is None:
        return

    async def join() -> None:
        await asyncio.wait({task})

    client.portal.call(join)


def test_the_prewarm_awaits_the_first_registry_pass_and_prewarms_onchain_agents(chain, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import reputation_svc
    from app.state import state

    order: list[str] = []
    real_sync, real_prewarm = registry_sync.sync_once, reputation_svc.start_prewarm

    async def sync_once() -> int:
        synced = await real_sync()
        order.append("sync")
        return synced

    def start_prewarm() -> None:
        order.append("prewarm" if ONCHAIN_ID in state.agents else "prewarm-without-onchain")
        real_prewarm()

    monkeypatch.setattr(registry_sync, "sync_once", sync_once)
    monkeypatch.setattr(reputation_svc, "start_prewarm", start_prewarm)

    with TestClient(app) as client:
        _join_prewarm(client)
        assert order[:2] == ["sync", "prewarm"]
        assert ONCHAIN_ID in chain.rep_reads  # the pre-warm read the on-chain agent


def test_a_hung_registry_pass_never_holds_boot_and_the_prewarm_stops_waiting_at_its_bound(chain, monkeypatch, caplog):
    """Boot does not wait for the registry pass at all — on the live registry a
    pass takes minutes, so the old bounded wait only ever added its whole bound
    to every wake. The pre-warm behind it still waits, bounded, then goes."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.services import reputation_svc

    monkeypatch.setattr(settings, "registry_boot_sync_timeout_seconds", 2.0)
    chain.hold_registry()
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            started = time.monotonic()
            with TestClient(app) as client:
                booted = time.monotonic() - started
                assert client.get("/health").status_code == 200
                assert reputation_svc._prewarm_task is None  # still waiting on the pass
                _join_prewarm(client)
                assert reputation_svc._prewarm_task is not None
                chain.release()
    finally:
        chain.release()

    assert booted < 1.0, f"boot was held {booted:.2f} s"
    assert ONCHAIN_ID not in chain.rep_reads  # the pre-warm did not wait past its bound
    assert any("did not finish within 2.0 s" in line for line in _warnings(caplog))


def test_a_slow_binding_store_holds_boot_no_longer_than_its_budget(monkeypatch, caplog):
    """A waking database is worth a second or two of the first request, and no
    more. Past the budget boot serves without the bound set; the load carries
    on and lands."""
    from fastapi.testclient import TestClient

    import app.main as main
    from app.services import binding_registry

    loaded: list[str] = []

    async def slow_load() -> bool:
        await asyncio.sleep(0.6)
        loaded.append("bindings")
        return True

    monkeypatch.setattr(main, "BOOT_BINDING_LOAD_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(main, "refresh_bound_ids", slow_load)
    monkeypatch.setattr(binding_registry, "_loaded", True)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        started = time.monotonic()
        with TestClient(main.app) as client:
            booted = time.monotonic() - started
            assert loaded == []
            client.portal.call(asyncio.sleep, 0.8)
            assert loaded == ["bindings"]  # not cancelled: it finished behind boot

    assert booted < 0.5
    assert any("did not load within 0.1 s of boot" in r.getMessage() for r in caplog.records)


def test_shutdown_keeps_its_order(monkeypatch):
    """The boot wait changed startup only. Shutdown still stops every
    background loop before the drains and closes the stores last, with the
    post-rating refreshes cancelled beside the pre-warm, before the read pool
    they run on is released."""
    from fastapi.testclient import TestClient

    import app.main as main
    from app.services import rating_writer, refund_reconcile, reputation_svc

    order: list[str] = []

    def recorded(name: str, real):
        if inspect.iscoroutinefunction(real):

            async def wrapper(*a, **k):
                order.append(name)
                return await real(*a, **k)

        else:

            def wrapper(*a, **k):
                order.append(name)
                return real(*a, **k)

        return wrapper

    for owner, attr in (
        (main, "stop_refresh_retry"),
        (rating_writer, "stop"),
        (reputation_svc, "stop_prewarm"),
        (reputation_svc, "stop_refreshes"),
        (reputation_svc, "shutdown_read_pool"),
        (registry_sync, "stop"),
        (refund_reconcile, "stop"),
        (main, "aclose_pdax_client"),
        (main, "close_binding_store"),
        (main, "close_dispute_store"),
    ):
        name = f"{owner.__name__.rsplit('.', 1)[-1]}.{attr}"
        monkeypatch.setattr(owner, attr, recorded(name, getattr(owner, attr)))

    with TestClient(main.app):
        assert order == []

    assert order == [
        "main.stop_refresh_retry",
        "rating_writer.stop",
        "reputation_svc.stop_prewarm",
        "reputation_svc.stop_refreshes",
        "reputation_svc.shutdown_read_pool",
        "registry_sync.stop",
        "refund_reconcile.stop",
        "main.aclose_pdax_client",
        "main.close_binding_store",
        "main.close_dispute_store",
    ]
