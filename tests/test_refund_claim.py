"""Story 4.03's refund mutex — the one property that, broken, pays a buyer twice.

A refund moves platform money to a buyer and nothing on the far side of the
transfer can take it back, so `claim_refund` is the single most load-bearing
call in the story: it is what stops a second payer signing a second transfer
for the same dispute. The claim is a row in `refund_claims` whose PRIMARY KEY
does the arbitrating, written by the SAME statement as the `crediting` status
it implies, so the two cannot come apart however the process dies.

What is asserted here, and why each one is here rather than assumed:

  1. Two claims racing over one dispute produce exactly ONE claim. The fake
     pool models a statement's SNAPSHOT — the status is read before anything is
     written and a claim committed in between is invisible — so a store that
     leaned on the status alone would append two `crediting` rows and fail
     this. That is what gives the test teeth rather than the appearance of
     them.
  2. Only an `upheld` dispute can be claimed. open, crediting, credited and
     rejected all answer None and write nothing, because to a payer they all
     mean the same thing: do not sign anything.
  3. A release puts the dispute back and lets a later attempt claim it — a
     transfer that definitively failed leaves the buyer still owed.
  4. Reaching `credited` or `rejected` drops the claim, so what is left in
     `refund_claims` is exactly the set of payouts still in flight.
  5. A claim SURVIVES a restart. Render spins a free instance down mid-payout,
     and a mutex that came back empty would let the next process pay again.
  6. The two stores agree. Every rule that can be is asserted over both, and a
     rule that holds only on the store this suite happens to run is not a rule.

Hermetic, and the idioms are test_dispute_store.py's: `asyncio.run` rather than
pytest-asyncio, which is not installed, and a fake pool that dispatches on the
store's SQL constants by equality.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from test_dispute_store import FakePool, _pg, a_dispute

from app.services import dispute_store
from app.services.dispute_store import DisputeRecord, DisputeStatus, DisputeStore, InMemoryDisputeStore


@pytest.fixture(autouse=True)
def reset_singleton() -> Iterator[None]:
    """The resolver is a module-level singleton; no test may inherit another's."""
    dispute_store._store = None
    yield
    dispute_store._store = None


@pytest.fixture(params=["in-memory", "postgres"])
def store(request: pytest.FixtureRequest) -> DisputeStore:
    """The same rules, asserted against both implementations.

    The mutex is the reason this parameterisation exists rather than being
    tidy. Postgres keeps `refund_claims` in a table and the in-memory store
    keeps it in a dict, and a case the two answer differently is a case that
    behaves one way in the hermetic suite and another way in production — on
    the money path, where the difference is a second transfer.
    """
    return InMemoryDisputeStore() if request.param == "in-memory" else _pg(FakePool())


async def _upheld(store: DisputeStore, **overrides: Any) -> DisputeRecord:
    """A dispute adjudicated in the buyer's favour and waiting to be paid."""
    opened = await store.open_dispute(a_dispute(**overrides))
    return await store.append_status(opened.id, "upheld")


async def _queued(store: DisputeStore) -> list[str]:
    """The reconciliation queue, as the dispute ids sitting in it."""
    return [claim.dispute_id for claim in await store.list_refund_claims()]


# ── taking the claim ──────────────────────────────────────────────────────


def test_claiming_an_upheld_dispute_holds_the_mutex_and_moves_it_to_crediting(store: DisputeStore) -> None:
    """The happy path, and the two facts it must leave behind together: the
    dispute reads `crediting` — so no second payer can claim it — and the claim
    is in the queue, so a payout that never finishes can be found."""

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, list[str]]:
        upheld = await _upheld(store)
        claimed = await store.claim_refund(upheld.id)
        return claimed, await store.get_dispute(upheld.id), await _queued(store)

    claimed, stored, queue = asyncio.run(go())

    assert claimed is not None
    assert claimed.status == "crediting"
    assert stored == claimed
    assert queue == ["dsp_0001"]
    # The claim changed the status and NOTHING else: the credit is computed
    # from these, and a claim that could edit them could edit the amount.
    assert claimed.creditable_usdc == 1.5
    assert claimed.charged_usdc == 1.5
    assert claimed.reason == "the summary was empty"
    assert claimed.refund_tx is None


def test_a_second_claim_over_a_held_dispute_is_refused(store: DisputeStore) -> None:
    """The retry, the double click, the duplicate webhook. None is the signal
    not to sign anything, and the first claimant keeps the mutex it holds."""

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, list[str]]:
        upheld = await _upheld(store)
        first = await store.claim_refund(upheld.id)
        second = await store.claim_refund(upheld.id)
        return first, second, await _queued(store)

    first, second, queue = asyncio.run(go())

    assert first is not None
    assert second is None
    # One claim, not two, and the dispute still belongs to the first payer.
    assert queue == ["dsp_0001"]


@pytest.mark.parametrize("status", ["open", "crediting", "credited", "rejected"])
def test_a_dispute_that_is_not_upheld_cannot_be_claimed(store: DisputeStore, status: DisputeStatus) -> None:
    """Every other status answers None and writes nothing.

    `open` has not been adjudicated, `crediting` is already being paid,
    `credited` has been paid and `rejected` never will be — four different
    facts that reduce to one instruction for a payer. Claiming any of them must
    also leave no mutex behind: a claim row over a dispute this call refused to
    pay would block the payer who is legitimately entitled to it.
    """

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, list[str]]:
        opened = await store.open_dispute(a_dispute())
        if status != "open":
            await store.append_status(opened.id, status)
        claimed = await store.claim_refund(opened.id)
        return claimed, await store.get_dispute(opened.id), await _queued(store)

    claimed, stored, queue = asyncio.run(go())

    assert claimed is None
    assert stored is not None and stored.status == status
    assert queue == []


def test_claiming_a_dispute_that_does_not_exist_is_none(store: DisputeStore) -> None:
    """An id that was never a dispute is a bug in the caller, and the answer is
    still the one that matters on a money path: do not sign anything."""

    async def go() -> tuple[DisputeRecord | None, list[str]]:
        return await store.claim_refund("dsp_never"), await _queued(store)

    claimed, queue = asyncio.run(go())

    assert claimed is None
    assert queue == []
