"""A degraded overview part must say so in the log, and say which part.

/api/metrics/overview reports an unreadable source as null and sets
`degraded`. That flag tells the dashboard; these tests pin what tells the
operator: one WARNING naming the part when it degrades, nothing more while it
stays degraded (the dashboard polls every few seconds), a repeat on the duty
cycle so a long outage leaves periodic evidence, and an INFO line when the
part is measured again. A benign state — nothing rated yet — is not an outage
and logs nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from app.routers import metrics as metrics_router
from app.seed import seed_registry
from app.services.dispute_store import InMemoryDisputeStore
from app.services.reputation_svc import RepInfo
from app.state import state

LOGGER_NAME = "app.routers.metrics"


@pytest.fixture(autouse=True)
def seeded_registry() -> None:
    """These tests call the part readers directly, so nothing else populates
    the registry (the app lifespan seeds it for the TestClient fixture)."""
    seed_registry()


@pytest.fixture(autouse=True)
def fresh_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The duty-cycle state is module-global and lives for the whole pytest
    process — fresh notes so each test observes a first transition."""
    for name, part in (("_trust_note", "trust"), ("_workflows_note", "workflows")):
        monkeypatch.setattr(metrics_router, name, metrics_router._SourceNote(part))


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


def _patch_reps(monkeypatch: pytest.MonkeyPatch, builder: Callable[[str], RepInfo]) -> None:
    async def fake(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        return {a: builder(a) for a in agent_ids}

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", fake)


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _run(caplog: pytest.LogCaptureFixture, coro: Coroutine[Any, Any, Any]) -> Any:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        return asyncio.run(coro)


async def _trust_polls(times: int) -> None:
    for _ in range(times):
        await metrics_router._trust(state.list_agents())


# ── the degradation is logged, naming the part ──────────────────


def test_a_degraded_trust_read_logs_one_warning_naming_the_part(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior", degraded=True))
    agents = state.list_agents()

    result = _run(caplog, metrics_router._trust(agents))

    assert result.degraded is True
    assert result.trust.avg is None
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert message.startswith("overview trust degraded:")
    assert f"{len(agents)}/{len(agents)} reputation reads degraded" in message
    assert "never as a stand-in value" in message


def test_an_unreadable_settlement_store_logs_naming_workflows(monkeypatch, caplog):
    class Down(InMemoryDisputeStore):
        async def count_settled_by_day(self) -> dict[int, int]:
            raise ConnectionError("database unreachable")

    monkeypatch.setattr(metrics_router, "get_dispute_store", Down)

    _run(caplog, metrics_router._workflows(1_790_000_000.0))

    message = _records(caplog)[0].getMessage()
    assert message.startswith("overview workflows degraded:")
    assert "ConnectionError: database unreachable" in message


def test_an_unrated_network_is_not_an_outage(monkeypatch, caplog):
    """Nothing rated on-chain yet is measured (avg null), not degraded."""
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior"))

    result = _run(caplog, metrics_router._trust(state.list_agents()))

    assert result.degraded is False
    assert _records(caplog) == []


def test_an_unexpected_raise_logs_a_traceback(monkeypatch, caplog):
    """fetch_reps is documented never to raise; if it does, that is a bug and
    must not be swallowed."""

    async def boom(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        raise RuntimeError("should never happen")

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", boom)

    _run(caplog, metrics_router._trust(state.list_agents()))

    record = _records(caplog)[0]
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is not None


def test_measured_trust_logs_nothing(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="onchain"))

    result = _run(caplog, metrics_router._trust(state.list_agents()))

    assert result.trust.avg == 4.5  # 9000 bps / 2000
    assert _records(caplog) == []


# ── rate limiting + transitions ─────────────────────────────────


def test_steady_degradation_logs_once_not_once_per_poll(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior", degraded=True))

    _run(caplog, _trust_polls(10))

    assert len(_records(caplog)) == 1, "the dashboard polls constantly; one outage is one line"


def test_the_repeat_interval_re_arms(monkeypatch, caplog):
    _patch_reps(monkeypatch, lambda a: _info(a, source="prior", degraded=True))
    monkeypatch.setattr(metrics_router, "_DEGRADED_LOG_INTERVAL_SECONDS", 0.0)

    _run(caplog, _trust_polls(2))

    # A long outage still leaves periodic evidence rather than one line at
    # the very start and then silence.
    assert len(_records(caplog)) == 2


def test_recovery_is_logged_once(monkeypatch, caplog):
    mode = {"degraded": True}

    async def fake(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        if mode["degraded"]:
            return {a: _info(a, source="prior", degraded=True) for a in agent_ids}
        return {a: _info(a, source="onchain") for a in agent_ids}

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", fake)

    async def run() -> None:
        await _trust_polls(1)
        mode["degraded"] = False
        await _trust_polls(3)

    _run(caplog, run())

    levels = [(r.levelno, r.getMessage()) for r in _records(caplog)]
    assert len(levels) == 2
    assert levels[0][0] == logging.WARNING
    assert levels[1] == (logging.INFO, "overview trust recovered: measured again")
