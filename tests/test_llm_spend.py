"""The price table and the daily spend ledger behind LLM_DAILY_SPEND_CAP_USD.

The Postgres tests run the ledger's real SQL (tests/conftest.py `pg_dsn`):
the increment is what lets several processes share one cap, and a fake pool
could only restate it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

import pytest

from app.config import settings
from app.llm import spend
from app.llm.errors import SpendCapReached
from app.llm.spend import InMemorySpendStore, PostgresSpendStore, SpendLedger, Usage

# 2026-10-06 12:00:00 UTC
NOON = datetime(2026, 10, 6, 12, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ── prices ─────────────────────────────────────────────────────────────────


def test_the_tier_models_are_priced_as_the_owner_set_them() -> None:
    million = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert spend.cost_usd("claude-opus-5-5", million) == pytest.approx(4 + 20 + 0.20)
    assert spend.cost_usd("claude-sonnet-5-5", million) == pytest.approx(2 + 10 + 0.20)
    assert spend.cost_usd("claude-haiku-4-5", million) == pytest.approx(1 + 5 + 0.10)
    assert spend.cost_usd("claude-opus-5-5", Usage(cache_write_tokens=1_000_000)) == pytest.approx(5.0)
    assert spend.jev_cost_usd(1_000_000) == pytest.approx(0.042)


def test_an_unknown_model_is_charged_the_dearest_rate_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    usage = Usage(input_tokens=1_000_000)
    with caplog.at_level(logging.WARNING, logger="app.llm.spend"):
        first = spend.cost_usd("claude-mystery-9", usage)
        spend.cost_usd("claude-mystery-9", usage)
    assert first == max(p.input for p in spend.PRICES.values())
    assert sum("claude-mystery-9" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(
    ("served", "listed"),
    [
        # What the API returns in `response.model`: Haiku 4.5 comes back dated
        # (the live eval of 2026-10-06 recorded exactly this id).
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-sonnet-5-5-20260915", "claude-sonnet-5-5"),
        ("claude-opus-5-5-20260901", "claude-opus-5-5"),
        ("claude-opus-5-20260601", "claude-opus-5"),
        # A pinned-version spelling of a known family is still that family.
        ("claude-haiku-4-5@20251001", "claude-haiku-4-5"),
        ("claude-sonnet-5-5-latest", "claude-sonnet-5-5"),
    ],
)
def test_a_served_model_id_is_priced_as_its_listed_model(
    served: str, listed: str, caplog: pytest.LogCaptureFixture
) -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    with caplog.at_level(logging.WARNING, logger="app.llm.spend"):
        assert spend.price_for(served) == spend.PRICES[listed]
        assert spend.cost_usd(served, usage) == spend.cost_usd(listed, usage)
    assert not caplog.records  # a known family is never "unknown"


def test_the_longest_listed_family_wins() -> None:
    """claude-opus-5 is a prefix of claude-opus-5-5; a dated Opus 5.5 is Opus 5.5."""
    assert spend.price_for("claude-opus-5-5-20260901").input == 4.0
    assert spend.price_for("claude-opus-5-20260601").input == 5.0


def test_a_lookalike_is_not_a_known_family(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.llm.spend"):
        assert spend.price_for("claude-haiku-4-55") == spend.price_for("claude-mystery-9")
    assert any("claude-haiku-4-55" in r.getMessage() for r in caplog.records)


# ── the ledger ─────────────────────────────────────────────────────────────


def _ledger(clock: Clock, store: InMemorySpendStore | None = None) -> SpendLedger:
    return SpendLedger(store or InMemorySpendStore(), clock=clock)


def test_spend_adds_up_within_a_day_and_resets_at_utc_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_spend_cap_usd", 1.0)
    clock = Clock(NOON)
    ledger = _ledger(clock)

    async def run() -> None:
        await ledger.record(model="claude-opus-5-5", purpose="planner", usage=Usage(1, 1), cost=0.6)
        await ledger.record(model="claude-opus-5-5", purpose="planner", usage=Usage(1, 1), cost=0.4)
        with pytest.raises(SpendCapReached) as caught:
            await ledger.check_budget()
        assert caught.value.retry_after == 12 * 3600
        clock.now = datetime(2026, 10, 7, 0, 0, 1, tzinfo=UTC).timestamp()
        await ledger.check_budget()  # a new UTC day, a new budget
        assert await ledger.spent_today() == 0.0

    asyncio.run(run())
    assert ledger.snapshot() == spend.SpendSnapshot(day="2026-10-07", spent_usd=0.0, cap_usd=1.0)


def test_the_snapshot_says_paused_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_spend_cap_usd", 0.5)
    ledger = _ledger(Clock(NOON))
    asyncio.run(ledger.record(model="claude-haiku-4-5", purpose="guard.fallback", usage=Usage(), cost=0.5))
    assert ledger.snapshot().paused is True


def test_another_process_spend_is_seen_after_the_sync_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_spend_cap_usd", 1.0)
    clock = Clock(NOON)
    shared = InMemorySpendStore()
    ours, theirs = _ledger(clock, shared), _ledger(clock, shared)

    async def run() -> None:
        await ours.check_budget()  # synced: nothing spent
        await theirs.record(model="claude-opus-5-5", purpose="planner", usage=Usage(), cost=2.0)
        await ours.check_budget()  # still inside the sync interval: not yet seen
        clock.now += 31
        with pytest.raises(SpendCapReached):
            await ours.check_budget()

    asyncio.run(run())


def test_a_failed_write_is_logged_and_the_spend_still_counts(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_daily_spend_cap_usd", 1.0)

    class Broken(InMemorySpendStore):
        async def add(self, *args: object) -> None:
            raise ConnectionError("db down")

        async def total(self, day: date) -> float:
            raise ConnectionError("db down")

    ledger = SpendLedger(Broken(), clock=Clock(NOON))

    async def run() -> None:
        with caplog.at_level(logging.WARNING, logger="app.llm.spend"):
            await ledger.record(model="claude-opus-5-5", purpose="planner", usage=Usage(), cost=1.5)
            with pytest.raises(SpendCapReached):
                await ledger.check_budget()

    asyncio.run(run())
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "not persisted" in messages and "unreadable" in messages


def test_record_refuses_a_bad_purpose_or_cost() -> None:
    ledger = _ledger(Clock(NOON))
    for purpose, cost in (("Planner Call", 0.1), ("", 0.1), ("planner", -1.0), ("planner", float("nan"))):
        with pytest.raises(ValueError):
            asyncio.run(ledger.record(model="m", purpose=purpose, usage=Usage(), cost=cost))


def test_seconds_until_reset_counts_to_the_next_utc_midnight() -> None:
    assert spend.seconds_until_reset(NOON) == 12 * 3600
    assert spend.seconds_until_reset(datetime(2026, 10, 6, 23, 59, 59, 500_000, tzinfo=UTC).timestamp()) == 1


def test_refresh_if_stale_reads_the_store_in_the_background(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySpendStore()
    today = datetime.now(UTC).date()
    asyncio.run(store.add(today, "claude-opus-5-5", "planner", Usage(), 0.25))
    spend.set_ledger(SpendLedger(store))

    async def probe() -> float:
        spend.refresh_if_stale()
        assert spend.snapshot().spent_usd == 0.0  # the probe never waits on the store
        spend.refresh_if_stale()  # one refresh at a time
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return spend.snapshot().spent_usd

    assert asyncio.run(probe()) == 0.25
    spend.set_ledger(SpendLedger(InMemorySpendStore()))
    spend.refresh_if_stale()  # stale, but no running loop: nothing scheduled, nothing raised


def test_the_process_ledger_follows_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    spend.set_ledger(None)
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@db.invalid/x")
    assert isinstance(spend.get_ledger().store, PostgresSpendStore)
    asyncio.run(spend.close_spend_store())
    monkeypatch.setattr(settings, "database_url", "")
    assert isinstance(spend.get_ledger().store, InMemorySpendStore)


# ── Postgres ───────────────────────────────────────────────────────────────


def test_postgres_rows_increment_per_day_model_and_purpose(pg_dsn: str) -> None:
    async def run() -> tuple[float, float, list[dict[str, object]]]:
        import asyncpg

        store = PostgresSpendStore(pg_dsn)
        try:
            day, other = date(2026, 10, 6), date(2026, 10, 5)
            await store.add(day, "claude-opus-5-5", "planner", Usage(100, 10, 1000, 0), 0.5)
            await store.add(day, "claude-opus-5-5", "planner", Usage(50, 5, 0, 200), 0.25)
            await store.add(day, "jev-1.13.0", "guard", Usage(400, 0), 0.001)
            await store.add(other, "claude-opus-5-5", "planner", Usage(1, 1), 9.0)
            total, before = await store.total(day), await store.total(date(2026, 1, 1))
        finally:
            await store.close()
        conn = await asyncpg.connect(pg_dsn)
        try:
            rows = [
                dict(r)
                for r in await conn.fetch(
                    "SELECT model, purpose, calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens"
                    " FROM llm_spend WHERE day = $1 ORDER BY model",
                    day,
                )
            ]
        finally:
            await conn.close()
        return total, before, rows

    total, before, rows = asyncio.run(run())
    assert total == pytest.approx(0.751) and before == 0.0
    assert rows == [
        {
            "model": "claude-opus-5-5",
            "purpose": "planner",
            "calls": 2,
            "input_tokens": 150,
            "output_tokens": 15,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 200,
        },
        {
            "model": "jev-1.13.0",
            "purpose": "guard",
            "calls": 1,
            "input_tokens": 400,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    ]


def test_postgres_concurrent_writers_add_rather_than_overwrite(pg_dsn: str) -> None:
    async def run() -> float:
        writers = [PostgresSpendStore(pg_dsn) for _ in range(3)]
        try:
            day = date(2026, 10, 6)
            await asyncio.gather(
                *(w.add(day, "claude-sonnet-5-5", "improver", Usage(1, 1), 0.01) for w in writers for _ in range(10))
            )
            return await writers[0].total(day)
        finally:
            for w in writers:
                await w.close()

    assert asyncio.run(run()) == pytest.approx(0.30)


def test_a_restarted_process_resumes_the_day_from_postgres(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The free instance restarts after every idle spell; the cap must survive it."""
    monkeypatch.setattr(settings, "llm_daily_spend_cap_usd", 1.0)

    async def before_restart() -> None:
        ledger = SpendLedger(PostgresSpendStore(pg_dsn))
        try:
            await ledger.record(model="claude-opus-5-5", purpose="planner", usage=Usage(), cost=1.2)
        finally:
            await ledger.store.close()

    async def after_restart() -> None:
        ledger = SpendLedger(PostgresSpendStore(pg_dsn))
        try:
            with pytest.raises(SpendCapReached) as caught:
                await ledger.check_budget()
            assert caught.value.spent_usd == pytest.approx(1.2)
        finally:
            await ledger.store.close()

    asyncio.run(before_restart())
    asyncio.run(after_restart())


def test_record_nowait_counts_at_once_and_persists_in_the_background() -> None:
    store = InMemorySpendStore()
    spend.set_ledger(SpendLedger(store))

    async def run() -> tuple[float, int]:
        spend.record_nowait(model="claude-opus-5-5", purpose="worker.code.gen", usage=Usage(10, 20), cost=0.5)
        counted, rows_before = spend.snapshot().spent_usd, len(store.rows)
        await spend.close_spend_store()  # shutdown waits for the write in flight
        return counted, rows_before

    counted, rows_before = asyncio.run(run())
    assert counted == 0.5 and rows_before == 0
    assert [row for row in store.rows.values()] == [(1, Usage(10, 20), 0.5)]
    # With no loop to write on, the in-memory total still holds it.
    ledger = SpendLedger(InMemorySpendStore())
    ledger.record_nowait(model="claude-opus-5-5", purpose="planner", usage=Usage(), cost=0.25)
    assert ledger.snapshot().spent_usd == 0.25
