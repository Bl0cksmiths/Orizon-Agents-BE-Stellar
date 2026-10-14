"""The in-memory store's field-by-field merge on `append_status`, pinned one field at a time.

Each optional field a transition names replaces what the dispute held; each one
it leaves as None keeps it. `resolved_at` is stamped once from the clock and
never moved; `rating_confirmed` can be set to True but never taken back; a
finished dispute drops its refund claim and an unfinished one keeps it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from app.services import dispute_store
from app.services.dispute_store import DisputeRecord, InMemoryDisputeStore

CLOCK = 1_700_000_500.0


def a_dispute(**overrides: Any) -> DisputeRecord:
    base = DisputeRecord(
        id="dsp_fields",
        job_id_hex="ef" * 32,
        task_id="task_fields",
        step_index=0,
        agent_id="agt_writer",
        payer="G" + "C" * 55,
        reason="the summary was empty",
        status="open",
        charged_usdc=1.5,
        creditable_usdc=1.5,
        opened_at=1_700_000_100.0,
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispute_store.time, "time", lambda: CLOCK)


def transitions(*calls: dict[str, Any], start: DisputeRecord | None = None) -> list[DisputeRecord | None]:
    store = InMemoryDisputeStore()

    async def go() -> list[DisputeRecord | None]:
        opened = await store.open_dispute(start or a_dispute())
        return [await store.append_status(opened.id, **call) for call in calls]

    return asyncio.run(go())


FIELDS: dict[str, tuple[Any, Any]] = {
    "refund_tx": ("tx_first", "tx_second"),
    "rating_tx": ("tx_rate_1", "tx_rate_2"),
    "note": ("first note", "second note"),
    "credited_usdc": (0.5, 0.75),
    "inflight_usdc": (0.25, 0.4),
}


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_a_named_field_replaces_and_an_unnamed_one_keeps(field: str) -> None:
    first, second = FIELDS[field]
    set_once, kept, replaced = transitions(
        {"status": "upheld", field: first},
        {"status": "upheld"},
        {"status": "upheld", field: second},
    )
    assert getattr(set_once, field) == first
    assert getattr(kept, field) == first
    assert getattr(replaced, field) == second


def test_an_unnamed_field_on_a_fresh_dispute_stays_none() -> None:
    (updated,) = transitions({"status": "upheld"})
    assert updated is not None
    assert (updated.refund_tx, updated.rating_tx, updated.note) == (None, None, None)
    assert (updated.credited_usdc, updated.inflight_usdc, updated.rating_confirmed) == (None, None, None)


def test_the_resolution_is_stamped_from_the_clock_once_and_never_moved() -> None:
    stamped, kept, named = transitions(
        {"status": "upheld"},
        {"status": "upheld"},
        {"status": "upheld", "resolved_at": 1.0},
    )
    assert stamped.resolved_at == CLOCK and stamped.updated_at == CLOCK
    assert kept.resolved_at == CLOCK
    assert named.resolved_at == 1.0


def test_a_resolution_already_held_is_kept_over_the_clock() -> None:
    (updated,) = transitions({"status": "upheld"}, start=a_dispute(resolved_at=0.0))
    assert updated.resolved_at == 0.0


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ((None,), None),
        ((False,), False),
        ((True,), True),
        ((False, None), False),
        ((False, True), True),
        ((True, False), True),
        ((True, None), True),
    ],
)
def test_a_rating_confirmation_is_recorded_and_never_taken_back(
    answers: tuple[bool | None, ...], expected: bool | None
) -> None:
    results = transitions(*({"status": "credited", "rating_confirmed": a} for a in answers))
    assert results[-1].rating_confirmed is expected


@pytest.mark.parametrize(("status", "dropped"), [("credited", True), ("rejected", True), ("upheld", False)])
def test_only_a_finished_dispute_drops_its_refund_claim(status: str, dropped: bool) -> None:
    store = InMemoryDisputeStore()

    async def go() -> bool:
        opened = await store.open_dispute(a_dispute(status="upheld"))
        assert await store.claim_refund(opened.id) is not None
        await store.append_status(opened.id, status)
        return opened.id in store._refund_claims

    assert asyncio.run(go()) is not dropped
