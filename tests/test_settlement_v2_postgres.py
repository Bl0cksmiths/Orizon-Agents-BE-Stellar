"""A v2 settlement's per-step payouts and receipts, through the real SQL.

They live inside the existing JSONB `steps` column, so the table's shape does
not change and no migration statement runs: what has to hold is that a v2
record round-trips whole, that a row written before ADR 0010 still reads (as
a v1 record, with no per-step payment), and that the in-memory store agrees.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from pg_support import execute, run, settlements
from test_dispute_store import JOB, a_settlement

from app.services.dispute_store import InMemoryDisputeStore, PostgresDisputeStore, SettlementStep

V2_STEPS = (
    SettlementStep(0, "agt_01h8", "w.seed", 0.0, True, "did it", paid_usdc=0.0, unpaid_reason="no_onchain_owner"),
    SettlementStep(1, "ext_op", "w.ext", 0.02, True, "did more", paid_usdc=0.02, receipt_id_hex="a0" * 16),
    SettlementStep(2, "ext_two", None, 0.05, False, None, paid_usdc=0.0),
)


@pytest.fixture
def pg(pg_dsn: str) -> PostgresDisputeStore:
    return PostgresDisputeStore(pg_dsn)


def test_a_v2_settlement_round_trips_through_postgres(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    record = a_settlement(steps=V2_STEPS, settled_usdc=0.02)

    async def go():
        await pg.record_settlement(record)
        return await pg.get_settlement(JOB), await settlements(pg_dsn)

    read, rows = run(pg, go())

    assert read == record
    stored = json.loads(rows[0]["steps"])
    assert [s["receipt_id_hex"] for s in stored] == [None, "a0" * 16, None]
    assert [s["unpaid_reason"] for s in stored] == ["no_onchain_owner", None, None]


def test_a_row_written_before_v2_still_reads_as_a_v1_record(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    """The keys are absent on every row already in the table: they read as
    None — "no per-step payment" — never as a zero payment."""
    old_steps = json.dumps(
        [{"step_index": 0, "agent_id": "a", "agent_name": None, "price_usdc": 0.05, "delivered": True}]
    )

    async def go():
        await pg.record_settlement(a_settlement())  # creates the table
        await execute(pg_dsn, "UPDATE workflow_settlements SET steps = $1::jsonb", old_steps)
        return await pg.get_settlement(JOB)

    read = run(pg, go())

    assert read is not None
    [step] = read.steps
    assert (step.paid_usdc, step.receipt_id_hex, step.unpaid_reason, step.output_summary) == (None, None, None, None)


def test_the_in_memory_store_keeps_the_same_fields() -> None:
    record = a_settlement(steps=V2_STEPS)
    memory = InMemoryDisputeStore()

    async def go():
        await memory.record_settlement(record)
        return await memory.get_settlement(JOB)

    assert asyncio.run(go()) == dataclasses.replace(record)
