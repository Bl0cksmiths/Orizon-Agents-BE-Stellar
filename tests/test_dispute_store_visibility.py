"""Which dispute store is running must be answerable without using it (D-063).

The store that holds settlements and disputes is Postgres when DATABASE_URL is
set and an in-memory fallback otherwise, and the fallback loses every record on
restart. Ruling that out (D-058) is a check an operator performs after every
deploy. It used to be impossible on a quiet instance: the store was resolved at
first use, so its line appeared inside the first request that touched a
dispute, carrying that request's id, and no endpoint reported it at all.

These tests pin both answers: the line is in the boot log before any request
is served, and /readiness names the store on demand, without the DSN.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.main import app
from app.services import dispute_store

STORE_LOGGER = "app.services.dispute_store"
# A DSN with a recognisable password, so a leak of any part of it is findable.
DSN = "postgresql://orizon:dsn-password-7c1e@db.internal.example:5432/orizon"


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch):
    """A store resolved from this test's configuration, and no database.

    The binding registry reads its own store at boot from the same
    DATABASE_URL; it is stubbed so the Postgres cases dial nothing, and the
    dispute store's driver import is booby-trapped so that a boot which DID
    dial the dispute database fails the test rather than a socket.
    """

    def _no_driver() -> Any:
        raise AssertionError("the dispute store dialled its database — boot must not")

    async def _no_bindings() -> None:
        return None

    monkeypatch.setattr(dispute_store, "_import_asyncpg", _no_driver)
    monkeypatch.setattr(main, "refresh_bound_ids", _no_bindings)
    monkeypatch.setattr(main, "start_refresh_retry", lambda: None)
    monkeypatch.setattr(settings, "database_url", "")
    dispute_store._store = None
    yield
    dispute_store._store = None


def _store_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == STORE_LOGGER and "dispute store:" in r.getMessage()]


# ── the boot log ───────────────────────────────────────────────────────────


def test_the_in_memory_store_is_named_at_boot_before_any_request(caplog: pytest.LogCaptureFixture) -> None:
    """No request is made: the line must come from the lifespan alone, and at
    WARNING, because on this path records of money that moved are not kept."""
    with caplog.at_level(logging.INFO, logger=STORE_LOGGER), TestClient(app):
        lines = _store_lines(caplog)
        assert isinstance(dispute_store._store, dispute_store.InMemoryDisputeStore)

    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert lines[0].levelno == logging.WARNING
    assert "dispute store: in-memory" in lines[0].getMessage()


def test_the_postgres_store_is_named_at_boot_before_any_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The same, with DATABASE_URL set — and nothing dialled to say so, and
    the DSN (which carries the password) nowhere in the line."""
    monkeypatch.setattr(settings, "database_url", DSN)
    with caplog.at_level(logging.INFO, logger=STORE_LOGGER), TestClient(app):
        lines = _store_lines(caplog)
        assert isinstance(dispute_store._store, dispute_store.PostgresDisputeStore)

    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert lines[0].levelno == logging.INFO
    message = lines[0].getMessage()
    assert "dispute store: postgres" in message
    assert "dsn-password-7c1e" not in message and "db.internal.example" not in message


# ── /readiness ─────────────────────────────────────────────────────────────


def test_readiness_reports_memory_with_no_database_url() -> None:
    with TestClient(app) as client:
        body = client.get("/readiness").json()

    assert body["disputes"] == {"store": "memory"}


def test_readiness_reports_postgres_with_a_database_url_and_never_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No database exists here, and none is needed: the probe reports the
    store this process SELECTED, which is what D-058's check asks, and it
    dials nothing to do it (the driver import is booby-trapped above). No
    part of the DSN — password, host, user, database — may reach an
    unauthenticated route."""
    monkeypatch.setattr(settings, "database_url", DSN)
    with TestClient(app) as client:
        response = client.get("/readiness")

    assert response.json()["disputes"] == {"store": "postgres"}
    for fragment in ("dsn-password-7c1e", "db.internal.example", "orizon:", "postgresql://", "5432"):
        assert fragment not in response.text
