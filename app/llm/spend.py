"""What each model call costs, and the daily ledger behind LLM_DAILY_SPEND_CAP_USD.

Every Claude and jev call is priced from its reported usage and added to a
per-UTC-day ledger; `check_budget()` runs before every call and raises
`SpendCapReached` once the day's total reaches the cap, so AI planning pauses
with a notice instead of spending past it.

The ledger is persisted the way the other stores here are (snapshot_store,
binding_store): asyncpg imported lazily, a pool that holds nothing while idle,
idempotent DDL on first use, and an in-memory store whenever DATABASE_URL is
unset. Persisting matters because the free instance restarts from the image
after every idle spell — an in-process total would reset the cap on each wake.
One row per (day, model, purpose), incremented atomically in SQL, so several
processes add up rather than overwrite each other.

The cap is checked against a running total held in memory and re-read from
the database at most every `_SYNC_SECONDS`, so a call costs no extra query.
The bound is therefore soft by design: calls already in flight when the cap is
crossed still finish (and are recorded), and another process's spend is seen
within the sync interval. A database that cannot be reached is logged and the
in-memory total keeps the cap working for this process — persistence never
makes a call fail.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from ..config import settings
from ..services.pg_schema import create_schema
from .errors import SpendCapReached

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    """Token counts of one call. `input_tokens` excludes cache reads and writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class Price:
    """USD per million tokens. `cache_write` is the 5-minute TTL rate (1.25x input)."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# Claude API list prices. The three tier models first; then the models a
# server-side refusal fallback can hand a request to, which bill at their own
# rates (claude-api docs: model-migration.md, Opus 5 / Opus 5.5 sections).
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_read=0.10, cache_write=1.25),
    "claude-opus-5": Price(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Price(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
    "claude-sonnet-5": Price(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
}
# A model this table does not know is charged at the dearest known rate, so a
# new or mistyped id can only make the cap trip early, never late.
_UNKNOWN = Price(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25)

# jev bills input only: $0.042 per million tokens; output is free.
JEV_INPUT_USD_PER_MTOK = 0.042

_warned_unknown: set[str] = set()


def price_for(model: str) -> Price:
    """The model's price; an unknown model gets the dearest known rate, with one warning."""
    price = PRICES.get(model)
    if price is None:
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning("llm spend: no price for model %r; charging it at the highest known rate", model)
        return _UNKNOWN
    return price


def cost_usd(model: str, usage: Usage) -> float:
    """USD for one call's usage on `model`."""
    p = price_for(model)
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_read_tokens * p.cache_read
        + usage.cache_write_tokens * p.cache_write
    ) / 1_000_000


def jev_cost_usd(input_tokens: int) -> float:
    """USD for one jev call: input tokens only."""
    return input_tokens * JEV_INPUT_USD_PER_MTOK / 1_000_000


# ── the store ──────────────────────────────────────────────────────────────

_POOL_MIN_SIZE = 0
_POOL_MAX_SIZE = 2
_POOL_CONNECT_TIMEOUT = 10.0
_POOL_COMMAND_TIMEOUT = 5.0

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_spend (
    day                DATE NOT NULL,
    model              TEXT NOT NULL,
    purpose            TEXT NOT NULL,
    calls              BIGINT NOT NULL,
    input_tokens       BIGINT NOT NULL,
    output_tokens      BIGINT NOT NULL,
    cache_read_tokens  BIGINT NOT NULL,
    cache_write_tokens BIGINT NOT NULL,
    cost_usd           DOUBLE PRECISION NOT NULL,
    updated_at         DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (day, model, purpose)
)
"""

# An increment, never an overwrite: concurrent writers (two requests, or two
# processes across a deploy) each add their own call.
_ADD_SQL = """
INSERT INTO llm_spend AS s
    (day, model, purpose, calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
     cost_usd, updated_at)
VALUES ($1, $2, $3, 1, $4, $5, $6, $7, $8, $9)
ON CONFLICT (day, model, purpose) DO UPDATE SET
    calls = s.calls + 1,
    input_tokens = s.input_tokens + EXCLUDED.input_tokens,
    output_tokens = s.output_tokens + EXCLUDED.output_tokens,
    cache_read_tokens = s.cache_read_tokens + EXCLUDED.cache_read_tokens,
    cache_write_tokens = s.cache_write_tokens + EXCLUDED.cache_write_tokens,
    cost_usd = s.cost_usd + EXCLUDED.cost_usd,
    updated_at = EXCLUDED.updated_at
"""

_TOTAL_SQL = "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_spend WHERE day = $1"


class SpendStore(Protocol):
    async def add(self, day: date, model: str, purpose: str, usage: Usage, cost: float) -> None: ...

    async def total(self, day: date) -> float: ...

    async def close(self) -> None: ...


class InMemorySpendStore:
    """The store when DATABASE_URL is unset: lives and dies with the process."""

    def __init__(self) -> None:
        self.rows: dict[tuple[date, str, str], tuple[int, Usage, float]] = {}

    async def add(self, day: date, model: str, purpose: str, usage: Usage, cost: float) -> None:
        calls, held, spent = self.rows.get((day, model, purpose), (0, Usage(), 0.0))
        self.rows[(day, model, purpose)] = (calls + 1, held + usage, spent + cost)

    async def total(self, day: date) -> float:
        return sum(spent for (d, _m, _p), (_c, _u, spent) in self.rows.items() if d == day)

    async def close(self) -> None:
        return None


class PostgresSpendStore:
    """One table, one row per (day, model, purpose), created on first use."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ready_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                import asyncpg  # lazily: only a deployment with DATABASE_URL needs the driver

                pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                    timeout=_POOL_CONNECT_TIMEOUT,
                    command_timeout=_POOL_COMMAND_TIMEOUT,
                )
                try:
                    # Under the DDL lock: two processes first using the ledger
                    # at once (a deploy's old and new instance) must not race.
                    await create_schema(pool, "llm_spend", _CREATE_TABLE_SQL, timeout=_POOL_COMMAND_TIMEOUT)
                except BaseException:
                    await pool.close()
                    raise
                self._pool = pool
            return self._pool

    async def add(self, day: date, model: str, purpose: str, usage: Usage, cost: float) -> None:
        pool = await self._ready_pool()
        await pool.execute(
            _ADD_SQL,
            day,
            model,
            purpose,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            cost,
            time.time(),
            timeout=_POOL_COMMAND_TIMEOUT,
        )

    async def total(self, day: date) -> float:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_TOTAL_SQL, day, timeout=_POOL_COMMAND_TIMEOUT)
        return float(row["total"]) if row is not None else 0.0

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()


# ── the ledger ─────────────────────────────────────────────────────────────

# How stale the in-memory total may get before the next check re-reads the
# database (to see another process's spend). A sum over a day's rows — a
# few dozen at most — so this is cheap; it only bounds how often.
_SYNC_SECONDS = 30.0

_PURPOSE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")


@dataclass(frozen=True)
class SpendSnapshot:
    """Today's figures as this process holds them, without any I/O (for /readiness)."""

    day: str  # ISO date, UTC
    spent_usd: float
    cap_usd: float

    @property
    def paused(self) -> bool:
        return self.spent_usd >= self.cap_usd


def _utc_day(now: float) -> date:
    return datetime.fromtimestamp(now, UTC).date()


def seconds_until_reset(now: float | None = None) -> int:
    """Whole seconds until the next UTC midnight, when the day's spend resets (at least 1)."""
    moment = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    midnight = datetime.combine(moment.date() + timedelta(days=1), datetime.min.time(), UTC)
    return max(1, math.ceil((midnight - moment).total_seconds()))


class SpendLedger:
    """Today's running total, checked before and added to after every call."""

    def __init__(self, store: SpendStore, *, clock: Any = time.time) -> None:
        self.store = store
        self._clock = clock
        self._day = _utc_day(clock())
        self._spent = 0.0
        self._synced_at: float | None = None
        self._sync_lock = asyncio.Lock()

    def _roll(self) -> date:
        today = _utc_day(self._clock())
        if today != self._day:
            self._day, self._spent, self._synced_at = today, 0.0, None
        return today

    async def _sync(self) -> None:
        now = self._clock()
        if self._synced_at is not None and now - self._synced_at < _SYNC_SECONDS:
            return
        async with self._sync_lock:
            if self._synced_at is not None and self._clock() - self._synced_at < _SYNC_SECONDS:
                return
            day = self._day
            try:
                stored = await self.store.total(day)
            except Exception as e:
                logger.warning(
                    "llm spend: ledger unreadable, holding this process's total: %s: %s", type(e).__name__, e
                )
                stored = 0.0
            if day == self._day:
                # Never below what this process has recorded: a write of ours
                # that failed must not hand the budget back.
                self._spent = max(self._spent, stored)
                self._synced_at = self._clock()

    @property
    def stale(self) -> bool:
        """Whether the held total is due a re-read from the store."""
        self._roll()
        return self._synced_at is None or self._clock() - self._synced_at >= _SYNC_SECONDS

    async def spent_today(self) -> float:
        self._roll()
        await self._sync()
        return self._spent

    async def check_budget(self) -> None:
        """Raise `SpendCapReached` once today's spend has reached the cap."""
        spent = await self.spent_today()
        cap = settings.llm_daily_spend_cap_usd
        if spent >= cap:
            raise SpendCapReached(spent_usd=spent, cap_usd=cap, retry_after=seconds_until_reset(self._clock()))

    async def record(self, *, model: str, purpose: str, usage: Usage, cost: float) -> None:
        """Add one call to today's total and persist it (a failed write is logged, never raised)."""
        if not _PURPOSE.fullmatch(purpose):
            raise ValueError(f"purpose must match {_PURPOSE.pattern}: {purpose!r}")
        if not (math.isfinite(cost) and cost >= 0):
            raise ValueError(f"cost must be a finite, non-negative USD amount: {cost!r}")
        day = self._roll()
        self._spent += cost
        try:
            await self.store.add(day, model, purpose, usage, cost)
        except Exception as e:
            logger.warning(
                "llm spend: %s call for %s not persisted (%.6f USD held in memory): %s: %s",
                model,
                purpose,
                cost,
                type(e).__name__,
                e,
            )

    def snapshot(self) -> SpendSnapshot:
        self._roll()
        return SpendSnapshot(
            day=self._day.isoformat(), spent_usd=round(self._spent, 6), cap_usd=settings.llm_daily_spend_cap_usd
        )


_ledger: SpendLedger | None = None


def get_ledger() -> SpendLedger:
    """The process's ledger, on the store DATABASE_URL selects, made at first use."""
    global _ledger
    if _ledger is None:
        store: SpendStore = PostgresSpendStore(settings.database_url) if settings.database_url else InMemorySpendStore()
        _ledger = SpendLedger(store)
    return _ledger


def set_ledger(ledger: SpendLedger | None) -> None:
    """Swap the process's ledger (tests); None makes the next use choose afresh."""
    global _ledger
    _ledger = ledger


async def close_spend_store() -> None:
    """Release the ledger's pool (shutdown)."""
    global _ledger
    ledger, _ledger = _ledger, None
    if ledger is not None:
        try:
            await ledger.store.close()
        except Exception as e:
            logger.warning("llm spend store did not close cleanly: %s", e)


async def check_budget() -> None:
    """Raise `SpendCapReached` once today's spend has reached LLM_DAILY_SPEND_CAP_USD."""
    await get_ledger().check_budget()


async def record(*, model: str, purpose: str, usage: Usage, cost: float) -> None:
    """Add one priced call to today's ledger."""
    await get_ledger().record(model=model, purpose=purpose, usage=usage, cost=cost)


async def spent_today() -> float:
    """Today's spend in USD, re-read from the database when the held total is stale."""
    return await get_ledger().spent_today()


def snapshot() -> SpendSnapshot:
    """Today's spend and cap as held in memory — no I/O, for /readiness."""
    return get_ledger().snapshot()


_refresh: asyncio.Task[float] | None = None


def refresh_if_stale() -> None:
    """Start one background re-read of today's total when the held one is stale.

    For /readiness, which never waits on the database: a fresh process holds
    0 until its first check, and this lets the next probe see the real total.
    """
    global _refresh
    if not get_ledger().stale or (_refresh is not None and not _refresh.done()):
        return
    try:
        _refresh = asyncio.get_running_loop().create_task(spent_today())
    except RuntimeError:  # no running loop: nothing to schedule on
        _refresh = None
