"""ADR 0015's settlement fields through the real SQL: planned and authorized stroops.

`planned_stroops` lives inside the JSONB `steps` column; `authorized_stroops`
is a new NULLABLE column added with `ADD COLUMN IF NOT EXISTS`, NUMERIC because
the escrow's amount is an i128. A table that predates it must gain it on the
next start, and every row already in it must read "not recorded".
"""

from __future__ import annotations

import pytest
from pg_support import execute, run, settlements
from test_dispute_store import JOB, a_settlement

from app.services.dispute_store import PostgresDisputeStore, SettlementStep

STEPS = (
    SettlementStep(0, "agt_01h8", "w.seed", 0.0, True, "did it", paid_usdc=0.0, planned_stroops=120_000),
    SettlementStep(1, "ext_op", "w.ext", 0.02, True, "did more", paid_usdc=0.02, planned_stroops=200_000),
)


@pytest.fixture
def pg(pg_dsn: str) -> PostgresDisputeStore:
    return PostgresDisputeStore(pg_dsn)


def test_planned_and_authorized_stroops_round_trip(pg: PostgresDisputeStore, pg_dsn: str) -> None:
    # Past what a BIGINT holds: the escrow's max_amount is an i128.
    record = a_settlement(steps=STEPS, settled_usdc=0.02, authorized_stroops=2**100)

    async def go():
        await pg.record_settlement(record)
        return await pg.get_settlement(JOB), await settlements(pg_dsn)

    read, rows = run(pg, go())

    assert read == record
    assert read is not None and read.authorized_stroops == 2**100
    assert [s.planned_stroops for s in read.steps] == [120_000, 200_000]


def test_a_table_from_before_the_column_gains_it_and_old_rows_read_none(pg_dsn: str) -> None:
    first = PostgresDisputeStore(pg_dsn)

    async def before():
        await first.record_settlement(a_settlement())  # creates the table
        await execute(pg_dsn, "ALTER TABLE workflow_settlements DROP COLUMN authorized_stroops")
        await execute(
            pg_dsn,
            "UPDATE workflow_settlements SET steps = $1::jsonb",
            '[{"step_index": 0, "agent_id": "a", "agent_name": null, "price_usdc": 0.05, "delivered": true}]',
        )

    run(first, before())

    later = PostgresDisputeStore(pg_dsn)  # the next deploy: its schema pass adds the column

    async def after():
        return await later.get_settlement(JOB)

    read = run(later, after())

    assert read is not None
    assert read.authorized_stroops is None
    assert [s.planned_stroops for s in read.steps] == [None]
