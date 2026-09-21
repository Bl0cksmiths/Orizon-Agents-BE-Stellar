"""The three facts a buyer's dispute receipt needs that the record lacked (4.06).

Each is defaulted so that every record built today — by the store, by 4.02's
opening path, by every existing test — keeps building unchanged, and so that a
row read from before 4.06 answers "not known" rather than a wrong value.
"""

from __future__ import annotations

from dataclasses import fields

from app.services.dispute_store import DisputeRecord


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
