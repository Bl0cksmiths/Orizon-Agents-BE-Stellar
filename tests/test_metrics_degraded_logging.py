"""avg_trust must say which number it served.

/api/metrics/overview blends a live on-chain trust average with a seeded
presentation baseline (4.86). The swap used to happen in total silence — a
bare `except Exception: return seeded` plus an unlogged "no on-chain
evidence" path — so the dashboard could alternate between measured truth and
a demo constant with nothing in the log to say which. These tests pin the
logging, and pin that it stays rate-limited: the frontend polls this route
every few seconds.
"""

from __future__ import annotations

import logging

import pytest

from app.routers import metrics as metrics_router
from app.seed import seed_registry
from app.services.reputation_svc import RepInfo
from app.state import state

LOGGER_NAME = "app.routers.metrics"


@pytest.fixture(autouse=True)
def seeded_registry():
    """These tests call _avg_trust directly, so nothing else populates the
    registry (the app lifespan seeds it for the TestClient fixture)."""
    seed_registry()


@pytest.fixture(autouse=True)
def reset_trust_log_state(monkeypatch):
    """The duty-cycle state is module-global and lives for the whole pytest
    process — reset it so each test observes a first transition."""
    monkeypatch.setattr(metrics_router, "_trust_degraded", None, raising=False)
    monkeypatch.setattr(metrics_router, "_trust_logged_at", 0.0, raising=False)


def _info(agent_id: str, *, source: str, degraded: bool = False) -> RepInfo:
    onchain = source == "onchain"
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=9000 if onchain else 7000,
        lower_bound_bps=0,
        avg_bps=9000 if onchain else 0,
        count=3 if onchain else 0,
        weight=10_000_000 if onchain else 0,
        disputed=0,
        dispute_rate_bps=0,
        source=source,
        degraded=degraded,
    )


def _patch_reps(monkeypatch, builder) -> None:
    async def fake(agent_ids: list[str], timeout_seconds: float = 2.5) -> dict[str, RepInfo]:
        return {a: builder(a) for a in agent_ids}

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", fake)


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _run(caplog, coro_fn):
    import asyncio

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        return asyncio.run(coro_fn())


# ── the fallback is logged ──────────────────────────────────────


def test_degraded_reads_log_a_warning(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior", degraded=True))
    agents = state.list_agents()

    value = _run(caplog, lambda: metrics_router._avg_trust(agents))

    assert value == pytest.approx(sum(a.rep for a in agents) / len(agents))
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert f"{len(agents)}/{len(agents)} reputation reads degraded" in records[0].getMessage()
    assert "not measured trust" in records[0].getMessage()


def test_unrated_network_is_reported_as_such_not_as_an_outage(monkeypatch, caplog):
    """Nothing rated on-chain yet is a benign state; the message must not
    claim reads degraded, or an operator cannot tell the two apart."""
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior"))

    _run(caplog, lambda: metrics_router._avg_trust(state.list_agents()))

    message = _records(caplog)[0].getMessage()
    assert "no agent has on-chain rating evidence yet" in message
    assert "degraded" not in message


def test_empty_registry_is_logged(caplog):
    value = _run(caplog, lambda: metrics_router._avg_trust([]))

    assert value == metrics_router.DEMO_FALLBACK_TRUST
    assert "registry is empty" in _records(caplog)[0].getMessage()


def test_unexpected_raise_logs_a_traceback(monkeypatch, caplog):
    """fetch_reps is documented never to raise; if it does, that is a bug and
    must not be swallowed the way the old bare `except` swallowed it."""

    async def boom(agent_ids: list[str], timeout_seconds: float = 2.5) -> dict[str, RepInfo]:
        raise RuntimeError("should never happen")

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", boom)
    agents = state.list_agents()

    value = _run(caplog, lambda: metrics_router._avg_trust(agents))

    assert value == pytest.approx(sum(a.rep for a in agents) / len(agents))
    record = _records(caplog)[0]
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is not None


def test_measured_trust_logs_nothing(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="onchain"))

    value = _run(caplog, lambda: metrics_router._avg_trust(state.list_agents()))

    assert value == 4.5  # 9000 bps / 2000
    assert _records(caplog) == []


# ── rate limiting + transitions ─────────────────────────────────


def test_steady_degradation_logs_once_not_once_per_poll(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior", degraded=True))
    agents = state.list_agents()

    def poll_ten_times():
        async def run():
            for _ in range(10):
                await metrics_router._avg_trust(agents)

        return run()

    _run(caplog, lambda: poll_ten_times())

    assert len(_records(caplog)) == 1, "the dashboard polls constantly; one outage is one line"


def test_the_repeat_interval_re_arms(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior", degraded=True))
    monkeypatch.setattr(metrics_router, "_TRUST_LOG_INTERVAL_SECONDS", 0.0)
    agents = state.list_agents()

    async def run():
        await metrics_router._avg_trust(agents)
        await metrics_router._avg_trust(agents)

    _run(caplog, lambda: run())

    # A long outage still leaves periodic evidence rather than one line at
    # the very start and then silence.
    assert len(_records(caplog)) == 2


def test_recovery_is_logged(monkeypatch, caplog):
    agents = state.list_agents()
    mode = {"degraded": True}

    async def fake(agent_ids: list[str], timeout_seconds: float = 2.5) -> dict[str, RepInfo]:
        if mode["degraded"]:
            return {a: _info(a, source="prior", degraded=True) for a in agent_ids}
        return {a: _info(a, source="onchain") for a in agent_ids}

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", fake)

    async def run():
        await metrics_router._avg_trust(agents)
        mode["degraded"] = False
        await metrics_router._avg_trust(agents)

    _run(caplog, lambda: run())

    levels = [(r.levelno, r.getMessage()) for r in _records(caplog)]
    assert len(levels) == 2
    assert levels[0][0] == logging.WARNING
    assert levels[1][0] == logging.INFO
    assert "recovered" in levels[1][1]


# ── the wire contract is unchanged ──────────────────────────────


def test_overview_body_shape_is_untouched_by_the_logging(client):
    """The frontend's `Overview` type is the contract; logging the fallback
    must not have added or removed a key."""
    body = client.get("/api/metrics/overview").json()
    assert set(body) == {
        "agents_online",
        "tasks_per_sec",
        "avg_completion",
        "avg_trust",
        "throughput",
        "skills",
    }
