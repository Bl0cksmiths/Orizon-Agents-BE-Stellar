"""The three facts a buyer's dispute receipt needs that the record lacked (4.06).

Each is defaulted so that every record built today — by the store, by 4.02's
opening path, by every existing test — keeps building unchanged, and so that a
row read from before 4.06 answers "not known" rather than a wrong value.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from app.services import dispute_store
from app.services.dispute_store import DisputeRecord

# The receipt's columns and the SQL type each is added with — the type a
# DisputeRecord field of that name round-trips through.
RECEIPT_COLUMNS = (
    ("credited_usdc", "DOUBLE PRECISION"),
    ("updated_at", "DOUBLE PRECISION"),
    ("rating_confirmed", "BOOLEAN"),
)


def _opened() -> DisputeRecord:
    return DisputeRecord(
        id="dsp_0011223344556677",
        job_id_hex="00" * 16,
        task_id="tsk_receipt",
        step_index=0,
        agent_id="researcher",
        payer="G" + "A" * 55,
        reason="the summary cited nothing",
        status="open",
        charged_usdc=0.05,
        creditable_usdc=0.05,
        opened_at=1_700_000_000.0,
    )


def test_a_record_built_without_the_receipt_fields_says_not_known() -> None:
    record = _opened()
    assert record.credited_usdc is None
    assert record.updated_at is None
    assert record.rating_confirmed is None


def test_the_receipt_fields_trail_every_existing_field() -> None:
    # Appended, never inserted: a positional construction anywhere keeps its
    # meaning, and the store's row mapping keeps its order.
    names = [f.name for f in fields(DisputeRecord)]
    assert names[-3:] == ["credited_usdc", "updated_at", "rating_confirmed"]


# ── the schema: a table that already exists ───────────────────────────────


@pytest.mark.parametrize(("column", "sql_type"), RECEIPT_COLUMNS)
def test_each_receipt_column_is_added_to_a_table_that_already_exists(column: str, sql_type: str) -> None:
    """`dispute_events` has been live since 4.02, and CREATE TABLE IF NOT
    EXISTS does nothing whatever to a table that is already there. Each ALTER
    is the whole of the deploy for its column — the repo has no migration tool
    — and without it the first INSERT naming the column would fail every
    dispute write on a service that had already run once. Pinned the way 4.03
    pinned `note`.

    Nullable in both places, and deliberately: ADD COLUMN gives every row
    already written a NULL, which is exactly the "not known" a row from before
    4.06 must read as — a NOT NULL here would need a default, and any default
    would state a credit or a timestamp nobody recorded."""
    ddl = dispute_store._CREATE_DISPUTES_SQL

    assert f"ALTER TABLE dispute_events ADD COLUMN IF NOT EXISTS {column} {sql_type};" in ddl
    # And in the CREATE as well, so a fresh database gets it without the ALTER.
    (declared,) = [line.strip() for line in ddl.splitlines() if line.strip().startswith(f"{column} ")]
    assert declared.rstrip(",").split() == [column, *sql_type.split()]
