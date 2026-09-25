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
from test_dispute_durability import process
from test_dispute_store import FakePool, _pg, a_dispute, a_settlement

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


# ── giving it back ────────────────────────────────────────────────────────


def test_a_release_restores_upheld_and_a_later_claim_succeeds(store: DisputeStore) -> None:
    """What a transfer that definitively FAILED has to leave behind.

    The buyer is still owed, so the dispute has to end up back where a second
    attempt can find it — and the mutex has to be gone with it, or that second
    attempt would be refused and the credit would never be paid.
    """

    async def go() -> tuple[DisputeRecord | None, list[str], DisputeRecord | None, list[str]]:
        upheld = await _upheld(store)
        await store.claim_refund(upheld.id)
        released = await store.release_refund_claim(upheld.id)
        after_release = await _queued(store)
        reclaimed = await store.claim_refund(upheld.id)
        return released, after_release, reclaimed, await _queued(store)

    released, after_release, reclaimed, after_reclaim = asyncio.run(go())

    assert released is not None and released.status == "upheld"
    assert after_release == []
    assert reclaimed is not None and reclaimed.status == "crediting"
    assert after_reclaim == ["dsp_0001"]


def test_a_failed_attempts_hash_never_follows_the_dispute_into_the_next_one(store: DisputeStore) -> None:
    """The refund hash is cleared on a release and on the claim after it.

    The sequence is 4.03's reconciliation path: a transfer times out and its
    in-flight hash is recorded on the `crediting` row; an operator finds it
    never settled and releases the claim; the next uphold claims again. Before
    4.06 the release and the re-claim both copied that hash forward, so for the
    whole second attempt the buyer's receipt linked a transaction that FAILED
    as the refund in flight. A release only ever happens when nothing landed,
    and a claim starts a payout with no transaction yet — neither row has a
    refund hash to show.
    """

    async def go() -> tuple[DisputeRecord, DisputeRecord | None, DisputeRecord | None]:
        upheld = await _upheld(store)
        await store.claim_refund(upheld.id)
        timed_out = await store.append_status(upheld.id, "crediting", refund_tx="tx_never_settled")
        released = await store.release_refund_claim(upheld.id)
        reclaimed = await store.claim_refund(upheld.id)
        return timed_out, released, reclaimed

    timed_out, released, reclaimed = asyncio.run(go())

    assert timed_out.refund_tx == "tx_never_settled"  # the in-flight hash was on record
    assert released is not None and released.refund_tx is None
    assert reclaimed is not None and reclaimed.status == "crediting"
    assert reclaimed.refund_tx is None


@pytest.mark.parametrize("status", ["open", "upheld", "credited", "rejected"])
def test_a_dispute_that_is_not_crediting_cannot_be_released(store: DisputeStore, status: DisputeStatus) -> None:
    """Only a payout in flight can be handed back.

    `upheld` is the case that matters: a release that answered it would let a
    caller rewind a dispute it never claimed, and on this path rewinding means
    making a dispute somebody else may be paying claimable by a second payer.
    """

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, list[str]]:
        opened = await store.open_dispute(a_dispute())
        if status != "open":
            await store.append_status(opened.id, status)
        released = await store.release_refund_claim(opened.id)
        return released, await store.get_dispute(opened.id), await _queued(store)

    released, stored, queue = asyncio.run(go())

    assert released is None
    assert stored is not None and stored.status == status
    assert queue == []


def test_releasing_twice_does_not_rewind_a_second_time(store: DisputeStore) -> None:
    """The second release finds a dispute that is no longer `crediting` and
    says so, rather than appending another transition to a settled trail."""

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None]:
        upheld = await _upheld(store)
        await store.claim_refund(upheld.id)
        return await store.release_refund_claim(upheld.id), await store.release_refund_claim(upheld.id)

    first, second = asyncio.run(go())

    assert first is not None and first.status == "upheld"
    assert second is None


def test_releasing_a_dispute_that_does_not_exist_is_none(store: DisputeStore) -> None:
    assert asyncio.run(store.release_refund_claim("dsp_never")) is None


# ── ending the payout ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["credited", "rejected"])
def test_finishing_a_dispute_drops_its_claim(store: DisputeStore, status: DisputeStatus) -> None:
    """A dispute that has finished is not mid-payout, so it leaves the queue —
    and it leaves without the caller remembering to do anything, because a
    reconciliation queue that lists finished work is one operators learn to
    ignore."""

    async def go() -> tuple[list[str], DisputeRecord, list[str], DisputeRecord | None]:
        upheld = await _upheld(store)
        await store.claim_refund(upheld.id)
        held = await _queued(store)
        finished = await store.append_status(upheld.id, status, refund_tx="tx_refund")
        return held, finished, await _queued(store), await store.claim_refund(upheld.id)

    held, finished, queue, reclaimed = asyncio.run(go())

    assert held == ["dsp_0001"]
    assert finished.status == status
    assert queue == []
    # Dropping the mutex does not make the dispute payable again. The status
    # refuses now, and it refuses forever.
    assert reclaimed is None


def test_a_claim_does_not_move_the_moment_the_dispute_resolved(store: DisputeStore) -> None:
    """`crediting` is not a resolution, and neither is the `upheld` a release
    restores.

    A transition that re-dated the dispute would record the moment a payout was
    ATTEMPTED as the moment the buyer was made whole — and since resolved_at is
    stamped once and never moved, the row that finally credits them would carry
    that wrong moment too.
    """

    async def go() -> tuple[DisputeRecord, DisputeRecord | None, DisputeRecord | None]:
        opened = await store.open_dispute(a_dispute())
        upheld = await store.append_status(opened.id, "upheld", resolved_at=1_700_009_999.0)
        claimed = await store.claim_refund(opened.id)
        return upheld, claimed, await store.release_refund_claim(opened.id)

    upheld, claimed, released = asyncio.run(go())

    assert upheld.resolved_at == 1_700_009_999.0
    assert claimed is not None and claimed.resolved_at == 1_700_009_999.0
    assert released is not None and released.resolved_at == 1_700_009_999.0


# ── the race the mutex exists for ─────────────────────────────────────────


def test_two_concurrent_claims_produce_exactly_one_claim(store: DisputeStore) -> None:
    """The race, run as a race.

    Both calls are in flight at once. Against the fake pool each statement
    takes its snapshot, yields, and only then writes — which is what a real
    statement does, and it is why the status BOTH claimants read says `upheld`.
    Nothing except the PRIMARY KEY can separate them at that point, so a store
    that decided on the status alone would pay this buyer twice and fail here.
    """

    async def go() -> tuple[list[DisputeRecord | None], list[str], DisputeRecord | None]:
        upheld = await _upheld(store)
        raced = await asyncio.gather(store.claim_refund(upheld.id), store.claim_refund(upheld.id))
        return list(raced), await _queued(store), await store.get_dispute(upheld.id)

    raced, queue, stored = asyncio.run(go())

    assert len([result for result in raced if result is not None]) == 1
    assert len([result for result in raced if result is None]) == 1
    assert queue == ["dsp_0001"]
    assert stored is not None and stored.status == "crediting"


def test_the_losing_claimant_writes_no_row() -> None:
    """The loser's INSERT selects through `claim`, which returned nothing, so
    it appends no event row either.

    One `crediting` transition in the trail rather than a pair — two would read
    to anyone answering a chargeback as two payouts, which is precisely the
    thing that must not have happened.
    """
    pool = FakePool()
    store = _pg(pool)

    async def go() -> list[DisputeRecord | None]:
        upheld = await _upheld(store)
        return list(await asyncio.gather(store.claim_refund(upheld.id), store.claim_refund(upheld.id)))

    raced = asyncio.run(go())

    assert len([result for result in raced if result is not None]) == 1
    assert [row["status"] for row in pool.disputes] == ["open", "upheld", "crediting"]
    assert list(pool.claims) == ["dsp_0001"]


# ── atomicity, on the shape of the call ───────────────────────────────────


def test_a_claim_is_one_statement_and_so_is_a_release() -> None:
    """Asserted structurally, because behaviour cannot show it.

    A single statement is its own transaction: the claim row and the
    `crediting` row are written together or not at all. Two statements have a
    window between them, and a process that died in that window — Render spins
    a free instance down whenever it idles — would leave a claim held over a
    dispute still reading `upheld`. No later claim can pay that buyer, because
    the mutex refuses every claimant and a release refuses a dispute that is
    not `crediting`. There is no way back from it, so the test is on the shape
    of the call rather than only on what it returns.
    """
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[list[str], list[str]]:
        upheld = await _upheld(store)
        mark = len(pool.statements)
        await store.claim_refund(upheld.id)
        claim = pool.statements[mark:]
        mark = len(pool.statements)
        await store.release_refund_claim(upheld.id)
        return claim, pool.statements[mark:]

    claim, release = asyncio.run(go())

    assert claim == [dispute_store._CLAIM_REFUND_SQL]
    assert release == [dispute_store._RELEASE_REFUND_CLAIM_SQL]


def test_finishing_a_dispute_drops_the_claim_in_the_same_statement() -> None:
    """The same argument one step later. A DELETE issued after the transition
    is a statement that can fail to run, and `refund_claims` would then hold a
    lock over a dispute that has already been paid — no money lost, but a
    reconciliation queue listing finished work is one nobody reads.

    The row lock is the statement before it, inside the same transaction: it
    changes nothing and writes nothing, and the DELETE and the event row are
    still the one statement they have to be."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> list[str]:
        upheld = await _upheld(store)
        await store.claim_refund(upheld.id)
        mark = len(pool.statements)
        await store.append_status(upheld.id, "credited", refund_tx="tx_refund")
        return pool.statements[mark:]

    assert asyncio.run(go()) == [dispute_store._LOCK_DISPUTE_SQL, dispute_store._APPEND_STATUS_SQL]
    assert pool.claims == {}


def test_a_claim_left_over_a_payable_dispute_blocks_instead_of_paying_twice() -> None:
    """The one state the mutex cannot repair, asserted so that it reads as a
    decision rather than an accident.

    Nothing in this store produces it — every write keeps the claim and the
    status together in one statement — but a row inserted from outside, or a
    dispute dragged back to `upheld` by hand, would leave a claim held over a
    dispute that reads payable. The claim wins and the refund is REFUSED, which
    is the direction that fails safe: the alternative is a second transfer out
    of the platform wallet. A release cannot clear it either, because dropping
    a claim over a dispute that is not `crediting` is exactly the move that
    would let a second payer in while the first is still signing.

    What makes that liveable is that the row is in the reconciliation queue,
    where an operator can see it — and the way out is the only safe one there
    is: decide from the chain whether the buyer was paid, and record that
    decision, which drops the claim with it.
    """
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, list[str]]:
        upheld = await _upheld(store)
        pool.claims[upheld.id] = 1_700_000_500.0
        blocked = await store.claim_refund(upheld.id)
        return blocked, await store.release_refund_claim(upheld.id), await _queued(store)

    blocked, released, queue = asyncio.run(go())

    assert blocked is None
    assert released is None
    assert queue == ["dsp_0001"]

    resolved = asyncio.run(store.append_status("dsp_0001", "credited", refund_tx="tx_found_on_chain"))

    assert resolved.status == "credited"
    assert asyncio.run(_queued(store)) == []


def test_a_refused_verdict_leaves_the_mutex_over_a_live_payout(store: DisputeStore) -> None:
    """The reject that ran over a transfer already on the network.

    An adjudicator reads a dispute while it is `open`, and by the time their
    `rejected` reaches the store it has been upheld, claimed and paid for. The
    precondition refuses the verdict — but the mutex is dropped by a CTE beside
    that INSERT, and a CTE runs whether or not the INSERT writes anything. So
    the refusal used to cost the claim anyway: the buyer disappeared from the
    reconciliation queue while their transfer was still in flight, and the
    dispute was left claimable by the next payer along."""

    async def go() -> tuple[DisputeRecord | None, DisputeRecord | None, list[str]]:
        upheld = await _upheld(store)
        await store.claim_refund(upheld.id)
        refused = await store.append_status(upheld.id, "rejected", note="not upheld", expected_status="open")
        return refused, await store.get_dispute(upheld.id), await _queued(store)

    refused, current, queue = asyncio.run(go())

    assert refused is None
    # The payout is untouched: still mid-flight, still held, still findable.
    assert current is not None and current.status == "crediting"
    assert queue == ["dsp_0001"]


def test_a_verdict_on_a_dispute_with_no_history_drops_no_claim_row() -> None:
    """A claim row whose dispute this store has never heard of is the one row
    an operator most needs to see — it is money that may have left the wallet
    with nothing to account for it. Writing a verdict against the id used to
    delete it silently, because the DELETE only ever looked at the status being
    written."""
    pool = FakePool()
    pool.claims["dsp_ghost"] = 1_700_000_500.0
    store = _pg(pool)

    with pytest.raises(KeyError):
        asyncio.run(store.append_status("dsp_ghost", "credited", refund_tx="tx_guess"))

    assert list(pool.claims) == ["dsp_ghost"]


def test_the_mutex_delete_is_gated_on_the_transition_being_written() -> None:
    """Asserted on the SQL, because the two halves of the statement can only
    disagree here: the INSERT selects through the precondition and the DELETE
    is a separate CTE that has to repeat it, or the mutex moves for a
    transition the trail never recorded."""
    sql = dispute_store._APPEND_STATUS_SQL
    delete = sql.split("finished AS (")[1].split("\n)\n")[0]

    assert "DELETE FROM refund_claims" in delete
    assert "$10::text IS NULL OR latest.status = $10::text" in delete


def test_a_verdict_racing_a_claim_does_not_drop_the_claim_it_lost_to() -> None:
    """The two halves of finding the wedge, in one interleaving.

    A payer claims the dispute and starts signing; an adjudicator's `rejected`,
    computed from a read taken while it was still `open`, arrives in the middle
    of that. The verdict is refused — the dispute has moved — and the claim
    protecting the transfer has to survive the refusal, or the buyer drops off
    the reconciliation queue while their money is still in flight."""
    pool = FakePool()
    store = _pg(pool)

    async def go() -> tuple[Any, Any, DisputeRecord | None, list[str]]:
        upheld = await _upheld(store)
        claimed, refused = await asyncio.gather(
            store.claim_refund(upheld.id),
            store.append_status(upheld.id, "rejected", note="not upheld", expected_status="open"),
        )
        return claimed, refused, await store.get_dispute(upheld.id), await _queued(store)

    claimed, refused, current, queue = asyncio.run(go())

    assert claimed is not None and claimed.status == "crediting"
    assert refused is None
    assert current is not None and current.status == "crediting"
    assert queue == ["dsp_0001"]


# ── across a restart ──────────────────────────────────────────────────────


def test_a_payout_in_flight_when_the_process_died_is_still_blocked_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this table is durable for.

    A refund is claimed, the transfer goes out, and the instance is spun down
    before anything comes back — Render does that to a free service whenever it
    idles, and a submitted transaction may still land. The next process must
    not be able to claim that dispute: it cannot know whether the money moved,
    and the one thing it must not do is send it again.

    Modelled the way tests/test_dispute_durability.py models a restart. The
    DATABASE survives the boundary; everything the process held — the store
    object, its pool, the singleton — does not.
    """
    database = FakePool()

    with process(monkeypatch, database) as store:
        asyncio.run(store.record_settlement(a_settlement()))
        opened = asyncio.run(store.open_dispute(a_dispute()))
        asyncio.run(store.append_status(opened.id, "upheld"))
        claimed = asyncio.run(store.claim_refund(opened.id))
        assert claimed is not None and claimed.status == "crediting"
        claimed_at = database.claims[opened.id]

    assert dispute_store._store is None
    assert database.closed == 1

    with process(monkeypatch, database) as store:
        restored = asyncio.run(store.get_dispute(opened.id))
        assert restored is not None and restored.status == "crediting"
        # Still blocked: the mutex came back held, so this process refuses to
        # pay a buyer the last one may already have paid.
        assert asyncio.run(store.claim_refund(opened.id)) is None
        # And still visible, with the moment it was taken — which is how an
        # operator knows how long this buyer has been waiting on it.
        queue = asyncio.run(store.list_refund_claims())
        assert [claim.dispute_id for claim in queue] == [opened.id]
        assert queue[0].claimed_at == claimed_at


def test_a_stuck_payout_can_still_be_released_after_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocked is not stranded.

    Once a human has established that the transfer definitively failed, the
    dispute goes back to `upheld` in a later process exactly as it would have
    in the one that claimed it — and the buyer, who is still owed, can be paid.
    """
    database = FakePool()

    with process(monkeypatch, database) as store:
        opened = asyncio.run(store.open_dispute(a_dispute()))
        asyncio.run(store.append_status(opened.id, "upheld"))
        assert asyncio.run(store.claim_refund(opened.id)) is not None

    with process(monkeypatch, database) as store:
        released = asyncio.run(store.release_refund_claim(opened.id))
        assert released is not None and released.status == "upheld"
        assert asyncio.run(store.list_refund_claims()) == ()
        reclaimed = asyncio.run(store.claim_refund(opened.id))
        assert reclaimed is not None and reclaimed.status == "crediting"


# ── the two stores, side by side ──────────────────────────────────────────


async def _payout_trace(store: DisputeStore) -> list[tuple[str, str | None, tuple[str, ...]]]:
    """One dispute through every move a payout can make.

    Each step records what the call answered and what the reconciliation queue
    held afterwards, so two implementations can be compared on the whole path
    rather than one assertion at a time.
    """
    upheld = await _upheld(store)
    trace: list[tuple[str, str | None, tuple[str, ...]]] = []

    async def note(label: str, record: DisputeRecord | None) -> None:
        held = await store.list_refund_claims()
        trace.append((label, None if record is None else record.status, tuple(c.dispute_id for c in held)))

    await note("upheld", upheld)
    await note("claim", await store.claim_refund(upheld.id))
    await note("claim again", await store.claim_refund(upheld.id))
    await note("release", await store.release_refund_claim(upheld.id))
    await note("release again", await store.release_refund_claim(upheld.id))
    await note("reclaim", await store.claim_refund(upheld.id))
    await note("credit", await store.append_status(upheld.id, "credited", refund_tx="tx_refund"))
    await note("claim after credit", await store.claim_refund(upheld.id))
    return trace


def test_the_two_stores_take_the_same_path_through_a_payout() -> None:
    """The tests above assert each rule on both stores; this one asserts they
    agree STEP BY STEP, including on what the queue holds in between.

    That is where a store which inferred the mutex from the status instead of
    keeping one would drift without failing anything else — and the expected
    trace is written out rather than only compared, so two stores agreeing on
    the wrong answer fails as loudly as two that disagree.
    """
    in_memory = asyncio.run(_payout_trace(InMemoryDisputeStore()))
    postgres = asyncio.run(_payout_trace(_pg(FakePool())))

    assert in_memory == postgres
    assert in_memory == [
        ("upheld", "upheld", ()),
        ("claim", "crediting", ("dsp_0001",)),
        ("claim again", None, ("dsp_0001",)),
        ("release", "upheld", ()),
        ("release again", None, ()),
        ("reclaim", "crediting", ("dsp_0001",)),
        ("credit", "credited", ()),
        ("claim after credit", None, ()),
    ]


def test_an_adjudicators_note_survives_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejection is the outcome most likely to be contested, so the argument
    for it has to outlive the process that made it exactly as the buyer's
    `reason` does. A note held only in a log line is a note nobody can produce
    weeks later, which is the asymmetry the column closes.

    It sits beside the claim's restart test because the restart idiom it needs
    lives in tests/test_dispute_durability.py, which story 4.03 does not own.
    """
    database = FakePool()
    note = "  step 0 delivered; the brief did not ask for charts\n"

    with process(monkeypatch, database) as store:
        opened = asyncio.run(store.open_dispute(a_dispute()))
        asyncio.run(store.append_status(opened.id, "rejected", note=note))

    with process(monkeypatch, database) as store:
        restored = asyncio.run(store.get_dispute(opened.id))
        assert restored is not None
        assert restored.status == "rejected"
        assert restored.note == note
        # The opening row never carried one, so the trail still shows a dispute
        # opened on the buyer's reason alone and refused later, with the
        # platform's answer on its own line.
        assert [row["note"] for row in database.disputes] == [None, note]
