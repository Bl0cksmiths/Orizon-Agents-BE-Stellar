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

Durably, that is three tables. `workflow_settlements` holds one row per settled
workflow, the step breakdown in a single JSON column; `dispute_events` holds one
row per status transition, so a dispute's current state is its newest row and
its history is the audit trail a chargeback is answered with. One dispute per
(job_id_hex, step_index) is enforced by a partial UNIQUE INDEX rather than by a
read in Python, because two requests for the same step arrive at once and only
the database can settle which of them opened it.

`refund_claims` is the odd one out and the most important (story 4.03): one row
per dispute currently being paid, and the only table here that is not evidence.
It is a mutex, held across a transfer that moves platform money to a buyer and
cannot be undone, and it is written and dropped by the same statements that
move the dispute in and out of `crediting` so the lock and the status cannot
disagree. What is left in it is the queue a human reconciles.
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

from ..config import settings

logger = logging.getLogger(__name__)

# A dispute's lifecycle. `open` is all story 4.02 ever writes; 4.03 adjudicates
# (`upheld` or `rejected`) and pays the credit (`credited`), and 4.04 records
# the on-chain rating.
#
# `crediting` is not a state anybody adjudicates INTO — it is the refund claim
# itself, made durable. A payout has a window between "we decided to pay" and
# "we know whether the transfer landed", and on the far side of that window a
# timed-out submission may still settle. Parking the dispute in `crediting`
# for the duration is what stops a retry paying twice: the claim is the lock,
# and it outlives the process that took it.
DisputeStatus = Literal["open", "upheld", "crediting", "credited", "rejected"]

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
# migration tooling in this repo and three tables do not justify introducing any:
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
# `note` arrives with story 4.03's adjudication, and `dispute_events` already
# exists wherever 4.02 ran — so the column needs the ALTER as well as its place
# in the CREATE. CREATE TABLE IF NOT EXISTS does nothing whatever to a table
# that is already there, and the first INSERT naming a column the deployed
# table lacks would fail every dispute write on the service. ADD COLUMN IF NOT
# EXISTS keeps the whole block idempotent, which is the property this schema is
# maintained by in place of a migration tool.
#
# `credited_usdc`, `updated_at` and `rating_confirmed` arrive with story 4.06's
# receipt and take the same road for the same reason: the table they join has
# been live since 4.02. All three are NULLABLE, and that is the migration rather
# than laxity — ADD COLUMN gives every row already written a NULL, and NULL is
# exactly what DisputeRecord promises a reader for a row from before 4.06:
# "not known", never a zero credit or a 1970 timestamp.
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
    note            TEXT,
    credited_usdc   DOUBLE PRECISION,
    updated_at      DOUBLE PRECISION,
    rating_confirmed BOOLEAN,
    opening         BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE dispute_events ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE dispute_events ADD COLUMN IF NOT EXISTS credited_usdc DOUBLE PRECISION;
ALTER TABLE dispute_events ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION;
ALTER TABLE dispute_events ADD COLUMN IF NOT EXISTS rating_confirmed BOOLEAN;
CREATE UNIQUE INDEX IF NOT EXISTS dispute_events_one_per_step_idx
    ON dispute_events (job_id_hex, step_index) WHERE opening;
CREATE INDEX IF NOT EXISTS dispute_events_dispute_idx
    ON dispute_events (dispute_id, id DESC);
CREATE INDEX IF NOT EXISTS dispute_events_step_idx
    ON dispute_events (job_id_hex, step_index, id DESC);
CREATE INDEX IF NOT EXISTS dispute_events_task_idx
    ON dispute_events (task_id, dispute_id, id DESC);
"""

# The refund mutex (story 4.03). One row per dispute that is mid-payout, and
# the PRIMARY KEY is the whole mechanism: `INSERT ... ON CONFLICT DO NOTHING`
# is atomic in a single statement, so exactly one of any number of concurrent
# claimants inserts and the rest come back empty.
#
# A separate table rather than a partial unique index over `dispute_events`,
# for two reasons. That table is append-only, so a uniqueness rule scoped to
# "is crediting" would forbid the SECOND claim after a failed transfer was
# released — and a buyer who was not paid must stay payable. And an advisory
# lock would not work here either: this store issues one statement per call,
# and every CTE in a statement shares the snapshot taken before the lock could
# be acquired, so the lock would guard nothing.
#
# Rows are deleted on release and on completion, always by the same statement
# that writes the status the transition implies — so a row that outlives its
# payout is a dispute genuinely stuck mid-flight, which is exactly what an
# operator needs to find during reconciliation. `list_refund_claims` is that
# read, and it is why the claim time is stored.
#
# Where the two failure modes cannot both be closed, this table BLOCKS rather
# than forgets, and that is a decision rather than an accident. A claim that
# evaporates lets a buyer be paid twice out of the platform wallet, and nothing
# takes the second transfer back; a claim that outlives its payout only delays
# one, and the delay is visible in the queue. So a claim held over a dispute
# that is NOT `crediting` — which no path here can produce, but a hand-written
# row or a hand-edited status could — refuses every later claim, and cannot be
# released either, because dropping a claim that another payer may still be
# signing against is the double payment this table exists to prevent. The way
# out is deliberately the slow one: establish from the chain whether the buyer
# was paid, then record that decision with append_status, which drops the claim
# in the same statement.
_CREATE_REFUND_CLAIMS_SQL = """
CREATE TABLE IF NOT EXISTS refund_claims (
    dispute_id  TEXT PRIMARY KEY,
    claimed_at  DOUBLE PRECISION NOT NULL
);
"""

# The reconciliation queue, oldest claim first — every payout that started and
# has not finished, which on this path means every buyer who may be waiting on
# a transfer nobody is going to retry for them.
#
# `dispute_id` breaks a tie between two claims taken in the same clock tick, so
# two reads of an unchanged table cannot come back in different orders. No
# LIMIT: a queue with enough rows to need paging is an incident, and truncating
# it would hide exactly the row that made it one.
_SELECT_REFUND_CLAIMS_SQL = """
SELECT dispute_id, claimed_at
FROM refund_claims
ORDER BY claimed_at, dispute_id
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
       charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
       credited_usdc, updated_at, rating_confirmed
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
       charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
       credited_usdc, updated_at, rating_confirmed
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
       charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
       credited_usdc, updated_at, rating_confirmed
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
#
# `updated_at` ($17) is the opening moment itself — open_dispute passes
# `opened_at` — because opening IS the dispute's first change of state, and a
# receipt reading "last changed: never" beside "opened at 14:32" would be
# answering a question it has the answer to.
_INSERT_DISPUTE_SQL = """
INSERT INTO dispute_events (
    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
    charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
    credited_usdc, updated_at, rating_confirmed, opening
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, TRUE)
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
# transition that names only a refund_tx keeps the rating_tx and the
# adjudicator's note already recorded. The note is carried exactly that way and
# for the same reason: it is the explanation the buyer was given for a
# rejection, and a later transition that blanked it would leave their receipt
# saying "rejected" with no reason, which is the one thing 4.06 made a
# rejection unable to say.
# `resolved_at` falls through three values in order — the one the caller gave,
# the one already on the record, then $7, this process's clock — so the moment a
# dispute was first resolved is stamped once and never moved by a later event.
# The casts are explicit because an untyped NULL parameter inside COALESCE is
# ambiguous to the planner.
#
# Story 4.06's receipt adds three columns, and they split the same way.
# `credited_usdc` ($8) and `rating_confirmed` ($9) are facts a transition may
# or may not know, so they are COALESCEd like the hashes: the rating that lands
# after a credit names no amount, and must not blank the one the buyer was
# paid. COALESCE is also what lets a rating move from unconfirmed to confirmed.
# It returns its first NON-NULL argument, and FALSE is not NULL — so a caller
# naming TRUE replaces a recorded FALSE, while a caller naming nothing passes
# NULL and keeps it. The argument order is the whole of that: written the other
# way round, COALESCE(latest.rating_confirmed, $9) would make the first answer
# permanent, and a rating that timed out would read "unconfirmed" forever after
# the ledger vouched for it.
#
# `updated_at` is $7 outright and never COALESCEd, because it is the one column
# every transition exists to move. It is the same reading of the clock as the
# `resolved_at` fallback, so the transition that first resolves a dispute
# records the two as equal, and every later one moves only `updated_at`.
#
# `opening` is FALSE, and that is load-bearing rather than cosmetic: a
# transition row that claimed to be an opening would collide with its own
# dispute's opening row in the partial unique index, and every resolution in
# the system would fail.
#
# `finished` drops the refund mutex when this transition ends the dispute, in
# the same statement rather than in a second call after it. A dispute that has
# been credited or rejected is not mid-payout, and a claim row that outlived
# the credit it was taken for would leave `refund_claims` holding a lock over a
# dispute that is already paid — which costs nobody money but makes the
# reconciliation queue lie, and a queue that lists finished work is a queue
# operators learn to ignore. The rule lives in the SQL and not in an `if` above
# the call, so a future transition cannot forget it. It runs even when `latest`
# is empty, which is harmless: a dispute that does not exist holds no mutex.
_APPEND_STATUS_SQL = """
WITH latest AS (
    SELECT *
    FROM dispute_events
    WHERE dispute_id = $1
    ORDER BY id DESC
    LIMIT 1
),
finished AS (
    DELETE FROM refund_claims
    WHERE dispute_id = $1 AND $2 IN ('credited', 'rejected')
    RETURNING dispute_id
)
INSERT INTO dispute_events (
    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
    charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
    credited_usdc, updated_at, rating_confirmed, opening
)
SELECT latest.dispute_id, latest.job_id_hex, latest.task_id, latest.step_index,
       latest.agent_id, latest.payer, latest.reason, $2,
       latest.charged_usdc, latest.creditable_usdc, latest.opened_at,
       COALESCE($6::double precision, latest.resolved_at, $7::double precision),
       COALESCE($3::text, latest.refund_tx),
       COALESCE($4::text, latest.rating_tx),
       COALESCE($5::text, latest.note),
       COALESCE($8::double precision, latest.credited_usdc),
       $7::double precision,
       COALESCE($9::boolean, latest.rating_confirmed),
       FALSE
FROM latest
RETURNING dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
          charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
          credited_usdc, updated_at, rating_confirmed
"""


# Appending a transition row that changes ONLY the status: every other column
# is copied from `latest` verbatim. Both refund-mutex transitions are that
# shape and differ in a single clause — what gates the row — so they share one
# statement instead of spelling the eighteen-column list out twice more, where
# a column added later would go missing from one of them without failing
# anything.
#
# `updated_at` ($2) is the one exception, and it is not a fact about the
# dispute but about this row: the moment the status changed, which is what a
# claim and a release both do. A buyer whose refund has sat in `crediting`
# since this morning is owed that time on their receipt, and a row that
# carried the previous transition's forward would tell them it happened
# whenever the dispute was upheld. For a claim, $2 is the same reading of the
# clock that dates the mutex row, so the queue and the trail agree to the
# instant on when the payout began.
#
# `resolved_at` is carried forward rather than stamped, and that is the
# load-bearing difference from _APPEND_STATUS_SQL. Neither `crediting` nor the
# `upheld` a release restores is a RESOLUTION: a dispute mid-payout has not
# been resolved, and one handed back has been resolved even less. Stamping the
# claim would date the dispute from the moment a payout was ATTEMPTED — and
# since COALESCE keeps the first value forever, the row that finally credits
# the buyer would report that moment instead of its own.
#
# `refund_tx` is the one fact deliberately NOT carried: both rows written here
# are written when no refund has landed. A claim starts a payout that has no
# transaction yet, and a release happens only when nothing landed — a cap
# refusal, a definitive FAILED, a timed-out hash reconciled as never settled.
# Copying the last hash forward put a transaction that FAILED on the buyer's
# receipt as the refund "in flight" for the whole of the next attempt (found
# in story 4.06). The dead hash is not lost: the append-only trail still holds
# it on the row that recorded it.
_APPEND_UNRESOLVED_ROW = """
INSERT INTO dispute_events (
    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
    charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
    credited_usdc, updated_at, rating_confirmed, opening
)
SELECT latest.dispute_id, latest.job_id_hex, latest.task_id, latest.step_index,
       latest.agent_id, latest.payer, latest.reason, '{status}',
       latest.charged_usdc, latest.creditable_usdc, latest.opened_at,
       latest.resolved_at, NULL::text, latest.rating_tx, latest.note,
       latest.credited_usdc, $2::double precision, latest.rating_confirmed,
       FALSE
FROM latest {gate}
RETURNING dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
          charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, rating_tx, note,
          credited_usdc, updated_at, rating_confirmed
"""


# Take the mutex AND move the dispute to `crediting`, in ONE statement.
#
# Two statements cannot do this safely, and the version that tried is worth
# naming: insert the claim, read the status back, append `crediting`. The gap
# between the insert and the append is a window the process can die in — Render
# spins a free instance down whenever it idles — and what it leaves behind is a
# claim row over a dispute still reading `upheld`. Nothing can pay that buyer
# afterwards: the claim refuses every later claimant, and a release refuses
# because the dispute is not `crediting`. They are owed money that no code path
# can send them.
#
# One statement has no such window. A single statement is its own transaction,
# so either the claim row and the `crediting` row are both there or neither is,
# whatever happens to the process between them.
#
# `claim` is where concurrency is settled, and it is settled by the PRIMARY KEY
# and not by the status it reads. Both CTEs share one snapshot, taken before
# either ran, so two claimants racing each other BOTH see `upheld` — a status
# can rule a claim out, never arbitrate between two. The unique index is not
# snapshot-based: exactly one insert lands, the loser's ON CONFLICT DO NOTHING
# returns nothing, and the main INSERT selects through `claim`, so the loser
# writes no event row either.
_CLAIM_REFUND_CTES = """
WITH latest AS (
    SELECT *
    FROM dispute_events
    WHERE dispute_id = $1
    ORDER BY id DESC
    LIMIT 1
),
claim AS (
    INSERT INTO refund_claims (dispute_id, claimed_at)
    SELECT $1, $2 FROM latest WHERE latest.status = 'upheld'
    ON CONFLICT (dispute_id) DO NOTHING
    RETURNING dispute_id
)"""

_CLAIM_REFUND_SQL = _CLAIM_REFUND_CTES + _APPEND_UNRESOLVED_ROW.format(
    status="crediting",
    gate="JOIN claim ON claim.dispute_id = latest.dispute_id",
)

# Give the mutex back AND put the dispute back to `upheld`, in ONE statement,
# for the reason the claim is one: the two are the same fact recorded twice and
# must not be able to come apart. Dropping the mutex first would let another
# payer claim a dispute still reading `crediting`, which then refuses the
# credit a buyer is owed; writing the status first and dying before the DELETE
# would leave a claim row over an `upheld` dispute, which is the wedge
# _CLAIM_REFUND_SQL describes and which nothing can undo.
#
# The gate is the STATUS rather than the claim row, deliberately. A dispute
# that somehow reached `crediting` without a mutex row would be stuck forever
# if a release refused to act without one, and `append_status` is public enough
# that "somehow" is not hypothetical. Gating on the status makes this call
# REPAIR that state instead of preserving it: the dispute goes back to `upheld`
# where it can be claimed again, and the DELETE is a no-op.
_RELEASE_REFUND_CLAIM_CTES = """
WITH latest AS (
    SELECT *
    FROM dispute_events
    WHERE dispute_id = $1
    ORDER BY id DESC
    LIMIT 1
),
released AS (
    DELETE FROM refund_claims
    WHERE dispute_id = $1 AND EXISTS (SELECT 1 FROM latest WHERE latest.status = 'crediting')
    RETURNING dispute_id
)"""

_RELEASE_REFUND_CLAIM_SQL = _RELEASE_REFUND_CLAIM_CTES + _APPEND_UNRESOLVED_ROW.format(
    status="upheld",
    gate="WHERE latest.status = 'crediting'",
)


# Ceiling on `SettlementStep.output_summary`. The summary is untrusted — an
# external agent's own words — and the summarizer bounds only one of its two
# branches, so the store states the limit a writer must clean to rather than
# trusting whatever arrives. Long enough for the one line a buyer reads to
# recognise the step; short enough that a hostile endpoint cannot turn a
# settlement row into a dumping ground.
#
# It bounds the CONTENT, not the stored length: `sanitize_untrusted` cuts at
# this many characters and then appends its ` …[truncated]` marker, so a cut
# summary is stored a few characters longer — the same trade the dispute
# reason makes, and deliberately so, because a reader must be able to tell a
# summary that was cut from one that simply ended.
OUTPUT_SUMMARY_MAX_CHARS = 280


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
    # What the step produced, in the one line the trace already showed for it
    # (story 4.05). Kept HERE because the trace is not: it lives in memory, is
    # evicted and is lost on restart, while a buyer has the whole window to
    # dispute — and "what did this step give me" is the evidence a dispute is
    # about. None for a step that delivered nothing, and for every settlement
    # recorded before this field existed.
    output_summary: str | None = None


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

    `note` is the adjudicator's answer TO THE BUYER: why their dispute was
    rejected, written for them and shown to them on their receipt (story 4.06)
    — so it is written in words the buyer can read, never in the platform's
    internal shorthand or about anybody else's dispute. Every rejection carries
    one, because `dispute_svc.reject` refuses to record a rejection without it:
    a rejection with no explanation is worse than no dispute system. The
    buyer's side is durable from the moment they open it (`reason`, frozen
    there), and an upheld dispute leaves an amount and a transaction hash
    behind — a refusal that said nothing would make the outcome most likely to
    be contested the one with nothing to contest. None on a dispute that was
    never rejected. It is kept EXACTLY as given: bounding and sanitising
    untrusted text is the caller's job, and a store that edited evidence on its
    way in would be a worse store.
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
    note: str | None = None
    # What the refund ACTUALLY transferred, recorded when the dispute is
    # credited (story 4.06). Not `creditable_usdc`: that is the promise frozen
    # at opening, and the transfer is the minimum of it, the step price at the
    # fraction now in force, and what the charge moved (ADR 0008 D4) — so the
    # two can differ, and a receipt that printed the promise beside a Stellar
    # Expert link showing another amount would contradict its own evidence.
    # None until credited, and for every dispute credited before 4.06.
    credited_usdc: float | None = None
    # When this dispute last changed state, in epoch seconds on our own clock
    # (story 4.06). `resolved_at` is stamped once, at the first decision, so a
    # refund that timed out and was reconciled hours later has no other record
    # of when it was credited — and the buyer is owed the time of the step they
    # are actually looking at. None only for records read from before 4.06.
    updated_at: float | None = None
    # Whether `rating_tx` is known to have LANDED (story 4.06). The hash alone
    # cannot say: story 4.04 records it on a SUCCESS and also on a TIMEOUT, so
    # the evidence exists the moment it does — which means a receipt reading
    # the hash as "the agent was rated" could claim a consequence that never
    # happened. True once the ledger has vouched for it (a SUCCESS, or a
    # replay confirming an earlier attempt); False while it is only in flight;
    # None when no rating was ever submitted, or for records from before 4.06.
    rating_confirmed: bool | None = None


@dataclass(frozen=True)
class RefundClaim:
    """One payout that started and has not finished.

    A claim is taken before anything is signed and dropped when the dispute is
    credited or rejected, so a claim that is still held is a refund that began
    and did not end: a transfer that timed out and by D3 is never retried
    automatically, a process that died mid-payout, a buyer still waiting.

    `claimed_at` is how long they have been waiting, which is the number that
    decides whether this one needs a human now.
    """

    dispute_id: str
    claimed_at: float


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
        note: str | None = None,
        resolved_at: float | None = None,
        credited_usdc: float | None = None,
        rating_confirmed: bool | None = None,
    ) -> DisputeRecord: ...

    async def claim_refund(self, dispute_id: str) -> DisputeRecord | None: ...

    async def release_refund_claim(self, dispute_id: str) -> DisputeRecord | None: ...

    async def list_refund_claims(self) -> tuple[RefundClaim, ...]: ...

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
        # `refund_claims`, modelled rather than inferred from the status. The
        # status alone would be enough to make this store behave correctly, and
        # that is the trap: the two implementations would then differ in what
        # they HOLD, and a case that only one of them gets right is a case the
        # hermetic suite cannot find. Insertion order is claim order, which is
        # the order a reconciliation queue is read in.
        self._refund_claims: dict[str, float] = {}

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
        # Opening is the dispute's first change of state, so it is stamped with
        # the moment it was opened — the rule _INSERT_DISPUTE_SQL writes.
        record = replace(record, updated_at=record.opened_at)
        self._disputes[record.id] = record
        while len(self._disputes) > _MAX_IN_MEMORY:
            dropped, _ = self._disputes.popitem(last=False)
            # Its mutex goes with it: a claim over a dispute that no longer
            # exists would sit in the queue as a payout nobody can look up.
            self._refund_claims.pop(dropped, None)
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
        note: str | None = None,
        resolved_at: float | None = None,
        credited_usdc: float | None = None,
        rating_confirmed: bool | None = None,
    ) -> DisputeRecord:
        current = self._disputes.get(dispute_id)
        if current is None:
            raise KeyError(dispute_id)
        # One reading of the clock for both timestamps, as _APPEND_STATUS_SQL
        # reads $7 once: the transition that first resolves a dispute must
        # record the same moment as its resolution and as its last change.
        now = time.time()
        # `is not None` throughout, never truthiness, and for rating_confirmed
        # it is the rule rather than style: False is an answer to record, and
        # only None means "this transition does not say".
        updated = replace(
            current,
            status=status,
            refund_tx=refund_tx if refund_tx is not None else current.refund_tx,
            rating_tx=rating_tx if rating_tx is not None else current.rating_tx,
            note=note if note is not None else current.note,
            resolved_at=resolved_at if resolved_at is not None else (current.resolved_at or now),
            credited_usdc=credited_usdc if credited_usdc is not None else current.credited_usdc,
            updated_at=now,
            rating_confirmed=rating_confirmed if rating_confirmed is not None else current.rating_confirmed,
        )
        self._disputes[dispute_id] = updated
        if status in ("credited", "rejected"):
            # A dispute that has finished is not mid-payout. Postgres drops the
            # mutex inside the statement that writes this row; here there is no
            # statement to be inside, but the rule is the same one.
            self._refund_claims.pop(dispute_id, None)
        return updated

    async def claim_refund(self, dispute_id: str) -> DisputeRecord | None:
        """Take the exclusive right to pay this dispute, or return None.

        The claim is the whole of story 4.03's idempotency, so it is a
        CONDITIONAL transition and never a read followed by a write: only a
        dispute sitting in `upheld` and held by nobody can be claimed, and
        claiming moves it to `crediting` in the same step. A second caller — a
        retry, a double click, a duplicate webhook — gets None, which is the
        signal to return the existing record rather than pay again.

        Atomic for free, where Postgres buys the same guarantee with a PRIMARY
        KEY: there is no await between reading the status and writing it, so
        no second caller can be running in between.

        None is deliberately not an error and does not say why: already
        claimed, already credited, still open and never adjudicated, or
        rejected all mean the same thing to a payer, which is *do not sign
        anything*. The caller reads the record back if it needs to explain.
        """
        current = self._disputes.get(dispute_id)
        if current is None or current.status != "upheld" or dispute_id in self._refund_claims:
            return None
        # One reading of the clock dates both the claim and the transition, as
        # _CLAIM_REFUND_SQL's $2 does.
        now = time.time()
        self._refund_claims[dispute_id] = now
        # No refund hash on a claim: this payout has no transaction yet, and the
        # Postgres statement clears it the same way (see _APPEND_UNRESOLVED_ROW).
        claimed = replace(current, status="crediting", updated_at=now, refund_tx=None)
        self._disputes[dispute_id] = claimed
        return claimed

    async def release_refund_claim(self, dispute_id: str) -> DisputeRecord | None:
        """Hand the claim back, so an unpaid dispute can be paid later.

        Released ONLY when the caller knows with certainty that nothing was
        signed, or that what was signed definitively failed on-chain — a cap
        refusal, a rejected submission, a transfer that came back FAILED. In
        those cases the buyer is still owed, and leaving the dispute stuck in
        `crediting` would make a retry impossible.

        A submission that TIMED OUT is the case this must not be used for: the
        transaction may still settle, so the claim stays held and the dispute
        stays in `crediting` until a human reconciles it. Paying that buyer
        twice is a worse failure than paying them late.
        """
        current = self._disputes.get(dispute_id)
        if current is None or current.status != "crediting":
            return None
        # Gated on the STATUS and never on the claim, so a dispute somehow left
        # in `crediting` without one is repaired rather than stranded — the
        # rule _RELEASE_REFUND_CLAIM_SQL follows, and for the same reason.
        self._refund_claims.pop(dispute_id, None)
        released = replace(current, status="upheld", updated_at=time.time())
        self._disputes[dispute_id] = released
        return released

    async def list_refund_claims(self) -> tuple[RefundClaim, ...]:
        """Every payout still in flight, oldest first.

        Insertion order is claim order, so the dict needs no sorting to read
        the way the Postgres queue does.
        """
        return tuple(
            RefundClaim(dispute_id=dispute_id, claimed_at=at) for dispute_id, at in self._refund_claims.items()
        )

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
                "output_summary": s.output_summary,
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
            # `.get`, not `[...]`: every row written before story 4.05 lacks the
            # key, and those settlements are still inside their windows.
            output_summary=s.get("output_summary"),
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
                await self._pool.execute(_CREATE_REFUND_CLAIMS_SQL)
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
        whether the value is set. The receipt's three columns follow the same
        rule for the reason they are nullable at all — a row written before 4.06
        says "not known", and a receipt that turned that into a credit of 0.0 or
        an unconfirmed rating would state a fact nobody recorded.
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
            note=row["note"],
            credited_usdc=None if row["credited_usdc"] is None else float(row["credited_usdc"]),
            updated_at=None if row["updated_at"] is None else float(row["updated_at"]),
            rating_confirmed=None if row["rating_confirmed"] is None else bool(row["rating_confirmed"]),
        )

    async def open_dispute(self, record: DisputeRecord) -> DisputeRecord:
        """Insert the dispute, or raise DuplicateDisputeError with the one that
        beat it to this step.

        The record is returned as written, which is the record given with
        `updated_at` stamped from its own `opened_at`: nothing about it is
        assigned by the database, so there is still no row to read back. The
        loser's branch costs one extra read and only ever runs on a genuine
        collision — a double click, a retried POST, two tabs — which is the
        moment worth spending a round trip on.
        """
        pool = await self._ready_pool()
        record = replace(record, updated_at=record.opened_at)
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
            record.note,
            record.credited_usdc,
            record.updated_at,
            record.rating_confirmed,
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
        note: str | None = None,
        resolved_at: float | None = None,
        credited_usdc: float | None = None,
        rating_confirmed: bool | None = None,
    ) -> DisputeRecord:
        """Append the transition and return the dispute as it now stands.

        Returning the updated record is what lets 4.03 and 4.04 credit or rate a
        dispute without reading it back, so the value they act on is the row
        that was written rather than a second read that a concurrent transition
        could have moved underneath them.

        Every keyword is "leave it as recorded" at None, and only at None: the
        receipt's `credited_usdc` and `rating_confirmed` are carried forward
        exactly as the hashes are, and a `rating_confirmed=False` is written as
        the answer it is. `updated_at` is not a keyword at all — every row this
        appends is stamped with the moment it was appended.

        KeyError for an unknown id, matching InMemoryDisputeStore: the INSERT
        selects from the dispute's own history, so no history means no row
        written and nothing returned. A dispute id that does not exist is a bug
        in the caller, not a state this store can be in.
        """
        pool = await self._ready_pool()
        # Our own clock, in epoch seconds, for the reason every other timestamp
        # here is: the record handed back must be the row that was stored, not a
        # value the database rendered in whatever timezone it happens to run in.
        # It is always the row's `updated_at`, and its `resolved_at` only when
        # neither the caller nor the record already has a resolution time — see
        # COALESCE in _APPEND_STATUS_SQL.
        now = time.time()
        # The statement also drops the refund mutex when `status` finishes the
        # dispute, so what remains in `refund_claims` is exactly the set of
        # payouts still in flight rather than a pile of spent locks.
        row = await pool.fetchrow(
            _APPEND_STATUS_SQL,
            dispute_id,
            status,
            refund_tx,
            rating_tx,
            note,
            resolved_at,
            now,
            credited_usdc,
            rating_confirmed,
        )
        if row is None:
            raise KeyError(dispute_id)
        return self._to_dispute(row)

    async def claim_refund(self, dispute_id: str) -> DisputeRecord | None:
        """Take the exclusive right to pay this dispute, or return None.

        One statement does all of it — win the mutex, check the dispute is
        still `upheld`, move it to `crediting` — so a process that dies
        mid-call leaves a dispute that is either fully claimed or untouched,
        never a claim row stranded over a dispute nobody can pay.

        None is deliberately not an error and does not say why: already
        claimed, already credited, still open and never adjudicated, or
        rejected all mean the same thing to a payer, which is *do not sign
        anything*. The caller reads the record back if it needs to explain.
        """
        pool = await self._ready_pool()
        row = await pool.fetchrow(_CLAIM_REFUND_SQL, dispute_id, time.time())
        return None if row is None else self._to_dispute(row)

    async def release_refund_claim(self, dispute_id: str) -> DisputeRecord | None:
        """Put a still-unpaid dispute back where another attempt can find it.

        One statement drops the mutex and restores `upheld` together, so no
        ordering of the two can strand a dispute: there is no moment at which
        the claim is gone while the status still says a payout is in flight,
        and none at which the status is back while the claim still blocks it.

        Released ONLY when the caller knows with certainty that nothing was
        signed, or that what was signed definitively FAILED on-chain — a cap
        refusal, a rejected submission, a transfer that came back FAILED. In
        those cases the buyer is still owed, and leaving the dispute stuck in
        `crediting` would make a retry impossible.

        A submission that TIMED OUT is the case this must not be used for: the
        transaction may still settle, so the claim stays held and the dispute
        stays in `crediting` until a human reconciles it. Paying that buyer
        twice is a worse failure than paying them late.
        """
        pool = await self._ready_pool()
        # Our own clock for the row's `updated_at`, never the database's.
        row = await pool.fetchrow(_RELEASE_REFUND_CLAIM_SQL, dispute_id, time.time())
        return None if row is None else self._to_dispute(row)

    async def list_refund_claims(self) -> tuple[RefundClaim, ...]:
        """Every payout still in flight, oldest first.

        The mutex doubles as the reconciliation queue, and this is the read
        that makes that true rather than aspirational. D3 forbids retrying a
        timed-out transfer, so the ONLY way a buyer whose refund hung gets
        paid is a human finding them — and a lock nobody can list is a buyer
        nobody can find.
        """
        pool = await self._ready_pool()
        rows = await pool.fetch(_SELECT_REFUND_CLAIMS_SQL)
        return tuple(RefundClaim(dispute_id=row["dispute_id"], claimed_at=float(row["claimed_at"])) for row in rows)

    async def close(self) -> None:
        # Cleared before the await so a close racing a request cannot hand out
        # the pool that is being torn down, and so a second close is a no-op.
        pool, self._pool = self._pool, None
        self._ready = False
        if pool is not None:
            await pool.close()


_store: DisputeStore | None = None


def get_dispute_store() -> DisputeStore:
    """The process's dispute store, built on first use from `database_url`.

    Resolved lazily — at the first call, not at import — so what is read is the
    configuration the process is actually running with, and picking Postgres
    dials nothing until something needs it.

    A module-level singleton rather than `@lru_cache`, for binding_store's
    reason: a cached resolver would pin whichever store the FIRST import
    happened to resolve, so a DATABASE_URL that arrived later would be silently
    ignored and the service would keep writing dispute windows to memory while
    reporting success — precisely the failure this seam exists to prevent.
    Tests reset it by assigning `_store = None`.
    """
    global _store
    if _store is None:
        dsn = settings.database_url.strip()
        if dsn:
            _store = PostgresDisputeStore(dsn)
            # The DSN carries the database password: report the choice, never
            # the value.
            logger.info("dispute store: postgres (DATABASE_URL is set) — settlements and disputes survive a restart")
        else:
            _store = InMemoryDisputeStore()
            # The in-memory path cannot honour a window that outlives the
            # process, so a deployment running it has to be able to find that
            # out from its own startup log rather than from a lost dispute.
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
