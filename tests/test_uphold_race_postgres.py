"""Two adjudicators upholding one dispute at once, through the real claim.

`dispute_svc.uphold` is the path that moves the platform's money, and every
race test in tests/test_adjudication.py runs it over the in-memory store, whose
calls never suspend — its "races" are interleavings counted in event-loop
ticks. The refund mutex that actually decides who pays is a PRIMARY KEY in
Postgres, and nothing ran two real upholds into it at once.

These do: the real `uphold`, the real `PostgresDisputeStore` over a real
Postgres (conftest `pg_dsn`), the real SQL. Only the chain is stubbed, at
`refund_svc.execute_refund`, where it counts the transfers signed. The answer
has to be one transfer, however the two callers interleave.

Where the interleaving matters, it is FORCED rather than hoped for: a barrier
holds each caller at the store call under test until both have arrived, and
then releases them together onto two connections. What happens after that is
Postgres's to decide, which is the point.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

import pytest
from pg_support import claims, statuses
from stellar_sdk import Keypair

from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
from app.services.dispute_store import DisputeRecord, PostgresDisputeStore, SettlementRecord, SettlementStep
from app.services.dispute_svc import DisputeError

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
TASK = "tsk_raced"
LANDED: dict[str, Any] = {"status": "SUCCESS", "hash": "tx_credit", "ledger": 4242}
REJECTED: dict[str, Any] = {"status": "FAILED", "hash": "tx_rejected"}

T = TypeVar("T")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, pg_dsn: str) -> Iterator[PostgresDisputeStore]:
    """The process's dispute store, resolved the way the app resolves it, over
    a real database — on a deployment configured to pay credits.

    The rating (story 4.04) is switched off: it is written only after the
    credit is recorded, and this file is about the credit."""
    monkeypatch.setattr(settings, "database_url", pg_dsn)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "stellar_asset_sac", "CSAC" + "7Z2Q" * 12)
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    monkeypatch.setattr(settings, "reputation_enabled", False)
    dispute_store._store = None
    resolved = dispute_store.get_dispute_store()
    assert isinstance(resolved, PostgresDisputeStore)
    yield resolved
    dispute_store._store = None


class Settler:
    """The chain as `refund_svc.execute_refund` sees it: every transfer signed
    is recorded, and answered from a script (SUCCESS once it runs out)."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.signed: list[tuple[str, float]] = []

    async def __call__(self, buyer: str, amount_usdc: float, *, dispute_id: str | None = None) -> dict[str, Any]:
        self.signed.append((buyer, amount_usdc))
        # A transfer takes a network round trip, and the other caller is free
        # to run while it does.
        await asyncio.sleep(0.05)
        return self.answers.pop(0) if self.answers else LANDED


def _gate(method: Callable[..., Awaitable[Any]], barrier: asyncio.Barrier) -> Callable[..., Awaitable[Any]]:
    """`method`, entered only once `barrier` has every party waiting at it."""

    async def gated(*args: Any, **kwargs: Any) -> Any:
        # Bounded, so a caller that never arrives fails the test instead of
        # hanging it.
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return await method(*args, **kwargs)

    return gated


async def closing(coro: Awaitable[T]) -> T:
    """Await `coro`, then shut the store down inside the same loop — an asyncpg
    pool belongs to the loop that dialled it."""
    try:
        return await coro
    finally:
        await dispute_store.close_dispute_store()


async def _dispute(store: PostgresDisputeStore) -> DisputeRecord:
    """A settled workflow and a dispute of its first step, as 4.02 records them."""
    payer = Keypair.random().public_key
    now = time.time()
    await store.record_settlement(
        SettlementRecord(
            task_id=TASK,
            payer=payer,
            auth_id_hex="ab" * 16,
            job_id_hex=JOB,
            charge_tx="tx_charge",
            proof_tx="tx_proof",
            settled_usdc=0.12,
            steps=(
                SettlementStep(step_index=0, agent_id="agt_writer", agent_name=None, price_usdc=0.05, delivered=True),
                SettlementStep(step_index=1, agent_id="agt_seo", agent_name=None, price_usdc=0.07, delivered=True),
            ),
            settled_at=now,
            window_closes_at=now + 3600.0,
        )
    )
    return await store.open_dispute(
        DisputeRecord(
            id=dispute_store.new_dispute_id(),
            job_id_hex=JOB,
            task_id=TASK,
            step_index=0,
            agent_id="agt_writer",
            payer=payer,
            reason="the draft ignored half the brief",
            status="open",
            charged_usdc=0.05,
            creditable_usdc=0.05,
            opened_at=now,
        )
    )


def test_two_upholds_racing_into_the_claim_sign_one_transfer(
    store: PostgresDisputeStore, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry after a FAILED transfer, adjudicated twice at once.

    A first uphold's transfer definitively FAILED, so the claim was released and
    the dispute is `upheld` again — payable, and already past the verdict, so
    both callers skip the compare-and-set and go straight for the claim. The
    barrier lets neither claim until both are there. Postgres then takes one
    claim row; the other INSERT conflicts, writes nothing, and that caller
    answers with the record instead of signing a second transfer."""
    chain = Settler(REJECTED)
    monkeypatch.setattr(refund_svc, "execute_refund", chain)

    async def go() -> tuple[Any, ...]:
        dispute = await _dispute(store)
        try:
            await dispute_svc.uphold(dispute.id)
        except DisputeError as refused:
            first = refused.code
        after_failure = await store.get_dispute(dispute.id)
        held_after_failure = await claims(pg_dsn)

        monkeypatch.setattr(store, "claim_refund", _gate(store.claim_refund, asyncio.Barrier(2)))
        raced = await asyncio.gather(
            dispute_svc.uphold(dispute.id), dispute_svc.uphold(dispute.id), return_exceptions=True
        )
        return (
            dispute,
            first,
            after_failure,
            held_after_failure,
            list(raced),
            await store.get_dispute(dispute.id),
            await claims(pg_dsn),
            await statuses(pg_dsn, dispute.id),
        )

    dispute, first, after_failure, held_after_failure, raced, final, held, trail = asyncio.run(closing(go()))

    # The FAILED attempt: one transfer, refused, the claim handed back.
    assert first == "refund_failed"
    assert after_failure is not None and after_failure.status == "upheld"
    assert held_after_failure == {}

    # The race: exactly one more transfer, for the step's price, to the payer.
    assert chain.signed == [(dispute.payer, 0.05), (dispute.payer, 0.05)]
    assert all(isinstance(result, DisputeRecord) for result in raced), raced
    assert sorted(result.status for result in raced) in (["credited", "crediting"], ["credited", "credited"])
    assert final is not None and final.status == "credited" and final.refund_tx == "tx_credit"
    assert held == {}
    # One claim after the release, not two: the loser wrote no `crediting` row.
    assert trail == ["open", "upheld", "crediting", "upheld", "crediting", "credited"]


def test_two_upholds_of_an_open_dispute_sign_one_transfer(
    store: PostgresDisputeStore, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both adjudicators read `open` and both try to record the verdict, at the
    same instant. The compare-and-set lets one through; the other re-reads, finds
    the dispute already taken, and signs nothing — whether it then sees it
    `upheld`, `crediting` or `credited` depends on timing, and each of those has
    an answer that is not a second transfer."""
    chain = Settler()
    monkeypatch.setattr(refund_svc, "execute_refund", chain)
    barrier = asyncio.Barrier(2)
    append = store.append_status

    async def gated_verdict(dispute_id: str, status: Any, **kwargs: Any) -> Any:
        if kwargs.get("expected_status") == "open":
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return await append(dispute_id, status, **kwargs)

    async def go() -> tuple[Any, ...]:
        dispute = await _dispute(store)
        monkeypatch.setattr(store, "append_status", gated_verdict)
        raced = await asyncio.gather(
            dispute_svc.uphold(dispute.id), dispute_svc.uphold(dispute.id), return_exceptions=True
        )
        return (
            dispute,
            list(raced),
            await store.get_dispute(dispute.id),
            await claims(pg_dsn),
            await statuses(pg_dsn, dispute.id),
        )

    dispute, raced, final, held, trail = asyncio.run(closing(go()))

    assert chain.signed == [(dispute.payer, 0.05)]
    paid = [r for r in raced if isinstance(r, DisputeRecord) and r.refund_tx == "tx_credit"]
    assert len(paid) >= 1
    for other in raced:
        if isinstance(other, DisputeError):
            assert other.code in ("adjudication_in_progress", "refund_in_flight"), other.code
        else:
            assert isinstance(other, DisputeRecord), other
    assert final is not None and final.status == "credited"
    assert held == {}
    # One verdict and one claim on the trail, whoever won.
    assert trail == ["open", "upheld", "crediting", "credited"]
