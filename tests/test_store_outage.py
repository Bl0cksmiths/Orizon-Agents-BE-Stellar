"""A database that cannot be reached is a 503 `store_unavailable`, never a 500.

The incident this pins: the Neon project behind the dispute and binding stores
ran out of compute quota, every connect failed with InsufficientResourcesError,
and every route that needed a record answered 500 `internal_error` — telling
each client it had found a bug, while /readiness went on saying "ready".

Each test drives the real store classes over a fake pool that fails the way a
dead database fails, through the real routes and the real exception handler.
Nothing dials a database: the "DSN" below names a host that does not exist, and
is here only to prove it never reaches a response or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import STORE_RETRY_AFTER_SECONDS, app
from app.services import binding_store, dispute_store, store_errors
from app.services.binding_store import PostgresBindingStore
from app.services.dispute_store import PostgresDisputeStore

DSN = "postgres://orizon:hunter2@ep-secret-host.example.invalid/orizon"
SECRETS = ("hunter2", "ep-secret-host")
TASK = "t_outage"
AGENT = "ext_outage"
DISPUTES = f"/api/tasks/{TASK}/disputes"
BINDING = f"/api/agents/{AGENT}/binding"

# Every way the driver says "no usable connection": the quota error that
# started this, the rest of SQLSTATE classes 08 and 53, the server refusing or
# shutting down, a pooled connection found closed, and the transport failing
# underneath — refused, unresolvable, or out of time.
OUTAGES: list[BaseException] = [
    asyncpg.InsufficientResourcesError("Your account or project has exceeded the compute time quota"),
    asyncpg.TooManyConnectionsError("too many connections for role"),
    asyncpg.PostgresConnectionError("connection failure"),
    asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
    asyncpg.CannotConnectNowError("the database system is starting up"),
    asyncpg.AdminShutdownError("terminating connection due to administrator command"),
    asyncpg.InterfaceError("connection is closed"),
    asyncpg.InterfaceError("pool is closing"),
    ConnectionRefusedError(111, "Connect call failed ('10.0.0.1', 5432)"),
    socket.gaierror(-2, "Name or service not known"),
    TimeoutError(),
    OSError("network is unreachable"),
]

# Errors that say the QUERY is wrong, not the database gone. A 503 for these
# would invite a retry that can never succeed, so they stay 500.
DEFECTS: list[BaseException] = [
    asyncpg.PostgresSyntaxError('syntax error at or near "SELEC"'),
    asyncpg.UniqueViolationError("duplicate key value violates unique constraint"),
    asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
    asyncpg.InterfaceError("cannot perform operation: another operation is in progress"),
    RuntimeError("a bug"),
]


def _ids(errors: list[BaseException]) -> list[str]:
    return [f"{type(e).__name__}:{e}"[:60] for e in errors]


class FailingPool:
    """An asyncpg pool over a database that fails every call with `error`."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def _fail(self) -> Any:
        self.calls += 1
        raise self.error

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail()

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail()

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail()

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail()

    async def close(self) -> None:
        return None


class EmptyPool:
    """A reachable database with nothing in it."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetchrow(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        return None

    async def fetch(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls += 1
        return []

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fresh_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no outage on record."""
    monkeypatch.setattr(store_errors, "_health", {})


@pytest.fixture
def api() -> Iterator[TestClient]:
    """The app without its lifespan, and with 500s returned rather than raised."""
    yield TestClient(app, raise_server_exceptions=False)


def _dispute_store(monkeypatch: pytest.MonkeyPatch, pool: Any) -> PostgresDisputeStore:
    store = PostgresDisputeStore(DSN, pool=pool)
    store._ready = True  # the DDL ran on a connection that has since died
    monkeypatch.setattr(dispute_store, "_store", store)
    return store


def _binding_store(monkeypatch: pytest.MonkeyPatch, pool: Any) -> PostgresBindingStore:
    store = PostgresBindingStore(DSN, pool=pool)
    store._ready = True
    monkeypatch.setattr("app.routers.binding.get_binding_store", lambda: store)
    return store


def _assert_outage(response: Any, store: str) -> None:
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(STORE_RETRY_AFTER_SECONDS)
    body = response.json()
    assert body["detail"] == "store_unavailable"
    assert body["error"]["code"] == "store_unavailable"
    assert body["error"]["message"] == f"the {store} store is temporarily unavailable; try again shortly"
    assert not any(secret in response.text for secret in SECRETS)


# ── 503, not 500 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("error", OUTAGES, ids=_ids(OUTAGES))
def test_a_dispute_read_over_a_dead_database_is_a_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    _dispute_store(monkeypatch, FailingPool(error))

    _assert_outage(api.get(DISPUTES), "dispute record")


@pytest.mark.parametrize("error", OUTAGES, ids=_ids(OUTAGES))
def test_a_binding_read_over_a_dead_database_is_a_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    _binding_store(monkeypatch, FailingPool(error))

    _assert_outage(api.get(BINDING), "agent binding")


def test_the_first_connect_failing_in_the_ddl_is_a_503_too(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a cold process the first connect is the schema's, inside `_ready_pool`."""
    store = _dispute_store(monkeypatch, FailingPool(asyncpg.InsufficientResourcesError("quota")))
    store._ready = False

    _assert_outage(api.get(DISPUTES), "dispute record")


def test_a_pool_that_cannot_be_created_is_a_503(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _binding_store(monkeypatch, None)
    store._ready = False

    async def unreachable(**kwargs: Any) -> Any:
        raise ConnectionRefusedError(111, "Connect call failed")

    class Driver:
        create_pool = staticmethod(unreachable)

    monkeypatch.setattr(binding_store, "_import_asyncpg", lambda: Driver)

    _assert_outage(api.get(BINDING), "agent binding")


@pytest.mark.parametrize("error", DEFECTS, ids=_ids(DEFECTS))
def test_a_query_that_is_wrong_is_still_a_500(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    pool = FailingPool(error)
    _dispute_store(monkeypatch, pool)

    response = api.get(DISPUTES)

    assert pool.calls == 1
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "retry-after" not in response.headers
    assert store_errors.store_health(dispute_store.STORE_NAME).available is None


def test_a_reachable_store_answers_as_before(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _dispute_store(monkeypatch, EmptyPool())
    _binding_store(monkeypatch, EmptyPool())

    disputes = api.get(DISPUTES)
    binding = api.get(BINDING)

    assert disputes.status_code == 200
    assert disputes.json()["disputes"] == []
    assert binding.status_code == 404
    assert binding.json()["error"]["code"] == "binding_not_found"


# ── the outage, logged once ───────────────────────────────────────────────


def test_an_outage_is_logged_once_by_class_and_its_end_once(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = FailingPool(asyncpg.InsufficientResourcesError(f"quota exceeded for {DSN}"))
    store = _dispute_store(monkeypatch, pool)
    caplog.set_level(logging.DEBUG, logger="app.services.store_errors")

    assert api.get(DISPUTES).status_code == 503
    assert api.get(DISPUTES).status_code == 503
    store._pool = EmptyPool()
    assert api.get(DISPUTES).status_code == 200

    ours = [r for r in caplog.records if r.name == "app.services.store_errors"]
    warnings = [r.getMessage() for r in ours if r.levelno == logging.WARNING]
    recoveries = [r.getMessage() for r in ours if r.levelno == logging.INFO]
    assert warnings == [
        "dispute record store unavailable (InsufficientResourcesError): "
        "answering 503 store_unavailable until a call gets through"
    ]
    assert len(recoveries) == 1
    assert recoveries[0].startswith("dispute record store reachable again after")
    assert not any(secret in r.getMessage() for r in ours for secret in SECRETS)


# ── readiness ─────────────────────────────────────────────────────────────


def test_readiness_reports_a_store_it_could_not_reach_without_dialling_it(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FailingPool(asyncpg.InsufficientResourcesError("quota"))
    _dispute_store(monkeypatch, pool)

    before = api.get("/readiness").json()["disputes"]
    assert api.get(DISPUTES).status_code == 503
    calls = pool.calls
    during = api.get("/readiness")

    # Nothing exercised the store yet, so nothing is claimed about it.
    assert (before["store"], before["available"], before["cause"]) == ("postgres", None, None)
    # The probe answers, from what the failed request learned...
    assert during.status_code != 500
    disputes = during.json()["disputes"]
    assert disputes["store"] == "postgres"
    assert disputes["available"] is False
    assert disputes["cause"] == "InsufficientResourcesError"
    assert isinstance(disputes["unavailable_since"], float)
    assert not any(secret in during.text for secret in SECRETS)
    # ...and never by connecting: a probe that dials keeps Neon awake.
    assert pool.calls == calls


def test_readiness_reports_the_store_reachable_once_a_call_gets_through(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _dispute_store(monkeypatch, FailingPool(OSError("unreachable")))
    assert api.get(DISPUTES).status_code == 503
    store._pool = EmptyPool()
    assert api.get(DISPUTES).status_code == 200

    disputes = api.get("/readiness").json()["disputes"]

    assert (disputes["available"], disputes["unavailable_since"], disputes["cause"]) == (True, None, None)


def test_the_background_reconcile_read_feeds_readiness_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only requests: any call through the store, such as the sweep's."""
    store = _dispute_store(monkeypatch, FailingPool(asyncpg.TooManyConnectionsError("too many")))

    with pytest.raises(store_errors.StoreUnavailable) as caught:
        asyncio.run(store.list_refund_claims())

    assert caught.value.store == dispute_store.STORE_NAME
    assert isinstance(caught.value.__cause__, asyncpg.TooManyConnectionsError)
    assert store_errors.store_health(dispute_store.STORE_NAME).cause == "TooManyConnectionsError"


# ── the binding pool's bounds ─────────────────────────────────────────────


def test_the_binding_pool_bounds_its_connect_and_its_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncpg's defaults are a minute to connect and no bound on a command."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return FailingPool(TimeoutError())

    class Driver:
        create_pool = staticmethod(fake_create_pool)

    monkeypatch.setattr(binding_store, "_import_asyncpg", lambda: Driver)

    with pytest.raises(store_errors.StoreUnavailable):
        asyncio.run(PostgresBindingStore(DSN).get(AGENT))

    assert captured["timeout"] == binding_store._POOL_CONNECT_TIMEOUT
    assert captured["command_timeout"] == binding_store._POOL_COMMAND_TIMEOUT


# ── the spec ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/tasks/{task_id}/disputes", "get"),
        ("/api/disputes/{dispute_id}", "get"),
        ("/api/disputes", "post"),
        ("/api/agents/{agent_id}/binding", "get"),
        ("/api/agents/{agent_id}/bind", "post"),
        ("/api/agents/{agent_id}/bind", "delete"),
    ],
)
def test_the_spec_documents_the_503_on_the_store_routes(api: TestClient, path: str, method: str) -> None:
    row = api.get("/openapi.json").json()["paths"][path][method]["responses"]["503"]

    assert "`store_unavailable`" in row["description"]
    assert row["content"]["application/json"]["schema"]["$ref"].endswith("ErrorEnvelope")


def test_a_route_with_a_503_of_its_own_documents_both(api: TestClient) -> None:
    spec = api.get("/openapi.json").json()["paths"]

    challenge = spec["/api/disputes/challenge"]["post"]["responses"]["503"]["description"]
    bind = spec["/api/agents/{agent_id}/bind"]["post"]["responses"]["503"]["description"]

    assert "`challenge_capacity_dispute`" in challenge and "`store_unavailable`" in challenge
    assert "`registry_unavailable`" in bind and "`store_unavailable`" in bind
