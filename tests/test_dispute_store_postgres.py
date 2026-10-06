"""The dispute store's SQL, run on a real Postgres where running it is the only proof.

Every test here requests `pg_dsn` (tests/conftest.py), so each one runs the
store's real statements — asyncpg, READ COMMITTED, the store's own pool —
against a real Postgres 16 in a schema of its own. They are the claims a fake
pool cannot make, because each is about what the DATABASE does with a
statement rather than what the statement says:

  - the row lock every append queues on, made deterministic: a second
    transaction holds the lock and the append must wait for it and then read
    what it wrote; and while an append runs, nobody else can take the lock
    until its write commits;
  - two claims racing one dispute, lined up deterministically, so that only
    the PRIMARY KEY can tell them apart;
  - a dozen verdicts racing one dispute, and ten opens racing one step;
  - a `dispute_events` table exactly as story 4.02 created it, migrated in
    place by the store's DDL on first use;
  - money through DOUBLE PRECISION and JSONB, bit for bit;
  - the newest row, by `id`, as the dispute's current state;
  - the facts a transition carries forward: the refund hash, the credited
    amount, and a rating confirmation that never goes back to unconfirmed.

The races that the ordinary store tests already run on both stores live with
them (tests/test_refund_claim.py, tests/test_dispute_store.py).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import asyncpg
import pytest
from pg_support import claims, events, execute, fetch, run, settlements, statuses
from test_dispute_store import JOB, TASK, a_dispute, a_settlement

from app.services import dispute_store
from app.services.dispute_store import (
    DisputeRecord,
    DuplicateDisputeError,
    PostgresDisputeStore,
    SettlementRecord,
    SettlementStep,
)


@pytest.fixture
def pg(pg_dsn: str) -> PostgresDisputeStore:
    return PostgresDisputeStore(pg_dsn)


async def _upheld(store: PostgresDisputeStore, **overrides: Any) -> DisputeRecord:
    opened = await store.open_dispute(a_dispute(**overrides))
    upheld = await store.append_status(opened.id, "upheld", expected_status="open")
    assert upheld is not None
    return upheld


# ── the row lock ──────────────────────────────────────────────────────────


async def _waiting_on_a_lock(dsn: str, task: asyncio.Task[Any]) -> bool:
    """Whether `task` came to wait on a row lock before it finished.

    Watched in pg_stat_activity rather than inferred from a sleep: the store's
    own sessions carry this test's `application_name`, so a backend of theirs
    reporting a Lock wait IS the append queueing. An append that never waits
    finishes instead, and the loop sees that first."""
    while not task.done():
        waiting = await fetch(
            dsn,
            "SELECT 1 FROM pg_stat_activity WHERE application_name = current_setting('application_name')"
            " AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()",
        )
        if waiting:
            return True
        await asyncio.sleep(0.01)
    return False


def test_an_append_waits_for_the_lock_another_transaction_holds_and_reads_after_it(
    pg: PostgresDisputeStore, pg_dsn: str
) -> None:
    """The lost update, made deterministic.

    Another transaction takes the dispute's lock and appends the credit — the
    refund hash and the amount — and has not committed. A rating append arriving
    now must QUEUE on that lock, and once it is let through must read the row
    the other transaction wrote: at READ COMMITTED the statement after the lock
    takes a fresh snapshot, and that is the only reason the credit is not lost.
    Without the lock the append runs straight through, copies forward a record
    that never knew about the credit, and the receipt shows a rating and no
    refund for a buyer who was paid."""

    async def go() -> tuple[bool, DisputeRecord | None]:
        upheld = await _upheld(pg)
        await pg.claim_refund(upheld.id)
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            # Written by hand, not with the store's constants, so the holder
            # stays a correct concurrent writer whatever those say.
            await holder.execute("SELECT 1 FROM dispute_events WHERE dispute_id = $1 AND opening FOR UPDATE", upheld.id)
            await holder.execute(
                """
                INSERT INTO dispute_events (
                    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
                    charged_usdc, creditable_usdc, opened_at, resolved_at, refund_tx, credited_usdc,
                    updated_at, opening
                )
                SELECT dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, 'credited',
                       charged_usdc, creditable_usdc, opened_at, resolved_at, 'tx_paid', 1.5, 1.0, FALSE
                FROM dispute_events WHERE dispute_id = $1 AND opening
                """,
                upheld.id,
            )
            ours = asyncio.create_task(
                pg.append_status(upheld.id, "credited", rating_tx="tx_rating", rating_confirmed=True)
            )
            waited = await _waiting_on_a_lock(pg_dsn, ours)
            await transaction.commit()
        finally:
            await holder.close()
        return waited, await ours

    waited, final = run(pg, go())

    assert waited, "the append ran through a lock another transaction held"
    assert final is not None
    assert (final.refund_tx, final.credited_usdc, final.rating_tx) == ("tx_paid", 1.5, "tx_rating")


def test_an_append_holds_the_lock_from_its_read_until_its_write_commits(
    pg: PostgresDisputeStore, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: the lock is held ACROSS the append, not merely taken.

    `append_status` locks, reads the clock, then writes. The clock read is the
    one moment between its two statements, so the test stops there and asks,
    from another session, for the same row lock with NOWAIT. It must be refused.
    If the lock statement took no lock, or ran outside the transaction and so
    released it the instant it returned, the probe gets the row — and so could a
    second appender, which would then copy forward the same stale `latest`."""
    probes: list[str] = []
    armed: list[str] = []

    async def probe(dispute_id: str) -> str:
        conn = await asyncpg.connect(pg_dsn)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT 1 FROM dispute_events WHERE dispute_id = $1 AND opening FOR UPDATE NOWAIT", dispute_id
                )
            return "free"
        except asyncpg.exceptions.LockNotAvailableError:
            return "held"
        finally:
            await conn.close()

    def probe_from_another_thread(dispute_id: str) -> str:
        # The store's loop is parked inside the clock read, so the probe runs
        # on a loop of its own.
        answer: list[str] = []
        worker = threading.Thread(target=lambda: answer.append(asyncio.run(probe(dispute_id))))
        worker.start()
        worker.join(timeout=30)
        return answer[0] if answer else "no answer"

    class Clock:
        @staticmethod
        def time() -> float:
            if armed:
                probes.append(probe_from_another_thread(armed.pop()))
            return time.time()

    async def go() -> DisputeRecord | None:
        upheld = await _upheld(pg)
        monkeypatch.setattr(dispute_store, "time", Clock)
        armed.append(upheld.id)
        return await pg.append_status(upheld.id, "rejected", note="probe", expected_status="upheld")

    written = run(pg, go())

    assert written is not None and written.status == "rejected"
    assert probes == ["held"]


@pytest.mark.parametrize("holder_ends", ["commit", "rollback"])
def test_a_claim_racing_a_claim_is_settled_by_the_primary_key(
    pg: PostgresDisputeStore, pg_dsn: str, holder_ends: str
) -> None:
    """Two claimants that both read `upheld`, made deterministic.

    Another transaction has claimed the dispute — its mutex row and its
    `crediting` row are written — and has not committed. Our claim starts now,
    so its snapshot still says `upheld`: the status cannot stop it, and only the
    PRIMARY KEY can. Its INSERT must wait on the other transaction's row; if
    that transaction commits, ours must come back with nothing and write
    nothing, and if it rolls back, ours must win. A twenty-way gather rarely
    lines two claims up this closely, because each statement is quick."""

    async def go() -> tuple[bool, DisputeRecord | None, list[str], dict[str, float]]:
        upheld = await _upheld(pg)
        holder = await asyncpg.connect(pg_dsn)
        try:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute("INSERT INTO refund_claims (dispute_id, claimed_at) VALUES ($1, 1.0)", upheld.id)
            await holder.execute(
                """
                INSERT INTO dispute_events (
                    dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, status,
                    charged_usdc, creditable_usdc, opened_at, updated_at, opening
                )
                SELECT dispute_id, job_id_hex, task_id, step_index, agent_id, payer, reason, 'crediting',
                       charged_usdc, creditable_usdc, opened_at, 1.0, FALSE
                FROM dispute_events WHERE dispute_id = $1 AND opening
                """,
                upheld.id,
            )
            ours = asyncio.create_task(pg.claim_refund(upheld.id))
            waited = await _waiting_on_a_lock(pg_dsn, ours)
            await (transaction.commit() if holder_ends == "commit" else transaction.rollback())
        finally:
            await holder.close()
        return waited, await ours, await statuses(pg_dsn, upheld.id), await claims(pg_dsn)

    waited, claimed, trail, held = run(pg, go())

    assert waited, "the claim did not wait on the other claimant's uncommitted mutex row"
    assert list(held) == ["dsp_0001"]
    if holder_ends == "commit":
        assert claimed is None
        assert trail == ["open", "upheld", "crediting"]
        assert held["dsp_0001"] == 1.0  # still the other claimant's row
    else:
        assert claimed is not None and claimed.status == "crediting"
        assert trail == ["open", "upheld", "crediting"]
        assert held["dsp_0001"] != 1.0


def test_a_dozen_concurrent_verdicts_leave_exactly_one(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    """Twelve adjudications of one `open` dispute, upholds and rejects mixed,
    every one of them computed from the same `open` read. Exactly one lands;
    the rest are refused by the precondition and write nothing."""

    async def go() -> tuple[list[DisputeRecord | None], list[str]]:
        opened = await pg.open_dispute(a_dispute())
        results = await asyncio.gather(
            *(
                pg.append_status(opened.id, "upheld", expected_status="open")
                if i % 2
                else pg.append_status(opened.id, "rejected", note="no", expected_status="open")
                for i in range(12)
            )
        )
        return list(results), await statuses(pg_dsn, opened.id)

    results, trail = run(pg, go())
    written = [record for record in results if record is not None]

    assert len(written) == 1
    assert trail == ["open", written[0].status]


# ── opening ───────────────────────────────────────────────────────────────


def test_ten_concurrent_opens_of_one_step_make_one_dispute(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    """A buyer's ten tabs, one step. One opening row; every other request is
    answered with the dispute that won, never with an error it cannot read."""

    async def go() -> tuple[list[Any], list[dict[str, Any]]]:
        results = await asyncio.gather(
            *(pg.open_dispute(a_dispute(id=f"dsp_{i:04d}", reason=f"try {i}")) for i in range(10)),
            return_exceptions=True,
        )
        return list(results), await events(pg_dsn)

    results, rows = run(pg, go())

    opened = [r for r in results if isinstance(r, DisputeRecord)]
    refused = [r for r in results if isinstance(r, DuplicateDisputeError)]
    assert len(opened) == 1
    assert len(refused) == 9
    assert all(r.existing == opened[0] for r in refused)
    assert [row["dispute_id"] for row in rows] == [opened[0].id]


# ── the table story 4.02 created ─────────────────────────────────────────

# Verbatim from 4bd31ac ("added the append-only dispute events table"), the
# commit that first shipped the table: no note, credited_usdc, updated_at or
# rating_confirmed, and no refund_claims table at all. A deployment that ran
# 4.02 has exactly this, and the store has no migration tool — its DDL is the
# migration.
_DISPUTES_AS_4_02_CREATED_THEM = """
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


def test_a_table_created_by_story_4_02_is_migrated_in_place(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    """A live 4.02 table, with a dispute already upheld in it, meets today's
    store. The first call must add the four later columns without touching a
    row, the old dispute must read with them as "not known", and it must then
    be claimable, creditable and rateable like any other."""

    async def seed() -> None:
        await execute(pg_dsn, _DISPUTES_AS_4_02_CREATED_THEM)
        for status, opening in (("open", True), ("upheld", False)):
            await execute(
                pg_dsn,
                "INSERT INTO dispute_events (dispute_id, job_id_hex, task_id, step_index, agent_id, payer,"
                " reason, status, charged_usdc, creditable_usdc, opened_at, resolved_at, opening)"
                " VALUES ('dsp_old', $1, $2, 0, 'agt_writer', 'GPAYER', 'from 4.02', $3, 1.5, 1.5, 100.0,"
                " $4, $5)",
                JOB,
                TASK,
                status,
                None if opening else 200.0,
                opening,
            )

    asyncio.run(seed())

    async def go() -> tuple[Any, ...]:
        old = await pg.get_dispute("dsp_old")
        claimed = await pg.claim_refund("dsp_old")
        credited = await pg.append_status("dsp_old", "credited", refund_tx="tx_refund", credited_usdc=1.5)
        rated = await pg.append_status("dsp_old", "credited", rating_tx="tx_rating", rating_confirmed=True)
        columns = await fetch(
            pg_dsn,
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'dispute_events'"
            " AND column_name IN ('note', 'credited_usdc', 'updated_at', 'rating_confirmed')",
        )
        return old, claimed, credited, rated, columns, await events(pg_dsn, "dsp_old")

    old, claimed, credited, rated, columns, rows = run(pg, go())

    # The later columns are there, typed as the record reads them.
    assert {c["column_name"]: c["data_type"] for c in columns} == {
        "note": "text",
        "credited_usdc": "double precision",
        "updated_at": "double precision",
        "rating_confirmed": "boolean",
    }
    # The rows 4.02 wrote read as "not known", never as a zero or a 1970.
    assert old is not None and old.status == "upheld" and old.reason == "from 4.02"
    assert (old.note, old.credited_usdc, old.updated_at, old.rating_confirmed) == (None, None, None, None)
    assert (rows[0]["updated_at"], rows[1]["updated_at"]) == (None, None)
    # And the dispute is payable through today's code.
    assert claimed is not None and claimed.status == "crediting" and claimed.resolved_at == 200.0
    assert credited is not None and credited.credited_usdc == 1.5 and credited.updated_at is not None
    assert rated is not None and (rated.refund_tx, rated.rating_tx, rated.rating_confirmed) == (
        "tx_refund",
        "tx_rating",
        True,
    )
    assert [row["status"] for row in rows] == ["open", "upheld", "crediting", "credited", "credited"]

    # A second boot runs the same DDL over the migrated table, and changes nothing.
    again = PostgresDisputeStore(pg_dsn)
    assert run(again, again.get_dispute("dsp_old")) == rated


# ── money through the column types ────────────────────────────────────────

# Values chosen to catch a lossy type anywhere on the way: a sum with no exact
# binary form, one stroop, a product just off it, the ledger's full seven
# places, and an amount whose times-1e7 lands just below an integer.
AMOUNTS = (0.1 + 0.2, 1e-7, 0.0000001 * 3, 123.4567891, 0.05, 2.1e-6)


def test_money_round_trips_through_double_precision_and_jsonb_exactly(pg: PostgresDisputeStore) -> None:
    """A credit is bounded by the step price and by what settled, and paid to
    the stroop, so every amount the store holds has to come back as the float
    that went in — through DOUBLE PRECISION columns and through the JSONB step
    breakdown alike. NUMERIC or REAL here, or a JSON encoder that rounded,
    would move a buyer's credit by a stroop in either direction."""

    async def go() -> tuple[list[tuple[float, float, float | None]], SettlementRecord | None]:
        steps = tuple(
            SettlementStep(step_index=i, agent_id="agt", agent_name=None, price_usdc=amount, delivered=True)
            for i, amount in enumerate(AMOUNTS)
        )
        await pg.record_settlement(a_settlement(settled_usdc=AMOUNTS[0], steps=steps))
        read: list[tuple[float, float, float | None]] = []
        for i, amount in enumerate(AMOUNTS):
            opened = await pg.open_dispute(
                a_dispute(id=f"dsp_{i:04d}", step_index=i, charged_usdc=amount, creditable_usdc=amount)
            )
            await pg.append_status(opened.id, "credited", refund_tx="tx", credited_usdc=amount)
            stored = await pg.get_dispute(opened.id)
            assert stored is not None
            read.append((stored.charged_usdc, stored.creditable_usdc, stored.credited_usdc))
        return read, await pg.get_settlement(JOB)

    read, settled = run(pg, go())

    assert read == [(amount, amount, amount) for amount in AMOUNTS]
    assert settled is not None and settled.settled_usdc == AMOUNTS[0]
    assert tuple(step.price_usdc for step in settled.steps) == AMOUNTS


# ── the newest row is the dispute ─────────────────────────────────────────


def test_every_read_answers_with_the_newest_row_by_id(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    """The current state is the row with the highest `id`, and nothing else.

    The trail goes open → upheld → crediting → upheld → crediting: a status that
    repeats, rows whose timestamps were stamped by one process's clock and so
    may tie, and a claim that has to read `upheld` — the fourth row, not the
    first — to go through at all. Every read the store makes must agree with
    the table's own `ORDER BY id DESC`."""

    async def go() -> tuple[Any, ...]:
        upheld = await _upheld(pg)
        await pg.claim_refund(upheld.id)
        await pg.release_refund_claim(upheld.id)
        reclaimed = await pg.claim_refund(upheld.id)
        newest = await fetch(pg_dsn, "SELECT status FROM dispute_events ORDER BY id DESC LIMIT 1")
        return (
            reclaimed,
            await pg.get_dispute(upheld.id),
            await pg.find_dispute(JOB, 0),
            await pg.list_disputes_for_task(TASK),
            newest[0]["status"],
            await statuses(pg_dsn, upheld.id),
        )

    reclaimed, by_id, by_step, listed, newest, trail = run(pg, go())

    assert trail == ["open", "upheld", "crediting", "upheld", "crediting"]
    assert newest == "crediting"
    assert reclaimed is not None and reclaimed.status == "crediting"
    assert by_id == by_step == reclaimed
    assert listed == (reclaimed,)


def test_settlements_are_read_newest_first_by_id_too(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    async def go() -> tuple[SettlementRecord | None, int]:
        for closes in (1_700_100_000.0, 1_700_300_000.0, 1_700_200_000.0):
            await pg.record_settlement(a_settlement(window_closes_at=closes))
        return await pg.get_settlement(JOB), len(await settlements(pg_dsn))

    latest, count = run(pg, go())

    # The last row WRITTEN, not the latest window: a settlement is a fact about
    # a moment, and the newest fact wins.
    assert count == 3
    assert latest is not None and latest.window_closes_at == 1_700_200_000.0


# ── what a transition carries forward ─────────────────────────────────────


def test_a_later_transition_carries_the_refund_hash_and_the_credit_forward(
    pg: PostgresDisputeStore, pg_dsn: str
) -> None:
    """The rating lands after the credit and names neither the refund hash nor
    the amount. Both must survive it on the new row — they are the buyer's proof
    of payment — and so must the rating through a note added after it."""

    async def go() -> tuple[Any, ...]:
        upheld = await _upheld(pg)
        await pg.claim_refund(upheld.id)
        await pg.append_status(upheld.id, "credited", refund_tx="tx_paid", credited_usdc=1.25)
        rated = await pg.append_status(upheld.id, "credited", rating_tx="tx_rating", rating_confirmed=True)
        noted = await pg.append_status(upheld.id, "credited", note="reconciled")
        return rated, noted, await events(pg_dsn, upheld.id)

    rated, noted, rows = run(pg, go())

    for record in (rated, noted):
        assert record is not None
        assert (record.refund_tx, record.credited_usdc, record.rating_tx) == ("tx_paid", 1.25, "tx_rating")
    assert noted is not None and noted.note == "reconciled"
    # On the rows themselves, not only on what the call returned.
    assert [row["refund_tx"] for row in rows] == [None, None, None, "tx_paid", "tx_paid", "tx_paid"]


def test_a_rating_confirmation_never_goes_back_to_unconfirmed(pg: PostgresDisputeStore) -> None:
    """NULL is "not said", FALSE is "submitted, not yet seen", TRUE is "the
    ledger vouched for it". A later FALSE — a retry that timed out — must not
    undo a TRUE, and a transition that says nothing must not turn NULL into
    FALSE."""

    async def go() -> list[bool | None]:
        upheld = await _upheld(pg)
        await pg.claim_refund(upheld.id)
        seen: list[bool | None] = []
        for kwargs in (
            {"refund_tx": "tx_paid"},
            {"rating_tx": "tx_r1", "rating_confirmed": False},
            {"rating_tx": "tx_r1", "rating_confirmed": True},
            {"rating_tx": "tx_r2", "rating_confirmed": False},
            {},
        ):
            record = await pg.append_status(upheld.id, "credited", **kwargs)
            assert record is not None
            seen.append(record.rating_confirmed)
        return seen

    assert run(pg, go()) == [None, False, True, True, True]


def test_every_row_is_dated_by_the_transition_that_wrote_it(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    """`updated_at` is the one column every transition moves — the claim and the
    release included — and it is never carried forward from the row before."""

    async def go() -> list[dict[str, Any]]:
        upheld = await _upheld(pg)
        await pg.claim_refund(upheld.id)
        await pg.release_refund_claim(upheld.id)
        await pg.claim_refund(upheld.id)
        await pg.append_status(upheld.id, "credited", refund_tx="tx_paid")
        return await events(pg_dsn, upheld.id)

    before = time.time()
    rows = run(pg, go())
    after = time.time()

    stamps = [row["updated_at"] for row in rows]
    # The opening row is dated by its own `opened_at`; every later one by the
    # moment it was appended, in order.
    assert stamps[0] == rows[0]["opened_at"]
    assert all(before <= stamp <= after for stamp in stamps[1:])
    assert stamps[1:] == sorted(stamps[1:])
    assert asyncio.run(claims(pg_dsn)) == {}


# ── concurrent first use ──────────────────────────────────────────────────


def test_concurrent_first_uses_create_the_schema_without_a_race(pg_dsn: str) -> None:
    """Several processes creating the schema at once — an old and a new instance
    across a deploy — must queue on the DDL lock, not fail on the catalog's
    unique index (app/services/pg_schema.py)."""

    async def first_uses() -> None:
        stores = [dispute_store.PostgresDisputeStore(pg_dsn) for _ in range(8)]
        try:
            await asyncio.gather(*(store._ready_pool() for store in stores))
        finally:
            for store in stores:
                await store.close()

    asyncio.run(first_uses())
