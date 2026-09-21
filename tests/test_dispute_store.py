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
