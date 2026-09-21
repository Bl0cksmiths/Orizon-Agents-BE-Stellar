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


# ── the statements, column by column ──────────────────────────────────────


def _written(sql: str) -> dict[str, str]:
    """What an `INSERT INTO dispute_events (...) SELECT ... FROM latest` writes
    into each column, as the SQL spells it.

    Paired POSITIONALLY, which is how Postgres pairs them, and split on the
    commas at parenthesis depth zero so a COALESCE stays one expression. The
    fake pool cannot stand in for this: it implements its own COALESCE, so a
    statement whose arguments were swapped would pass every behavioural test
    against it and still write the wrong value in production.
    """
    named, rest = sql.split("INSERT INTO dispute_events (", 1)[1].split(")", 1)
    selected = rest.split("SELECT", 1)[1].split("\nFROM latest", 1)[0]
    expressions: list[str] = []
    depth, current = 0, ""
    for char in selected:
        if char == "," and depth == 0:
            expressions.append(current)
            current = ""
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        current += char
    expressions.append(current)
    columns = [column.strip() for column in named.split(",")]
    return dict(zip(columns, (" ".join(e.split()) for e in expressions), strict=True))


def test_a_transition_carries_the_receipt_forward_and_dates_its_own_row() -> None:
    """`credited_usdc` and `rating_confirmed` are COALESCEd with the caller's
    value FIRST. COALESCE returns its first non-NULL argument and FALSE is not
    NULL, so a caller naming TRUE replaces a recorded FALSE — the confirmation
    a timed-out rating is owed once it lands — while a caller naming nothing
    passes NULL and keeps what is there. The other order would make the first
    answer permanent.

    `updated_at` is the clock outright, never COALESCEd, and the same reading
    ($7) the resolution time falls back to."""
    written = _written(dispute_store._APPEND_STATUS_SQL)

    assert written["credited_usdc"] == "COALESCE($8::double precision, latest.credited_usdc)"
    assert written["rating_confirmed"] == "COALESCE($9::boolean, latest.rating_confirmed)"
    assert written["updated_at"] == "$7::double precision"
    assert written["resolved_at"] == "COALESCE($6::double precision, latest.resolved_at, $7::double precision)"


@pytest.mark.parametrize(
    ("name", "status"),
    [("_CLAIM_REFUND_SQL", "'crediting'"), ("_RELEASE_REFUND_CLAIM_SQL", "'upheld'")],
    ids=["claim", "release"],
)
def test_a_claim_and_a_release_change_only_the_status_and_when_it_changed(name: str, status: str) -> None:
    """4.03's rule for the two mutex transitions, now with the one exception
    4.06 makes: every column is copied from `latest` verbatim — the credited
    amount and the rating confirmation included — except the status, the
    moment the status changed, and the opening flag no transition may claim.

    Asserted as the complement, so a column added later that either statement
    forgot to carry forward fails here rather than quietly writing NULL."""
    written = _written(getattr(dispute_store, name))

    moved = {column: expression for column, expression in written.items() if expression != f"latest.{column}"}
    assert moved == {"status": status, "updated_at": "$2::double precision", "opening": "FALSE"}


def test_no_statement_dates_a_row_by_the_database_clock() -> None:
    """`updated_at` is stamped by every writer, so every writer is a place a
    `now()` could creep in — and the claim and the release, which 4.02's
    version of this rule never covered, now write a timestamp too. Every
    `_*_SQL` constant is checked, found by NAME, so a statement added later is
    covered without anyone remembering to list it."""
    statements = {name: sql for name, sql in vars(dispute_store).items() if name.endswith("_SQL")}

    # The introspection finding nothing would make every assertion below vacuous.
    assert {"_INSERT_DISPUTE_SQL", "_APPEND_STATUS_SQL", "_CLAIM_REFUND_SQL", "_RELEASE_REFUND_CLAIM_SQL"} <= (
        statements.keys()
    )
    for name, sql in statements.items():
        upper = sql.upper()
        for clock in ("NOW()", "CURRENT_TIMESTAMP", "LOCALTIMESTAMP", "CLOCK_TIMESTAMP", "STATEMENT_TIMESTAMP"):
            assert clock not in upper, (name, clock)
