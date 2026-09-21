"""An upheld dispute, end to end: opened, upheld, credited, then rated (story 4.04).

`tests/test_adjudication.py` stubs the rating at its service seam to pin WHEN
it is written relative to the refund. This file goes one layer down and fakes
the CHAIN instead — `sc.submit_rating_async`, the one call the rating makes —
so the real derivation, the real weight and the real mapping from the ledger's
answers to an outcome all run, and what is asserted is what `uphold` does with
each answer:

  - the rating is submitted with `kind="dispute"` under the DERIVED id, never
    the sealed job's own, which the settler's auto-rating already holds;
  - SUCCESS records the hash; a repeat uphold is refused as a replay at
    simulation and changes nothing;
  - a TIMEOUT records its in-flight hash, and the retry settles it — a replay
    if it landed, a fresh hash if it never did;
  - a FAILED rating records nothing and is retryable;
  - a replay with nothing on record is a COLLISION: loud, and the buyer keeps
    the credit;
  - across all of it the refund is signed exactly once, and the agent's cached
    score is dropped exactly when a rating is known to have landed.

The fake ledger models the one contract rule every one of those turns on: the
replay guard on `Rated(agent_id, job_id)`, checked at simulation, before any
transaction exists. Hermetic: the in-memory dispute store, real ed25519 keys,
the settler's transfer stubbed at `execute_refund`. No network, no database.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import time
from typing import Any

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_rating, dispute_store, dispute_svc, refund_svc, reputation_svc
from app.services import external_binding as eb
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep
from app.services.dispute_svc import dispute_message
from app.state import state
from app.trace_bus import bus

SVC_LOGGER = "app.services.dispute_svc"

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
AGENT = "agt_writer"
# One agent serving two steps of one job: the case 4.01's job-only derivation
# collided on, so the flow carries it by default.
STEPS = (
    SettlementStep(step_index=0, agent_id=AGENT, agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id=AGENT, agent_name="Copywriter", price_usdc=0.07, delivered=True),
)
LANDED: dict[str, Any] = {"status": "SUCCESS", "hash": "tx_credit", "ledger": 4242}


def derived(step: int) -> bytes:
    return dispute_rating.dispute_job_id(bytes.fromhex(JOB), step)


class Ledger:
    """The ReputationLedger, as `sc.submit_rating_async` reaches it.

    Holds the replay guard's keys — seeded with the settler's auto-rating of
    every step under the SEALED job id, as a settled workflow leaves them — and
    refuses a key it has seen with `ContractError` #7 at simulation, exactly
    where the real client raises it. `script` queues how the next submissions
    that pass simulation end:

      - `land`: SUCCESS, and the key is taken;
      - `fail`: the ledger FAILED the transaction, nothing taken;
      - `lost`: the poll ran out and the transaction never landed;
      - `late`: the poll ran out but the transaction landed after all;
      - `raise`: the submit raised, with no hash — and it landed.

    Anything unscripted lands.
    """

    def __init__(self) -> None:
        self.rated: set[tuple[str, bytes]] = {(s.agent_id, bytes.fromhex(JOB)) for s in STEPS}
        self.script: list[str] = []
        self.submits: list[dict[str, Any]] = []
        self.replays = 0
        self._hashes = (f"tx_rating_{n}" for n in itertools.count(1))

    async def __call__(
        self, agent_id: str, job_id: bytes, rating: int, weight: int, payer: str, kind: str
    ) -> dict[str, Any]:
        if (agent_id, job_id) in self.rated:
            self.replays += 1
            raise sc.ContractError("HostError: Error(Contract, #7)", 7)
        self.submits.append(
            {"agent_id": agent_id, "job_id": job_id, "rating": rating, "weight": weight, "payer": payer, "kind": kind}
        )
        how = self.script.pop(0) if self.script else "land"
        if how in ("land", "late", "raise"):
            self.rated.add((agent_id, job_id))
        if how == "raise":
            raise ConnectionError("the RPC dropped the connection after the submit")
        tx = next(self._hashes)
        if how == "land":
            return {"status": "SUCCESS", "hash": tx}
        if how == "fail":
            return {"status": "FAILED", "hash": tx}
        return {"status": "timeout", "hash": tx}


class Settler:
    """The settler's credit transfer, counted: the refund must be signed once."""

    def __init__(self) -> None:
        self.transfers: list[tuple[str, float]] = []

    async def __call__(self, buyer: str, amount_usdc: float) -> dict[str, Any]:
        self.transfers.append((buyer, amount_usdc))
        return LANDED


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Every singleton these paths reach, reset around each test, on a
    deployment that has switched refunds on and is configured to rate."""
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    dispute_store._store = None
    eb._challenges.clear()
    state.tasks.clear()
    state.traces.clear()
    state.task_order.clear()
    bus._subs.clear()
    bus._closed.clear()
    yield
    dispute_store._store = None
    eb._challenges.clear()
    state.tasks.clear()
    state.traces.clear()
    state.task_order.clear()
    bus._subs.clear()
    bus._closed.clear()


@pytest.fixture
def ledger(monkeypatch) -> Ledger:
    fake = Ledger()
    monkeypatch.setattr(sc, "submit_rating_async", fake)
    return fake


@pytest.fixture
def settler(monkeypatch) -> Settler:
    fake = Settler()
    monkeypatch.setattr(refund_svc, "execute_refund", fake)
    return fake


@pytest.fixture
def invalidated(monkeypatch) -> list[str]:
    """Every agent whose cached score was dropped — through the real call, so
    the cache is really invalidated as well as counted."""
    dropped: list[str] = []
    real = reputation_svc.invalidate_rep

    def _record(agent_id: str) -> None:
        dropped.append(agent_id)
        real(agent_id)

    monkeypatch.setattr(reputation_svc, "invalidate_rep", _record)
    return dropped


def _seed(payer: str) -> SettlementRecord:
    now = time.time()
    record = SettlementRecord(
        task_id=TASK,
        payer=payer,
        auth_id_hex="ab" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=0.12,
        steps=STEPS,
        settled_at=now,
        window_closes_at=now + 3600.0,
    )
    asyncio.run(dispute_store.get_dispute_store().record_settlement(record))
    return record


def open_dispute(step: int = 0, payer: Keypair | None = None) -> DisputeRecord:
    """Open a dispute the way a buyer does: a signed challenge, then the claim."""
    payer = payer or Keypair.random()
    if asyncio.run(dispute_store.get_dispute_store().get_settlement(JOB)) is None:
        _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, step))
    signature = base64.b64encode(payer.sign(dispute_message(JOB, step, nonce).encode("utf-8"))).decode("ascii")
    return asyncio.run(
        dispute_svc.open_dispute(
            job_id_hex=JOB,
            step_index=step,
            reason="the draft ignored half the brief",
            payer=payer.public_key,
            nonce=nonce,
            signature_b64=signature,
        )
    )


def uphold(dispute_id: str) -> DisputeRecord:
    return asyncio.run(dispute_svc.uphold(dispute_id))


def svc_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and r.levelno == logging.ERROR]


# ── the whole path ──────────────────────────────────────────────


def test_an_upheld_dispute_is_credited_then_rated_under_the_derived_id(ledger, settler, invalidated) -> None:
    """Open, uphold, credited, rated — and the rating is the one the story
    describes: kind `dispute`, so it counts against the agent's dispute rate;
    the DERIVED id, because the sealed one is already taken; the dispute score;
    the settler's own weight for the step; on behalf of the payer."""
    dispute = open_dispute()
    assert ledger.submits == []  # a dispute is a claim until it is upheld and paid

    rated = uphold(dispute.id)

    assert rated.status == "credited"
    assert rated.refund_tx == "tx_credit"
    assert rated.rating_tx == "tx_rating_1"
    assert ledger.submits == [
        {
            "agent_id": AGENT,
            "job_id": derived(0),
            "rating": dispute_rating.DISPUTE_RATING,
            "weight": reputation_svc.rating_weight_stroops(0.05),
            "payer": dispute.payer,
            "kind": "dispute",
        }
    ]
    assert ledger.submits[0]["job_id"] != bytes.fromhex(JOB)
    assert ledger.submits[0]["job_id"][:8] == bytes.fromhex(JOB)[:8]  # linked to the job in plain sight
    assert settler.transfers == [(dispute.payer, 0.05)]
    assert invalidated == [AGENT]
    assert asyncio.run(dispute_svc.get_dispute(dispute.id)) == rated


def test_a_repeat_uphold_after_a_landed_rating_is_a_replay_and_changes_nothing(ledger, settler, invalidated) -> None:
    """The retry path's cheap case, and the reason it is safe to take on every
    uphold: the rating already landed, so the ledger refuses the second at
    SIMULATION — no transaction, no fee — and the dispute is answered exactly
    as it was. The replay confirms the landing, so the score is dropped again
    rather than trusted to still be fresh."""
    dispute = open_dispute()
    rated = uphold(dispute.id)

    again = uphold(dispute.id)

    assert again == rated  # the same record, hash and all
    assert ledger.replays == 1
    assert len(ledger.submits) == 1  # nothing was submitted the second time
    assert len(settler.transfers) == 1
    assert invalidated == [AGENT, AGENT]


# ── a timeout: recorded at once, settled by the retry ───────────


def test_a_timed_out_rating_that_never_landed_is_replaced_by_the_retry(ledger, settler, invalidated, caplog) -> None:
    """The in-flight hash is on the record the moment it exists, because if
    it lands that IS the evidence. This one never did — so the retry passes
    simulation, lands a fresh rating, and its hash replaces the dead one. The
    score is not dropped for a rating nobody knows landed, and is dropped the
    moment one does."""
    dispute = open_dispute()
    ledger.script = ["lost"]

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        unconfirmed = uphold(dispute.id)

    assert unconfirmed.status == "credited" and unconfirmed.refund_tx == "tx_credit"
    assert unconfirmed.rating_tx == "tx_rating_1"
    assert invalidated == []
    (logged,) = svc_errors(caplog)
    assert "unconfirmed" in logged and "tx_rating_1" in logged

    settled = uphold(dispute.id)

    assert settled.rating_tx == "tx_rating_2"
    assert ledger.replays == 0 and len(ledger.submits) == 2
    assert invalidated == [AGENT]
    assert len(settler.transfers) == 1


def test_a_timed_out_rating_that_landed_is_confirmed_by_the_retry(ledger, settler, invalidated) -> None:
    """The other way a timeout ends: the transaction landed after the poll
    gave up. The retry is refused as a replay, and because this dispute has a
    hash on record that is CONFIRMATION, not a collision — the recorded hash
    is kept, and only now is the score known to have moved."""
    dispute = open_dispute()
    ledger.script = ["late"]
    unconfirmed = uphold(dispute.id)
    assert unconfirmed.rating_tx == "tx_rating_1" and invalidated == []

    confirmed = uphold(dispute.id)

    assert confirmed == unconfirmed
    assert ledger.replays == 1 and len(ledger.submits) == 1
    assert invalidated == [AGENT]
    assert len(settler.transfers) == 1


# ── a failure, and a collision ──────────────────────────────────


def test_a_failed_rating_records_nothing_and_the_retry_lands_it(ledger, settler, invalidated, caplog) -> None:
    """The ledger FAILED the transaction, which means nothing was written — so
    nothing is recorded, not even the failed hash, and the dispute reads paid
    but unrated. The ERROR line carries every id a reconciliation needs. The
    retry lands it, and the refund is not touched by either."""
    dispute = open_dispute()
    ledger.script = ["fail"]

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        failed = uphold(dispute.id)

    assert failed.status == "credited" and failed.refund_tx == "tx_credit"
    assert failed.rating_tx is None
    assert invalidated == []
    (logged,) = svc_errors(caplog)
    for fact in ("failed", dispute.id, JOB, derived(0).hex(), AGENT, dispute.payer):
        assert fact in logged

    landed = uphold(dispute.id)

    assert landed.rating_tx == "tx_rating_2"
    assert invalidated == [AGENT]
    assert len(settler.transfers) == 1


def test_a_replay_with_nothing_on_record_is_a_loud_collision_and_the_buyer_keeps_the_credit(
    ledger, settler, invalidated, caplog
) -> None:
    """D4. The ledger already holds a rating under this dispute's derived id,
    and this dispute has never recorded writing one — so it is not a retry
    that landed, and must never be read as one. The credit stands (the buyer
    was paid, and that is not the rating's to undo), the record shows no
    rating, the score is not dropped, and the operator gets an ERROR naming
    the collision and every id. A repeat meets the same wall, loudly again,
    and still signs no second credit."""
    dispute = open_dispute()
    ledger.rated.add((AGENT, derived(0)))

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        collided = uphold(dispute.id)
        again = uphold(dispute.id)

    assert collided.status == "credited" and collided.refund_tx == "tx_credit"
    assert collided.rating_tx is None  # never reads as fully resolved
    assert again == collided
    assert ledger.submits == [] and ledger.replays == 2
    assert invalidated == []
    assert len(settler.transfers) == 1
    logged = svc_errors(caplog)
    assert len(logged) == 2
    for fact in ("COLLISION", dispute.id, AGENT, JOB, derived(0).hex(), dispute.payer):
        assert fact in logged[0]
