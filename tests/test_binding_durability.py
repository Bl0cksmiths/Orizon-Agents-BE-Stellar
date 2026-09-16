"""Story 2.01 AC-5 — "the binding survives a backend restart", actually tested.

This is the most load-bearing claim story 2.01 makes and nothing exercised it.
tests/test_binding_store.py asserts the SQL a fake pool receives, which proves
the statements are right and proves nothing about the restart; `refresh_bound_ids`
— the one thing that turns a stored row back into a routable agent — had no test
of any kind. A binding that is durable in the database and invisible to the
planner satisfies the letter of AC-5 and none of its point.

A restart is modelled the way the failure actually happens: the STORE survives
(that is what DATABASE_URL buys) and everything the process held in memory does
not — the routable set, the `_loaded` flag, the read cache. `process()` below is
that boundary, and the assertions in between are the ones that would have caught
a service that boots with an empty routable set and quietly never fills it.

The second half covers the way that boot can fail. `refresh_bound_ids`
deliberately swallows a store error so an unreadable store cannot stop the
service — and until now nothing ever retried, so a database that was merely SLOW
to wake (a cold serverless Postgres, exactly what `PostgresBindingStore`'s
min_size=0 pool is designed around) left every externally operated agent
unroutable for the whole process lifetime, with a redeploy as the only fix.

Hermetic: conftest blanks `stellar_agent_registry`, so the 1.02 sync loop that
lifespan starts no-ops and nothing here reaches the network. The chain lookup the
bind route needs is stubbed, and so is the bind-time DNS resolution.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from stellar_sdk import Keypair

from app.agents.workers.external_http import ExternalHttpWorker
from app.main import app
from app.services import binding_registry, external_binding
from app.services.binding_store import InMemoryBindingStore
from app.stellar import cache as rcache

REGISTRY_LOGGER = "app.services.binding_registry"

AGENT = "ext_durable1"
ENDPOINT = "https://operator.example/run"


@pytest.fixture(autouse=True)
def isolated_registry():
    """Give every test a cold binding registry, and leave one behind.

    All of this is module state shared with the rest of the suite, and these
    tests deliberately drive it through states — unloaded, failing, retrying —
    that nothing else expects to inherit.
    """
    saved = (
        set(binding_registry._bound_ids),
        binding_registry._loaded,
        binding_registry._load_failing,
    )
    binding_registry._bound_ids.clear()
    binding_registry._loaded = False
    binding_registry._load_failing = False
    yield
    if binding_registry._retry_task is not None:
        asyncio.run(binding_registry.stop_refresh_retry())
    binding_registry._bound_ids.clear()
    binding_registry._bound_ids.update(saved[0])
    binding_registry._loaded, binding_registry._load_failing = saved[1], saved[2]
    rcache.clear()


def owned_by(monkeypatch, owner: str) -> None:
    async def _fake(agent_id: str) -> str:
        return owner

    monkeypatch.setattr(external_binding, "resolve_owner", _fake)


def no_dns(monkeypatch) -> None:
    async def _fake(url: str) -> tuple[str, ...]:
        return ("93.184.216.34",)

    monkeypatch.setattr("app.routers.binding.resolve_and_check", _fake)


@contextmanager
def process(monkeypatch, store):
    """One backend process, start to stop.

    Entering runs the real lifespan — including the `refresh_bound_ids` that is
    under test — against `store`. Leaving runs the real shutdown and then throws
    away everything this process held in memory, which is what a restart does to
    a container that comes back from the image. `store` is what DOES survive.
    """
    monkeypatch.setattr("app.routers.binding.get_binding_store", lambda: store)
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: store)
    with TestClient(app) as client:
        yield client
    binding_registry._bound_ids.clear()
    binding_registry._loaded = False
    binding_registry._load_failing = False
    rcache.clear()


def sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def bind(client, keypair: Keypair, agent_id: str) -> None:
    issued = client.post(f"/api/agents/{agent_id}/bind/challenge", json={"endpoint_url": ENDPOINT})
    assert issued.status_code == 200
    r = client.post(
        f"/api/agents/{agent_id}/bind",
        json={"endpoint_url": ENDPOINT, "signature": sign(keypair, issued.json()["message"])},
    )
    assert r.status_code == 200


def unbind(client, keypair: Keypair, agent_id: str) -> None:
    issued = client.post(f"/api/agents/{agent_id}/unbind/challenge")
    assert issued.status_code == 200
    r = client.request(
        "DELETE",
        f"/api/agents/{agent_id}/bind",
        json={"signature": sign(keypair, issued.json()["message"])},
    )
    assert r.status_code == 200


# ── AC-5 ────────────────────────────────────────────────────────


def test_a_binding_survives_a_restart_and_the_agent_is_dispatchable_again(monkeypatch):
    """The whole of AC-5 in one test: bind in one process, restart, and find the
    agent routable in the next one without the operator touching anything."""
    durable = InMemoryBindingStore()
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    no_dns(monkeypatch)

    with process(monkeypatch, durable) as client:
        bind(client, kp, AGENT)
        assert binding_registry.is_dispatchable(AGENT) is True

    # The process is gone. Nothing it learned at runtime is left — this is the
    # state the service boots into, and the state it used to stay in.
    assert binding_registry.is_dispatchable(AGENT) is False
    assert binding_registry.is_bound(AGENT) is None

    with process(monkeypatch, durable) as client:
        # Restored by the startup load alone: no bind, no request, nothing the
        # operator had to do.
        assert binding_registry.is_dispatchable(AGENT) is True
        assert binding_registry.is_bound(AGENT) is True
        # And routable in the sense that actually matters — the dispatch path
        # resolves a worker pointed at the endpoint that was stored.
        worker = asyncio.run(binding_registry.resolve_worker(AGENT))
        assert isinstance(worker, ExternalHttpWorker)
        assert worker.endpoint_url == ENDPOINT
        assert client.get(f"/api/agents/{AGENT}/binding").status_code == 200


def test_a_revoked_binding_does_not_come_back_after_a_restart(monkeypatch):
    """The companion claim, and the one a naive `SELECT DISTINCT agent_id`
    would break: a revocation an operator performed because their host was
    compromised must not be undone by the next redeploy."""
    durable = InMemoryBindingStore()
    kp = Keypair.random()
    owned_by(monkeypatch, kp.public_key)
    no_dns(monkeypatch)

    with process(monkeypatch, durable) as client:
        bind(client, kp, AGENT)
        unbind(client, kp, AGENT)
        assert binding_registry.is_dispatchable(AGENT) is False

    with process(monkeypatch, durable) as client:
        assert binding_registry.is_dispatchable(AGENT) is False
        # Not ignorance this time — the load succeeded and the answer is "no".
        assert binding_registry.is_bound(AGENT) is False
        assert asyncio.run(binding_registry.resolve_worker(AGENT)) is None
        assert client.get(f"/api/agents/{AGENT}/binding").status_code == 404


# ── the boot that could not read the store ──────────────────────


class _FlakyStore:
    """Unreadable for its first `failures` reads, then fine.

    The cold-serverless-Postgres shape: the pool opens a connection on demand
    (min_size=0), the database is asleep, and the first read or two time out
    while it wakes.
    """

    def __init__(self, failures: int, agent_ids: frozenset[str]) -> None:
        self._failures = failures
        self._agent_ids = agent_ids
        self.reads = 0

    async def list_agent_ids(self) -> frozenset[str]:
        self.reads += 1
        if self.reads <= self._failures:
            raise RuntimeError("connection to the binding store timed out")
        return self._agent_ids


def _instant_retries(monkeypatch, attempts: int) -> None:
    """Keep the schedule's shape and drop its wall-clock cost."""
    monkeypatch.setattr(binding_registry, "_REFRESH_RETRY_DELAYS", (0.0,) * attempts)


def test_a_boot_time_store_failure_recovers_on_retry(monkeypatch):
    store = _FlakyStore(failures=2, agent_ids=frozenset({AGENT}))
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: store)
    _instant_retries(monkeypatch, 5)

    async def boot() -> None:
        # The boot read fails and is swallowed, exactly as it is in production.
        assert await binding_registry.refresh_bound_ids() is False
        assert binding_registry.is_dispatchable(AGENT) is False
        # And the marketplace says "we do not know", not "this agent is broken".
        assert binding_registry.is_bound(AGENT) is None
        await binding_registry._refresh_with_retry()

    asyncio.run(boot())

    assert store.reads == 3  # the boot attempt plus two retries, then it lands
    assert binding_registry._loaded is True
    assert binding_registry.is_dispatchable(AGENT) is True
    assert binding_registry.is_bound(AGENT) is True


def test_the_retry_stops_as_soon_as_one_attempt_lands(monkeypatch):
    # Bounded in both directions: it must not keep reading a store that has
    # already answered.
    store = _FlakyStore(failures=1, agent_ids=frozenset({AGENT}))
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: store)
    _instant_retries(monkeypatch, 5)

    async def boot() -> None:
        await binding_registry.refresh_bound_ids()
        await binding_registry._refresh_with_retry()

    asyncio.run(boot())

    assert store.reads == 2


def test_the_retry_is_bounded_and_says_so_when_it_gives_up(monkeypatch, caplog):
    """Unbounded retrying would be the third variant of this bug, not a fix:
    the service would look healthy forever while every external agent stayed
    unroutable. It stops, and it leaves a line a human can act on."""
    store = _FlakyStore(failures=99, agent_ids=frozenset({AGENT}))
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: store)
    _instant_retries(monkeypatch, 3)

    with caplog.at_level(logging.DEBUG, logger=REGISTRY_LOGGER):

        async def boot() -> None:
            await binding_registry.refresh_bound_ids()
            await binding_registry._refresh_with_retry()

        asyncio.run(boot())

    assert store.reads == 4  # the boot attempt plus exactly three retries
    assert binding_registry._loaded is False

    warnings = [r for r in caplog.records if r.name == REGISTRY_LOGGER and r.levelno >= logging.WARNING]
    # One traceback for the first failure, one give-up line at the end — not a
    # traceback per attempt. registry_sync's coalescing discipline.
    assert len(warnings) == 2
    assert sum(1 for r in warnings if r.exc_info) == 1
    assert "unroutable" in warnings[-1].getMessage()
    assert warnings[-1].exc_info is None


def test_lifespan_schedules_the_retry_only_when_the_boot_load_failed(monkeypatch):
    """The wiring. A task on the failing path, and — just as important — no
    task at all on the healthy one, which is every normal boot."""
    _instant_retries(monkeypatch, 0)  # the schedule is empty, so it cannot race
    failing = _FlakyStore(failures=99, agent_ids=frozenset())
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: failing)

    with TestClient(app):
        assert failing.reads >= 1
    # Shutdown cancelled and reaped it rather than leaving a pending task for
    # the loop to destroy on the way out.
    assert binding_registry._retry_task is None

    binding_registry._loaded = False
    healthy = _FlakyStore(failures=0, agent_ids=frozenset({AGENT}))
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: healthy)

    with TestClient(app):
        assert binding_registry._retry_task is None
        assert binding_registry._loaded is True


def test_scheduling_the_retry_twice_leaves_one_retry(monkeypatch):
    # Idempotent, so a second call — a re-entered lifespan, or a future caller
    # that wants to nudge the load — cannot leave two schedules racing each
    # other through the same store.
    store = _FlakyStore(failures=99, agent_ids=frozenset())
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: store)
    _instant_retries(monkeypatch, 0)

    async def go() -> None:
        binding_registry.start_refresh_retry()
        first = binding_registry._retry_task
        binding_registry.start_refresh_retry()
        assert binding_registry._retry_task is first
        await binding_registry.stop_refresh_retry()

    asyncio.run(go())

    assert binding_registry._retry_task is None


# ── the reload must not undo what this process knows ────────────


class _StoreThatBindsMidRead:
    """A store whose snapshot predates a bind this process served.

    Not contrived: the retry runs while the service is answering requests, and
    `list_agent_ids` is a point-in-time read. A bind that lands after that point
    is already in the store, so the process's own knowledge of it is newer than
    the answer coming back.
    """

    def __init__(self, agent_id: str, late: str) -> None:
        self._agent_id = agent_id
        self._late = late

    async def list_agent_ids(self) -> frozenset[str]:
        binding_registry.note_bound(self._late)
        return frozenset({self._agent_id})


class _StoreThatRevokesMidRead:
    """The mirror, and the dangerous direction: a revocation this process served
    after the snapshot was taken. Reinstating it would put a host the owner has
    just declared compromised back into the routable set."""

    def __init__(self, agent_id: str, revoked: str) -> None:
        self._agent_id = agent_id
        self._revoked = revoked

    async def list_agent_ids(self) -> frozenset[str]:
        binding_registry.note_unbound(self._revoked)
        return frozenset({self._agent_id, self._revoked})


def test_a_bind_served_while_the_load_was_in_flight_is_not_lost(monkeypatch):
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _StoreThatBindsMidRead(AGENT, "ext_late1"))

    assert asyncio.run(binding_registry.refresh_bound_ids()) is True

    assert binding_registry.is_dispatchable(AGENT) is True
    assert binding_registry.is_dispatchable("ext_late1") is True


def test_a_revocation_served_while_the_load_was_in_flight_is_not_undone(monkeypatch):
    binding_registry.note_bound("ext_revoked1")
    monkeypatch.setattr(
        binding_registry,
        "get_binding_store",
        lambda: _StoreThatRevokesMidRead(AGENT, "ext_revoked1"),
    )

    assert asyncio.run(binding_registry.refresh_bound_ids()) is True

    assert binding_registry.is_dispatchable(AGENT) is True
    assert binding_registry.is_dispatchable("ext_revoked1") is False
