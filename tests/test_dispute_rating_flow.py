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
from dataclasses import replace
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


class RefundTouched(BaseException):
    """A rating path reached the refund. A BaseException on purpose: the
    rating step answers every `Exception` with the paid record rather than an
    error, so an ordinary trap would be swallowed and the test would pass."""


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
    deployment that has switched refunds on and is configured to rate AND pay.

    The settler's pair — a signing key and the asset SAC — is set explicitly
    because `uphold` now asks `refund_svc.config_gap` for it before it claims
    anything, and neither is set in a hermetic run: the conftest blanks the key
    and CI has no `.env` to supply the SAC. Both are fictional and neither is
    read; the transfer is stubbed above the client.
    """
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    monkeypatch.setattr(settings, "stellar_asset_sac", "CSAC" + "7Z2Q" * 12)
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
    is kept, and only now is the score known to have moved.

    Story 4.06 makes the record say so as well. The timeout recorded its hash
    as UNCONFIRMED, and the replay moves it to confirmed — False to True, the
    one change this retry makes to the dispute, and the one a receipt needs
    before it may say the agent was rated. A further replay finds it already
    confirmed and writes nothing."""
    dispute = open_dispute()
    ledger.script = ["late"]
    unconfirmed = uphold(dispute.id)
    assert unconfirmed.rating_tx == "tx_rating_1" and invalidated == []
    assert unconfirmed.rating_confirmed is False

    confirmed = uphold(dispute.id)

    # The hash, the credit and everything else exactly as the timeout left
    # them: only the confirmation moved, and the moment it was recorded.
    assert confirmed.rating_confirmed is True
    assert confirmed == replace(unconfirmed, rating_confirmed=True, updated_at=confirmed.updated_at)
    assert confirmed.updated_at is not None and unconfirmed.updated_at is not None
    assert confirmed.updated_at >= unconfirmed.updated_at
    assert ledger.replays == 1 and len(ledger.submits) == 1
    assert invalidated == [AGENT]
    assert len(settler.transfers) == 1

    assert uphold(dispute.id) == confirmed
    assert ledger.replays == 2


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


def test_a_hashless_timeout_that_landed_is_reported_as_a_collision_never_as_resolved(
    ledger, settler, invalidated, caplog
) -> None:
    """The one case the record cannot tell apart, pinned so it stays on the
    safe side. A submit that raised leaves no hash to record, so when it did
    land the retry's replay finds nothing on record — indistinguishable here
    from somebody else's rating under the same key. D4 decides it: a
    collision, loud, with the unrecorded-attempt explanation in the line, and
    never a dispute that reads as resolved without evidence behind it."""
    dispute = open_dispute()
    ledger.script = ["raise"]
    lost_track = uphold(dispute.id)
    assert lost_track.rating_tx is None
    caplog.clear()  # the first attempt's own "unconfirmed" line is not what is under test

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        retried = uphold(dispute.id)

    assert retried.rating_tx is None
    assert invalidated == []
    (logged,) = svc_errors(caplog)
    assert "COLLISION" in logged and "timeout with no hash" in logged
    assert len(settler.transfers) == 1


# ── whether the rating is known to have landed (story 4.06) ─────


@pytest.mark.parametrize(
    ("how", "rating_tx", "rating_confirmed"),
    [
        ("land", "tx_rating_1", True),
        ("lost", "tx_rating_1", False),
        ("fail", None, None),
        ("raise", None, None),
        ("collision", None, None),
    ],
    ids=["success", "timeout", "failed", "hashless-timeout", "collision"],
)
def test_each_rating_answer_records_whether_the_rating_is_known_to_have_landed(
    ledger, settler, invalidated, how: str, rating_tx: str | None, rating_confirmed: bool | None
) -> None:
    """A hash on the record cannot say whether the rating landed — a SUCCESS
    and a TIMEOUT both leave one — so a receipt that read it as "the agent was
    rated" could claim a consequence that never happened. `rating_confirmed`
    says it, from the ledger's own answer through the real mapping: True for a
    SUCCESS, False for a timeout whose in-flight hash is recorded, and nothing
    at all where nothing was recorded — a FAILED rating, a timeout that
    returned no hash, and a collision."""
    dispute = open_dispute()
    if how == "collision":
        ledger.rated.add((AGENT, derived(0)))
    else:
        ledger.script = [how]

    rated = uphold(dispute.id)

    assert rated.status == "credited" and rated.refund_tx == "tx_credit"
    assert rated.rating_tx == rating_tx
    assert rated.rating_confirmed is rating_confirmed
    assert asyncio.run(dispute_svc.get_dispute(dispute.id)) == rated
    assert len(settler.transfers) == 1


def test_a_failed_retry_leaves_an_unconfirmed_rating_as_it_was(ledger, settler, invalidated) -> None:
    """FAILED says nothing about an EARLIER attempt, so it changes nothing —
    including an unconfirmed one already on record. The timeout's hash never
    landed (the retry passed simulation, which proves it), and this retry did
    not land either: the dispute still carries that hash, still unconfirmed,
    and a later uphold still settles it."""
    dispute = open_dispute()
    ledger.script = ["lost", "fail"]
    unconfirmed = uphold(dispute.id)

    failed = uphold(dispute.id)

    assert failed == unconfirmed
    assert failed.rating_tx == "tx_rating_1" and failed.rating_confirmed is False
    assert len(ledger.submits) == 2 and ledger.replays == 0
    assert invalidated == []


def test_a_rating_recorded_before_the_confirmation_existed_is_confirmed_by_a_replay(
    ledger, settler, invalidated
) -> None:
    """A dispute rated before 4.06 has a `rating_tx` and no word on whether it
    landed — None, "not known". The next uphold is refused as a replay, which
    is the ledger vouching for that hash, so the record is brought up to date
    rather than left saying "not known" about a rating the chain holds."""
    dispute = open_dispute()
    rated = uphold(dispute.id)
    store = dispute_store.get_dispute_store()
    store._disputes[dispute.id] = replace(rated, rating_confirmed=None)

    confirmed = uphold(dispute.id)

    assert confirmed.rating_confirmed is True
    assert confirmed.rating_tx == rated.rating_tx
    assert ledger.replays == 1 and len(settler.transfers) == 1


# ── across disputes, and across every outcome ───────────────────


def test_one_agent_disputed_on_two_steps_is_rated_twice_without_a_collision(ledger, settler, invalidated) -> None:
    """The case 4.01's job-only derivation broke: one agent served both steps,
    so both disputes would have shared one key and the second would have been
    refused as a replay of the first — and, with nothing on record, reported
    as a collision. Each step derives its own id, so both land, each weighted
    by its own step's price."""
    payer = Keypair.random()
    first = open_dispute(0, payer)
    second = open_dispute(1, payer)

    rated = [uphold(first.id), uphold(second.id)]

    assert [r.rating_tx for r in rated] == ["tx_rating_1", "tx_rating_2"]
    assert [s["job_id"] for s in ledger.submits] == [derived(0), derived(1)]
    assert [s["weight"] for s in ledger.submits] == [
        reputation_svc.rating_weight_stroops(0.05),
        reputation_svc.rating_weight_stroops(0.07),
    ]
    assert ledger.replays == 0
    assert invalidated == [AGENT, AGENT]
    assert len(settler.transfers) == 2  # one credit per dispute, and no more


def test_every_rating_outcome_in_turn_never_re_signs_the_refund(monkeypatch, ledger, settler, invalidated) -> None:
    """One dispute walked through every answer the ledger can give, one
    uphold each: failed, lost in flight, landed late, then confirmed twice.
    After the first uphold every door back into the refund is booby-trapped,
    so a single stray call on any rating path fails here. And at each step the
    cache is dropped exactly when a rating is KNOWN to have landed — never for
    a failure, never for a timeout, every time a replay confirms one — and the
    record's `rating_confirmed` (4.06) says so at the same steps: nothing
    after the failure, False while in flight, True from the first replay."""
    dispute = open_dispute()
    ledger.script = ["fail"]
    first = uphold(dispute.id)
    assert (first.status, first.refund_tx, first.rating_tx) == ("credited", "tx_credit", None)
    assert first.rating_confirmed is None

    async def _refund_touched(*args: Any, **kwargs: Any) -> None:
        raise RefundTouched("a rating path reached the refund")

    store = dispute_store.get_dispute_store()
    monkeypatch.setattr(store, "claim_refund", _refund_touched)
    monkeypatch.setattr(store, "release_refund_claim", _refund_touched)
    monkeypatch.setattr(refund_svc, "credit_refund", _refund_touched)
    monkeypatch.setattr(refund_svc, "execute_refund", _refund_touched)

    ledger.script = ["lost", "late"]
    walk = [
        # (rating_tx on the record afterwards, rating_confirmed, cache drops so far)
        ("tx_rating_2", False, 0),  # lost in flight: recorded, not known to have landed
        ("tx_rating_3", False, 0),  # the retry passed simulation and landed late: still unknown
        ("tx_rating_3", True, 1),  # a replay with a hash on record: it landed
        ("tx_rating_3", True, 2),  # and again, confirmed and unchanged
    ]
    for rating_tx, confirmed, drops in walk:
        answered = uphold(dispute.id)
        assert (answered.status, answered.refund_tx, answered.rating_tx) == ("credited", "tx_credit", rating_tx)
        assert answered.rating_confirmed is confirmed
        assert invalidated == [AGENT] * drops

    assert len(settler.transfers) == 1
    assert len(ledger.submits) == 3 and ledger.replays == 2


# ── when the rating step itself breaks ──────────────────────────


def test_a_rating_that_cannot_be_formed_is_a_records_problem_not_a_failed_refund(
    ledger, settler, invalidated, caplog
) -> None:
    """A settlement whose job id is not the 16 bytes the chain seals cannot
    derive a dispute id, so `submit_dispute_rating` raises before anything is
    submitted. By then the buyer has been paid, so it is answered with the
    paid record — not an error that would call the refund a failure — and the
    ERROR says a retry will not mend it, rather than inviting one."""
    dispute = open_dispute()
    short = JOB[:-2]  # 15 bytes
    store = dispute_store.get_dispute_store()
    settlement = asyncio.run(store.get_settlement(JOB))
    assert settlement is not None
    asyncio.run(store.record_settlement(replace(settlement, job_id_hex=short)))
    store._disputes[dispute.id] = replace(dispute, job_id_hex=short)

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        paid = uphold(dispute.id)

    assert paid.status == "credited" and paid.refund_tx == "tx_credit"
    assert paid.rating_tx is None
    assert ledger.submits == [] and invalidated == []
    (logged,) = svc_errors(caplog)
    assert "could not be formed" in logged and "will not mend" in logged
    assert "derived=underivable" in logged and dispute.id in logged


def test_a_landed_rating_the_store_would_not_record_is_logged_with_its_hash(
    monkeypatch, ledger, settler, invalidated, caplog
) -> None:
    """The one write after a landed rating. If the store refuses it, the
    rating is still on-chain — so the score is still dropped, the paid record
    is still the answer, and the hash is in an ERROR line telling the operator
    to record it, because a later replay will otherwise find nothing on record
    and report this dispute's own rating as a collision."""
    dispute = open_dispute()
    store = dispute_store.get_dispute_store()
    real_append = store.append_status

    async def _refuses_the_rating(dispute_id: str, status: Any, **kwargs: Any) -> DisputeRecord:
        if kwargs.get("rating_tx"):
            raise ConnectionError("the database went away")
        return await real_append(dispute_id, status, **kwargs)

    monkeypatch.setattr(store, "append_status", _refuses_the_rating)

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        paid = uphold(dispute.id)

    assert paid.status == "credited" and paid.refund_tx == "tx_credit"
    assert paid.rating_tx is None
    assert invalidated == [AGENT]  # it landed, whatever the store says
    (logged,) = svc_errors(caplog)
    assert "was SUCCESS but could not be recorded" in logged and "tx=tx_rating_1" in logged


def test_a_confirmation_the_store_would_not_record_is_left_for_the_next_uphold(
    monkeypatch, ledger, settler, invalidated, caplog
) -> None:
    """The write a replay now makes (4.06). If the store refuses it, the
    rating's hash is already on record and only its confirmation is missing —
    so there is nothing for a human to write by hand: the paid record is the
    answer, still unconfirmed, the ERROR says to uphold again rather than to
    edit the record, and the next uphold is refused as a replay again and
    records the confirmation then."""
    dispute = open_dispute()
    ledger.script = ["late"]
    unconfirmed = uphold(dispute.id)
    store = dispute_store.get_dispute_store()
    real_append = store.append_status

    async def _refuses_the_confirmation(dispute_id: str, status: Any, **kwargs: Any) -> DisputeRecord:
        if kwargs.get("rating_confirmed") is True:
            raise ConnectionError("the database went away")
        return await real_append(dispute_id, status, **kwargs)

    monkeypatch.setattr(store, "append_status", _refuses_the_confirmation)
    caplog.clear()  # the timeout's own "unconfirmed" line is not what is under test

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        answered = uphold(dispute.id)

    assert answered == unconfirmed and answered.rating_confirmed is False
    assert invalidated == [AGENT]  # the replay proved it landed, whatever the store says
    (logged,) = svc_errors(caplog)
    assert "confirmation could not be recorded" in logged and "tx=tx_rating_1" in logged
    assert "by hand" not in logged

    monkeypatch.setattr(store, "append_status", real_append)
    confirmed = uphold(dispute.id)

    assert confirmed.rating_confirmed is True and confirmed.rating_tx == "tx_rating_1"
    assert len(settler.transfers) == 1


# ── the observer the operator tool reads the ledger's answer through ──


def test_the_rating_observer_is_told_the_ledgers_own_answer(ledger, settler, invalidated) -> None:
    """A rating that timed out and one that landed both leave a `rating_tx` on
    the record, so the record cannot tell an operator which they are looking
    at. `on_rating` is how the tool asks the ledger instead: told once per
    uphold, exactly as the ledger answered, and the hash it hears is the one
    the record keeps."""
    dispute = open_dispute()
    heard: list[dispute_rating.RatingOutcome] = []
    ledger.script.append("lost")

    first = asyncio.run(dispute_svc.uphold(dispute.id, on_rating=heard.append))
    retried = asyncio.run(dispute_svc.uphold(dispute.id, on_rating=heard.append))

    assert [outcome.status for outcome in heard] == ["TIMEOUT", "SUCCESS"]
    assert first.rating_tx == heard[0].tx_hash  # both calls leave a hash on record...
    assert retried.rating_tx == heard[1].tx_hash  # ...and only the observer says which one landed
    assert heard[0].job_id_hex == derived(0).hex()
    assert settler.transfers == [(dispute.payer, 0.05)]  # told twice, paid once


def test_the_rating_observer_hears_nothing_when_no_rating_was_submitted(
    monkeypatch, ledger, settler, invalidated
) -> None:
    """Told only when the ledger was actually asked. A credit refused at the
    cap never reaches the rating, so there is no answer to hand over — and an
    observer told something here would be told a verdict nobody gave."""
    dispute = open_dispute()
    heard: list[dispute_rating.RatingOutcome] = []
    monkeypatch.setattr(settings, "max_refund_usdc", 0.01)

    with pytest.raises(dispute_svc.DisputeError):
        asyncio.run(dispute_svc.uphold(dispute.id, on_rating=heard.append))

    assert heard == []
    assert ledger.submits == []


def test_an_observer_that_raises_cannot_turn_a_paid_dispute_into_a_failure(
    ledger, settler, invalidated, caplog
) -> None:
    """The observer is the caller's code, and it runs after the credit has
    landed — where this module's rule is that nothing is raised. A fault in it
    is logged against the dispute, and the rating it was told about is still
    recorded as the ledger answered it."""
    dispute = open_dispute()

    def broken(_outcome: dispute_rating.RatingOutcome) -> None:
        raise RuntimeError("the operator tool's own bug")

    rated = asyncio.run(dispute_svc.uphold(dispute.id, on_rating=broken))

    assert rated.status == "credited"
    assert rated.rating_tx == "tx_rating_1"
    assert settler.transfers == [(dispute.payer, 0.05)]
    assert any("observer raised" in message for message in svc_errors(caplog))
