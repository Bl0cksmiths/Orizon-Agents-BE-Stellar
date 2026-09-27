"""What the in-memory dispute store may forget, and what it must not.

The store is bounded, so it forgets. The rule these tests hold it to is that it
never forgets a dispute while it still remembers the settlement the dispute
belongs to: a settlement and its disputes are kept or dropped as one unit. A
step whose settlement is held must always show every dispute ever recorded on
it; a step whose settlement is gone cannot be disputed at all.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random

import pytest

from app.services import dispute_store
from app.services.dispute_store import (
    DisputeRecord,
    DuplicateDisputeError,
    InMemoryDisputeStore,
    SettlementRecord,
    SettlementStep,
)

PAYER = "G" + "B" * 55
STORE_LOGGER = "app.services.dispute_store"
STEPS = tuple(
    SettlementStep(step_index=i, agent_id=f"agt_{i}", agent_name=None, price_usdc=1.0, delivered=True) for i in range(3)
)


def _job(n: int) -> str:
    return f"{n:064x}"


def _settlement(n: int) -> SettlementRecord:
    return SettlementRecord(
        task_id=f"task_{n}",
        payer=PAYER,
        auth_id_hex="a1" * 16,
        job_id_hex=_job(n),
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=3.0,
        steps=STEPS,
        settled_at=1_700_000_000.0 + n,
        window_closes_at=1_700_086_400.0 + n,
    )


def _dispute(dispute_id: str, n: int, step_index: int) -> DisputeRecord:
    return DisputeRecord(
        id=dispute_id,
        job_id_hex=_job(n),
        task_id=f"task_{n}",
        step_index=step_index,
        agent_id=f"agt_{step_index}",
        payer=PAYER,
        reason="the output was empty",
        status="open",
        charged_usdc=1.0,
        creditable_usdc=1.0,
        opened_at=1_700_000_100.0 + n,
    )


async def _assert_retained_settlements_keep_their_disputes(
    store: InMemoryDisputeStore, ledger: dict[str, DisputeRecord]
) -> None:
    """Every dispute ever opened on a settlement the store still holds is still held."""
    for dispute in ledger.values():
        if await store.get_settlement(dispute.job_id_hex) is None:
            continue
        assert await store.get_dispute(dispute.id) is not None, dispute.id
        found = await store.find_dispute(dispute.job_id_hex, dispute.step_index)
        assert found is not None and found.id == dispute.id, dispute.id


def test_a_paid_step_can_never_be_disputed_again_while_its_settlement_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credited dispute carries a refund hash: money moved for that step. For
    as long as the store still holds the settlement, it must still hold that
    dispute, and a second dispute on the step must be refused with the first."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 3)
    store = InMemoryDisputeStore()

    async def fill() -> None:
        await store.record_settlement(_settlement(0))
        await store.open_dispute(_dispute("dsp_paid", 0, 0))
        await store.append_status("dsp_paid", "credited", refund_tx="tx_refund", credited_usdc=1.0)
        await store.record_settlement(_settlement(1))
        await store.open_dispute(_dispute("dsp_1_0", 1, 0))
        await store.record_settlement(_settlement(2))
        await store.open_dispute(_dispute("dsp_2_0", 2, 0))
        await store.open_dispute(_dispute("dsp_1_1", 1, 1))
        await store.open_dispute(_dispute("dsp_2_1", 2, 1))

    asyncio.run(fill())

    if asyncio.run(store.get_settlement(_job(0))) is None:
        # Gone together: the step is not disputable at all, which is the safe
        # failure (the service answers `unknown_job`).
        assert asyncio.run(store.get_dispute("dsp_paid")) is None
        return

    original = asyncio.run(store.find_dispute(_job(0), 0))
    assert original is not None and original.id == "dsp_paid"
    assert original.status == "credited" and original.refund_tx == "tx_refund"
    with pytest.raises(DuplicateDisputeError) as refused:
        asyncio.run(store.open_dispute(_dispute("dsp_again", 0, 0)))
    assert refused.value.existing.id == "dsp_paid"
    assert asyncio.run(store.get_dispute("dsp_again")) is None


def test_settlements_leave_with_their_disputes_and_never_without_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pushed past its cap, the store drops the oldest settlement and every
    (finished) dispute under it together, and the dropped step cannot be
    disputed anew."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 2)
    store = InMemoryDisputeStore()

    async def fill() -> None:
        for n in range(3):
            await store.record_settlement(_settlement(n))
            for step in range(3):
                await store.open_dispute(_dispute(f"dsp_{n}_{step}", n, step))
                await store.append_status(f"dsp_{n}_{step}", "rejected", note="delivered as asked")

    asyncio.run(fill())

    assert asyncio.run(store.get_settlement(_job(0))) is None
    assert all(asyncio.run(store.get_dispute(f"dsp_0_{step}")) is None for step in range(3))
    for n in (1, 2):
        assert asyncio.run(store.get_settlement(_job(n))) is not None
        assert all(asyncio.run(store.get_dispute(f"dsp_{n}_{step}")) is not None for step in range(3))


@pytest.mark.parametrize("seed", range(40))
def test_every_retained_settlement_keeps_every_dispute_ever_opened_on_it(
    monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """The invariant as a property: after any sequence of settlements, disputes
    and transitions pushed well past the caps, no retained settlement is missing
    a dispute that was ever opened on it — and memory stays bounded by what is
    still owed."""
    cap = 4
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", cap)
    rng = random.Random(seed)
    store = InMemoryDisputeStore()
    ledger: dict[str, DisputeRecord] = {}
    settled: list[int] = []

    async def run() -> None:
        for op in range(200):
            action = rng.random()
            if action < 0.3 or not settled:
                n = len(settled)
                await store.record_settlement(_settlement(n))
                settled.append(n)
            elif action < 0.85:
                # Any job ever settled, held or not, and any of its steps.
                n = rng.choice(settled)
                step = rng.randrange(len(STEPS))
                dispute = _dispute(f"dsp_{op}", n, step)
                try:
                    ledger[dispute.id] = await store.open_dispute(dispute)
                except DuplicateDisputeError as dup:
                    assert dup.existing.id in ledger
            else:
                held = [d for d in ledger.values() if d.id in store._disputes]
                if held:
                    target = rng.choice(held)
                    await store.append_status(target.id, "credited", refund_tx=f"tx_{op}", credited_usdc=1.0)

            await _assert_retained_settlements_keep_their_disputes(store, ledger)
            if action < 0.85:
                # Straight after an insertion the store is within its cap,
                # unless everything it could have dropped still owes something.
                assert len(store._settlements) <= cap or all(store._is_pinned(j) for j in store._settlements)
            assert len(store._disputes) <= len(store._disputes_by_job) * len(STEPS)

    asyncio.run(run())


def test_a_dispute_on_a_job_with_no_settlement_is_bounded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service never files a dispute without a settlement, but the store
    must not grow without bound if something does."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 2)
    store = InMemoryDisputeStore()

    async def fill() -> None:
        for n in range(5):
            await store.open_dispute(_dispute(f"dsp_{n}", n, 0))
            await store.append_status(f"dsp_{n}", "rejected", note="no such workflow")

    asyncio.run(fill())

    assert sorted(store._disputes) == ["dsp_3", "dsp_4"]
    assert asyncio.run(store.find_dispute(_job(0), 0)) is None


def test_the_order_disputes_are_opened_in_does_not_decide_what_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disputing an old settlement does not refresh it: settlements are still
    dropped oldest-settled first, and the newest settlement of a task still
    wins."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 2)
    store = InMemoryDisputeStore()

    async def fill() -> None:
        await store.record_settlement(_settlement(0))
        await store.record_settlement(dataclasses.replace(_settlement(1), task_id="task_0"))
        await store.open_dispute(_dispute("dsp_0", 0, 0))
        await store.append_status("dsp_0", "credited", refund_tx="tx_refund", credited_usdc=1.0)
        await store.record_settlement(_settlement(2))

    asyncio.run(fill())

    assert asyncio.run(store.get_settlement(_job(0))) is None
    assert asyncio.run(store.get_dispute("dsp_0")) is None
    newest = asyncio.run(store.get_settlement_by_task("task_0"))
    assert newest is not None and newest.job_id_hex == _job(1)


# ── what is still owed is never shed ──────────────────────────────────────


async def _bring_to(store: InMemoryDisputeStore, dispute_id: str, status: str) -> None:
    """Walk a freshly opened dispute to `status` the way the service does."""
    if status in ("upheld", "crediting"):
        await store.append_status(dispute_id, "upheld", expected_status="open")
    if status == "crediting":
        assert await store.claim_refund(dispute_id) is not None


@pytest.mark.parametrize("status", ["open", "upheld", "crediting"])
def test_an_unfinished_dispute_keeps_its_settlement_far_past_the_cap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: str
) -> None:
    """An open dispute is owed an adjudication, an upheld one a payment, and a
    crediting one a reconciliation of a transfer whose outcome may be unknown.
    The settlement it hangs on outlives the cap many times over, while the
    settlements around it that owe nothing are shed as usual."""
    monkeypatch.setattr(dispute_store, "_MAX_IN_MEMORY", 2)
    store = InMemoryDisputeStore()

    async def fill() -> None:
        await store.record_settlement(_settlement(0))
        await store.open_dispute(_dispute("dsp_owed", 0, 0))
        await _bring_to(store, "dsp_owed", status)
        for n in range(1, 20):
            await store.record_settlement(_settlement(n))

    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        asyncio.run(fill())

    assert list(store._settlements) == [_job(0), _job(19)]
    owed = asyncio.run(store.find_dispute(_job(0), 0))
    assert owed is not None and owed.id == "dsp_owed" and owed.status == status
    claims = [c.dispute_id for c in asyncio.run(store.list_refund_claims())]
    assert claims == (["dsp_owed"] if status == "crediting" else [])
    assert not [r for r in caplog.records if r.name == STORE_LOGGER and r.levelno >= logging.ERROR]
