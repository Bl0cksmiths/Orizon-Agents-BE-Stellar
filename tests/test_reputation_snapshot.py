"""GET /api/stellar/reputation served from a snapshot, and kept honest about ratings.

The batch is one ledger read per agent under a 2.5 s deadline, so it is built
behind the request (app/services/snapshots.py). What must hold: a poll inside
the read TTL costs no reads; a poll past it is answered at once while one
rebuild runs; a rating that lands retires the snapshot, so the next read is
never the pre-rating score; the single-agent route reuses a fresh clean row and
nothing else; and the response carries validators a cache can use.

Hermetic: `reputation_svc.fetch_reps` / `fetch_rep` are the seams.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import stellar as stellar_router
from app.services import registry_sync, reputation_svc, snapshots
from app.services.reputation_svc import RepInfo
from app.state import state


def _info(agent_id: str, smoothed: int, *, degraded: bool = False, stale: bool = False) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=smoothed - 500,
        avg_bps=smoothed,
        count=3,
        weight=30_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="prior" if degraded else "onchain",
        degraded=degraded,
        stale=stale,
        stale_age_seconds=20.0 if stale else None,
    )


class _Ledger:
    """What fetch_reps answers, and how often it was asked."""

    def __init__(self) -> None:
        self.scores: dict[str, int] = {}
        self.degraded: set[str] = set()
        self.batch_calls = 0
        self.single_calls = 0
        self.delay = 0.0

    def info(self, agent_id: str) -> RepInfo:
        return _info(agent_id, self.scores.get(agent_id, 8000), degraded=agent_id in self.degraded)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> _Ledger:
    led = _Ledger()

    async def fetch_reps(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        led.batch_calls += 1
        if led.delay:
            await asyncio.sleep(led.delay)
        return {a: led.info(a) for a in agent_ids}

    async def fetch_rep(agent_id: str) -> RepInfo:
        led.single_calls += 1
        return led.info(agent_id)

    monkeypatch.setattr(reputation_svc, "fetch_reps", fetch_reps)
    monkeypatch.setattr(reputation_svc, "fetch_rep", fetch_rep)
    return led


def _any_agent() -> str:
    return sorted(state.agents)[0]


def _batch(client: TestClient, **headers: str) -> object:
    return client.get("/api/stellar/reputation", headers=headers)


# ── the batch ───────────────────────────────────────────────────────────────
def test_polls_inside_the_ttl_cost_one_batch_read(client: TestClient, ledger: _Ledger) -> None:
    first = _batch(client)
    second = _batch(client)
    assert first.status_code == second.status_code == 200  # type: ignore[attr-defined]
    assert first.json() == second.json()  # type: ignore[attr-defined]
    assert ledger.batch_calls == 1


def test_the_body_is_exactly_the_batch_shape(client: TestClient, ledger: _Ledger) -> None:
    body = _batch(client).json()  # type: ignore[attr-defined]
    assert set(body) == {"reputations", "floor_bps", "prior_bps"}
    assert set(body["reputations"]) == set(state.agents)
    row = body["reputations"][_any_agent()]
    assert set(row) == set(stellar_router.ReputationInfo.model_fields)
    assert row["smoothed_bps"] == 8000


def test_the_batch_carries_validators_and_a_short_shared_lifetime(client: TestClient, ledger: _Ledger) -> None:
    r = _batch(client)
    headers = r.headers  # type: ignore[attr-defined]
    assert headers["etag"].startswith('W/"')
    assert headers["last-modified"].endswith("GMT")
    assert headers["cache-control"].startswith("public, max-age=")
    assert headers["cache-control"].endswith("stale-while-revalidate=15")
    again = _batch(client, **{"If-None-Match": headers["etag"]})
    assert again.status_code == 304  # type: ignore[attr-defined]
    assert again.content == b""  # type: ignore[attr-defined]


def test_an_expired_batch_is_served_at_once_while_it_rebuilds(
    client: TestClient, ledger: _Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stellar_router.reputation_cell, "fresh_seconds", 0.05)
    first = _batch(client).json()  # type: ignore[attr-defined]
    ledger.delay = 1.0
    ledger.scores[_any_agent()] = 6000
    time.sleep(0.1)
    started = time.perf_counter()
    stale = _batch(client).json()  # type: ignore[attr-defined]
    assert time.perf_counter() - started < 0.2
    assert stale == first


def test_a_landed_rating_retires_the_batch_for_the_very_next_read(client: TestClient, ledger: _Ledger) -> None:
    """The S8 lesson, one layer up: after a rating lands the next read is the
    post-rating batch, never the snapshot from before it."""
    agent = _any_agent()
    assert _batch(client).json()["reputations"][agent]["smoothed_bps"] == 8000  # type: ignore[attr-defined]
    ledger.scores[agent] = 5500
    reputation_svc.invalidate_rep(agent)
    assert _batch(client).json()["reputations"][agent]["smoothed_bps"] == 5500  # type: ignore[attr-defined]
    assert ledger.batch_calls == 2


def test_a_batch_that_cannot_be_built_is_503_never_a_fabricated_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        raise RuntimeError("bug")

    monkeypatch.setattr(reputation_svc, "fetch_reps", broken)
    r = TestClient(app).get("/api/stellar/reputation")
    assert r.status_code == 503
    assert r.json()["detail"] == "reputation_unavailable"


def test_the_batch_is_kept_warm_and_rebuilt_on_a_registry_pass() -> None:
    schedule = next(s for s in snapshots._schedules if s.cell is stellar_router.reputation_cell)
    assert schedule.every_seconds <= 60
    assert schedule.fingerprint is not None
    assert schedule.fingerprint() == registry_sync.status().last_full_sync_at


# ── the single-agent route ─────────────────────────────────────────────────
def test_the_single_route_reuses_a_fresh_clean_row(client: TestClient, ledger: _Ledger) -> None:
    agent = _any_agent()
    batch_row = _batch(client).json()["reputations"][agent]  # type: ignore[attr-defined]
    single = client.get(f"/api/stellar/reputation/{agent}").json()
    assert single == batch_row
    assert ledger.single_calls == 0


def test_the_single_route_reads_an_agent_whose_batch_row_was_degraded(client: TestClient, ledger: _Ledger) -> None:
    agent = _any_agent()
    ledger.degraded.add(agent)
    _batch(client)
    ledger.degraded.clear()
    single = client.get(f"/api/stellar/reputation/{agent}").json()
    assert ledger.single_calls == 1
    assert single["degraded"] is False


def test_the_single_route_reads_an_agent_a_rating_has_landed_on(client: TestClient, ledger: _Ledger) -> None:
    agent = _any_agent()
    _batch(client)
    ledger.scores[agent] = 5500
    reputation_svc.invalidate_rep(agent)
    single = client.get(f"/api/stellar/reputation/{agent}").json()
    assert ledger.single_calls == 1
    assert single["smoothed_bps"] == 5500


def test_the_single_route_reads_an_agent_once_the_batch_is_past_its_ttl(
    client: TestClient, ledger: _Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _any_agent()
    _batch(client)
    monkeypatch.setattr(stellar_router.reputation_cell, "fresh_seconds", 0.0)
    client.get(f"/api/stellar/reputation/{agent}")
    assert ledger.single_calls == 1


def test_an_unknown_agent_is_still_404_before_any_read(client: TestClient, ledger: _Ledger) -> None:
    _batch(client)
    r = client.get("/api/stellar/reputation/nobody_registered_this")
    assert r.status_code == 404
    assert ledger.single_calls == 0


# ── change listeners ─────────────────────────────────────────────────────────
def test_invalidate_rep_tells_every_listener_and_survives_a_broken_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    heard: list[str] = []

    def broken(_agent_id: str) -> None:
        raise RuntimeError("listener bug")

    monkeypatch.setattr(reputation_svc, "_change_listeners", [broken, heard.append])
    with caplog.at_level("WARNING"):
        reputation_svc.invalidate_rep("agt_x")
    assert heard == ["agt_x"]
    assert "reputation change listener failed for agt_x" in caplog.text


def test_the_post_rating_read_landing_is_a_change_too(monkeypatch: pytest.MonkeyPatch) -> None:
    heard: list[str] = []
    monkeypatch.setattr(reputation_svc, "_change_listeners", [heard.append])

    async def read_ok(agent_id: str) -> tuple[RepInfo, str | None]:
        return _info(agent_id, 7000), None

    async def read_failed(agent_id: str) -> tuple[RepInfo, str | None]:
        return _info(agent_id, 7000, degraded=True), "rpc down"

    monkeypatch.setattr(reputation_svc, "_read_rep", read_ok)
    asyncio.run(reputation_svc._refresh("agt_ok"))
    monkeypatch.setattr(reputation_svc, "_read_rep", read_failed)
    asyncio.run(reputation_svc._refresh("agt_failed"))
    assert heard == ["agt_ok"]
