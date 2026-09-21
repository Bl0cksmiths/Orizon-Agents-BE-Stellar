"""Where a settlement and its disputes live — durably (story 4.02, ADR 0002).

A dispute window is a promise made to a buyer at settlement time: *you may
dispute this work until 14:32 tomorrow*. Everything the promise rests on has to
outlive the process that made it, and nothing in `app/state.py` does. That store
holds 200 tasks, **evicts finished ones first** — which is exactly the set a
buyer disputes — and loses all of it on restart, which Render's free tier does
whenever the service idles. A window measured in hours cannot live there.

Nor can the facts a dispute is judged against be recovered afterwards: the
`job_id` is a local in `_settle_onchain`, the payer is a parameter of `_run`, and
there has never been a per-step charged amount or a settlement timestamp
anywhere. So settlement is recorded here, once, at the moment it happens.

The shape follows `binding_store.py` deliberately, down to the lazy driver
import and the append-only tables: same seam, same failure modes, one pattern to
learn. Timestamps are epoch seconds from our own clock, never the database's, so
no timezone conversion sits between what was promised and what is later read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

# A dispute's lifecycle. `open` is all story 4.02 ever writes; 4.03 pays the
# credit (`credited`) and 4.04 records the on-chain rating, while an
# adjudication that goes the other way ends at `rejected`.
DisputeStatus = Literal["open", "upheld", "credited", "rejected"]

# Retention for the in-memory fallback ONLY — the store that runs when
# DATABASE_URL is unset (local dev and the hermetic test suite). Postgres keeps
# everything; this cap exists so a long-lived local process cannot grow without
# bound, and it is logged when it bites so nobody mistakes a dropped record for
# a bug in the window arithmetic.
_MAX_IN_MEMORY = 500


# Pool sizing for the Postgres store, taken from binding_store for the reasons
# it gives there rather than by habit.
#
# min_size=0 is the load-bearing one. A free Render instance idles, is spun
# down, and its TCP sockets die with it; a pool that insists on keeping a live
# connection wakes up holding a dead one and hands it to the first request. At
# zero the pool holds nothing while nothing is happening and dials on demand,
# which is also what a serverless Postgres (Neon) wants.
#
# max_size is small because uvicorn runs --workers 1 (render.yaml): this is the
# whole service's connection budget rather than one worker's share of it, and it
# is spent alongside the binding store's own pool, so five here is five more
# connections than that one already holds.
_POOL_MIN_SIZE = 0
_POOL_MAX_SIZE = 5


def _import_asyncpg() -> Any:
    """Import the driver at first Postgres use, never at module import.

    This module is imported on every boot — by the settlement path, and so by
    the hermetic suite and by any checkout that installed only the dev
    requirements. asyncpg is needed solely when DATABASE_URL is set, so
    importing it at module scope would turn an optional dependency into a
    mandatory one and break test collection wherever it is absent. Deferring it
    means a missing driver surfaces here, at the moment something actually
    wanted a database, with the fix in the message instead of as an ImportError
    from an unrelated module three imports away.
    """
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "DATABASE_URL is set but asyncpg is not installed, so settlements and disputes cannot be "
            "stored durably. Install it (`pip install -r requirements.txt`, asyncpg>=0.30,<1) or clear "
            "DATABASE_URL to fall back to the in-memory store."
        ) from exc
    return asyncpg


# The schema, created on first use with CREATE TABLE IF NOT EXISTS. There is no
# migration tooling in this repo and two tables do not justify introducing any:
# the DDL is idempotent, so every boot and every redeploy converges on the same
# schema with no migration step that could fail a deploy at 3am.
#
# `workflow_settlements` is one row per settled workflow and it is APPEND-ONLY,
# like everything durable in this service. Nothing updates a settlement:
# `window_closes_at` is the closing time the buyer was promised and
# `settled_usdc` is what actually moved on-chain, so a row that can be rewritten
# is a row that can quietly move a deadline or raise a credit ceiling after the
# fact. A workflow that somehow settles twice appends a second row and the
# newest one wins (id DESC) — which also means a retried settlement write can
# never fail the path that has just moved money.
#
# `steps` is the whole breakdown in ONE JSONB column (steps_to_json /
# steps_from_json). A child table would cost a join and a transaction for a
# value that is only ever read whole, with the settlement it belongs to. JSONB
# rather than TEXT so the database rejects a malformed breakdown at write time
# instead of a dispute discovering it a day later; asyncpg's default codec for
# jsonb is `str` in both directions, so those two helpers remain the whole of
# the conversion.
#
# Timestamps are DOUBLE PRECISION epoch seconds written from OUR clock — never
# SQL now() — matching SettlementRecord exactly, so no timezone conversion sits
# between what was promised and what is later read.
#
# Both indexes are (key, id DESC) rather than (key): every read here wants the
# NEWEST row for a key, and ordering is by the surrogate `id` rather than by a
# timestamp because two writes landing in the same clock tick must still have a
# defined newest, which a float cannot promise.
_CREATE_SETTLEMENTS_SQL = """
CREATE TABLE IF NOT EXISTS workflow_settlements (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL,
    payer            TEXT NOT NULL,
    auth_id_hex      TEXT NOT NULL,
    job_id_hex       TEXT NOT NULL,
    charge_tx        TEXT,
    proof_tx         TEXT,
    settled_usdc     DOUBLE PRECISION NOT NULL,
    steps            JSONB NOT NULL,
    settled_at       DOUBLE PRECISION NOT NULL,
    window_closes_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_settlements_job_idx
    ON workflow_settlements (job_id_hex, id DESC);
CREATE INDEX IF NOT EXISTS workflow_settlements_task_idx
    ON workflow_settlements (task_id, id DESC);
"""


# `dispute_events` is APPEND-ONLY in the strong sense: a dispute is never
# updated, every status transition INSERTs another row, and the NEWEST row for
# a dispute_id IS that dispute's current state. Story 4.03 pays an upheld
# dispute its credit and 4.04 records the on-chain rating; both go through
# append_status, and neither can overwrite what the buyer was told when the
# dispute was opened. The history is the audit trail — who disputed what, when
# it was upheld, which transaction paid it — and that trail is the evidence the
# marketplace answers a chargeback with, so trading it for an UPDATE would be
# trading away the point of the feature.
#
# Every row carries the WHOLE record rather than a delta, for binding_store's
# tombstone reason: a history row that has to be read alongside its neighbours
# to mean anything is a worse audit record than one that states what happened
# on its own line. It also makes the current state one indexed row rather than
# a fold over a history.
#
# `opening` marks the row that CREATED the dispute (append_status writes FALSE)
# and exists for one purpose: it is the predicate of the partial unique index
# that makes the duplicate rule a database constraint.
#
#   One dispute per (job_id_hex, step_index) is a product rule, and two clicks
#   on "dispute this step" — or a retried request — arrive concurrently. A read
#   in Python cannot enforce it: both requests find nothing and both insert.
#   Neither can a read-then-insert inside one transaction, which is the tempting
#   fix and is NOT a fix at READ COMMITTED (the default, and asyncpg's): both
#   transactions take their snapshot before either has committed, both see no
#   dispute, and both insert. Only SERIALIZABLE or an explicit lock would save
#   it, and both cost every unrelated write in the table.
#
#   A UNIQUE INDEX costs nothing, needs no isolation level and cannot be
#   bypassed by a future caller who forgets the rule. It is PARTIAL — `WHERE
#   opening` — because the table is append-only: the second, third and fourth
#   rows of a dispute repeat its (job_id_hex, step_index) and a total unique
#   index would reject every status transition. Scoping it to the one row that
#   opened the dispute says exactly the rule and nothing more, and it keeps
#   holding after a dispute is resolved, so a rejected dispute cannot be
#   re-opened as a second dispute of the same step.
#
# The three read indexes carry (key..., id DESC) so "the newest row for this
# dispute / this step / this task" is served from the index without a sort.
_CREATE_DISPUTES_SQL = """
CREATE TABLE IF NOT EXISTS dispute_events (
    id              BIGSERIAL PRIMARY KEY,
    dispute_id      TEXT NOT NULL,
    job_id_hex      TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    step_index      INTEGER NOT NULL,
    agent_id        TEXT NOT NULL,
    payer           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,
    charged_usdc    DOUBLE PRECISION NOT NULL,
    creditable_usdc DOUBLE PRECISION NOT NULL,
    opened_at       DOUBLE PRECISION NOT NULL,
    resolved_at     DOUBLE PRECISION,
    refund_tx       TEXT,
    rating_tx       TEXT,
    opening         BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS dispute_events_one_per_step_idx
    ON dispute_events (job_id_hex, step_index) WHERE opening;
CREATE INDEX IF NOT EXISTS dispute_events_dispute_idx
    ON dispute_events (dispute_id, id DESC);
CREATE INDEX IF NOT EXISTS dispute_events_step_idx
    ON dispute_events (job_id_hex, step_index, id DESC);
CREATE INDEX IF NOT EXISTS dispute_events_task_idx
    ON dispute_events (task_id, dispute_id, id DESC);
"""


# The newest settlement for one job, and for one task.
#
# `ORDER BY id DESC LIMIT 1` rather than a unique key on job_id_hex, because
# the table is append-only: if a workflow ever settles twice — a retried
# charge, a replayed callback — the second row is the truth and the first is
# history. Both reads are served straight off the (key, id DESC) indexes.
#
# `steps::text` rather than `steps`: the column is JSONB, and rendering it as
# text guarantees the value asyncpg hands back is the string steps_from_json
# parses, whatever json codec a future caller may set on the pool.
_SELECT_SETTLEMENT_BY_JOB_SQL = """
SELECT task_id, payer, auth_id_hex, job_id_hex, charge_tx, proof_tx,
       settled_usdc, steps::text AS steps, settled_at, window_closes_at
FROM workflow_settlements
WHERE job_id_hex = $1
ORDER BY id DESC
LIMIT 1
"""

_SELECT_SETTLEMENT_BY_TASK_SQL = """
SELECT task_id, payer, auth_id_hex, job_id_hex, charge_tx, proof_tx,
       settled_usdc, steps::text AS steps, settled_at, window_closes_at
FROM workflow_settlements
WHERE task_id = $1
ORDER BY id DESC
LIMIT 1
"""


# Record a settlement. A plain INSERT with no RETURNING: the caller already
# holds the record it handed us — settled_at and window_closes_at included,
# both stamped by the execution path's own clock — so there is nothing to read
# back, and nothing here is derived from the row.
#
# `$8::jsonb` states the cast rather than leaving it to inference, so the
# breakdown is validated as JSON by the database on the way in.
_INSERT_SETTLEMENT_SQL = """
INSERT INTO workflow_settlements (
    task_id, payer, auth_id_hex, job_id_hex, charge_tx, proof_tx,
    settled_usdc, steps, settled_at, window_closes_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
"""


# A dispute's current state is its NEWEST event row, so every read here is the
# same shape: filter, `ORDER BY id DESC`, take one. No fold over the history and
# no join, because each row already carries the whole record.
_SELECT_DISPUTE_SQL = """
SELECT dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
       charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx
FROM dispute_events
WHERE dispute_id = $1
ORDER BY id DESC
LIMIT 1
"""

# The dispute of one step, which is how a second "dispute this step" request
# finds the first one to answer with. Safe as a LIMIT 1 precisely because of the
# partial unique index: a (job_id_hex, step_index) pair can only ever have had
# one dispute opened against it, so its newest event row is that dispute's
# current state rather than one of several disputes' states.
_SELECT_DISPUTE_BY_STEP_SQL = """
SELECT dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
       charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx
FROM dispute_events
WHERE job_id_hex = $1 AND step_index = $2
ORDER BY id DESC
LIMIT 1
"""

# Every dispute of one task, each collapsed to its current state. DISTINCT ON
# (dispute_id) with ORDER BY dispute_id, id DESC keeps the newest row per
# dispute — the (task_id, dispute_id, id DESC) index serves that ordering
# directly — and the outer ORDER BY re-sorts them the way a human reads a
# receipt: oldest dispute first, and a stable tiebreak by step for two opened in
# the same clock tick. A caller listing a task's disputes must not see them
# shuffle between two identical requests.
_SELECT_DISPUTES_FOR_TASK_SQL = """
SELECT dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
       charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx
FROM (
    SELECT DISTINCT ON (dispute_id) *
    FROM dispute_events
    WHERE task_id = $1
    ORDER BY dispute_id, id DESC
) AS latest
ORDER BY opened_at, step_index
"""


# Open a dispute — the statement the duplicate rule is enforced by.
#
# `opening` is TRUE, so this row (and only this row) is covered by
# dispute_events_one_per_step_idx. Two concurrent requests for the same step
# therefore cannot both land: the second blocks until the first commits and is
# then refused by the index, whatever isolation level either of them runs at.
#
# ON CONFLICT ... DO NOTHING rather than catching a unique violation, for two
# reasons. It keeps a loser on the ordinary return path instead of an exception
# whose class would have to be imported from asyncpg — the one import this
# module goes out of its way not to make at module scope — and DO NOTHING is
# the only ON CONFLICT clause that does not modify the conflicting row, so the
# append-only rule still holds (DO UPDATE would be an UPDATE wearing a hat).
# The conflict target repeats the index predicate, `WHERE opening`, because
# that is how Postgres infers a PARTIAL index; without it the statement would
# not match this index at all.
#
# RETURNING is how the caller learns which it was: a row means this insert won
# the step, no row means another dispute already owns it and open_dispute reads
# that one back to hand to DuplicateDisputeError.
_INSERT_DISPUTE_SQL = """
INSERT INTO dispute_events (
    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
    charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, opening
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, TRUE)
ON CONFLICT (job_id_hex, step_index) WHERE opening DO NOTHING
RETURNING dispute_id
"""


# Move a dispute to a new status by APPENDING its next event row — the whole of
# what stories 4.03 (credited) and 4.04 (rated) do to a dispute.
#
# One statement, for binding_store's reason: the `latest` CTE and the INSERT
# share a snapshot, so there is no window between reading the current row and
# writing the one that supersedes it, and a credit and a rating landing
# together cannot each write a row that forgets the other's.
#
# The immutable half of the record is copied forward from `latest` rather than
# re-supplied by the caller. A caller that had to restate the payer, the reason
# and the charged amount on every transition is a caller that can restate them
# WRONGLY, and this table is evidence.
#
# COALESCE is what makes a partial update mean "leave the rest alone": a
# transition that names only a refund_tx keeps the rating_tx already recorded.
# `resolved_at` falls through three values in order — the one the caller gave,
# the one already on the record, then $6, this process's clock — so the moment a
# dispute was first resolved is stamped once and never moved by a later event.
# The casts are explicit because an untyped NULL parameter inside COALESCE is
# ambiguous to the planner.
#
# `opening` is FALSE, and that is load-bearing rather than cosmetic: a
# transition row that claimed to be an opening would collide with its own
# dispute's opening row in the partial unique index, and every resolution in
# the system would fail.
_APPEND_STATUS_SQL = """
WITH latest AS (
    SELECT *
    FROM dispute_events
    WHERE dispute_id = $1
    ORDER BY id DESC
    LIMIT 1
)
INSERT INTO dispute_events (
    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
    charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, opening
)
SELECT latest.dispute_id, latest.job_id_hex, latest.task_id, latest.step_index,
       latest.agent_id, latest.payer, latest.reason, $2,
       latest.charged_usdc, latest.creditable_usdc, latest.opened_at,
       COALESCE($5::double precision, latest.resolved_at, $6::double precision),
       COALESCE($3::text, latest.refund_tx),
       COALESCE($4::text, latest.rating_tx),
       FALSE
FROM latest
RETURNING dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
          charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx
"""


@dataclass(frozen=True)
class SettlementStep:
    """One step of a settled workflow, as it was charged.

    `price_usdc` is the step's own price — the number a credit for this step is
    computed from. `delivered` is whether the step actually produced output: a
    step that failed was never part of what the buyer paid for, so it cannot be
    disputed (there is nothing to credit).
    """

    step_index: int
    agent_id: str
    agent_name: str | None
    price_usdc: float
    delivered: bool


@dataclass(frozen=True)
class SettlementRecord:
    """What a paid workflow settled as, and until when it can be disputed.

    Frozen because a record is evidence, not state. `settled_usdc` is the amount
    that actually moved on-chain, not the sum of the plan's estimates — the
    charge floors its total to dust, and a credit computed from an estimate
    could exceed what was ever paid.

    `window_closes_at` is stamped here rather than recomputed on read: the buyer
    was told a closing time, and tuning `DISPUTE_WINDOW_SECONDS` afterwards must
    not move the deadline for work already done.
    """

    task_id: str
    payer: str
    auth_id_hex: str
    job_id_hex: str
    charge_tx: str | None
    proof_tx: str | None
    settled_usdc: float
    steps: tuple[SettlementStep, ...]
    settled_at: float
    window_closes_at: float

    def step(self, step_index: int) -> SettlementStep | None:
        """The settled step at `step_index`, or None if this job has no such step."""
        return next((s for s in self.steps if s.step_index == step_index), None)


@dataclass(frozen=True)
class DisputeRecord:
    """One buyer's dispute of one settled step.

    `charged_usdc` is what that step cost and `creditable_usdc` what an upheld
    dispute would credit back under the policy in force when it was opened —
    both frozen at opening time so a later policy change cannot rewrite what the
    buyer was shown.
    """

    id: str
    job_id_hex: str
    task_id: str
    step_index: int
    agent_id: str
    payer: str
    reason: str
    status: DisputeStatus
    charged_usdc: float
    creditable_usdc: float
    opened_at: float
    resolved_at: float | None = None
    refund_tx: str | None = None
    rating_tx: str | None = None


def new_dispute_id() -> str:
    """A dispute id: unguessable, so `GET /api/disputes/{id}` needs no account."""
    return f"dsp_{secrets.token_hex(8)}"


class DuplicateDisputeError(Exception):
    """This step already has a dispute. Carries it, so the caller can return it.

    One dispute per `(job_id, step)` is a product rule, not a database detail:
    the second attempt is answered with the first dispute unchanged rather than
    an error the buyer cannot act on.
    """

    def __init__(self, existing: DisputeRecord) -> None:
        super().__init__(f"step {existing.step_index} of job {existing.job_id_hex} is already disputed")
        self.existing = existing


class DisputeStore(Protocol):
    """The seam between the dispute rules and wherever the records actually live.

    Every method is awaitable even in the in-memory implementation that needs
    none of it, so moving to Postgres is a configuration change rather than a
    rewrite of every call site.
    """

    async def record_settlement(self, record: SettlementRecord) -> None: ...

    async def get_settlement(self, job_id_hex: str) -> SettlementRecord | None: ...

    async def get_settlement_by_task(self, task_id: str) -> SettlementRecord | None: ...

    async def open_dispute(self, record: DisputeRecord) -> DisputeRecord: ...

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None: ...

    async def find_dispute(self, job_id_hex: str, step_index: int) -> DisputeRecord | None: ...

    async def list_disputes_for_task(self, task_id: str) -> tuple[DisputeRecord, ...]: ...

    async def append_status(
        self,
        dispute_id: str,
        status: DisputeStatus,
        *,
        refund_tx: str | None = None,
        rating_tx: str | None = None,
        resolved_at: float | None = None,
    ) -> DisputeRecord: ...

    async def close(self) -> None: ...


class InMemoryDisputeStore:
    """The fallback when DATABASE_URL is unset: local dev and the test suite.

    Bounded and insertion-ordered. It is NOT durable, and says so at the one
    moment that matters — when a record it was given is dropped — because a
    dispute that silently evaporates is worse than a feature that was never
    offered.
    """

    def __init__(self) -> None:
        self._settlements: OrderedDict[str, SettlementRecord] = OrderedDict()
        self._disputes: OrderedDict[str, DisputeRecord] = OrderedDict()

    async def record_settlement(self, record: SettlementRecord) -> None:
        self._settlements[record.job_id_hex] = record
        self._settlements.move_to_end(record.job_id_hex)
        while len(self._settlements) > _MAX_IN_MEMORY:
            dropped, _ = self._settlements.popitem(last=False)
            logger.warning(
                "in-memory dispute store full (%d): dropped settlement %s — its window can no longer be honoured;"
                " set DATABASE_URL to persist settlements",
                _MAX_IN_MEMORY,
                dropped,
            )

    async def get_settlement(self, job_id_hex: str) -> SettlementRecord | None:
        return self._settlements.get(job_id_hex)

    async def get_settlement_by_task(self, task_id: str) -> SettlementRecord | None:
        return next(
            (r for r in reversed(self._settlements.values()) if r.task_id == task_id),
            None,
        )

    async def open_dispute(self, record: DisputeRecord) -> DisputeRecord:
        existing = await self.find_dispute(record.job_id_hex, record.step_index)
        if existing is not None:
            raise DuplicateDisputeError(existing)
        self._disputes[record.id] = record
        while len(self._disputes) > _MAX_IN_MEMORY:
            dropped, _ = self._disputes.popitem(last=False)
            logger.warning(
                "in-memory dispute store full (%d): dropped dispute %s — set DATABASE_URL to persist disputes",
                _MAX_IN_MEMORY,
                dropped,
            )
        return record

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        return self._disputes.get(dispute_id)

    async def find_dispute(self, job_id_hex: str, step_index: int) -> DisputeRecord | None:
        return next(
            (d for d in self._disputes.values() if d.job_id_hex == job_id_hex and d.step_index == step_index),
            None,
        )

    async def list_disputes_for_task(self, task_id: str) -> tuple[DisputeRecord, ...]:
        return tuple(d for d in self._disputes.values() if d.task_id == task_id)

    async def append_status(
        self,
        dispute_id: str,
        status: DisputeStatus,
        *,
        refund_tx: str | None = None,
        rating_tx: str | None = None,
        resolved_at: float | None = None,
    ) -> DisputeRecord:
        current = self._disputes.get(dispute_id)
        if current is None:
            raise KeyError(dispute_id)
        updated = replace(
            current,
            status=status,
            refund_tx=refund_tx if refund_tx is not None else current.refund_tx,
            rating_tx=rating_tx if rating_tx is not None else current.rating_tx,
            resolved_at=resolved_at if resolved_at is not None else (current.resolved_at or time.time()),
        )
        self._disputes[dispute_id] = updated
        return updated

    async def close(self) -> None:
        """Nothing to release — kept so the seam is one shape, not two."""
        return None


def steps_to_json(steps: tuple[SettlementStep, ...]) -> str:
    """The step breakdown as the one JSON column Postgres stores it in.

    A child table would need a join and a transaction for a value that is only
    ever read whole, with the settlement it belongs to.
    """
    return json.dumps(
        [
            {
                "step_index": s.step_index,
                "agent_id": s.agent_id,
                "agent_name": s.agent_name,
                "price_usdc": s.price_usdc,
                "delivered": s.delivered,
            }
            for s in steps
        ],
        separators=(",", ":"),
    )


def steps_from_json(raw: str) -> tuple[SettlementStep, ...]:
    """Inverse of `steps_to_json`, tolerant of a row written by an older build."""
    return tuple(
        SettlementStep(
            step_index=int(s["step_index"]),
            agent_id=str(s["agent_id"]),
            agent_name=s.get("agent_name"),
            price_usdc=float(s["price_usdc"]),
            delivered=bool(s["delivered"]),
        )
        for s in json.loads(raw)
    )


class PostgresDisputeStore:
    """The durable half of story 4.02: a dispute window that outlives the process.

    Deliberately thin — asyncpg, a handful of SQL constants, no ORM and no
    migration framework — because there are two tables, and a dependency that
    has to be understood before a deploy can be debugged is worse than the SQL
    it replaces.

    The pool and the schema are both created LAZILY, on the first call that
    needs them, so constructing the store never does I/O: importing this module,
    resolving the singleton and booting the app all stay offline, and a database
    that is briefly unreachable at boot costs a failed request rather than a
    failed deploy.

    `pool` is injectable for exactly one reason, stated rather than disguised:
    the test suite is hermetic and has no database, so it passes a fake pool and
    asserts the SQL this class actually sends.
    """

    def __init__(self, dsn: str, *, pool: Any | None = None) -> None:
        self._dsn = dsn
        self._pool: Any | None = pool
        # Whether the DDL has been run against THIS pool. Separate from the pool
        # itself so close() can reset both and a later call rebuilds them.
        self._ready = False
        # Serializes first use: a burst of concurrent settlements on a cold
        # process must create one pool and run the DDL once, not one per call.
        self._lock = asyncio.Lock()

    async def _ready_pool(self) -> Any:
        if self._ready and self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await self._create_pool()
            if not self._ready:
                # Two statements rather than one string, so each table keeps its
                # own rationale above it. asyncpg runs argument-less queries
                # through the simple protocol, which is what lets one execute()
                # carry a table and its indexes together.
                await self._pool.execute(_CREATE_SETTLEMENTS_SQL)
                await self._pool.execute(_CREATE_DISPUTES_SQL)
                self._ready = True
        return self._pool

    async def _create_pool(self) -> Any:
        asyncpg = _import_asyncpg()
        return await asyncpg.create_pool(dsn=self._dsn, min_size=_POOL_MIN_SIZE, max_size=_POOL_MAX_SIZE)

    async def record_settlement(self, record: SettlementRecord) -> None:
        pool = await self._ready_pool()
        # Every value written is the record's own. In particular `settled_at`
        # and `window_closes_at` are NOT re-derived here: the window closes when
        # the buyer was told it closes, which is a fact about the moment the
        # workflow settled, not about the moment this row reached the database.
        await pool.execute(
            _INSERT_SETTLEMENT_SQL,
            record.task_id,
            record.payer,
            record.auth_id_hex,
            record.job_id_hex,
            record.charge_tx,
            record.proof_tx,
            record.settled_usdc,
            steps_to_json(record.steps),
            record.settled_at,
            record.window_closes_at,
        )

    async def get_settlement(self, job_id_hex: str) -> SettlementRecord | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_SETTLEMENT_BY_JOB_SQL, job_id_hex)
        return None if row is None else self._to_settlement(row)

    async def get_settlement_by_task(self, task_id: str) -> SettlementRecord | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_SETTLEMENT_BY_TASK_SQL, task_id)
        return None if row is None else self._to_settlement(row)

    @staticmethod
    def _to_settlement(row: Any) -> SettlementRecord:
        """Map one asyncpg Record back to the record that was stored.

        The floats are coerced explicitly because a DOUBLE PRECISION column can
        come back as a Decimal through a proxy or a rewritten query, and money
        that is sometimes a float and sometimes a Decimal is a subtraction that
        raises in the middle of a refund.
        """
        return SettlementRecord(
            task_id=row["task_id"],
            payer=row["payer"],
            auth_id_hex=row["auth_id_hex"],
            job_id_hex=row["job_id_hex"],
            charge_tx=row["charge_tx"],
            proof_tx=row["proof_tx"],
            settled_usdc=float(row["settled_usdc"]),
            steps=steps_from_json(row["steps"]),
            settled_at=float(row["settled_at"]),
            window_closes_at=float(row["window_closes_at"]),
        )

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_DISPUTE_SQL, dispute_id)
        return None if row is None else self._to_dispute(row)

    async def find_dispute(self, job_id_hex: str, step_index: int) -> DisputeRecord | None:
        pool = await self._ready_pool()
        row = await pool.fetchrow(_SELECT_DISPUTE_BY_STEP_SQL, job_id_hex, step_index)
        return None if row is None else self._to_dispute(row)

    async def list_disputes_for_task(self, task_id: str) -> tuple[DisputeRecord, ...]:
        pool = await self._ready_pool()
        rows = await pool.fetch(_SELECT_DISPUTES_FOR_TASK_SQL, task_id)
        return tuple(self._to_dispute(row) for row in rows)

    @staticmethod
    def _to_dispute(row: Any) -> DisputeRecord:
        """Map one event row back to the dispute it is the current state of.

        `resolved_at` stays None rather than becoming 0.0 when the column is
        NULL: an open dispute has not been resolved, and an epoch-zero timestamp
        would read as "resolved in 1970" to every caller that only checks
        whether the value is set.
        """
        return DisputeRecord(
            id=row["dispute_id"],
            job_id_hex=row["job_id_hex"],
            task_id=row["task_id"],
            step_index=int(row["step_index"]),
            agent_id=row["agent_id"],
            payer=row["payer"],
            reason=row["reason"],
            status=row["status"],
            charged_usdc=float(row["charged_usdc"]),
            creditable_usdc=float(row["creditable_usdc"]),
            opened_at=float(row["opened_at"]),
            resolved_at=None if row["resolved_at"] is None else float(row["resolved_at"]),
            refund_tx=row["refund_tx"],
            rating_tx=row["rating_tx"],
        )

    async def open_dispute(self, record: DisputeRecord) -> DisputeRecord:
        """Insert the dispute, or raise DuplicateDisputeError with the one that
        beat it to this step.

        The record is returned unchanged on success: nothing about it is
        assigned by the database, so there is no row to read back. The loser's
        branch costs one extra read and only ever runs on a genuine collision —
        a double click, a retried POST, two tabs — which is the moment worth
        spending a round trip on.
        """
        pool = await self._ready_pool()
        won = await pool.fetchrow(
            _INSERT_DISPUTE_SQL,
            record.id,
            record.job_id_hex,
            record.task_id,
            record.step_index,
            record.agent_id,
            record.payer,
            record.reason,
            record.status,
            record.charged_usdc,
            record.creditable_usdc,
            record.opened_at,
            record.resolved_at,
            record.refund_tx,
            record.rating_tx,
        )
        if won is not None:
            return record
        # The index refused the row, so this step already has a dispute. Read it
        # and hand it to the caller inside the error: the product rule is that
        # the second attempt is answered with the first dispute, not with a
        # failure the buyer cannot act on.
        existing = await self.find_dispute(record.job_id_hex, record.step_index)
        if existing is None:  # pragma: no cover — the conflicting row is committed by now
            raise RuntimeError(
                f"dispute insert for job {record.job_id_hex} step {record.step_index} conflicted "
                "with a row that cannot be read back"
            )
        raise DuplicateDisputeError(existing)

    async def append_status(
        self,
        dispute_id: str,
        status: DisputeStatus,
        *,
        refund_tx: str | None = None,
        rating_tx: str | None = None,
        resolved_at: float | None = None,
    ) -> DisputeRecord:
        """Append the transition and return the dispute as it now stands.

        Returning the updated record is what lets 4.03 and 4.04 credit or rate a
        dispute without reading it back, so the value they act on is the row
        that was written rather than a second read that a concurrent transition
        could have moved underneath them.

        KeyError for an unknown id, matching InMemoryDisputeStore: the INSERT
        selects from the dispute's own history, so no history means no row
        written and nothing returned. A dispute id that does not exist is a bug
        in the caller, not a state this store can be in.
        """
        pool = await self._ready_pool()
        # Our own clock, in epoch seconds, for the reason every other timestamp
        # here is: the record handed back must be the row that was stored, not a
        # value the database rendered in whatever timezone it happens to run in.
        # It is only used when neither the caller nor the record already has a
        # resolution time — see COALESCE in _APPEND_STATUS_SQL.
        now = time.time()
        row = await pool.fetchrow(_APPEND_STATUS_SQL, dispute_id, status, refund_tx, rating_tx, resolved_at, now)
        if row is None:
            raise KeyError(dispute_id)
        return self._to_dispute(row)

    async def close(self) -> None:
        # Cleared before the await so a close racing a request cannot hand out
        # the pool that is being torn down, and so a second close is a no-op.
        pool, self._pool = self._pool, None
        self._ready = False
        if pool is not None:
            await pool.close()


_store: DisputeStore | None = None


def get_dispute_store() -> DisputeStore:
    """The process's dispute store, built on first use.

    A module-level singleton rather than `@lru_cache` for the same reason
    `binding_store` uses one: tests reset it by assigning `_store = None`.
    """
    global _store
    if _store is None:
        _store = InMemoryDisputeStore()
        logger.info(
            "dispute store: in-memory (DATABASE_URL is unset) — settlements and disputes are LOST on restart;"
            " set DATABASE_URL to persist them"
        )
    return _store


async def close_dispute_store() -> None:
    """Close the store and clear the singleton, so the next call rebuilds it."""
    global _store
    if _store is not None:
        await _store.close()
        _store = None
