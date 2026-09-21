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
from typing import Any

from app.services.dispute_store import (
    DisputeRecord,
    InMemoryDisputeStore,
    SettlementRecord,
    SettlementStep,
)

JOB = "ab" * 32
OTHER_JOB = "cd" * 32
TASK = "task_alpha"
PAYER = "G" + "B" * 55
AGENT = "agt_writer"

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
