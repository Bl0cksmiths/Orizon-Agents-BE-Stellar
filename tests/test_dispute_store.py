"""Story 4.02 — where a settlement and its disputes live (ADR 0002).

Four things are asserted here, and they are the four that can silently rot:

  1. The duplicate rule — one dispute per (job_id_hex, step_index) — holds even
     when two requests arrive at once, because in Postgres it is a UNIQUE INDEX
     rather than a read in Python. The concurrent case has its own test.
  2. The dispute table is APPEND-ONLY. A status transition INSERTs another row;
     nothing UPDATEs and nothing DELETEs, so the trail a chargeback is answered
     with cannot be rewritten by a later event.
  3. The reads are the ones the dispute rules actually make — by job, by task,
     by (job, step), by dispute id — and each answers with a dispute's CURRENT
     state rather than with some earlier row of its history.
  4. The store is chosen from `database_url` at first USE, not at import, so a
     DATABASE_URL that arrives later is honoured instead of ignored.

The suite is hermetic: no database, no network, no asyncpg. Postgres is
exercised against a fake pool injected into the store — one that dispatches on
this module's SQL constants BY EQUALITY — and the async calls run through
`asyncio.run`, the repo's idiom, since pytest-asyncio is not installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any

import pytest

from app.services import dispute_store
from app.services.dispute_store import (
    DisputeRecord,
    DuplicateDisputeError,
    InMemoryDisputeStore,
    SettlementRecord,
    SettlementStep,
)

JOB = "ab" * 32
OTHER_JOB = "cd" * 32
TASK = "task_alpha"
PAYER = "G" + "B" * 55
AGENT = "agt_writer"

STORE_LOGGER = "app.services.dispute_store"

STEPS = (
    SettlementStep(step_index=0, agent_id=AGENT, agent_name="Writer", price_usdc=1.5, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_charts", agent_name=None, price_usdc=2.25, delivered=False),
)


def a_settlement(**overrides: Any) -> SettlementRecord:
    """A settled workflow, as `_settle_onchain` would record one."""
    base = SettlementRecord(
        task_id=TASK,
        payer=PAYER,
        auth_id_hex="a1" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=3.75,
        steps=STEPS,
        settled_at=1_700_000_000.0,
        window_closes_at=1_700_086_400.0,
    )
    return dataclasses.replace(base, **overrides)


def a_dispute(**overrides: Any) -> DisputeRecord:
    """A dispute of step 0 of that workflow, as the endpoint would open one."""
    base = DisputeRecord(
        id="dsp_0001",
        job_id_hex=JOB,
        task_id=TASK,
        step_index=0,
        agent_id=AGENT,
        payer=PAYER,
        reason="the summary was empty",
        status="open",
        charged_usdc=1.5,
        creditable_usdc=1.5,
        opened_at=1_700_000_100.0,
    )
    return dataclasses.replace(base, **overrides)


# ── settlements, in memory ────────────────────────────────────────────────


def test_a_settlement_is_read_back_by_job_and_by_task() -> None:
    """Both reads exist because both callers do: the dispute endpoint has a job
    id from the buyer's receipt, the task view has only its own task id."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[SettlementRecord | None, SettlementRecord | None]:
        await store.record_settlement(a_settlement())
        return await store.get_settlement(JOB), await store.get_settlement_by_task(TASK)

    by_job, by_task = asyncio.run(go())

    assert by_job is not None and by_task is not None
    assert by_job == by_task
    assert by_job.window_closes_at == 1_700_086_400.0
    assert by_job.settled_usdc == 3.75


def test_an_unsettled_job_or_task_has_no_settlement() -> None:
    """None, not an exception: a workflow that was never paid for simply has no
    dispute window, and that is an answer rather than a failure."""
    store = InMemoryDisputeStore()

    assert asyncio.run(store.get_settlement(OTHER_JOB)) is None
    assert asyncio.run(store.get_settlement_by_task("task_never")) is None


def test_the_newest_settlement_wins_when_a_task_settles_twice() -> None:
    """A retried charge or a replayed callback must not leave the buyer with the
    window from the attempt that did not stick."""
    store = InMemoryDisputeStore()

    async def go() -> SettlementRecord | None:
        await store.record_settlement(a_settlement())
        await store.record_settlement(a_settlement(job_id_hex=OTHER_JOB, window_closes_at=1_700_172_800.0))
        return await store.get_settlement_by_task(TASK)

    latest = asyncio.run(go())

    assert latest is not None
    assert latest.job_id_hex == OTHER_JOB
    assert latest.window_closes_at == 1_700_172_800.0


def test_the_step_breakdown_is_addressable_by_step_index() -> None:
    """`step(i)` is what turns "dispute step 1" into the price a credit is
    computed from, and what refuses a step that never delivered."""
    store = InMemoryDisputeStore()

    async def go() -> SettlementRecord | None:
        await store.record_settlement(a_settlement())
        return await store.get_settlement(JOB)

    settled = asyncio.run(go())

    assert settled is not None
    assert settled.step(0) is not None and settled.step(0).price_usdc == 1.5  # type: ignore[union-attr]
    assert settled.step(1) is not None and settled.step(1).delivered is False  # type: ignore[union-attr]
    assert settled.step(7) is None


# ── disputes, in memory ───────────────────────────────────────────────────


def test_an_opened_dispute_is_found_by_its_id_and_by_its_step() -> None:
    """Two lookups, two callers: `GET /api/disputes/{id}` has the id the buyer
    was given, and a second "dispute this step" request has only the step."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord, DisputeRecord | None, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        return opened, await store.get_dispute(opened.id), await store.find_dispute(JOB, 0)

    opened, by_id, by_step = asyncio.run(go())

    assert opened.status == "open"
    assert by_id == opened
    assert by_step == opened


def test_an_unknown_dispute_is_none_rather_than_an_error() -> None:
    store = InMemoryDisputeStore()

    assert asyncio.run(store.get_dispute("dsp_never")) is None
    assert asyncio.run(store.find_dispute(JOB, 3)) is None


def test_a_second_dispute_of_the_same_step_is_refused_with_the_first() -> None:
    """The product rule: the second attempt is answered WITH the first dispute,
    unchanged, so a double click shows the buyer what they already opened rather
    than an error they cannot act on."""
    store = InMemoryDisputeStore()

    async def go() -> DisputeRecord:
        return await store.open_dispute(a_dispute())

    first = asyncio.run(go())

    with pytest.raises(DuplicateDisputeError) as excinfo:
        asyncio.run(store.open_dispute(a_dispute(id="dsp_0002", reason="a second try")))

    assert excinfo.value.existing == first
    assert excinfo.value.existing.reason == "the summary was empty"
    # The loser left nothing behind: one dispute, and the id it was given never
    # became a record.
    assert asyncio.run(store.get_dispute("dsp_0002")) is None


def test_a_different_step_or_a_different_job_is_not_a_duplicate() -> None:
    """The identity is the PAIR. One bad step in a five-step workflow must not
    stop the buyer disputing another, and two workflows are never each other's
    duplicate however their steps line up."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord, DisputeRecord]:
        await store.open_dispute(a_dispute())
        other_step = await store.open_dispute(a_dispute(id="dsp_0002", step_index=1))
        other_job = await store.open_dispute(a_dispute(id="dsp_0003", job_id_hex=OTHER_JOB))
        return other_step, other_job

    other_step, other_job = asyncio.run(go())

    assert other_step.id == "dsp_0002"
    assert other_job.id == "dsp_0003"
    assert len(store._disputes) == 3


def test_every_dispute_of_one_task_is_listed_and_no_other_task_s_is() -> None:
    """What the task view reads. A dispute of another task appearing here would
    show one buyer another buyer's complaint."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord, ...]:
        await store.open_dispute(a_dispute())
        await store.open_dispute(a_dispute(id="dsp_0002", step_index=1))
        await store.open_dispute(a_dispute(id="dsp_0003", task_id="task_beta", job_id_hex=OTHER_JOB))
        return await store.list_disputes_for_task(TASK)

    listed = asyncio.run(go())

    assert [d.id for d in listed] == ["dsp_0001", "dsp_0002"]
    assert asyncio.run(store.list_disputes_for_task("task_never")) == ()


def test_a_dispute_id_is_prefixed_and_unguessable() -> None:
    """The id is the only credential `GET /api/disputes/{id}` has — it is handed
    to the buyer and to nobody else — so it is long random hex rather than a
    counter anyone could walk to read another buyer's complaint."""
    first, second = dispute_store.new_dispute_id(), dispute_store.new_dispute_id()

    assert first.startswith("dsp_")
    assert len(first) == len("dsp_") + 16
    int(first.removeprefix("dsp_"), 16)  # hex, or this raises
    assert first != second


# ── status transitions, in memory ─────────────────────────────────────────


def test_a_transition_moves_the_status_and_stamps_the_resolution() -> None:
    """What story 4.03 calls when it upholds a dispute. The updated record is
    RETURNED, so the caller acts on what was written rather than on a second
    read."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        assert opened.resolved_at is None
        updated = await store.append_status(opened.id, "upheld")
        return updated, await store.get_dispute(opened.id)

    before = time.time()
    updated, stored = asyncio.run(go())
    after = time.time()

    assert updated.status == "upheld"
    assert updated.resolved_at is not None and before <= updated.resolved_at <= after
    assert stored == updated
    # Everything the buyer was told at opening time is still exactly what it was.
    assert updated.reason == "the summary was empty"
    assert updated.creditable_usdc == 1.5
    assert updated.opened_at == 1_700_000_100.0


def test_a_later_transition_keeps_what_an_earlier_one_recorded() -> None:
    """4.03 records the refund transaction and 4.04 the rating one, minutes
    apart. The second must not erase the first, and it must not move the moment
    the dispute was resolved."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord, DisputeRecord]:
        opened = await store.open_dispute(a_dispute())
        credited = await store.append_status(opened.id, "credited", refund_tx="tx_refund")
        rated = await store.append_status(opened.id, "credited", rating_tx="tx_rating")
        return credited, rated

    credited, rated = asyncio.run(go())

    assert rated.refund_tx == "tx_refund"
    assert rated.rating_tx == "tx_rating"
    assert rated.resolved_at == credited.resolved_at


def test_a_transition_may_name_the_moment_it_resolved() -> None:
    """A caller that already has the on-chain timestamp passes it rather than
    letting the store date the row from when it happened to be written."""
    store = InMemoryDisputeStore()

    async def go() -> DisputeRecord:
        opened = await store.open_dispute(a_dispute())
        return await store.append_status(opened.id, "rejected", resolved_at=1_700_009_999.0)

    assert asyncio.run(go()).resolved_at == 1_700_009_999.0


def test_a_transition_on_an_unknown_dispute_is_a_key_error() -> None:
    """Not a silently created record: a dispute id that does not exist is a bug
    in the caller, and a store that invented one would hide it."""
    store = InMemoryDisputeStore()

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_never", "upheld"))


# ── the bound on the in-memory store ──────────────────────────────────────


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == STORE_LOGGER]


def test_a_dropped_settlement_is_announced_rather_than_lost_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The cap is what keeps a long-lived local process bounded, and dropping a
    settlement silently retires a dispute window a buyer was promised. It is a
    WARNING naming the record and the fix, the way ramp_store's eviction is."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 2)
    store = InMemoryDisputeStore()

    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):

        async def go() -> None:
            for n in range(3):
                await store.record_settlement(a_settlement(job_id_hex=f"{n:064x}"))

        asyncio.run(go())

    assert len(store._settlements) == 2
    assert asyncio.run(store.get_settlement(f"{0:064x}")) is None
    lines = _messages(caplog)
    assert any(f"{0:064x}" in m and "DATABASE_URL" in m for m in lines)


def test_a_dropped_dispute_is_announced_too(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A dispute that evaporates is worse than a feature that was never
    offered: the buyer believes a complaint is on file."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 2)
    store = InMemoryDisputeStore()

    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):

        async def go() -> None:
            for n in range(3):
                await store.open_dispute(a_dispute(id=f"dsp_{n}", step_index=n))

        asyncio.run(go())

    assert len(store._disputes) == 2
    assert asyncio.run(store.get_dispute("dsp_0")) is None
    assert any("dsp_0" in m and "DATABASE_URL" in m for m in _messages(caplog))


def test_closing_the_in_memory_store_is_safe_twice() -> None:
    """There is no pool to release here, but close() must mean the same thing
    on both sides of the Protocol rather than being callable on only one."""
    store = InMemoryDisputeStore()

    async def go() -> None:
        await store.record_settlement(a_settlement())
        await store.close()
        await store.close()

    asyncio.run(go())


# ── the fake pool ─────────────────────────────────────────────────────────

# The INSERT parameter order, named. `$1..$n` of _INSERT_SETTLEMENT_SQL and
# _INSERT_DISPUTE_SQL respectively: reordering either statement without
# reordering these makes the fake store the wrong values, which every read test
# below then fails on.
_SETTLEMENT_COLUMNS = (
    "task_id",
    "payer",
    "auth_id_hex",
    "job_id_hex",
    "charge_tx",
    "proof_tx",
    "settled_usdc",
    "steps",
    "settled_at",
    "window_closes_at",
)
_DISPUTE_COLUMNS = (
    "dispute_id",
    "job_id_hex",
    "task_id",
    "step_index",
    "agent_id",
    "payer",
    "reason",
    "status",
    "charged_usdc",
    "creditable_usdc",
    "opened_at",
    "resolved_at",
    "refund_tx",
    "rating_tx",
)


def _newest(rows: list[dict[str, Any]], **where: Any) -> dict[str, Any] | None:
    """`WHERE <where> ORDER BY id DESC LIMIT 1` — list order is `id` order."""
    matching = [r for r in rows if all(r[column] == value for column, value in where.items())]
    return matching[-1] if matching else None


def _coalesce(*values: Any) -> Any:
    """SQL COALESCE: the first value that is not NULL."""
    return next((v for v in values if v is not None), None)


class FakePool:
    """Stands in for an asyncpg pool: records every statement and models the two
    tables just well enough to answer the queries the store sends.

    Rows are kept in plain lists, and list order IS the BIGSERIAL `id` order the
    real queries sort by — so "the newest row" means the same thing here as it
    does in Postgres, and an implementation that started UPDATEing rows instead
    of appending them would visibly change `disputes`.

    Statements are dispatched by EQUALITY against the module's own constants,
    never by sniffing for a substring. Three of them contain both `INSERT` and
    `dispute_events`, so substring matching would route a status transition into
    the opening branch and the tests would keep passing while asserting the
    wrong thing; equality means a statement this fake has not been taught fails
    loudly here instead.

    Every call awaits before it touches a row. That models what a pool really
    is — each statement atomic, statements interleaved — and it is what lets the
    concurrent test below tell a guard that lives in the index from one that
    lives in Python.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.settlements: list[dict[str, Any]] = []
        self.disputes: list[dict[str, Any]] = []
        self.closed = 0

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append(sql)
        await asyncio.sleep(0)
        if sql == dispute_store._INSERT_SETTLEMENT_SQL:
            self.settlements.append(dict(zip(_SETTLEMENT_COLUMNS, args, strict=True)))
            return "INSERT 0 1"
        assert sql in (
            dispute_store._CREATE_SETTLEMENTS_SQL,
            dispute_store._CREATE_DISPUTES_SQL,
        ), f"unexpected statement: {sql}"
        return "CREATE TABLE"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.statements.append(sql)
        await asyncio.sleep(0)
        if sql == dispute_store._SELECT_SETTLEMENT_BY_JOB_SQL:
            return _newest(self.settlements, job_id_hex=args[0])
        if sql == dispute_store._SELECT_SETTLEMENT_BY_TASK_SQL:
            return _newest(self.settlements, task_id=args[0])
        if sql == dispute_store._INSERT_DISPUTE_SQL:
            return self._open_dispute(args)
        if sql == dispute_store._APPEND_STATUS_SQL:
            return self._append_status(args)
        if sql == dispute_store._SELECT_DISPUTE_SQL:
            return _newest(self.disputes, dispute_id=args[0])
        assert sql == dispute_store._SELECT_DISPUTE_BY_STEP_SQL, f"unexpected statement: {sql}"
        return _newest(self.disputes, job_id_hex=args[0], step_index=args[1])

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append(sql)
        await asyncio.sleep(0)
        assert sql == dispute_store._SELECT_DISPUTES_FOR_TASK_SQL, f"unexpected statement: {sql}"
        # DISTINCT ON (dispute_id) ... ORDER BY dispute_id, id DESC keeps the
        # newest row per dispute; the outer ORDER BY re-sorts them for the
        # reader. A dict comprehension keeps the LAST occurrence, which is the
        # newest row.
        newest = {r["dispute_id"]: r for r in self.disputes if r["task_id"] == args[0]}
        return sorted(newest.values(), key=lambda r: (r["opened_at"], r["step_index"]))

    def _open_dispute(self, args: tuple[Any, ...]) -> dict[str, Any] | None:
        row = dict(zip(_DISPUTE_COLUMNS, args, strict=True)) | {"opening": True}
        # dispute_events_one_per_step_idx: UNIQUE (job_id_hex, step_index) WHERE
        # opening. ON CONFLICT ... DO NOTHING writes nothing and returns nothing.
        if any(
            r["opening"] and r["job_id_hex"] == row["job_id_hex"] and r["step_index"] == row["step_index"]
            for r in self.disputes
        ):
            return None
        self.disputes.append(row)
        return {"dispute_id": row["dispute_id"]}

    def _append_status(self, args: tuple[Any, ...]) -> dict[str, Any] | None:
        dispute_id, status, refund_tx, rating_tx, resolved_at, now = args
        latest = _newest(self.disputes, dispute_id=dispute_id)
        # `INSERT ... SELECT FROM latest`: with no history there is nothing to
        # select, so nothing is written and nothing comes back.
        if latest is None:
            return None
        row = latest | {
            "status": status,
            "resolved_at": _coalesce(resolved_at, latest["resolved_at"], now),
            "refund_tx": _coalesce(refund_tx, latest["refund_tx"]),
            "rating_tx": _coalesce(rating_tx, latest["rating_tx"]),
            "opening": False,
        }
        self.disputes.append(row)
        return row

    async def close(self) -> None:
        self.closed += 1

    @property
    def writes(self) -> list[str]:
        return [s for s in self.statements if "INSERT" in s or "UPDATE" in s or "DELETE" in s]


def _pg(pool: FakePool) -> dispute_store.PostgresDisputeStore:
    return dispute_store.PostgresDisputeStore("postgres://user:pw@example.invalid/db", pool=pool)


# ── the schema, in Postgres ───────────────────────────────────────────────


def test_the_schema_is_created_lazily_and_only_once() -> None:
    """No migration tooling exists in this repo, so the DDL ships with the
    store — but constructing it must not do I/O, and a hot process must not
    re-run the DDL on every settlement."""
    pool = FakePool()
    store = _pg(pool)

    assert pool.statements == []  # construction alone talks to nothing

    async def go() -> None:
        await store.record_settlement(a_settlement())
        await store.open_dispute(a_dispute())
        await store.get_settlement(JOB)

    asyncio.run(go())

    ddl = [s for s in pool.statements if "CREATE TABLE" in s]
    assert len(ddl) == 2
    assert any("CREATE TABLE IF NOT EXISTS workflow_settlements" in s for s in ddl)
    assert any("CREATE TABLE IF NOT EXISTS dispute_events" in s for s in ddl)


def test_the_duplicate_rule_is_an_index_and_not_only_a_read() -> None:
    """The constraint itself, asserted on the DDL: two requests racing for one
    step is a thing users do, and only the database can settle it."""
    ddl = dispute_store._CREATE_DISPUTES_SQL

    assert "CREATE UNIQUE INDEX IF NOT EXISTS dispute_events_one_per_step_idx" in ddl
    # PARTIAL — the table is append-only, so a total unique index on the pair
    # would reject every status transition after the opening row.
    assert "ON dispute_events (job_id_hex, step_index) WHERE opening" in ddl


def test_no_sql_in_the_module_mutates_a_row() -> None:
    """Belt and braces on the constants themselves, so a later edit that adds
    an UPDATE has to delete this test to land. ON CONFLICT DO NOTHING is the one
    conflict clause that leaves the conflicting row alone; DO UPDATE would be an
    UPDATE wearing a hat, and is refused here by name."""
    sql = " ".join(
        (
            dispute_store._CREATE_SETTLEMENTS_SQL,
            dispute_store._CREATE_DISPUTES_SQL,
            dispute_store._SELECT_SETTLEMENT_BY_JOB_SQL,
            dispute_store._SELECT_SETTLEMENT_BY_TASK_SQL,
            dispute_store._INSERT_SETTLEMENT_SQL,
            dispute_store._SELECT_DISPUTE_SQL,
            dispute_store._SELECT_DISPUTE_BY_STEP_SQL,
            dispute_store._SELECT_DISPUTES_FOR_TASK_SQL,
            dispute_store._INSERT_DISPUTE_SQL,
            dispute_store._APPEND_STATUS_SQL,
        )
    ).upper()

    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert "DO UPDATE" not in sql


def test_nothing_is_dated_by_the_database() -> None:
    """Every timestamp is epoch seconds from this process's clock. A now() in
    the SQL would date a record in whatever timezone the database runs in, and
    the window a buyer was promised would stop matching the window stored."""
    sql = " ".join(
        (
            dispute_store._CREATE_SETTLEMENTS_SQL,
            dispute_store._CREATE_DISPUTES_SQL,
            dispute_store._INSERT_SETTLEMENT_SQL,
            dispute_store._INSERT_DISPUTE_SQL,
            dispute_store._APPEND_STATUS_SQL,
        )
    ).upper()

    assert "NOW()" not in sql
    assert "CURRENT_TIMESTAMP" not in sql


# ── settlements, in Postgres ──────────────────────────────────────────────


def test_a_settlement_round_trips_through_the_json_column() -> None:
    """The step breakdown is stored as one JSON value, so the encode/decode pair
    is the only thing standing between a settled price and the credit computed
    from it a day later."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> SettlementRecord | None:
        await store.record_settlement(a_settlement())
        return await store.get_settlement(JOB)

    stored = asyncio.run(go())

    assert stored == a_settlement()
    assert stored is not None and stored.steps == STEPS
    # It really went through JSON: the row holds text, not the tuple.
    assert isinstance(pool.settlements[0]["steps"], str)


def test_a_settlement_is_also_read_back_by_task() -> None:
    pool = FakePool()
    store = _pg(pool)

    async def go() -> SettlementRecord | None:
        await store.record_settlement(a_settlement())
        return await store.get_settlement_by_task(TASK)

    stored = asyncio.run(go())

    assert stored is not None
    assert stored.job_id_hex == JOB
    assert stored.window_closes_at == 1_700_086_400.0


def test_settling_twice_appends_a_row_and_the_newest_one_wins() -> None:
    """Append-only, on the money path: the second settlement must not fail on a
    unique key, and the first must stay on the audit trail."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> SettlementRecord | None:
        await store.record_settlement(a_settlement())
        await store.record_settlement(a_settlement(job_id_hex=OTHER_JOB, window_closes_at=1_700_172_800.0))
        return await store.get_settlement_by_task(TASK)

    latest = asyncio.run(go())

    assert latest is not None
    assert latest.job_id_hex == OTHER_JOB
    assert len(pool.settlements) == 2
    assert all("INSERT INTO workflow_settlements" in s for s in pool.writes)


def test_an_unsettled_job_or_task_reads_as_none_in_postgres() -> None:
    pool = FakePool()
    store = _pg(pool)

    assert asyncio.run(store.get_settlement(OTHER_JOB)) is None
    assert asyncio.run(store.get_settlement_by_task("task_never")) is None


# ── disputes, in Postgres ─────────────────────────────────────────────────


def test_an_opened_dispute_is_read_back_by_id_and_by_step() -> None:
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord, DisputeRecord | None, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        return opened, await store.get_dispute(opened.id), await store.find_dispute(JOB, 0)

    opened, by_id, by_step = asyncio.run(go())

    assert by_id == opened == a_dispute()
    assert by_step == opened
    assert len(pool.disputes) == 1
    assert pool.disputes[0]["opening"] is True
    assert all("INSERT INTO dispute_events" in s for s in pool.writes)


def test_an_unknown_dispute_reads_as_none_in_postgres() -> None:
    pool = FakePool()
    store = _pg(pool)

    assert asyncio.run(store.get_dispute("dsp_never")) is None
    assert asyncio.run(store.find_dispute(JOB, 9)) is None


def test_a_duplicate_dispute_is_refused_by_the_index_and_answered_with_the_first() -> None:
    """The second insert conflicts with dispute_events_one_per_step_idx and does
    nothing, so the loser writes NO row — and the caller gets the dispute that
    already exists rather than a failure it cannot explain to the buyer."""
    pool = FakePool()
    store = _pg(pool)

    first = asyncio.run(store.open_dispute(a_dispute()))

    with pytest.raises(DuplicateDisputeError) as excinfo:
        asyncio.run(store.open_dispute(a_dispute(id="dsp_0002", reason="a second try")))

    assert excinfo.value.existing == first
    assert len(pool.disputes) == 1
    assert asyncio.run(store.get_dispute("dsp_0002")) is None


def test_a_different_step_or_job_is_not_refused_in_postgres() -> None:
    """The index is on the PAIR — scoping it to the job alone would let one bad
    step block every other dispute of the same workflow."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> None:
        await store.open_dispute(a_dispute())
        await store.open_dispute(a_dispute(id="dsp_0002", step_index=1))
        await store.open_dispute(a_dispute(id="dsp_0003", job_id_hex=OTHER_JOB))

    asyncio.run(go())

    assert len(pool.disputes) == 3


def test_a_task_s_disputes_are_listed_oldest_first_and_nobody_else_s() -> None:
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord, ...]:
        await store.open_dispute(a_dispute(id="dsp_0002", step_index=1, opened_at=1_700_000_200.0))
        await store.open_dispute(a_dispute())
        await store.open_dispute(a_dispute(id="dsp_0003", task_id="task_beta", job_id_hex=OTHER_JOB))
        return await store.list_disputes_for_task(TASK)

    listed = asyncio.run(go())

    # Sorted by the moment each was opened, not by the order the rows landed.
    assert [d.id for d in listed] == ["dsp_0001", "dsp_0002"]
    assert asyncio.run(store.list_disputes_for_task("task_never")) == ()


def test_two_concurrent_disputes_of_one_step_produce_one_dispute() -> None:
    """The race the index exists for, run as a race.

    Both calls are in flight at once and the fake interleaves their statements
    the way a pool does, so each one reaches the table having seen a step with
    no dispute on it. A guard that lived in Python would open two disputes here
    and credit the buyer twice; the guard that lives in the index lets exactly
    one row land and tells the other request which dispute already owns the
    step.
    """
    pool = FakePool()
    store = _pg(pool)

    async def go() -> list[Any]:
        return await asyncio.gather(
            store.open_dispute(a_dispute(id="dsp_first", reason="the first reason")),
            store.open_dispute(a_dispute(id="dsp_second", reason="the second reason")),
            return_exceptions=True,
        )

    results = asyncio.run(go())

    opened = [r for r in results if isinstance(r, DisputeRecord)]
    refused = [r for r in results if isinstance(r, DuplicateDisputeError)]
    assert len(opened) == 1
    assert len(refused) == 1
    # One row written, and it is the winner's — not a row per request, and not
    # a row whose reason belongs to the request that lost.
    assert len(pool.disputes) == 1
    assert refused[0].existing == opened[0]
    assert asyncio.run(store.find_dispute(JOB, 0)) == opened[0]


# ── status transitions, in Postgres ───────────────────────────────────────


def test_a_transition_appends_a_row_and_leaves_the_opening_one_alone() -> None:
    """The append-only rule at its most tempting breaking point: the obvious
    implementation of "mark this credited" is an UPDATE, and that would erase
    the evidence of what the dispute said when it was opened."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> DisputeRecord:
        opened = await store.open_dispute(a_dispute())
        return await store.append_status(opened.id, "credited", refund_tx="tx_refund")

    credited = asyncio.run(go())

    assert credited.status == "credited"
    assert credited.refund_tx == "tx_refund"
    assert len(pool.disputes) == 2
    # The opening row still says exactly what it always said.
    assert pool.disputes[0]["status"] == "open"
    assert pool.disputes[0]["refund_tx"] is None
    assert pool.disputes[0]["opening"] is True
    # And the new row is NOT an opening, or it would collide with its own
    # dispute in the partial unique index.
    assert pool.disputes[1]["opening"] is False
    assert not any("UPDATE" in s or "DELETE" in s for s in pool.statements)


def test_the_current_state_is_the_newest_row() -> None:
    """Every read answers with the latest event, so a resolved dispute never
    reads as open again — and the immutable half of the record is carried
    forward by the SQL rather than restated by the caller."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, tuple[DisputeRecord, ...]]:
        opened = await store.open_dispute(a_dispute())
        await store.append_status(opened.id, "upheld")
        await store.append_status(opened.id, "credited", refund_tx="tx_refund")
        await store.append_status(opened.id, "credited", rating_tx="tx_rating")
        return (
            await store.get_dispute(opened.id),
            await store.find_dispute(JOB, 0),
            await store.list_disputes_for_task(TASK),
        )

    by_id, by_step, listed = asyncio.run(go())

    assert by_id is not None
    assert by_id.status == "credited"
    assert by_id.refund_tx == "tx_refund"  # not erased by the transition after it
    assert by_id.rating_tx == "tx_rating"
    assert by_id.reason == "the summary was empty"
    assert by_id.charged_usdc == 1.5
    assert by_step == by_id
    # Four rows, one dispute: the list collapses a history to current states.
    assert len(pool.disputes) == 4
    assert listed == (by_id,)


def test_the_moment_a_dispute_resolved_is_stamped_once_and_never_moved() -> None:
    """From this process's clock, on the transition that first resolved it. A
    later event re-dating it would rewrite when the buyer was made whole."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord, DisputeRecord]:
        opened = await store.open_dispute(a_dispute())
        return (
            await store.append_status(opened.id, "upheld"),
            await store.append_status(opened.id, "credited", refund_tx="tx_refund"),
        )

    before = time.time()
    upheld, credited = asyncio.run(go())
    after = time.time()

    assert upheld.resolved_at is not None and before <= upheld.resolved_at <= after
    assert credited.resolved_at == upheld.resolved_at
    assert pool.disputes[-1]["resolved_at"] == upheld.resolved_at


def test_a_caller_may_supply_the_moment_it_resolved() -> None:
    pool = FakePool()
    store = _pg(pool)

    async def go() -> DisputeRecord:
        opened = await store.open_dispute(a_dispute())
        return await store.append_status(opened.id, "rejected", resolved_at=1_700_009_999.0)

    assert asyncio.run(go()).resolved_at == 1_700_009_999.0


def test_a_transition_on_an_unknown_dispute_is_a_key_error_in_postgres() -> None:
    """`INSERT ... SELECT FROM latest` writes nothing when there is no history,
    so the store says so instead of inventing a dispute out of an id."""
    pool = FakePool()
    store = _pg(pool)

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_never", "upheld"))

    assert pool.disputes == []


# ── the pool ──────────────────────────────────────────────────────────────


def test_close_closes_the_pool_once_and_is_safe_twice() -> None:
    """Lifespan shutdown calls close unconditionally, and a close racing a
    request must not hand out the pool it is tearing down."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> None:
        await store.record_settlement(a_settlement())
        await store.close()
        await store.close()

    asyncio.run(go())

    assert pool.closed == 1
    assert store._pool is None


def test_the_pool_is_opened_with_min_size_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A free Render instance idles and its sockets die with it — a pool that
    insists on a live connection wakes up holding a dead one and hands it to the
    first buyer opening a dispute."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> FakePool:
        captured.update(kwargs)
        return FakePool()

    class FakeAsyncpg:
        create_pool = staticmethod(fake_create_pool)

    monkeypatch.setattr(dispute_store, "_import_asyncpg", lambda: FakeAsyncpg)
    store = dispute_store.PostgresDisputeStore("postgres://user:pw@example.invalid/db")

    asyncio.run(store.get_dispute("dsp_never"))

    assert captured["min_size"] == 0
    assert captured["max_size"] == dispute_store._POOL_MAX_SIZE
    assert captured["dsn"] == "postgres://user:pw@example.invalid/db"


def test_a_missing_driver_names_the_fix() -> None:
    """asyncpg is imported at first use, not at module scope, so this suite —
    and any checkout without the driver — imports and collects cleanly."""
    try:
        import asyncpg  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        pytest.skip("asyncpg is installed, so the missing-driver path cannot be reached")

    with pytest.raises(RuntimeError) as excinfo:
        dispute_store._import_asyncpg()

    message = str(excinfo.value)
    assert "asyncpg" in message and "DATABASE_URL" in message


# ── get_dispute_store / close_dispute_store ───────────────────────────────

DSN = "postgres://user:sup3rsecret@db.example.invalid/orizon"


@pytest.fixture(autouse=True)
def reset_singleton() -> Any:
    """The resolver is a module-level singleton; no test may inherit another's."""
    dispute_store._store = None
    yield
    dispute_store._store = None


def test_an_empty_database_url_selects_the_in_memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default, and what the hermetic suite and local dev run on."""
    monkeypatch.setattr(dispute_store.settings, "database_url", "")

    assert isinstance(dispute_store.get_dispute_store(), InMemoryDisputeStore)


def test_a_database_url_selects_postgres_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving must not do I/O: a database that is briefly unreachable at boot
    has to cost a failed request, not a failed deploy."""
    monkeypatch.setattr(dispute_store.settings, "database_url", DSN)

    store = dispute_store.get_dispute_store()

    assert isinstance(store, dispute_store.PostgresDisputeStore)
    assert store._pool is None  # nothing dialled yet


def test_the_store_is_resolved_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispute_store.settings, "database_url", "")

    assert dispute_store.get_dispute_store() is dispute_store.get_dispute_store()


def test_the_resolver_is_not_lru_cached() -> None:
    """The obvious tidy-up, and wrong in a way that is invisible until
    production: a cached resolver would pin whichever store the first import
    resolved and silently ignore a DATABASE_URL set later, writing dispute
    windows to memory while reporting success."""
    assert not hasattr(dispute_store.get_dispute_store, "cache_clear")
    assert not hasattr(dispute_store.get_dispute_store, "cache_info")


def test_a_database_url_that_arrives_later_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half of the test above."""
    monkeypatch.setattr(dispute_store.settings, "database_url", "")
    assert isinstance(dispute_store.get_dispute_store(), InMemoryDisputeStore)

    monkeypatch.setattr(dispute_store.settings, "database_url", DSN)
    asyncio.run(dispute_store.close_dispute_store())

    assert isinstance(dispute_store.get_dispute_store(), dispute_store.PostgresDisputeStore)


def test_close_releases_the_store_and_clears_the_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispute_store.settings, "database_url", "")
    first = dispute_store.get_dispute_store()
    asyncio.run(first.record_settlement(a_settlement()))

    asyncio.run(dispute_store.close_dispute_store())
    second = dispute_store.get_dispute_store()

    assert second is not first
    assert asyncio.run(second.get_settlement(JOB)) is None


def test_close_is_a_no_op_when_nothing_was_ever_resolved() -> None:
    """Lifespan shutdown calls this unconditionally, including on a process that
    never settled a workflow."""
    asyncio.run(dispute_store.close_dispute_store())

    assert dispute_store._store is None


def test_the_choice_is_logged_but_the_dsn_never_is(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A DSN carries the database password; the log says which store was chosen
    and nothing else."""
    monkeypatch.setattr(dispute_store.settings, "database_url", DSN)

    with caplog.at_level(logging.INFO, logger=STORE_LOGGER):
        dispute_store.get_dispute_store()

    lines = _messages(caplog)
    assert any("postgres" in m and "survive a restart" in m for m in lines)
    assert not any("sup3rsecret" in m or DSN in m for m in lines)


def test_the_in_memory_default_announces_that_it_loses_disputes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dispute surviving a restart is false on this path, so a deployment
    running it must be able to find that out from its own startup log rather
    than from a buyer's complaint that vanished."""
    monkeypatch.setattr(dispute_store.settings, "database_url", "")

    with caplog.at_level(logging.INFO, logger=STORE_LOGGER):
        dispute_store.get_dispute_store()

    assert any("in-memory" in m and "LOST on restart" in m for m in _messages(caplog))
