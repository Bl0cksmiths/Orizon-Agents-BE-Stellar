"""Shared test setup — force a hermetic, offline configuration before the app
is imported so tests never touch OpenAI, PDAX, or the real signing key."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Iterator
from typing import Any

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.stellar import client as _stellar_client


class _NoLiveRpc(RuntimeError):
    """Raised by the hermetic suite's Soroban server stand-in."""


def _no_live_rpc(*, submit: bool = False):
    raise _NoLiveRpc("hermetic suite: no live Soroban RPC — patch the call under test")


@pytest.fixture(autouse=True)
def hermetic_settings():
    """Neutralize anything secret/live that .env may have provided, and
    restore every setting mutated by a test."""
    saved = {
        "stellar_signing_key": settings.stellar_signing_key,
        "api_key": settings.api_key,
        "max_charge_usdc": settings.max_charge_usdc,
        "stellar_reputation_ledger": settings.stellar_reputation_ledger,
        "stellar_agent_registry": settings.stellar_agent_registry,
        "stellar_admin_address": settings.stellar_admin_address,
    }
    settings.stellar_signing_key = ""
    settings.api_key = ""
    # Reads need a source address to build a simulation envelope, and the
    # default is "" — so without this the suite only passes on a machine whose
    # gitignored .env happens to supply one, and fails in CI with
    # "no source address; set STELLAR_ADMIN_ADDRESS". Pinning a known-valid
    # public key here keeps the suite hermetic; it is an identifier, not a
    # secret, and nothing in the suite reaches the network.
    settings.stellar_admin_address = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
    # A live ledger id in .env would make reputation reads hit testnet RPC —
    # tests must stay offline, so force the prior-fallback path.
    settings.stellar_reputation_ledger = ""
    # Same reasoning for the agent registry: a live id in .env would let the
    # 1.02 registry-sync loop (started by lifespan, which every TestClient
    # runs) fire real testnet RPC from inside the hermetic suite.
    settings.stellar_agent_registry = ""
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


@pytest.fixture(autouse=True)
def no_live_rpc(monkeypatch):
    """Every Soroban call in the suite fails fast instead of reaching testnet.

    Blanking the ledger and registry ids above keeps the DEFAULT paths offline,
    but a test that arms a ledger id to reach the on-chain branch — and every
    background read lifespan starts (the ratings writer's scorer check, the
    reputation pre-warm) — would otherwise dial the real RPC whenever a test
    forgot to patch the one call it exercises. Such a test passes on a laptop
    with a network and on nothing else, and what it asserts is testnet's state
    that day. `_server` is the single constructor every read and write goes
    through, so replacing it here closes all of them; a test that means to
    drive the client patches `_server` (or the call above it) itself, which
    overrides this.
    """
    monkeypatch.setattr(_stellar_client, "_server", _no_live_rpc)


@pytest.fixture(autouse=True)
def fresh_planner_limiter(monkeypatch):
    """Every test starts with an empty per-client /decompose budget.

    The limiter is module-global and every TestClient shares one client key,
    so without this the suite's free-form decompose calls accumulate across
    tests and a test run late enough is answered 429 for work it never did.
    A test that exercises the limiter swaps in its own, which overrides this.
    """
    from app.routers import orchestrator as _orchestrator_router
    from app.security import KeyedRateLimiter

    monkeypatch.setattr(
        _orchestrator_router,
        "_planner_limiter",
        KeyedRateLimiter(lambda: settings.decompose_rate_limit_per_minute),
    )


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── real Postgres ─────────────────────────────────────────────────────────
#
# The dispute store's SQL is the money path's last line: the refund mutex, the
# compare-and-set on a verdict and the row lock every append queues on all live
# in it. A fake pool can only re-implement what the SQL is MEANT to do, so a
# test that runs against one checks the fake. The tests that request `pg_dsn`
# run the store's real statements against a real Postgres 16 instead.
#
# Where the database comes from, in order:
#
#   1. ORIZON_TEST_PG_DSN, when set. CI sets it to its postgres service. An
#      explicit DSN that cannot be reached FAILS every test that wanted it, and
#      never skips: somebody asked for this database, so its absence is a fault.
#   2. ORIZON_TEST_PG_REQUIRED, when set, forbids the fallbacks below. CI sets
#      it too, so a job whose DSN went missing fails rather than quietly running
#      the suite without its real-SQL tests.
#   3. pgserver (requirements-dev.txt), which starts a throwaway Postgres 16 for
#      this session from a pip wheel and deletes it at exit.
#   4. Otherwise the tests SKIP, with the reason saying how to get a database.
#      A developer without one still runs every other test.
#
# Isolation is a fresh SCHEMA per test, named into the connection string as
# `search_path` so every connection the store's own pool dials lands in it.
# A rolled-back transaction cannot isolate these tests: the store opens its own
# pool and commits its own transactions, and the races under test need several
# sessions to see each other's committed rows. A database per test would
# isolate them as well, but costs a CREATE DATABASE (a copy of the template)
# every time for no extra separation, since every statement in the store names
# its tables unqualified. The schema is also the connection's
# `application_name`, so teardown can end any session a failed test left
# holding a lock before it drops the schema.

PG_DSN_ENV = "ORIZON_TEST_PG_DSN"
PG_REQUIRED_ENV = "ORIZON_TEST_PG_REQUIRED"

# Every test that reached the database, by outcome, for the required-mode check
# at the end of the session.
_pg_ran: list[str] = []


def _pg_required() -> bool:
    return os.environ.get(PG_REQUIRED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: runs the dispute store's SQL against a real Postgres (tests/conftest.py pg_dsn)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test that needs the database, so `-m postgres` runs the real-SQL
    lane on its own and the required-mode check below can find it."""
    for item in items:
        if "pg_dsn" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.postgres)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    """In required mode a real-Postgres test may not skip, for any reason.

    The fixture already fails rather than skips there; this also catches a
    `skipif` or a `pytest.skip()` added to one of the tests later, which would
    otherwise remove a money-path check from CI without failing anything."""
    report = yield
    if item.get_closest_marker("postgres") is None:
        return report
    if report.skipped and not hasattr(report, "wasxfail") and _pg_required():
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        report.outcome = "failed"
        report.longrepr = f"{PG_REQUIRED_ENV} is set, so a real-Postgres test may not skip: {reason}"
    elif report.when == "call":
        _pg_ran.append(report.outcome)
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """In required mode, a session that ran no real-Postgres test at all fails.

    That is the case the per-test check cannot see: the tests deselected,
    renamed out of collection or deleted outright."""
    if not _pg_required() or session.config.option.collectonly or exitstatus != pytest.ExitCode.OK:
        return
    if not _pg_ran:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            # The progress line is still open in -q mode; end it first.
            reporter.write("\n")
            reporter.write_line(
                f"{PG_REQUIRED_ENV} is set but no real-Postgres test ran: the dispute store's SQL went untested",
                red=True,
            )


async def _pg_execute(dsn: str, *statements: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


def _pg_with(dsn: str, **server_settings: str) -> str:
    """The DSN with server settings appended as query parameters, which asyncpg
    applies to every connection it opens from it."""
    extra = "&".join(f"{key}={value}" for key, value in server_settings.items())
    return f"{dsn}{'&' if '?' in dsn else '?'}{extra}"


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A reachable Postgres for the session, or a skip that says how to get one."""
    dsn = os.environ.get(PG_DSN_ENV, "").strip()
    if dsn:
        try:
            asyncio.run(_pg_execute(dsn, "SELECT 1"))
        except Exception as exc:
            pytest.fail(f"{PG_DSN_ENV} is set but that database cannot be reached: {exc!r}", pytrace=False)
        yield dsn
        return
    if _pg_required():
        pytest.fail(
            f"{PG_REQUIRED_ENV} is set but {PG_DSN_ENV} is not: the real-Postgres tests would not run",
            pytrace=False,
        )
    try:
        import pgserver
    except ModuleNotFoundError:
        pytest.skip(
            f"no Postgres for the real-SQL store tests: install pgserver (pip install -r requirements-dev.txt)"
            f" or set {PG_DSN_ENV}"
        )
    # pgserver narrates every pg_ctl call at INFO, and its own atexit hook
    # would do so after pytest has closed the streams it logs to.
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    try:
        server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
        uri = server.get_uri()
        asyncio.run(_pg_execute(uri, "SELECT 1"))
    except Exception as exc:
        pytest.skip(f"pgserver is installed but could not start a Postgres: {exc!r}")
    try:
        yield uri
    finally:
        # Stopped and deleted here, while the session is still running, rather
        # than by pgserver's atexit hook after it.
        server.cleanup()


@pytest.fixture
def pg_dsn(pg_server: str) -> Iterator[str]:
    """A DSN whose every connection works in a schema of its own, dropped after
    the test with everything in it."""
    schema = f"t_{uuid.uuid4().hex[:20]}"
    asyncio.run(_pg_execute(pg_server, f"CREATE SCHEMA {schema}"))
    try:
        yield _pg_with(pg_server, search_path=schema, application_name=schema)
    finally:
        asyncio.run(
            _pg_execute(
                pg_server,
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                f" WHERE application_name = '{schema}' AND pid <> pg_backend_pid()",
                f"DROP SCHEMA {schema} CASCADE",
            )
        )
