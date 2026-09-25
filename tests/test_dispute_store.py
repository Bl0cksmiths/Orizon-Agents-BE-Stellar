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
    DisputeStore,
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


# An adjudicator's note as written, ragged edges and all: the assertions on it
# are equality assertions, so anything the store trimmed or escaped shows up.
NOTE = "  step 0 delivered; the brief did not ask for charts\n"


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


def test_both_stores_list_a_task_s_disputes_in_the_same_order() -> None:
    """The two stores must not disagree about order, and only this test can
    tell: every other test in this suite runs against the in-memory one, which
    used to answer in INSERTION order while Postgres answers oldest-first.

    Insertion order is the order this process happened to see them — a restart,
    an eviction or a dispute opened against an older settlement all change it —
    so a task view read from memory and a task view read from the database
    listed a buyer's own disputes differently, and nothing hermetic could
    notice. The disputes below are opened NEWEST first, so an implementation
    that returns them as they arrived cannot pass."""

    async def listed(store: DisputeStore) -> list[str]:
        await store.open_dispute(a_dispute(id="dsp_late", step_index=1, opened_at=1_700_000_900.0))
        await store.open_dispute(a_dispute(id="dsp_early", step_index=0, opened_at=1_700_000_100.0))
        return [d.id for d in await store.list_disputes_for_task(TASK)]

    in_memory = asyncio.run(listed(InMemoryDisputeStore()))
    postgres = asyncio.run(listed(_pg(FakePool())))

    assert in_memory == postgres == ["dsp_early", "dsp_late"]


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


def test_an_adjudicators_note_is_kept_and_not_erased_by_a_later_transition() -> None:
    """The platform's half of the argument, made as durable as the buyer's.

    A buyer's `reason` is on the record from the moment they open the dispute.
    A rejection recorded only a status and a timestamp, which is backwards —
    rejection is the outcome most likely to be contested — and 4.04's rating
    lands minutes later, so a transition that blanked the note would take it
    off the record almost immediately.
    """
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord, DisputeRecord]:
        opened = await store.open_dispute(a_dispute())
        assert opened.note is None
        rejected = await store.append_status(opened.id, "rejected", note=NOTE)
        return rejected, await store.append_status(opened.id, "rejected", rating_tx="tx_rating")

    rejected, rated = asyncio.run(go())

    assert rejected.note == NOTE
    # The rating transition named no note, so the one already recorded stands.
    assert rated.note == NOTE
    assert rated.rating_tx == "tx_rating"
    # And the buyer's side is untouched by the platform's.
    assert rated.reason == "the summary was empty"


def test_an_adjudicators_note_is_stored_exactly_as_given() -> None:
    """Bounding and sanitising untrusted text is the caller's job — the
    adjudication path already does it. A store that trimmed or escaped evidence
    on its way in would quietly change what the platform is on record as having
    said."""
    store = InMemoryDisputeStore()

    async def go() -> DisputeRecord:
        opened = await store.open_dispute(a_dispute())
        return await store.append_status(opened.id, "rejected", note=NOTE)

    assert asyncio.run(go()).note == NOTE


def test_a_transition_on_an_unknown_dispute_is_a_key_error() -> None:
    """Not a silently created record: a dispute id that does not exist is a bug
    in the caller, and a store that invented one would hide it."""
    store = InMemoryDisputeStore()

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_never", "upheld"))


# ── the precondition on a transition ──────────────────────────────────────


def test_a_stale_decision_cannot_drag_a_credited_dispute_back_to_upheld() -> None:
    """The double payment, in four lines.

    A caller reads a dispute, decides, and appends; the append is a round trip
    later, and in between the dispute can be adjudicated, claimed and paid by
    somebody else. An unconditional append writes the stale decision anyway,
    and `credited` dragged back to `upheld` is a dispute that can be claimed
    and paid a SECOND time out of the platform wallet, with nothing on-chain
    to take the second transfer back.

    Naming the status the decision was read from is what refuses it."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        # What the stale caller read, one round trip ago.
        stale = await store.get_dispute(opened.id)
        assert stale is not None and stale.status == "open"
        # Meanwhile the dispute is upheld, claimed and paid.
        await store.append_status(opened.id, "upheld")
        await store.claim_refund(opened.id)
        await store.append_status(opened.id, "credited", refund_tx="tx_paid", credited_usdc=1.5)
        refused = await store.append_status(opened.id, "upheld", expected_status=stale.status)
        return refused, await store.get_dispute(opened.id), await store.claim_refund(opened.id)

    refused, current, second_claim = asyncio.run(go())

    assert refused is None
    # Nothing was written: the dispute still reads as paid, with its hash.
    assert current is not None and current.status == "credited"
    assert current.refund_tx == "tx_paid"
    # And so the second payout cannot even be claimed, let alone signed.
    assert second_claim is None


def test_a_stale_decision_cannot_pay_a_dispute_that_was_rejected() -> None:
    """The other half of the same bug, and the one that pays out money the
    adjudicator refused: `rejected` dragged back to `upheld` is a dispute
    decided AGAINST the buyer becoming claimable and payable."""
    store = InMemoryDisputeStore()

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        await store.append_status(opened.id, "rejected", note=NOTE)
        refused = await store.append_status(opened.id, "upheld", expected_status="open")
        return refused, await store.get_dispute(opened.id)

    refused, current = asyncio.run(go())

    assert refused is None
    assert current is not None and current.status == "rejected"
    # The buyer's explanation is still the one they were given.
    assert current.note == NOTE


def test_a_transition_that_names_no_expectation_still_lands_unconditionally() -> None:
    """The operator's reconciliation write (docs/disputes.md): a person who has
    read the chain is correcting the record ON PURPOSE, and their write has to
    land whatever the dispute currently says. Passing no expectation is how
    that is asked for, so the default cannot be a precondition."""
    store = InMemoryDisputeStore()

    async def go() -> DisputeRecord | None:
        opened = await store.open_dispute(a_dispute())
        await store.append_status(opened.id, "upheld")
        await store.claim_refund(opened.id)
        return await store.append_status(opened.id, "credited", refund_tx="tx_reconciled", credited_usdc=1.5)

    recorded = asyncio.run(go())

    assert recorded is not None and recorded.status == "credited"
    assert recorded.refund_tx == "tx_reconciled"


def test_a_transition_that_matches_the_expectation_is_written() -> None:
    """The precondition refuses a dispute that MOVED, and nothing else. An
    implementation that refused whenever an expectation was named would break
    every adjudication while passing the two tests above."""
    store = InMemoryDisputeStore()

    async def go() -> DisputeRecord | None:
        opened = await store.open_dispute(a_dispute())
        return await store.append_status(opened.id, "upheld", expected_status="open")

    upheld = asyncio.run(go())

    assert upheld is not None and upheld.status == "upheld"


def test_an_unknown_dispute_is_a_key_error_even_with_an_expectation() -> None:
    """None means "this dispute has moved"; KeyError means "there is no such
    dispute". Folding the second into the first would let a caller with a typo
    in an id read it as a lost race and move on."""
    store = InMemoryDisputeStore()

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_never", "upheld", expected_status="open"))


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
    "note",
    "credited_usdc",
    "updated_at",
    "rating_confirmed",
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
        # The refund mutex: dispute_id -> claimed_at, which is the whole of
        # `refund_claims`. A dict rather than a set because the claim time is
        # what makes the table readable as a reconciliation queue.
        self.claims: dict[str, float] = {}
        # Row locks, one per dispute id, taken by _LOCK_DISPUTE_SQL and held
        # until the transaction that took them ends. Postgres locks a row;
        # there are no rows to lock here, so the dispute id stands in for the
        # opening row every appender for that dispute queues on.
        self.row_locks: dict[str, asyncio.Lock] = {}
        # What each call passed as its bound. A pool call that names none can
        # wait forever, so the value is recorded rather than discarded.
        self.timeouts: list[float | None] = []
        self.acquired: list[float | None] = []
        self.closed = 0

    async def execute(self, sql: str, *args: Any, timeout: float | None = None) -> str:
        self.statements.append(sql)
        self.timeouts.append(timeout)
        await asyncio.sleep(0)
        if sql == dispute_store._INSERT_SETTLEMENT_SQL:
            self.settlements.append(dict(zip(_SETTLEMENT_COLUMNS, args, strict=True)))
            return "INSERT 0 1"
        assert sql in (
            dispute_store._CREATE_SETTLEMENTS_SQL,
            dispute_store._CREATE_DISPUTES_SQL,
            dispute_store._CREATE_REFUND_CLAIMS_SQL,
        ), f"unexpected statement: {sql}"
        return "CREATE TABLE"

    async def fetchrow(self, sql: str, *args: Any, timeout: float | None = None) -> dict[str, Any] | None:
        self.statements.append(sql)
        self.timeouts.append(timeout)
        await asyncio.sleep(0)
        if sql == dispute_store._SELECT_SETTLEMENT_BY_JOB_SQL:
            return _newest(self.settlements, job_id_hex=args[0])
        if sql == dispute_store._SELECT_SETTLEMENT_BY_TASK_SQL:
            return _newest(self.settlements, task_id=args[0])
        if sql == dispute_store._INSERT_DISPUTE_SQL:
            return self._open_dispute(args)
        if sql == dispute_store._APPEND_STATUS_SQL:
            return await self._append_status(args)
        if sql == dispute_store._SELECT_DISPUTE_SQL:
            return _newest(self.disputes, dispute_id=args[0])
        if sql == dispute_store._CLAIM_REFUND_SQL:
            return await self._claim_refund(args)
        if sql == dispute_store._RELEASE_REFUND_CLAIM_SQL:
            return await self._release_refund_claim(*args)
        assert sql == dispute_store._SELECT_DISPUTE_BY_STEP_SQL, f"unexpected statement: {sql}"
        return _newest(self.disputes, job_id_hex=args[0], step_index=args[1])

    async def fetch(self, sql: str, *args: Any, timeout: float | None = None) -> list[dict[str, Any]]:
        self.statements.append(sql)
        self.timeouts.append(timeout)
        await asyncio.sleep(0)
        if sql == dispute_store._SELECT_REFUND_CLAIMS_SQL:
            # ORDER BY claimed_at, dispute_id: oldest claim first, and a stable
            # tiebreak for two taken in the same clock tick.
            return [
                {"dispute_id": dispute_id, "claimed_at": at}
                for dispute_id, at in sorted(self.claims.items(), key=lambda claim: (claim[1], claim[0]))
            ]
        assert sql == dispute_store._SELECT_DISPUTES_FOR_TASK_SQL, f"unexpected statement: {sql}"
        # DISTINCT ON (dispute_id) ... ORDER BY dispute_id, id DESC keeps the
        # newest row per dispute; the outer ORDER BY re-sorts them for the
        # reader. A dict comprehension keeps the LAST occurrence, which is the
        # newest row.
        newest = {r["dispute_id"]: r for r in self.disputes if r["task_id"] == args[0]}
        return sorted(newest.values(), key=lambda r: (r["opened_at"], r["step_index"]))

    async def _claim_refund(self, args: tuple[Any, ...]) -> dict[str, Any] | None:
        """_CLAIM_REFUND_SQL: the `latest` CTE, the mutex insert and the event
        insert, as one statement sharing ONE snapshot.

        The snapshot is the part worth modelling. `latest` is read before
        anything is written, and a claim that commits in between is invisible
        to it — so the status this reads can rule a claim out but can never
        separate two claimants. The sleep below IS that window: everything
        after it runs with a status that may already be stale, and the PRIMARY
        KEY is the only thing left to arbitrate. An implementation that
        dropped `refund_claims` and leaned on the status alone would append two
        `crediting` rows here, which is exactly the double credit the table
        exists to prevent.
        """
        dispute_id, claimed_at = args
        latest = _newest(self.disputes, dispute_id=dispute_id)
        await asyncio.sleep(0)
        # `SELECT $1, $2 FROM latest WHERE latest.status = 'upheld'` selects
        # nothing, so the mutex row is not written and the main INSERT — which
        # JOINs `claim` — writes nothing either.
        if latest is None or latest["status"] != "upheld":
            return None
        if dispute_id in self.claims:  # ON CONFLICT (dispute_id) DO NOTHING
            return None
        self.claims[dispute_id] = claimed_at
        # The status changes and the row is dated by the claim's own clock
        # reading; resolved_at, the rating hash and the receipt's facts are
        # copied forward, because `crediting` is not a resolution. The refund
        # hash is cleared: this payout has no transaction yet.
        row = latest | {"status": "crediting", "updated_at": claimed_at, "refund_tx": None, "opening": False}
        self.disputes.append(row)
        return row

    async def _release_refund_claim(self, dispute_id: str, now: float) -> dict[str, Any] | None:
        """_RELEASE_REFUND_CLAIM_SQL: the DELETE and the `upheld` row, one
        statement and one snapshot.

        Both halves are gated on the SAME status from that one snapshot, so
        the mutex and the dispute cannot end up disagreeing about whether a
        payout is in flight — and the DELETE is a no-op rather than a
        precondition, which is what lets a release repair a `crediting`
        dispute whose claim row went missing.
        """
        latest = _newest(self.disputes, dispute_id=dispute_id)
        await asyncio.sleep(0)
        if latest is None or latest["status"] != "crediting":
            return None
        self.claims.pop(dispute_id, None)
        # Released only when nothing landed, so no refund hash goes with it.
        row = latest | {"status": "upheld", "updated_at": now, "refund_tx": None, "opening": False}
        self.disputes.append(row)
        return row

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

    async def _append_status(self, args: tuple[Any, ...]) -> dict[str, Any] | None:
        """_APPEND_STATUS_SQL: the `latest` CTE, the mutex DELETE and the event
        insert, as one statement — and ONE snapshot, modelled the way
        `_claim_refund` models its own.

        The sleep below IS that snapshot window. `latest` is read before
        anything is written, and a transition that commits in between is
        invisible to it, so everything after the sleep runs from a row that may
        already be stale. Modelling the append as an atomic read-modify-write
        instead — which this fake did until the window was found — certifies a
        guarantee no database gives: at READ COMMITTED two concurrent
        statements take their own snapshots, and this one blocks on nothing.
        """
        (
            dispute_id,
            status,
            refund_tx,
            rating_tx,
            note,
            resolved_at,
            now,
            credited_usdc,
            rating_confirmed,
            expected_status,
        ) = args
        latest = _newest(self.disputes, dispute_id=dispute_id)
        await asyncio.sleep(0)
        # `finished`: the mutex is dropped by the same statement that ends the
        # dispute. Being a data-modifying CTE it is evaluated whether or not
        # the INSERT beside it writes a row, so it is modelled before the early
        # return rather than after it — and it repeats the precondition, from
        # the same snapshot, because a transition that is refused must leave
        # the mutex where it is.
        if (
            status in ("credited", "rejected")
            and latest is not None
            and (expected_status is None or latest["status"] == expected_status)
        ):
            self.claims.pop(dispute_id, None)
        # `INSERT ... SELECT FROM latest`: with no history there is nothing to
        # select, so nothing is written and nothing comes back.
        if latest is None:
            return None
        # `WHERE $10::text IS NULL OR latest.status = $10::text` — the
        # precondition, read from the SAME snapshot the row would be copied
        # from, which is why it is checked on this side of the sleep.
        if expected_status is not None and latest["status"] != expected_status:
            return None
        row = latest | {
            "status": status,
            "resolved_at": _coalesce(resolved_at, latest["resolved_at"], now),
            "refund_tx": _coalesce(refund_tx, latest["refund_tx"]),
            "rating_tx": _coalesce(rating_tx, latest["rating_tx"]),
            "note": _coalesce(note, latest["note"]),
            "credited_usdc": _coalesce(credited_usdc, latest["credited_usdc"]),
            "updated_at": now,
            # The CASE, not a COALESCE: a confirmation the ledger has already
            # given cannot be undone by a later FALSE.
            "rating_confirmed": (
                True if latest["rating_confirmed"] else _coalesce(rating_confirmed, latest["rating_confirmed"])
            ),
            "opening": False,
        }
        self.disputes.append(row)
        return row

    def acquire(self, *, timeout: float | None = None) -> FakeAcquire:
        """`pool.acquire()`: one connection, as an async context manager.

        Only `append_status` asks for one, and only because its two statements
        have to run on the same connection inside one transaction — the first
        takes a row lock the second is read under.
        """
        self.acquired.append(timeout)
        return FakeAcquire(self)

    async def close(self) -> None:
        self.closed += 1

    @property
    def writes(self) -> list[str]:
        # The lock statement reads one row and locks it. `FOR UPDATE` puts the
        # word in the SQL without making the statement a write, so it is named
        # out rather than matched on.
        return [
            s
            for s in self.statements
            if s != dispute_store._LOCK_DISPUTE_SQL and ("INSERT" in s or "UPDATE" in s or "DELETE" in s)
        ]


class FakeAcquire:
    """What `pool.acquire()` returns: `async with` it for a connection."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakeConnection:
        await asyncio.sleep(0)
        return FakeConnection(self._pool)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeConnection:
    """One pooled connection: the pool's own statements, plus the two things a
    pool call cannot give — a transaction, and the row locks it holds until
    that transaction ends."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool
        self._held: list[asyncio.Lock] = []

    async def execute(self, sql: str, *args: Any, timeout: float | None = None) -> str:
        return await self._pool.execute(sql, *args, timeout=timeout)

    async def fetch(self, sql: str, *args: Any, timeout: float | None = None) -> list[dict[str, Any]]:
        return await self._pool.fetch(sql, *args, timeout=timeout)

    async def fetchrow(self, sql: str, *args: Any, timeout: float | None = None) -> dict[str, Any] | None:
        if sql != dispute_store._LOCK_DISPUTE_SQL:
            return await self._pool.fetchrow(sql, *args, timeout=timeout)
        self._pool.statements.append(sql)
        self._pool.timeouts.append(timeout)
        await asyncio.sleep(0)
        (dispute_id,) = args
        opening = next(
            (row for row in self._pool.disputes if row["opening"] and row["dispute_id"] == dispute_id),
            None,
        )
        # `SELECT ... FOR UPDATE` over no rows locks nothing and returns
        # nothing, which is how the store tells an unknown dispute apart from
        # one whose transition was refused.
        if opening is None:
            return None
        lock = self._pool.row_locks.setdefault(dispute_id, asyncio.Lock())
        # The wait itself. A second appender for this dispute stops here until
        # the first transaction ends, and reads `latest` only afterwards —
        # which is the whole of what the lock buys, and the reason it is a
        # separate statement rather than a FOR UPDATE on the `latest` CTE.
        await lock.acquire()
        self._held.append(lock)
        return {"locked": 1}

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeTransaction:
    """`conn.transaction()`. A row lock lasts until the transaction ends, and
    that is the half of it that makes the lock a mutex rather than a pause."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeTransaction:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for lock in self._conn._held:
            lock.release()
        self._conn._held.clear()


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
    # settlements, dispute events, and the refund mutex.
    assert len(ddl) == 3
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


def test_no_sql_in_the_module_mutates_a_dispute_event() -> None:
    """Belt and braces on the constants themselves, so a later edit that adds
    an UPDATE has to delete this test to land. ON CONFLICT DO NOTHING is the one
    conflict clause that leaves the conflicting row alone; DO UPDATE would be an
    UPDATE wearing a hat, and is refused here by name.

    Every `_*_SQL` constant is checked, found by NAME rather than listed one by
    one: a statement added later and forgotten here would be exactly the one
    free to start rewriting the audit trail.

    DELETE is allowed against `refund_claims` and against nothing else. That
    table is a mutex, not a record — it is MEANT to be released, and releasing
    it destroys no evidence — while every row of `dispute_events` is part of
    what a chargeback is answered with."""
    statements = {name: sql for name, sql in vars(dispute_store).items() if name.endswith("_SQL")}

    # The introspection finding nothing would make every assertion below vacuous.
    assert {"_INSERT_DISPUTE_SQL", "_APPEND_STATUS_SQL", "_CLAIM_REFUND_SQL"} <= statements.keys()
    for name, sql in statements.items():
        upper = sql.upper()
        # What is refused is the UPDATE *command*. The bare word proves
        # nothing on its own: `FOR UPDATE` is a row LOCK, which holds a row
        # against concurrent appends and changes none of it, and `updated_at`
        # is a column every transition writes.
        without_lock = upper.replace("FOR UPDATE", "")
        assert "UPDATE " not in without_lock, name
        assert "UPDATE\n" not in without_lock, name
        assert "DO UPDATE" not in upper, name
        for after_delete in upper.split("DELETE ")[1:]:
            assert after_delete.startswith("FROM REFUND_CLAIMS"), name


def test_every_statement_that_writes_an_event_names_the_same_columns() -> None:
    """Four statements INSERT into dispute_events — opening a dispute, a status
    transition, and the two refund-mutex transitions — and each spells the
    column list out in full.

    The fake pool below maps this module's INSERT parameters onto those names
    POSITIONALLY, exactly as Postgres does. A column added to one statement and
    forgotten in another would store every value after it under the wrong name,
    which is the kind of drift that reads correctly and pays the wrong amount."""
    inserts = [
        sql
        for name, sql in vars(dispute_store).items()
        if name.endswith("_SQL") and "INSERT INTO dispute_events" in sql
    ]

    assert len(inserts) == 4  # opening, transition, claim, release
    for sql in inserts:
        named = sql.split("INSERT INTO dispute_events (", 1)[1].split(")", 1)[0]
        assert tuple(column.strip() for column in named.split(",")) == _DISPUTE_COLUMNS + ("opening",)


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
    """Round-tripped exactly as given, save the one field the store assigns:
    opening is the dispute's first change of state, so `updated_at` is the
    moment it was opened (story 4.06)."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord, DisputeRecord | None, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        return opened, await store.get_dispute(opened.id), await store.find_dispute(JOB, 0)

    opened, by_id, by_step = asyncio.run(go())

    assert by_id == opened == a_dispute(updated_at=a_dispute().opened_at)
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
    # `dispute_events` stays append-only. The only DELETE this path may issue
    # is against `refund_claims`, which is a mutex rather than a record: it is
    # meant to be released, and releasing it destroys no history. The lock
    # statement is named out because `FOR UPDATE` carries the word without
    # changing a row — it reads one and holds it.
    assert not any(
        ("UPDATE" in s or "DELETE" in s) and "refund_claims" not in s
        for s in pool.statements
        if s != dispute_store._LOCK_DISPUTE_SQL
    )


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


def test_an_adjudicators_note_is_appended_and_read_back_in_postgres() -> None:
    """The column, end to end: written by the transition that names it, carried
    forward by the one that does not, and never written onto the opening row
    the buyer's complaint lives on."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        rejected = await store.append_status(opened.id, "rejected", note=NOTE)
        await store.append_status(opened.id, "rejected", rating_tx="tx_rating")
        return rejected, await store.get_dispute(opened.id)

    rejected, stored = asyncio.run(go())

    assert rejected.note == NOTE
    assert stored is not None and stored.note == NOTE and stored.rating_tx == "tx_rating"
    assert [row["note"] for row in pool.disputes] == [None, NOTE, NOTE]


def test_the_note_column_is_added_to_a_table_that_already_exists() -> None:
    """`dispute_events` predates the note — 4.02 created it — and CREATE TABLE
    IF NOT EXISTS does nothing whatever to a table that is already there. This
    ALTER is the whole of the deploy for this column, since the repo has no
    migration tool, and without it the first INSERT naming `note` would fail
    every dispute write on a service that had already run once."""
    ddl = dispute_store._CREATE_DISPUTES_SQL

    assert "ALTER TABLE dispute_events ADD COLUMN IF NOT EXISTS note TEXT" in ddl
    # And in the CREATE as well, so a fresh database gets it without the ALTER.
    assert any(line.strip().startswith("note ") for line in ddl.splitlines())


def test_a_transition_on_an_unknown_dispute_is_a_key_error_in_postgres() -> None:
    """`INSERT ... SELECT FROM latest` writes nothing when there is no history,
    so the store says so instead of inventing a dispute out of an id."""
    pool = FakePool()
    store = _pg(pool)

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_never", "upheld"))

    assert pool.disputes == []


def test_the_precondition_is_a_clause_of_the_writing_statement() -> None:
    """Asserted on the SQL itself, because a precondition checked in Python
    before the INSERT is not a precondition at all: the dispute can move
    between the read and the write, which is the whole bug. It has to be the
    same statement that does the writing."""
    sql = dispute_store._APPEND_STATUS_SQL

    assert "WHERE $10::text IS NULL OR latest.status = $10::text" in sql
    # And it gates the INSERT's own SELECT — not the `latest` CTE, which every
    # column of the new row is copied from.
    assert sql.index("FROM latest\nWHERE $10::text") > sql.index("INSERT INTO dispute_events")


def test_a_stale_transition_writes_no_row_in_postgres() -> None:
    """The append-only trail is the evidence a chargeback is answered with, so
    a refused transition must leave no trace in it — not a row recording a
    verdict the store declined to accept."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> DisputeRecord | None:
        opened = await store.open_dispute(a_dispute())
        await store.append_status(opened.id, "rejected", note=NOTE)
        return await store.append_status(opened.id, "upheld", expected_status="open")

    refused = asyncio.run(go())

    assert refused is None
    assert [row["status"] for row in pool.disputes] == ["open", "rejected"]


def test_a_refused_transition_is_told_apart_from_an_unknown_dispute() -> None:
    """RETURNING is empty for both, so the store reads the id back to say which
    — and pays for that read only on the path that has already failed."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> DisputeRecord | None:
        opened = await store.open_dispute(a_dispute())
        await store.append_status(opened.id, "upheld")
        return await store.append_status(opened.id, "credited", expected_status="open")

    assert asyncio.run(go()) is None

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_never", "credited", expected_status="open"))


# ── two transitions at once ───────────────────────────────────────────────


def test_two_concurrent_transitions_do_not_lose_each_other_s_facts() -> None:
    """The lost update, on the module's own example: a credit and a rating
    landing together.

    Both statements read the dispute's newest row, and each writes a row
    COALESCEd against what it read. Running at once — at READ COMMITTED,
    without a lock — they read the SAME row, and the one that commits second
    carries forward a record that never knew about the first. The buyer's
    receipt then shows a rating and no refund hash, for a refund that was paid.

    The fake models that window (see `_append_status`); what closes it is the
    row lock, which makes the second append read `latest` only after the first
    has written it."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> DisputeRecord | None:
        opened = await store.open_dispute(a_dispute())
        await store.append_status(opened.id, "upheld")
        await store.claim_refund(opened.id)
        await asyncio.gather(
            store.append_status(opened.id, "credited", refund_tx="tx_paid", credited_usdc=1.5),
            store.append_status(opened.id, "credited", rating_tx="tx_rating", rating_confirmed=True),
        )
        return await store.get_dispute(opened.id)

    final = asyncio.run(go())

    assert final is not None
    # Every fact either transition recorded is still on the record.
    assert final.refund_tx == "tx_paid"
    assert final.credited_usdc == 1.5
    assert final.rating_tx == "tx_rating"
    assert final.rating_confirmed is True


def test_two_concurrent_adjudications_produce_exactly_one_verdict() -> None:
    """Two adjudicators, one dispute, both deciding from the same `open` read.

    The precondition can only refuse the second if the second READS what the
    first wrote, and at READ COMMITTED a statement that started first never
    will. The lock is what orders them: the loser wakes, reads the verdict
    already recorded, and declines to write over it."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> list[DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        return list(
            await asyncio.gather(
                store.append_status(opened.id, "upheld", expected_status="open"),
                store.append_status(opened.id, "rejected", note=NOTE, expected_status="open"),
            )
        )

    results = asyncio.run(go())
    written = [record for record in results if record is not None]

    assert len(written) == 1
    # One verdict on the trail, beside the opening row — not two.
    assert [row["status"] for row in pool.disputes] == ["open", written[0].status]


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


def test_a_shutdown_racing_the_first_call_closes_the_pool_that_call_dialled() -> None:
    """Shutdown arrives while the first request is still dialling Postgres.

    `close()` used to run straight through: it found `self._pool` still empty,
    closed nothing, and the dial it did not wait for assigned a live pool
    afterwards — up to five sockets held open by a store nobody will call
    again, on a process that is trying to exit. Taking the creation lock is
    what makes the pool that was dialled the pool that is closed, and the
    caller still gets a pool rather than the None it would otherwise have to
    call a statement on."""
    store = dispute_store.PostgresDisputeStore("postgres://user:pw@example.invalid/db")
    dialled: list[FakePool] = []

    async def slow_create() -> FakePool:
        # The dial, mid-flight when the shutdown lands.
        await asyncio.sleep(0.01)
        pool = FakePool()
        dialled.append(pool)
        return pool

    store._create_pool = slow_create  # type: ignore[method-assign]

    async def go() -> Any:
        first = asyncio.create_task(store._ready_pool())
        await asyncio.sleep(0)
        await store.close()
        return await first

    got = asyncio.run(go())

    assert len(dialled) == 1
    assert dialled[0].closed == 1
    assert got is dialled[0]
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


def test_the_pool_is_opened_with_a_connect_and_a_command_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without these two, an unreachable database stops the service rather
    than failing its requests. asyncpg bounds a connect at 60 seconds, which
    is a hang as far as an HTTP request goes, and a COMMAND at nothing at
    all — a statement that never returns holds its connection for as long as
    the socket lives, and five of those is the whole pool."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> FakePool:
        captured.update(kwargs)
        return FakePool()

    class FakeAsyncpg:
        create_pool = staticmethod(fake_create_pool)

    monkeypatch.setattr(dispute_store, "_import_asyncpg", lambda: FakeAsyncpg)
    store = dispute_store.PostgresDisputeStore("postgres://user:pw@example.invalid/db")

    asyncio.run(store.get_dispute("dsp_never"))

    assert captured["timeout"] == dispute_store._POOL_CONNECT_TIMEOUT
    assert captured["command_timeout"] == dispute_store._POOL_COMMAND_TIMEOUT


def test_every_statement_and_every_acquire_names_its_own_bound() -> None:
    """The pool default is not enough on its own, because `acquire()` does not
    take one: a call that waits for a free connection waits forever unless it
    says otherwise. Every path here is walked, and the one that asks for a
    connection of its own is checked separately — a bound added to the pool
    and forgotten at a call site is the call that hangs."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> None:
        await store.record_settlement(a_settlement())
        await store.get_settlement(JOB)
        await store.get_settlement_by_task(TASK)
        opened = await store.open_dispute(a_dispute())
        await store.get_dispute(opened.id)
        await store.find_dispute(JOB, 0)
        await store.list_disputes_for_task(TASK)
        await store.append_status(opened.id, "upheld")
        await store.claim_refund(opened.id)
        await store.list_refund_claims()
        await store.release_refund_claim(opened.id)

    asyncio.run(go())

    # Every statement the store sent, the DDL included, named a bound.
    assert pool.statements and None not in pool.timeouts
    assert len(pool.timeouts) == len(pool.statements)
    # And the one call that takes a connection of its own bounded the wait for
    # it — longer than a command, so a waiter is never cut off before the
    # holder it is waiting for has been.
    assert pool.acquired == [dispute_store._POOL_ACQUIRE_TIMEOUT]
    assert dispute_store._POOL_ACQUIRE_TIMEOUT > dispute_store._POOL_COMMAND_TIMEOUT


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
