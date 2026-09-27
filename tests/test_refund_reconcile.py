"""The refund reconcile sweep (app/services/refund_reconcile.py).

Every safety property the sweep claims is proven here against BOTH stores, and
the ones that are about concurrency against a real Postgres (conftest
`pg_dsn`), because a compare-and-set is only as good as the SQL that runs it.

The chain is faked at `sc.get_transaction`, one layer below the sweep, and
answers with real envelopes: the ADR 0002 testnet refund captured read-only
(tests/refund_ledger_fixtures.py), and envelopes built here with the SDK and
never signed or sent. Nothing touches a network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from pg_support import events, run
from refund_ledger_fixtures import (
    REAL_AMOUNT_STROOPS,
    REAL_ASSET_SAC,
    REAL_NOT_FOUND_ANSWER,
    REAL_PAYER,
    REAL_REFUND_ENVELOPE_XDR,
    REAL_REFUND_HASH,
    REAL_SETTLER,
)
from stellar_sdk import Account, Keypair, Network, TransactionBuilder, scval
from test_dispute_store import a_dispute, a_settlement

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_store, dispute_svc, refund_reconcile, refund_svc
from app.services.dispute_store import (
    DisputeRecord,
    DisputeStore,
    InMemoryDisputeStore,
    PostgresDisputeStore,
    RefundClaim,
)
from app.services.dispute_svc import DisputeError
from app.services.refund_reconcile import (
    EXPIRY_MARGIN_SECONDS,
    MIN_CLAIM_AGE_SECONDS,
    REFUND_TX_TIMEOUT_SECONDS,
    Decision,
    reconcile_claim,
)

SETTLER = Keypair.random().public_key
PAYER = Keypair.random().public_key
SAC = REAL_ASSET_SAC
_ANOTHER_CONTRACT = "CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA"


@pytest.fixture(autouse=True)
def _no_pass_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass run here must not show up on another test's /readiness."""
    monkeypatch.setattr(refund_reconcile, "_last", None)


@pytest.fixture(params=["in-memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request: pytest.FixtureRequest) -> Iterator[DisputeStore]:
    """The store the sweep and the service both reach through the singleton."""
    chosen: DisputeStore = (
        InMemoryDisputeStore()
        if request.param == "in-memory"
        else PostgresDisputeStore(request.getfixturevalue("pg_dsn"))
    )
    dispute_store._store = chosen
    yield chosen
    dispute_store._store = None


@pytest.fixture
def pg(pg_dsn: str) -> Iterator[PostgresDisputeStore]:
    chosen = PostgresDisputeStore(pg_dsn)
    dispute_store._store = chosen
    yield chosen
    dispute_store._store = None


def a_refund_envelope(
    *, stroops: int, to: str = PAYER, source: str = SETTLER, contract: str = SAC, function: str = "transfer"
) -> tuple[str, str]:
    """A refund transaction built the way `_send_server_signed` builds one, and
    NOT signed: its hash covers no signature, so it is the hash the real one
    would have. Returns (hash, envelope XDR)."""
    tx = (
        TransactionBuilder(
            source_account=Account(source, 1),
            network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
            base_fee=100,
        )
        .append_invoke_contract_function_op(
            contract_id=contract,
            function_name=function,
            parameters=[scval.to_address(source), scval.to_address(to), scval.to_int128(stroops)],
        )
        .set_timeout(REFUND_TX_TIMEOUT_SECONDS)
        .build()
    )
    return tx.hash_hex(), tx.to_xdr()


def an_answer(tx_hash: str, status: str, *, envelope: str | None = None, **window: int) -> sc.LedgerTransaction:
    """What the RPC says about `tx_hash`, over a window that covers any claim
    taken in this test run unless the test says otherwise."""
    now = int(time.time())
    return sc.LedgerTransaction(
        tx_hash=tx_hash,
        status=status,
        latest_ledger=window.get("latest_ledger", 5_000_000),
        latest_ledger_close_time=window.get("latest_ledger_close_time", now),
        oldest_ledger=window.get("oldest_ledger", 4_000_000),
        oldest_ledger_close_time=window.get("oldest_ledger_close_time", now - 7 * 86_400),
        ledger=4_999_000 if status != "NOT_FOUND" else None,
        envelope_xdr=envelope,
    )


class Chain:
    """`sc.get_transaction`, scripted per hash, with every question recorded."""

    def __init__(self) -> None:
        self.answers: dict[str, sc.LedgerTransaction | BaseException | Callable[[], sc.LedgerTransaction]] = {}
        self.asked: list[str] = []

    def __call__(self, tx_hash: str) -> sc.LedgerTransaction:
        self.asked.append(tx_hash)
        answer = self.answers[tx_hash]
        if isinstance(answer, BaseException):
            raise answer
        return answer() if callable(answer) else answer


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> Chain:
    fake = Chain()
    monkeypatch.setattr(sc, "get_transaction", fake)
    return fake


async def in_flight(store: DisputeStore, tx_hash: str | None, *, inflight_usdc: float | None = 0.05) -> DisputeRecord:
    """A dispute parked the way a timed-out uphold leaves one: `crediting`,
    claim held, the in-flight hash and amount recorded."""
    await store.record_settlement(a_settlement(payer=PAYER, settled_usdc=3.75))
    opened = await store.open_dispute(a_dispute(payer=PAYER))
    await store.append_status(opened.id, "upheld", expected_status="open")
    claimed = await store.claim_refund(opened.id)
    assert claimed is not None
    if tx_hash is None and inflight_usdc is None:
        return claimed
    parked = await store.append_status(
        opened.id, "crediting", refund_tx=tx_hash, inflight_usdc=inflight_usdc, expected_status="crediting"
    )
    assert parked is not None
    return parked


async def the_claim(store: DisputeStore) -> RefundClaim:
    (claim,) = await store.list_refund_claims()
    return claim


async def decide(store: DisputeStore, *, age: float = MIN_CLAIM_AGE_SECONDS + 1) -> Decision:
    claim = await the_claim(store)
    return await reconcile_claim(claim, now=claim.claimed_at + age, settler=SETTLER, asset_sac=SAC)


async def credited_rows(store: DisputeStore, dsn: str | None, dispute_id: str) -> int:
    if dsn is None:
        current = await store.get_dispute(dispute_id)
        return int(current is not None and current.status == "credited")
    return sum(1 for row in await events(dsn, dispute_id) if row["status"] == "credited")


# ── SUCCESS ───────────────────────────────────────────────────────────────


def test_a_landed_transfer_is_recorded_credited_with_what_the_transaction_moved(
    store: DisputeStore, chain: Chain
) -> None:
    """The amount is read off the chain's copy of the transaction — 0.0512
    here — never off the dispute's promise (`creditable_usdc`, 1.5) or any
    other estimate. The claim is dropped by the same statement."""
    tx_hash, envelope = a_refund_envelope(stroops=512_000)
    chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)

    async def go() -> tuple[Decision, DisputeRecord | None, tuple[RefundClaim, ...]]:
        parked = await in_flight(store, tx_hash, inflight_usdc=0.0512)
        decision = await decide(store)
        return decision, await store.get_dispute(parked.id), await store.list_refund_claims()

    decision, current, claims = run(store, go())

    assert decision.action == "credited" and decision.chain == "SUCCESS"
    assert current is not None
    assert (current.status, current.refund_tx, current.credited_usdc) == ("credited", tx_hash, 0.0512)
    assert current.creditable_usdc == 1.5
    # The rating is left to the next uphold: nothing was signed here.
    assert current.rating_tx is None
    assert claims == ()


def test_the_real_testnet_refund_decodes_to_the_amount_it_moved(
    store: DisputeStore, chain: Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0002's proof refund, as Horizon holds it: 540000 stroops from the
    settler to the buyer over the asset SAC. Fed through as the SUCCESS it was."""
    chain.answers[REAL_REFUND_HASH] = an_answer(REAL_REFUND_HASH, "SUCCESS", envelope=REAL_REFUND_ENVELOPE_XDR)

    async def go() -> DisputeRecord | None:
        await store.record_settlement(a_settlement(payer=REAL_PAYER))
        opened = await store.open_dispute(a_dispute(payer=REAL_PAYER))
        await store.append_status(opened.id, "upheld")
        await store.claim_refund(opened.id)
        await store.append_status(opened.id, "crediting", refund_tx=REAL_REFUND_HASH, inflight_usdc=0.054)
        claim = await the_claim(store)
        decision = await reconcile_claim(
            claim, now=claim.claimed_at + 3600, settler=REAL_SETTLER, asset_sac=REAL_ASSET_SAC
        )
        assert decision.action == "credited", decision
        return await store.get_dispute(opened.id)

    current = run(store, go())

    assert current is not None and current.credited_usdc == REAL_AMOUNT_STROOPS / 10_000_000 == 0.054


def test_two_sweeps_racing_one_success_record_it_exactly_once(
    pg: PostgresDisputeStore, pg_dsn: str, chain: Chain
) -> None:
    """Two passes — two processes, in production — read the same SUCCESS and
    both write. The compare-and-set on `crediting` (and on the hash) lets one
    land; the other finds the verdict already recorded and writes nothing. One
    `credited` row, not two."""
    tx_hash, envelope = a_refund_envelope(stroops=500_000)
    chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)

    async def go() -> tuple[list[str], DisputeRecord]:
        parked = await in_flight(pg, tx_hash)
        claim = await the_claim(pg)
        decisions = await asyncio.gather(
            *(reconcile_claim(claim, now=claim.claimed_at + 3600, settler=SETTLER, asset_sac=SAC) for _ in range(4))
        )
        return sorted(d.action for d in decisions), parked

    actions, parked = run(pg, go())

    assert actions == ["already_settled", "already_settled", "already_settled", "credited"]
    assert asyncio.run(credited_rows(pg, pg_dsn, parked.id)) == 1
    assert [row["status"] for row in asyncio.run(events(pg_dsn, parked.id))][-1] == "credited"


def test_a_sweep_racing_a_manual_uphold_pays_nothing_and_records_the_credit_once(
    pg: PostgresDisputeStore, pg_dsn: str, chain: Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adjudicator upholds the dispute at the moment the sweep records its
    credit. Whichever runs first, no transfer is signed: before the credit the
    uphold meets `crediting` and refuses (`refund_in_flight`), after it the
    uphold meets `credited` and only re-attempts the rating. The credit lands
    on the record once."""
    tx_hash, envelope = a_refund_envelope(stroops=500_000)
    chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)
    signed: list[Any] = []

    async def _never_signed(*args: Any) -> dict[str, Any]:  # pragma: no cover - must never run
        signed.append(args)
        return {"status": "SUCCESS", "hash": "tx_second_payment"}

    async def _rating_only(credited: DisputeRecord, **_: Any) -> DisputeRecord:
        return credited

    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC)
    monkeypatch.setattr(refund_svc, "execute_refund", _never_signed)
    monkeypatch.setattr(dispute_svc, "_retry_rating", _rating_only)

    async def uphold(dispute_id: str) -> str:
        try:
            return (await dispute_svc.uphold(dispute_id)).status
        except DisputeError as refused:
            return refused.code

    async def go() -> tuple[str, str, str]:
        parked = await in_flight(pg, tx_hash)
        claim = await the_claim(pg)
        sweep, adjudicated = await asyncio.gather(
            reconcile_claim(claim, now=claim.claimed_at + 3600, settler=SETTLER, asset_sac=SAC),
            uphold(parked.id),
        )
        return parked.id, sweep.action, adjudicated

    dispute_id, sweep, adjudicated = run(pg, go())

    assert signed == []
    assert sweep == "credited"
    assert adjudicated in ("refund_in_flight", "credited")
    assert asyncio.run(credited_rows(pg, pg_dsn, dispute_id)) == 1


@pytest.mark.parametrize("verdict", ["SUCCESS", "FAILED", "NOT_FOUND"])
def test_a_verdict_never_touches_a_dispute_claimed_again_over_another_transfer(
    store: DisputeStore, chain: Chain, verdict: str
) -> None:
    """The lookup is slow; while it is out, the dispute is released and claimed
    again, and a NEW transfer is in flight. A verdict about the old hash —
    landed, failed, or expired — must not close or release the dispute over
    the new one: nothing is written, and the claim protecting the new
    transfer stays held. Released here, the next uphold would pay the buyer a
    second time the moment the new transfer lands."""
    old_hash, old_envelope = a_refund_envelope(stroops=500_000)
    moved: list[str] = []

    async def go() -> tuple[Decision, DisputeRecord | None, list[str]]:
        parked = await in_flight(store, old_hash)

        async def _meanwhile() -> None:
            await store.release_refund_claim(parked.id)
            await store.claim_refund(parked.id)
            await store.append_status(parked.id, "crediting", refund_tx="tx_new", inflight_usdc=0.05)
            moved.append(parked.id)

        loop = asyncio.get_running_loop()

        def _answer() -> sc.LedgerTransaction:
            asyncio.run_coroutine_threadsafe(_meanwhile(), loop).result(timeout=10)
            if verdict == "NOT_FOUND":
                return an_answer(old_hash, verdict, latest_ledger_close_time=int(time.time()) + 86_400)
            return an_answer(old_hash, verdict, envelope=old_envelope)

        chain.answers[old_hash] = _answer
        decision = await decide(store)
        return decision, await store.get_dispute(parked.id), [c.dispute_id for c in await store.list_refund_claims()]

    decision, current, held = run(store, go())

    assert moved and decision.action == "lost_race"
    assert current is not None and (current.status, current.refund_tx, current.credited_usdc) == (
        "crediting",
        "tx_new",
        None,
    )
    assert held == [current.id]


# ── FAILED ────────────────────────────────────────────────────────────────


def test_a_failed_transfer_releases_and_a_later_uphold_can_pay(
    store: DisputeStore, chain: Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    tx_hash, envelope = a_refund_envelope(stroops=500_000)
    chain.answers[tx_hash] = an_answer(tx_hash, "FAILED", envelope=envelope)
    paid: list[Any] = []

    async def _pays(buyer: str, amount_usdc: float, *, dispute_id: str | None = None) -> dict[str, Any]:
        paid.append((buyer, amount_usdc))
        return {"status": "SUCCESS", "hash": "tx_second_attempt"}

    async def _rated(credited: DisputeRecord, *_: Any, **__: Any) -> DisputeRecord:
        return credited

    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC)
    monkeypatch.setattr(settings, "max_refund_usdc", 10.0)
    monkeypatch.setattr(refund_svc, "execute_refund", _pays)
    monkeypatch.setattr(dispute_svc, "_rate_credited", _rated)

    async def go() -> tuple[Decision, DisputeRecord | None, DisputeRecord]:
        parked = await in_flight(store, tx_hash)
        decision = await decide(store)
        released = await store.get_dispute(parked.id)
        return decision, released, await dispute_svc.uphold(parked.id)

    decision, released, paid_now = run(store, go())

    assert decision.action == "released" and decision.chain == "FAILED"
    assert released is not None and (released.status, released.refund_tx, released.inflight_usdc) == (
        "upheld",
        None,
        None,
    )
    assert paid == [(PAYER, 1.5)]
    assert (paid_now.status, paid_now.refund_tx, paid_now.credited_usdc) == ("credited", "tx_second_attempt", 1.5)


# ── NOT_FOUND ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("past_the_bound_by", [-3600, -EXPIRY_MARGIN_SECONDS, 0])
def test_not_found_before_max_time_and_its_margin_never_releases(
    store: DisputeStore, chain: Chain, past_the_bound_by: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Up to and including the instant the ledger reaches maxTime plus the
    margin, an absent transaction may still land, and the claim stays held.

    The store's clock is held to whole seconds here so that "the instant" is a
    ledger close time that can be named exactly."""
    whole_seconds = SimpleNamespace(time=lambda: float(int(time.time())))
    monkeypatch.setattr(dispute_store, "time", whole_seconds)

    async def go() -> tuple[Decision, DisputeRecord | None, list[str]]:
        parked = await in_flight(store, "tx_unconfirmed")
        assert parked.updated_at is not None
        bound = int(parked.updated_at) + REFUND_TX_TIMEOUT_SECONDS + EXPIRY_MARGIN_SECONDS
        chain.answers["tx_unconfirmed"] = an_answer(
            "tx_unconfirmed", "NOT_FOUND", latest_ledger_close_time=bound + past_the_bound_by
        )
        decision = await decide(store)
        return decision, await store.get_dispute(parked.id), [c.dispute_id for c in await store.list_refund_claims()]

    decision, current, held = run(store, go())

    assert decision.action == "pending"
    assert current is not None and (current.status, current.refund_tx) == ("crediting", "tx_unconfirmed")
    assert held == [current.id]


def test_not_found_after_max_time_and_its_margin_releases(store: DisputeStore, chain: Chain) -> None:
    async def go() -> tuple[Decision, DisputeRecord | None, tuple[RefundClaim, ...]]:
        parked = await in_flight(store, "tx_expired")
        assert parked.updated_at is not None
        bound = parked.updated_at + REFUND_TX_TIMEOUT_SECONDS + EXPIRY_MARGIN_SECONDS
        chain.answers["tx_expired"] = an_answer("tx_expired", "NOT_FOUND", latest_ledger_close_time=int(bound) + 2)
        decision = await decide(store)
        return decision, await store.get_dispute(parked.id), await store.list_refund_claims()

    decision, current, claims = run(store, go())

    assert decision.action == "expired_released" and decision.chain == "NOT_FOUND"
    assert current is not None and (current.status, current.refund_tx) == ("upheld", None)
    assert claims == ()


def test_the_expiry_is_read_off_the_ledgers_clock_not_this_process(
    store: DisputeStore, chain: Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This process's clock says a day has passed; the ledger's says the
    transaction is still inside its window. The ledger wins, so nothing moves."""

    async def go() -> Decision:
        parked = await in_flight(store, "tx_young_on_chain")
        assert parked.updated_at is not None
        chain.answers["tx_young_on_chain"] = an_answer(
            "tx_young_on_chain", "NOT_FOUND", latest_ledger_close_time=int(parked.updated_at)
        )
        return await decide(store, age=86_400)

    assert run(store, go()).action == "pending"


# ── what is never touched ─────────────────────────────────────────────────


def _unchanged(store: DisputeStore, decision_for: Callable[[DisputeRecord], Any]) -> tuple[Decision, bool]:
    """Run one decision over a parked dispute; say whether anything moved."""

    async def go() -> tuple[Decision, bool]:
        parked = await in_flight(store, "tx_on_record")
        decision_for(parked)
        before = (await store.get_dispute(parked.id), await store.list_refund_claims())
        decision = await decide(store)
        after = (await store.get_dispute(parked.id), await store.list_refund_claims())
        return decision, before == after

    return run(store, go())


@pytest.mark.parametrize(
    "error", [ConnectionError("rpc unreachable"), TimeoutError("read timed out"), ValueError("bad json")]
)
def test_an_rpc_error_never_releases_and_never_credits(store: DisputeStore, chain: Chain, error: Exception) -> None:
    chain.answers["tx_on_record"] = error

    decision, unchanged = _unchanged(store, lambda _: None)

    assert decision.action == "rpc_error" and unchanged


def test_a_hash_older_than_the_rpc_s_history_never_releases_and_never_credits(
    store: DisputeStore, chain: Chain
) -> None:
    """The real answer about a refund that LANDED, a week later: NOT_FOUND,
    over a history that starts after the claim. Even with the ledger long past
    maxTime, it is left for a human — "forgotten" is not "never landed"."""

    def _real_gap(parked: DisputeRecord) -> None:
        window = {
            "latest_ledger": REAL_NOT_FOUND_ANSWER["latestLedger"],
            "latest_ledger_close_time": int(time.time()) + 30 * 86_400,
            "oldest_ledger": REAL_NOT_FOUND_ANSWER["oldestLedger"],
            # The history starts after this claim was taken.
            "oldest_ledger_close_time": int(time.time()) + 60,
        }
        chain.answers["tx_on_record"] = an_answer("tx_on_record", "NOT_FOUND", **window)

    decision, unchanged = _unchanged(store, _real_gap)

    assert decision.action == "history_gap" and unchanged


def test_an_unknown_rpc_status_never_releases(store: DisputeStore, chain: Chain) -> None:
    chain.answers["tx_on_record"] = dataclasses.replace(an_answer("tx_on_record", "NOT_FOUND"), status="PENDING")

    decision, unchanged = _unchanged(store, lambda _: None)

    assert decision.action == "rpc_error" and unchanged


@pytest.mark.parametrize(
    ("status", "envelope_for"),
    [
        ("SUCCESS", lambda: a_refund_envelope(stroops=500_000, to=Keypair.random().public_key)),
        ("SUCCESS", lambda: a_refund_envelope(stroops=500_000, source=Keypair.random().public_key)),
        ("SUCCESS", lambda: a_refund_envelope(stroops=500_000, contract=_ANOTHER_CONTRACT)),
        ("FAILED", lambda: a_refund_envelope(stroops=500_000, function="mint")),
        ("FAILED", lambda: a_refund_envelope(stroops=500_000, to=Keypair.random().public_key)),
    ],
    ids=[
        "landed-to-someone-else",
        "landed-from-someone-else",
        "landed-over-another-contract",
        "failed-not-a-transfer",
        "failed-to-someone-else",
    ],
)
def test_a_transaction_that_is_not_this_dispute_s_refund_is_never_acted_on(
    store: DisputeStore, chain: Chain, status: str, envelope_for: Callable[[], tuple[str, str]]
) -> None:
    """The hash on record is real and the chain answers for it, but its
    transaction pays somebody else, or from somebody else, or is not a SAC
    transfer at all — so it says nothing about THIS refund, whether it
    succeeded or failed. A FAILED stranger released here would unlock a
    second payment; a SUCCESS stranger recorded would close the dispute over
    a payment the buyer never got."""
    tx_hash, envelope = envelope_for()
    chain.answers[tx_hash] = an_answer(tx_hash, status, envelope=envelope)

    async def go() -> tuple[Decision, bool]:
        parked = await in_flight(store, tx_hash)
        before = (await store.get_dispute(parked.id), await store.list_refund_claims())
        decision = await decide(store)
        return decision, before == (await store.get_dispute(parked.id), await store.list_refund_claims())

    decision, unchanged = run(store, go())

    assert decision.action == "not_this_refund", decision
    assert unchanged


def _tagged(dispute_id: str) -> str:
    """The payer's M address carrying `dispute_id`'s refund id, as a refund pays it."""
    return scval.from_address(sc.muxed_addr(PAYER, refund_svc.refund_muxed_id(dispute_id))).address


@pytest.mark.parametrize(
    ("status", "action"),
    [("SUCCESS", "credited"), ("FAILED", "released")],
    ids=["landed", "failed"],
)
def test_a_refund_tagged_with_this_dispute_is_this_dispute_s_refund(
    store: DisputeStore, chain: Chain, status: str, action: str
) -> None:
    """A refund pays the payer's G address muxed with the dispute's refund id
    (`refund_svc.refund_muxed_id`), so the chain's copy names an M address, not
    the payer's G. That is this dispute's refund and is settled like one."""
    tx_hash, envelope = a_refund_envelope(stroops=500_000, to=_tagged(a_dispute().id))
    chain.answers[tx_hash] = an_answer(tx_hash, status, envelope=envelope)

    async def go() -> Decision:
        await in_flight(store, tx_hash)
        return await decide(store)

    decision = run(store, go())

    assert decision.action == action, decision


def test_a_refund_tagged_with_another_dispute_is_never_acted_on(store: DisputeStore, chain: Chain) -> None:
    """The same payer muxed with ANOTHER dispute's id is that dispute's credit.
    Recording it here would close this dispute over money paid for another."""
    tx_hash, envelope = a_refund_envelope(stroops=500_000, to=_tagged("dsp_" + "f" * 32))
    chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)

    async def go() -> tuple[Decision, bool]:
        parked = await in_flight(store, tx_hash)
        before = (await store.get_dispute(parked.id), await store.list_refund_claims())
        decision = await decide(store)
        return decision, before == (await store.get_dispute(parked.id), await store.list_refund_claims())

    decision, unchanged = run(store, go())

    assert decision.action == "not_this_refund", decision
    assert unchanged


def test_an_answer_about_another_transaction_is_never_acted_on(store: DisputeStore, chain: Chain) -> None:
    """The RPC's envelope must hash to the hash that was asked about: an
    answer carrying some other transaction — even a perfectly good refund to
    this very payer — is not an answer about the one on record."""
    _, envelope = a_refund_envelope(stroops=500_000)

    decision, unchanged = _unchanged(
        store, lambda _: chain.answers.update(tx_on_record=an_answer("tx_on_record", "SUCCESS", envelope=envelope))
    )

    assert decision.action == "not_this_refund" and "hashes to" in decision.detail
    assert unchanged


def test_a_chain_amount_that_disagrees_with_the_record_is_left_for_a_human(store: DisputeStore, chain: Chain) -> None:
    tx_hash, envelope = a_refund_envelope(stroops=499_999)

    async def go() -> tuple[Decision, DisputeRecord | None]:
        parked = await in_flight(store, tx_hash, inflight_usdc=0.05)
        chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)
        return await decide(store), await store.get_dispute(parked.id)

    decision, current = run(store, go())

    assert decision.action == "amount_mismatch"
    assert current is not None and current.status == "crediting"


def test_a_claim_with_no_hash_is_never_touched(store: DisputeStore, chain: Chain) -> None:
    """Nothing to look up, so nothing is looked up: no RPC call, no write. The
    human's step — reading the settler's history — is not the sweep's."""

    async def go() -> tuple[Decision, bool]:
        parked = await in_flight(store, None, inflight_usdc=0.05)
        before = (await store.get_dispute(parked.id), await store.list_refund_claims())
        decision = await decide(store, age=30 * 86_400)
        after = (await store.get_dispute(parked.id), await store.list_refund_claims())
        return decision, before == after

    decision, unchanged = run(store, go())

    assert decision.action == "no_hash" and unchanged
    assert chain.asked == []


def test_a_young_claim_is_not_even_looked_up(store: DisputeStore, chain: Chain) -> None:
    async def go() -> Decision:
        await in_flight(store, "tx_just_sent")
        return await decide(store, age=MIN_CLAIM_AGE_SECONDS - 1)

    assert run(store, go()).action == "young"
    assert chain.asked == []


def test_the_timeout_the_sweep_assumes_is_the_one_refunds_are_built_with() -> None:
    """The NOT_FOUND rule is only sound while REFUND_TX_TIMEOUT_SECONDS is at
    least the timeout the refund is actually built with."""
    source = inspect.getsource(sc._send_server_signed)
    assert f".set_timeout({REFUND_TX_TIMEOUT_SECONDS})" in source


# ── the pass ──────────────────────────────────────────────────────────────


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present")
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC)
    monkeypatch.setattr(sc, "signer_public_key", lambda: SETTLER)
    monkeypatch.setattr(
        sc, "contract_ids", lambda: dataclasses.replace(sc.ContractIds("", "", "", "", ""), asset_sac=SAC)
    )
    monkeypatch.setattr(refund_reconcile, "MIN_CLAIM_AGE_SECONDS", 0)
    monkeypatch.setattr(refund_reconcile, "_last", None)


def test_a_pass_reports_what_it_did_by_action(store: DisputeStore, chain: Chain, configured: None) -> None:
    tx_hash, envelope = a_refund_envelope(stroops=500_000)
    chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)

    async def go() -> refund_reconcile.SweepReport:
        await in_flight(store, tx_hash)
        return await refund_reconcile.sweep_once()

    report = run(store, go())

    assert report.skipped is None and report.outcomes == {"credited": 1}
    assert refund_reconcile.status()["last_outcomes"] == {"credited": 1}


def test_only_one_pass_runs_at_a_time(store: DisputeStore, chain: Chain, configured: None) -> None:
    """A second pass started while one is out on the chain does not queue
    behind it or read the same claims again: it returns at once."""
    released = threading.Event()

    def _slow() -> sc.LedgerTransaction:
        released.wait(timeout=10)
        return an_answer("tx_slow", "NOT_FOUND")

    chain.answers["tx_slow"] = _slow

    async def go() -> tuple[refund_reconcile.SweepReport, refund_reconcile.SweepReport]:
        await in_flight(store, "tx_slow")
        first = asyncio.create_task(refund_reconcile.sweep_once())
        while not chain.asked:
            await asyncio.sleep(0.01)
        second = await refund_reconcile.sweep_once()
        released.set()
        return await first, second

    first, second = run(store, go())

    assert first.skipped is None and first.outcomes == {"pending": 1}
    assert second.skipped == "a pass is already running"
    assert chain.asked == ["tx_slow"]


def test_a_slow_rpc_never_blocks_the_event_loop(store: DisputeStore, chain: Chain, configured: None) -> None:
    """The lookup runs in a worker thread, so the loop keeps serving while it
    is out — and a lookup that outlives its bound is an rpc_error, not a hang."""
    released = threading.Event()

    def _stuck() -> sc.LedgerTransaction:
        released.wait(timeout=10)
        return an_answer("tx_stuck", "NOT_FOUND")

    chain.answers["tx_stuck"] = _stuck

    async def go() -> tuple[refund_reconcile.SweepReport, int]:
        await in_flight(store, "tx_stuck")
        ticks = 0

        async def _tick() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_tick())
        try:
            report = await refund_reconcile.sweep_once()
        finally:
            ticker.cancel()
            released.set()
        return report, ticks

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(refund_reconcile, "LOOKUP_TIMEOUT_SECONDS", 0.3)
        report, ticks = run(store, go())

    assert report.outcomes == {"rpc_error": 1}
    assert ticks >= 10


def test_a_pass_on_a_deployment_that_cannot_pay_reads_nothing(
    store: DisputeStore, chain: Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No signing key and no SAC in the settings — the gap `uphold` refuses on —
    so the pass reads nothing, even with a client that could answer."""
    monkeypatch.setattr(settings, "stellar_signing_key", "")
    monkeypatch.setattr(sc, "signer_public_key", lambda: SETTLER)
    monkeypatch.setattr(sc, "contract_ids", lambda: sc.ContractIds("", "", "", "", SAC))
    monkeypatch.setattr(refund_reconcile, "MIN_CLAIM_AGE_SECONDS", 0)
    chain.answers["tx_on_record"] = an_answer("tx_on_record", "NOT_FOUND")

    async def go() -> refund_reconcile.SweepReport:
        await in_flight(store, "tx_on_record")
        return await refund_reconcile.sweep_once()

    report = run(store, go())

    assert report.skipped is not None and report.outcomes == {}
    assert chain.asked == []


def test_the_sweep_starts_only_with_both_switches_on(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    reported: list[bool] = []

    async def go() -> None:
        for sweep, refunds in ((False, False), (True, False), (False, True), (True, True)):
            monkeypatch.setattr(settings, "refund_reconcile_enabled", sweep)
            monkeypatch.setattr(settings, "dispute_refunds_enabled", refunds)
            started.append(refund_reconcile.start())
            reported.append(refund_reconcile.status()["enabled"])
            await refund_reconcile.stop()

    async def _idle() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(refund_reconcile, "_loop", _idle)
    asyncio.run(go())

    assert started == [False, False, False, True]
    # /readiness says "enabled" exactly when the sweep would run.
    assert reported == started


def test_a_claim_held_over_a_dispute_that_is_not_crediting_is_left_for_a_human(chain: Chain) -> None:
    """The wedge the store blocks on rather than forgets: a claim row over a
    dispute that is not mid-payout. Only a human reading the chain can say
    what it guards, so the sweep neither looks it up nor releases it."""
    memory = InMemoryDisputeStore()
    dispute_store._store = memory

    async def go() -> tuple[Decision, list[str]]:
        opened = await memory.open_dispute(a_dispute(payer=PAYER))
        await memory.append_status(opened.id, "upheld")
        memory._refund_claims[opened.id] = time.time() - 3600
        decision = await decide(memory)
        return decision, [c.dispute_id for c in await memory.list_refund_claims()]

    try:
        decision, held = asyncio.run(go())
    finally:
        dispute_store._store = None

    assert decision.action == "not_crediting"
    assert held == ["dsp_0001"]
    assert chain.asked == []


def test_a_claim_settled_after_the_queue_was_read_is_not_mistaken_for_a_wedge(
    store: DisputeStore, chain: Chain
) -> None:
    """A pass reads the queue, then each dispute. One settled in between —
    by another pass, or an operator — reads `credited` with its claim gone:
    finished work, not a claim held over a finished dispute. Nothing is
    looked up and nothing is raised to a human."""

    async def go() -> Decision:
        parked = await in_flight(store, "tx_on_record")
        stale = await the_claim(store)
        await store.append_status(parked.id, "credited", refund_tx="tx_on_record", credited_usdc=0.05)
        return await reconcile_claim(stale, now=stale.claimed_at + 3600, settler=SETTLER, asset_sac=SAC)

    decision = run(store, go())

    assert decision.action == "already_settled"
    assert chain.asked == []


def test_readiness_reports_the_sweep_and_its_last_pass_by_count_only(
    store: DisputeStore, chain: Chain, configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counts by action and when — never a dispute id or a hash, because the
    probe answers anybody."""
    from fastapi.testclient import TestClient

    from app.main import app

    tx_hash, envelope = a_refund_envelope(stroops=500_000)
    chain.answers[tx_hash] = an_answer(tx_hash, "SUCCESS", envelope=envelope)

    async def go() -> str:
        parked = await in_flight(store, tx_hash)
        await refund_reconcile.sweep_once()
        return parked.id

    dispute_id = run(store, go())
    monkeypatch.setattr(settings, "refund_reconcile_enabled", True)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(refund_reconcile, "start", lambda: False)
    with TestClient(app) as client:
        response = client.get("/readiness")

    reconcile = response.json()["disputes"]["reconcile"]
    assert reconcile["enabled"] is True and reconcile["running"] is False
    assert reconcile["last_outcomes"] == {"credited": 1} and reconcile["last_skipped"] is None
    assert isinstance(reconcile["last_run_at"], float)
    assert dispute_id not in response.text and tx_hash not in response.text


def test_the_app_starts_the_sweep_with_its_lifespan_and_stops_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    async def _idle() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(refund_reconcile, "_loop", _idle)
    monkeypatch.setattr(settings, "refund_reconcile_enabled", True)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    with TestClient(app) as client:
        during = client.get("/readiness").json()["disputes"]["reconcile"]
        task = refund_reconcile._task

    assert during["enabled"] is True and during["running"] is True
    assert task is not None and task.cancelled()
    assert refund_reconcile._task is None


def test_one_pass_that_raises_does_not_stop_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    passes: list[int] = []

    async def _pass() -> refund_reconcile.SweepReport:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("a bug in one pass")
        return refund_reconcile.SweepReport(0.0, 0.0)

    monkeypatch.setattr(refund_reconcile, "sweep_once", _pass)
    monkeypatch.setattr(settings, "refund_reconcile_interval_seconds", 0.001)

    async def go() -> None:
        task = asyncio.create_task(refund_reconcile._loop())
        while len(passes) < 3 and not task.done():
            await asyncio.sleep(0.005)
        assert not task.done()
        task.cancel()

    asyncio.run(go())

    assert passes[:3] == [0, 1, 2]
