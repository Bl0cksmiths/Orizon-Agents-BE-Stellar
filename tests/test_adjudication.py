"""Adjudicating a dispute, and paying the credit (app/services/dispute_svc.py).

Story 4.03, ADR 0002. `tests/test_dispute_svc.py` covers whether a dispute may
EXIST; this file covers whether it may be PAID, which is the same question with
money on the other side of it. What is asserted here is therefore not the
arithmetic — `refund_svc` owns that and `tests/test_refund_execution.py` proves
it — but the ORDER of the steps in `uphold`, because every way this service can
credit a buyer twice, or strand one who is owed, is an ordering mistake:

  1. a retry after a credit signs NOTHING and answers with the original hash,
     and it does not consult the refund claim to decide that;
  2. a FAILED transfer leaves the dispute `upheld`, so the buyer can still be
     paid;
  3. a TIMEOUT leaves it `crediting` and the next uphold refuses — the
     transfer may still land, so there is no safe retry (D3);
  4. a refusal before the signer — the cap, nothing to credit, a settlement
     that is gone — hands the claim back, because nothing was signed;
  5. a rejected dispute is never payable;
  6. the dispute rating (4.04) is written only once the credit is recorded,
     and no answer it gets — failure, timeout, collision, exception — reaches
     back into the refund or turns a paid credit into an error;
  7. a repeat uphold of a credited dispute retries the RATING and only the
     rating (D3), and an open, rejected or unpaid dispute is never rated.

Hermetic: the in-memory dispute store, an in-process challenge table and real
ed25519 keys, with the settler's SAC transfer stubbed at `execute_refund` and
the dispute rating at `dispute_rating.submit_dispute_rating` (its mapping from
the chain's answers is `tests/test_dispute_rating_flow.py`'s to prove). No
chain, no network, no database. Every process singleton these paths touch — the
store, the challenge table, app state and the trace bus — is reset per test.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import inspect
import logging
import time
from typing import Any, cast

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.config import settings
from app.schemas import Task
from app.services import dispute_rating, dispute_store, dispute_svc, refund_svc, reputation_svc
from app.services import external_binding as eb
from app.services.dispute_rating import RatingOutcome, RatingStatus
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep
from app.services.dispute_svc import DisputeError, dispute_message
from app.state import state
from app.trace_bus import bus

SVC_LOGGER = "app.services.dispute_svc"

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_seo", agent_name="SEO Brief", price_usdc=0.07, delivered=True),
)

# What `sc.invoke_with_server_key_async` hands back, in its own vocabulary —
# the three answers a submitted transfer can have. `timeout` is lowercase
# because that is the client's own word for it, and the hazard of the whole
# story hides behind it: the transfer MAY STILL LAND.
LANDED: dict[str, Any] = {"status": "SUCCESS", "hash": "tx_credit", "ledger": 4242}
REJECTED: dict[str, Any] = {"status": "FAILED", "hash": "tx_rejected"}
LOST: dict[str, Any] = {"status": "timeout", "hash": "tx_inflight"}


class SignedSomething(BaseException):
    """The booby-trapped settler fired: this path signed something it must not.

    A BaseException rather than an AssertionError on purpose. `credit_refund`
    maps ANY `Exception` out of the transfer to a TIMEOUT outcome — correctly,
    since a raise can happen either side of the submission — so an ordinary
    trap would be swallowed and the test would read a timeout instead of the
    failure it is there to catch.
    """


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Every singleton these paths reach, reset around each test.

    `dispute_refunds_enabled` is turned ON here because it ships OFF: these
    tests describe a deployment whose operator has deliberately enabled the
    refund path, which is the only deployment where any of this runs. The test
    that covers the switch itself turns it back off, so the default is asserted
    rather than assumed.
    """
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
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


class Rater:
    """The dispute rating, as `uphold` sees it: `dispute_rating.submit_dispute_rating`.

    Stubbed at the service seam rather than at the chain, because what this
    file asserts is WHEN the rating is written relative to the refund —
    `tests/test_dispute_rating_flow.py` fakes the chain beneath it and drives
    the real mapping. It models the one ledger rule the ordering leans on, the
    replay guard: once a dispute's rating has landed, every later attempt is
    answered REPLAY. `script` queues the answers to give before that.

    Each call records the dispute's status AS THE STORE HELD IT at that
    instant, because "had the credit already been recorded" is the question.
    """

    def __init__(self) -> None:
        self.script: list[RatingStatus] = []
        self.stored_status: list[str] = []
        self._landed: set[str] = set()

    @property
    def calls(self) -> int:
        return len(self.stored_status)

    async def __call__(self, dispute: DisputeRecord, settlement: SettlementRecord) -> RatingOutcome:
        stored = await dispute_store.get_dispute_store().get_dispute(dispute.id)
        self.stored_status.append(stored.status if stored else "missing")
        step = settlement.step(dispute.step_index)
        assert step is not None
        derived = dispute_rating.dispute_job_id(bytes.fromhex(dispute.job_id_hex), dispute.step_index).hex()
        weight = reputation_svc.rating_weight_stroops(step.price_usdc)
        status: RatingStatus = "REPLAY"
        if dispute.id not in self._landed:
            status = self.script.pop(0) if self.script else "SUCCESS"
        if status == "SUCCESS":
            self._landed.add(dispute.id)
        tx = {"SUCCESS": "tx_rating", "TIMEOUT": "tx_rating_inflight", "FAILED": "tx_rating_rejected"}.get(status)
        return RatingOutcome(status, tx, derived, dispute_rating.DISPUTE_RATING, weight)


@pytest.fixture(autouse=True)
def rater(monkeypatch) -> Rater:
    """Every uphold that credits now rates, so every test here has a rater —
    one that lands by default, which is what an ordinary credit meets.

    On a deployment configured to rate at all: the conftest blanks the ledger
    and the signing key to keep the suite offline, and `uphold` checks the
    same presence-only gate the settler does before it submits. Nothing here
    reaches either value — the settler and the rater are both stubbed."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", Keypair.random().secret)
    stub = Rater()
    monkeypatch.setattr(dispute_rating, "submit_dispute_rating", stub)
    return stub


@pytest.fixture(autouse=True)
def invalidated(monkeypatch) -> list[str]:
    """The agents whose cached score was dropped, in order."""
    dropped: list[str] = []
    monkeypatch.setattr(reputation_svc, "invalidate_rep", dropped.append)
    return dropped


def _sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def _seed(payer: str, *, settled_usdc: float = 0.12) -> SettlementRecord:
    """Record a settlement the way `execution_svc` will when a workflow seals."""
    now = time.time()
    record = SettlementRecord(
        task_id=TASK,
        payer=payer,
        auth_id_hex="ab" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=settled_usdc,
        steps=STEPS,
        settled_at=now,
        window_closes_at=now + 3600.0,
    )
    asyncio.run(dispute_store.get_dispute_store().record_settlement(record))
    return record


def a_dispute(*, step: int = 0, settled_usdc: float = 0.12) -> DisputeRecord:
    """A real `open` dispute, opened the way a buyer opens one.

    Through `open_dispute` with a genuine signature rather than written
    straight into the store: everything below judges what an adjudicator may do
    with a dispute, and the answer has to hold for the records story 4.02
    actually writes — including `creditable_usdc`, frozen at opening time,
    which is the first of the three bounds on what may be paid.
    """
    payer = Keypair.random()
    _seed(payer.public_key, settled_usdc=settled_usdc)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, step))
    return asyncio.run(
        dispute_svc.open_dispute(
            job_id_hex=JOB,
            step_index=step,
            reason="the draft ignored half the brief",
            payer=payer.public_key,
            nonce=nonce,
            signature_b64=_sign(payer, dispute_message(JOB, step, nonce)),
        )
    )


class Settler:
    """The chain, as `refund_svc.execute_refund` sees it.

    Stubbed at `execute_refund` rather than deeper, so the REAL mapping from a
    client dict to a `RefundOutcome` still runs — the distinction between
    FAILED and timeout is the thing under test, and a stub that returned
    outcomes directly would assert it away. Records every call, because "how
    many transfers did this path sign" is the question most of these tests ask.
    """

    def __init__(self, *results: dict[str, Any]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, float]] = []

    async def __call__(self, buyer: str, amount_usdc: float) -> dict[str, Any]:
        self.calls.append((buyer, amount_usdc))
        return self._results.pop(0) if self._results else LANDED


def settler(monkeypatch, *results: dict[str, Any]) -> Settler:
    stub = Settler(*results)
    monkeypatch.setattr(refund_svc, "execute_refund", stub)
    return stub


def no_signing(monkeypatch) -> None:
    """Trap every route from this process to the settler's key.

    Mirrors `test_opening_a_dispute_writes_nothing_on_chain`: the claim that a
    path signs nothing is only worth making if taking that path would fail
    loudly, so the ways out are booby-trapped and the path is then run.
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise SignedSomething("this path must not sign anything")

    monkeypatch.setattr(refund_svc, "execute_refund", _boom)
    for name in (
        "invoke_with_server_key",
        "invoke_with_server_key_async",
        "submit_signed_xdr",
        "submit_signed_xdr_async",
        "build_invoke_xdr",
        "signer_public_key",
        "_server",
    ):
        monkeypatch.setattr(sc, name, _boom)


# ── the happy path ──────────────────────────────────────────────


def test_upholding_a_dispute_credits_the_buyer_and_records_the_transaction(monkeypatch) -> None:
    dispute = a_dispute()
    chain = settler(monkeypatch, LANDED)

    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    assert credited.status == "credited"
    assert credited.refund_tx == "tx_credit"
    assert credited.resolved_at is not None
    # The step's own settled price, bounded by what the workflow actually
    # settled — one transfer, to the payer the settlement recorded.
    assert chain.calls == [(dispute.payer, 0.05)]
    # Everything the buyer was told when they opened it is untouched.
    assert credited.id == dispute.id
    assert credited.reason == dispute.reason
    assert credited.creditable_usdc == dispute.creditable_usdc == 0.05
    assert asyncio.run(dispute_svc.get_dispute(dispute.id)) == credited


def test_the_refund_is_claimed_before_anything_is_signed(monkeypatch) -> None:
    """D2, made visible. The claim is the lock, and a lock taken after the
    signature would be decoration: the store must already read `crediting` at
    the instant the transfer goes to the settler's key, so a concurrent caller
    arriving mid-transfer finds it claimed and cannot sign a second one."""
    dispute = a_dispute()
    status_while_signing: list[str] = []

    async def _observe(buyer: str, amount_usdc: float) -> dict[str, Any]:
        mid_flight = await dispute_store.get_dispute_store().get_dispute(dispute.id)
        assert mid_flight is not None
        status_while_signing.append(mid_flight.status)
        return LANDED

    monkeypatch.setattr(refund_svc, "execute_refund", _observe)

    assert asyncio.run(dispute_svc.uphold(dispute.id)).status == "credited"
    assert status_while_signing == ["crediting"]


# ── the acceptance criterion: a retry cannot double-credit ──────


def test_a_second_uphold_after_a_credit_signs_nothing_and_returns_the_first_hash(monkeypatch) -> None:
    """THE acceptance criterion of 4.03. An adjudicator double-clicks, a proxy
    retries a 502, a queue redelivers — the second uphold must answer with the
    dispute exactly as the first one left it and sign nothing at all."""
    dispute = a_dispute()
    chain = settler(monkeypatch, LANDED)
    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    no_signing(monkeypatch)
    again = asyncio.run(dispute_svc.uphold(dispute.id))

    assert again == credited  # the same record, byte for byte
    assert again.refund_tx == "tx_credit"
    assert again.resolved_at == credited.resolved_at
    assert len(chain.calls) == 1


def test_a_credited_dispute_is_answered_without_consulting_the_claim(monkeypatch, rater) -> None:
    """And it must not need the claim to reach that answer. The claim WOULD
    also refuse — a credited dispute is not `upheld` — but making the
    idempotency of a paid dispute depend on a lock in another table means a
    lock that was dropped, expired or never taken becomes a second payment.
    Two independent answers to "has this been paid", and this test is what
    stops the redundant-looking one being tidied away.

    4.04 changed what a repeat uphold DOES, and not this: it now re-attempts
    the dispute RATING, every time, and nothing else (D3). So every way back
    into the refund — the claim, its release, the credit and the transfer
    beneath it — is booby-trapped, and only the rating is let through."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    async def _must_not_be_asked(*args: Any, **kwargs: Any) -> None:
        raise SignedSomething("a credited dispute must never re-enter the refund path")

    store = dispute_store.get_dispute_store()
    monkeypatch.setattr(store, "claim_refund", _must_not_be_asked)
    monkeypatch.setattr(store, "release_refund_claim", _must_not_be_asked)
    monkeypatch.setattr(refund_svc, "credit_refund", _must_not_be_asked)
    no_signing(monkeypatch)

    assert asyncio.run(dispute_svc.uphold(dispute.id)) == credited
    # The rating WAS asked again — and, having landed the first time, the
    # ledger refused it as a replay, so the record is exactly as it was.
    assert rater.calls == 2


def test_a_claim_held_by_somebody_else_returns_the_record_rather_than_paying(monkeypatch) -> None:
    """The race the claim exists to close, forced rather than raced: another
    caller took it between the adjudication and this claim. They are paying, so
    this caller returns what the dispute now says instead of signing a second
    transfer — and it is not an error, because the buyer is being paid."""
    dispute = a_dispute()

    async def _lost_the_race(dispute_id: str) -> None:
        return None

    monkeypatch.setattr(dispute_store.get_dispute_store(), "claim_refund", _lost_the_race)
    no_signing(monkeypatch)

    answered = asyncio.run(dispute_svc.uphold(dispute.id))

    # The adjudication itself still stands — the dispute is upheld, and the
    # winner of the claim is the one paying it.
    assert answered.status == "upheld"
    assert answered.refund_tx is None


# ── the three answers a submitted transfer can have ─────────────


def test_a_failed_transfer_leaves_the_dispute_upheld_and_still_payable(monkeypatch) -> None:
    """FAILED is the one answer that says NO FUNDS MOVED, so it is the one
    answer that may release the claim. The buyer is still owed, the dispute
    goes back to `upheld`, and the next uphold claims it and pays."""
    dispute = a_dispute()
    chain = settler(monkeypatch, REJECTED, LANDED)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refund_failed"
    assert refused.value.status_code == 502
    assert refused.value.existing is None
    stranded = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert stranded is not None and stranded.status == "upheld"
    assert stranded.refund_tx is None  # nothing landed, so nothing is claimed to have

    # Payable again, which is the entire point of releasing the claim.
    credited = asyncio.run(dispute_svc.uphold(dispute.id))
    assert credited.status == "credited"
    assert credited.refund_tx == "tx_credit"
    assert len(chain.calls) == 2


def test_a_timed_out_transfer_keeps_the_claim_and_the_next_uphold_refuses(monkeypatch) -> None:
    """D3, and the reason this story exists at all. A timeout means the
    transfer is ON THE NETWORK and may still settle, so the claim is NOT
    released: the dispute stays `crediting`, carrying the in-flight hash a
    human reconciles from, and every later uphold refuses rather than paying a
    buyer who may already have been paid."""
    dispute = a_dispute()
    chain = settler(monkeypatch, LOST)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refund_unconfirmed"
    assert refused.value.status_code == 504
    assert "never retried" in refused.value.message
    in_flight = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert in_flight is not None and in_flight.status == "crediting"
    assert in_flight.refund_tx == "tx_inflight"  # the reconciliation starts from the record

    with pytest.raises(DisputeError) as again:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert again.value.code == "refund_in_flight"
    assert again.value.status_code == 409
    assert len(chain.calls) == 1  # the second uphold signed nothing


def test_a_timeout_logs_what_a_human_needs_to_reconcile_it(monkeypatch, caplog) -> None:
    """The dispute, the job, the buyer and the amount, in one ERROR line. A
    transfer whose fate is unknown is resolved by a person with a block
    explorer, and this line is where they start."""
    dispute = a_dispute()
    settler(monkeypatch, LOST)

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER), pytest.raises(DisputeError):
        asyncio.run(dispute_svc.uphold(dispute.id))

    # ERROR only, and from this module only: `a_dispute` logs the opening at
    # INFO and `refund_svc` logs its own view of the same timeout.
    logged = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and r.levelno == logging.ERROR]
    assert len(logged) == 1
    assert dispute.id in logged[0]
    assert JOB in logged[0]
    assert dispute.payer in logged[0]
    assert "0.0500000" in logged[0]
    assert "tx_inflight" in logged[0]


def test_an_unrecognised_transfer_status_is_treated_as_unconfirmed(monkeypatch) -> None:
    """A status nobody planned for is a transfer whose fate is unknown, which
    is the timeout hazard under another name. It must land on the conservative
    side — claim held, dispute `crediting` — and never on the one that releases
    a claim over a transfer that might have moved money."""
    dispute = a_dispute()
    settler(monkeypatch, {"status": "PENDING", "hash": "tx_who_knows"})

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refund_unconfirmed"
    stuck = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert stuck is not None and stuck.status == "crediting"
    assert stuck.refund_tx == "tx_who_knows"


# ── the master switch ───────────────────────────────────────────


def test_the_refund_switch_refuses_before_the_store_is_even_read(monkeypatch) -> None:
    """`DISPUTE_REFUNDS_ENABLED` ships OFF and gates the SERVICE, not only the
    route. An operator script that imports this module pays a buyer without
    ever reaching `require_adjudicator`, so the switch is checked here too —
    and checked first, so its answer cannot depend on any dispute's state."""
    dispute = a_dispute()
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)

    async def _must_not_be_read(dispute_id: str) -> None:
        raise AssertionError("the switch is checked before the store is touched")

    store = dispute_store.get_dispute_store()
    monkeypatch.setattr(store, "get_dispute", _must_not_be_read)
    no_signing(monkeypatch)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refunds_disabled"
    assert refused.value.status_code == 503
    # Not adjudicated, not claimed, not paid — the dispute is exactly as the
    # buyer left it.
    assert store._disputes[dispute.id].status == "open"


def test_a_cancelled_transfer_never_releases_the_claim(monkeypatch) -> None:
    """A shutdown cancel can land between the submit and its confirmation, and
    `CancelledError` is a BaseException that no `except Exception` sees. It
    must therefore reach no release: the transfer may have settled, so this has
    to behave exactly like a timeout — the claim stays held and the dispute
    stays `crediting` for a human to reconcile, rather than being handed back
    to a retry that would credit the buyer twice."""
    dispute = a_dispute()

    async def _cancelled_mid_flight(buyer: str, amount_usdc: float) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(refund_svc, "execute_refund", _cancelled_mid_flight)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispute_svc.uphold(dispute.id))

    unaccounted = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert unaccounted is not None and unaccounted.status == "crediting"


# ── refusals raised before the settler's key is touched ─────────


def test_a_credit_above_the_cap_never_reaches_the_signer(monkeypatch) -> None:
    """D5. The ceiling on ONE refund is checked while the number is being
    computed, so there is no amount in the service that has not been through
    it — and a refusal, never a clamp, because quietly paying the ceiling would
    hide the mistaken uphold the ceiling exists to catch."""
    dispute = a_dispute()
    monkeypatch.setattr(settings, "max_refund_usdc", 0.01)  # the step settled for 0.05
    no_signing(monkeypatch)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "refund_above_cap"
    assert refused.value.status_code == 409
    # Nothing was signed, so the claim went back: the buyer is still owed, and
    # raising the ceiling makes this dispute payable without touching it.
    unpaid = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert unpaid is not None and unpaid.status == "upheld"
    assert unpaid.refund_tx is None

    monkeypatch.setattr(settings, "max_refund_usdc", 1.0)
    settler(monkeypatch, LANDED)
    assert asyncio.run(dispute_svc.uphold(dispute.id)).status == "credited"


def test_a_credit_that_computes_to_nothing_is_refused_and_hands_the_claim_back(monkeypatch) -> None:
    """A fraction of zero means the policy now credits nothing for this step.
    That is not a transfer of 0 USDC to the ledger — it is a refusal, and like
    every other pre-signature refusal it releases the claim, because nothing
    was signed and the dispute must stay payable if the policy changes back."""
    dispute = a_dispute()
    monkeypatch.setattr(settings, "dispute_credited_fraction", 0.0)
    no_signing(monkeypatch)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "nothing_to_credit"
    assert refused.value.status_code == 409
    unpaid = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert unpaid is not None and unpaid.status == "upheld"


def test_a_dispute_whose_settlement_is_gone_cannot_be_priced(monkeypatch) -> None:
    """The amount is bounded by what the settlement says actually moved (D4),
    so with the settlement gone there is no number that is safe to pay. The
    in-memory store drops the oldest records under load and a dispute window is
    24 hours wide, so this is a state a real deployment reaches. The claim goes
    back, because a human may still be able to pay this buyer."""
    dispute = a_dispute()
    dispute_store.get_dispute_store()._settlements.clear()
    no_signing(monkeypatch)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "settlement_missing"
    assert refused.value.status_code == 409
    unpaid = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert unpaid is not None and unpaid.status == "upheld"


# ── rejection, and what a rejected dispute may never become ─────


def test_a_rejection_closes_an_open_dispute_without_paying_anything(monkeypatch) -> None:
    no_signing(monkeypatch)

    dispute = a_dispute()
    rejected = asyncio.run(dispute_svc.reject(dispute.id, note="the output matched the brief"))

    assert rejected.status == "rejected"
    assert rejected.refund_tx is None
    assert rejected.resolved_at is not None
    # The buyer's own evidence is untouched by the decision against it.
    assert rejected.reason == dispute.reason
    assert rejected.creditable_usdc == dispute.creditable_usdc
    assert asyncio.run(dispute_svc.get_dispute(dispute.id)) == rejected


def test_a_rejected_dispute_can_never_be_credited(monkeypatch) -> None:
    """The one outcome that must never become payable again. `uphold` refuses
    it outright, and it would also fail to be claimed — a rejected dispute is
    not `upheld` — so no single check carries this on its own."""
    dispute = a_dispute()
    asyncio.run(dispute_svc.reject(dispute.id, note="the output matched the brief"))
    no_signing(monkeypatch)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))

    assert refused.value.code == "dispute_rejected"
    assert refused.value.status_code == 409
    still_rejected = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert still_rejected is not None and still_rejected.status == "rejected"


def test_a_rejection_reason_never_reaches_the_log(monkeypatch, caplog) -> None:
    """The convention `open_dispute` set in 4.02, kept now that the reason is
    mandatory and written for the buyer: free text about one complaint goes on
    the record — and from there onto the buyer's receipt — never into the
    operator's log viewer. The line says an explanation was recorded and whom
    the decision concerns; reproducing the text would put unbounded
    per-complaint prose into a stream read for incidents.

    The reason is stored exactly as cleaning leaves it — here, the ragged
    edges an adjudicator's form leaves are trimmed and nothing else is — and
    a refused rejection logs its code and the dispute, never what was sent."""
    dispute = a_dispute()
    reason = "the SEO brief was delivered in full"

    with caplog.at_level(logging.INFO, logger=SVC_LOGGER):
        rejected = asyncio.run(dispute_svc.reject(dispute.id, note=f"  {reason}\n"))

    logged = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and "rejected" in r.getMessage()]
    assert len(logged) == 1
    assert reason not in logged[0]
    assert "noted=yes" in logged[0]
    assert dispute.id in logged[0] and JOB in logged[0] and dispute.payer in logged[0]
    # ...and it is on the record, cleaned and otherwise verbatim, which is the
    # other half of the same decision.
    assert rejected.note == reason

    caplog.clear()
    other = a_dispute(step=1)
    with caplog.at_level(logging.INFO, logger=SVC_LOGGER), pytest.raises(DisputeError):
        asyncio.run(dispute_svc.reject(other.id, note=" \t\x1b\x07 "))

    (refusal,) = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and "refused" in r.getMessage()]
    assert "rejection_reason_required" in refusal and other.id in refusal
    assert "\x1b" not in refusal and "\x07" not in refusal


@pytest.mark.parametrize("status", ["upheld", "crediting", "credited", "rejected"])
def test_a_dispute_that_is_not_open_cannot_be_rejected(monkeypatch, status: str) -> None:
    """Every other status is refused rather than absorbed, including `rejected`
    itself: a second rejection answered with the existing record would quietly
    accept one adjudicator overruling another, and would accept a rejection of
    a dispute that is mid-payout or already paid — which is the one thing an
    adjudicator most needs to be told they cannot do."""
    no_signing(monkeypatch)
    dispute = a_dispute()
    asyncio.run(dispute_store.get_dispute_store().append_status(dispute.id, status))

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.reject(dispute.id, note="changed my mind"))

    assert refused.value.code == "dispute_not_open"
    assert refused.value.status_code == 409
    assert status in refused.value.message  # the adjudicator is told what it IS
    unchanged = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert unchanged is not None and unchanged.status == status


@pytest.mark.parametrize(
    "adjudicate",
    [dispute_svc.uphold, functools.partial(dispute_svc.reject, note="the output matched the brief")],
    ids=["uphold", "reject"],
)
def test_an_id_nobody_issued_is_refused_by_both_decisions(monkeypatch, adjudicate) -> None:
    """One answer from both routes. A 404 from one and a 409 from the other
    would make them disagree about the same fact, and the API maps whatever
    this module says. The rejection carries a real reason, so the id is the
    only thing left for it to refuse."""
    no_signing(monkeypatch)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(adjudicate("dsp_nosuchdispute"))

    assert refused.value.code == "unknown_dispute"
    assert refused.value.status_code == 404
    assert refused.value.existing is None


# ── the trace on the workflow the refund came out of ────────────


def test_a_credit_is_traced_on_the_workflow_while_it_is_still_on_screen(monkeypatch) -> None:
    """A refund that never appears on the workflow it disputes is a refund the
    buyer has to be told about out of band. The line carries the SOW §3.8
    standard verbatim — the platform FUNDS this credit, the disputed agent
    keeps what it was paid — because this is the only message about a refund a
    buyer ever sees."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    # Two hours old, which is what a dispute actually looks like: the window is
    # 24 hours wide, so a credit lands long after the run it belongs to.
    state.add_task(
        Task(
            id=TASK,
            intent="write the launch post",
            agents=2,
            spent=0.12,
            status="complete",
            started_at=time.time() - 7200.0,
        )
    )

    async def go() -> tuple[DisputeRecord, Any, Any]:
        stream = bus.subscribe(TASK)
        credited = await dispute_svc.uphold(dispute.id)
        return credited, stream.get_nowait(), stream.get_nowait()

    credited, streamed, rated = asyncio.run(go())

    assert credited.status == "credited"
    # Stored on the task AND pushed to anyone watching it — the same lines, and
    # the credit FIRST: the rating is only written once the credit has landed.
    assert state.traces[TASK] == [streamed, rated]
    assert streamed.level == "cost"
    assert streamed.t.startswith("7200.")  # elapsed since the run began, not 00.000
    assert dispute.id in streamed.msg
    assert "0.0500000 USDC" in streamed.msg
    assert "funded by the platform" in streamed.msg
    assert "not clawed back from agent agt_writer" in streamed.msg
    assert "tx_credit" in streamed.msg


def test_a_landed_rating_is_traced_after_the_credit_in_plain_words(monkeypatch) -> None:
    """The credit line tells the buyer the agent kept its money; this is the
    line that says what the agent lost instead. So it states the consequence
    plainly — the score, that an upheld dispute earned it, and the evidence —
    at `proof`, the level every other on-chain rating is traced at."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    state.add_task(Task(id=TASK, intent="write the launch post", agents=2, spent=0.12, status="complete"))

    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    _, rating_line = state.traces[TASK]
    assert rating_line.level == "proof"
    assert f"agent agt_writer rated 10/100 for upheld dispute {dispute.id}" in rating_line.msg
    # The hash AND the derived id, so a reviewer finds the rating either way.
    assert "tx tx_rating" in rating_line.msg
    assert dispute_rating.dispute_job_id(bytes.fromhex(JOB), 0).hex() in rating_line.msg
    assert credited.rating_tx == "tx_rating"


@pytest.mark.parametrize("unlanded", ["FAILED", "TIMEOUT", "REPLAY"])
def test_a_rating_that_did_not_land_is_never_traced_as_one(monkeypatch, rater, unlanded: RatingStatus) -> None:
    """A "rated 10/100" line for evidence that never landed is the lie the
    settler's own trace was once fixed for. Only a SUCCESS earns the line; a
    failure, a timeout and a collision leave the credit line on its own."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    rater.script = [unlanded]
    state.add_task(Task(id=TASK, intent="write the launch post", agents=2, spent=0.12, status="complete"))

    asyncio.run(dispute_svc.uphold(dispute.id))

    assert [line.level for line in state.traces[TASK]] == ["cost"]


def test_a_credit_on_an_evicted_task_creates_no_trace_entry(monkeypatch) -> None:
    """The trap this guard exists for. `state.append_trace` is
    `traces.setdefault(task_id, []).append(line)`, and eviction only ever drops
    traces alongside a task still in `task_order` — so appending for a task
    that is gone would recreate an entry nothing will ever remove again, once
    per refund, invisibly, for the life of the process. The durable record of a
    refund is the dispute; the trace is decoration on a task still on screen."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    assert dispute.task_id not in state.tasks  # evicted hours before it was adjudicated

    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    assert credited.status == "credited"
    assert credited.refund_tx == "tx_credit"  # the credit is recorded where it counts
    assert state.traces == {}


def test_a_trace_that_fails_cannot_undo_a_landed_credit(monkeypatch) -> None:
    """By the time this line is written the transfer has settled and the store
    already reads `credited`. Letting a cosmetic failure raise out of `uphold`
    would answer a successful payout with a 500 and invite the one retry the
    whole path exists to make safe."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    state.add_task(Task(id=TASK, intent="write the launch post", agents=2, spent=0.12, status="complete"))

    async def _wedged(task_id: str, line: Any) -> None:
        raise RuntimeError("no subscriber survived")

    monkeypatch.setattr(bus, "publish", _wedged)

    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    assert credited.status == "credited"
    assert credited.refund_tx == "tx_credit"


def test_a_rejection_keeps_the_adjudicators_reason_on_the_record(monkeypatch) -> None:
    """The asymmetry this closes: the buyer's side of the argument is durable
    from the moment they raise it, and an upheld dispute leaves an amount and a
    hash behind — so a rejection with nothing written down was the outcome most
    likely to be contested and the one with no answer to contest."""
    no_signing(monkeypatch)
    dispute = a_dispute()

    rejected = asyncio.run(dispute_svc.reject(dispute.id, note="the SEO brief was delivered in full"))

    assert rejected.note == "the SEO brief was delivered in full"
    stored = asyncio.run(dispute_svc.get_dispute(dispute.id))
    assert stored is not None and stored.note == rejected.note
    # The platform's side is written beside the buyer's, never over it.
    assert rejected.reason == dispute.reason


def test_a_later_transition_does_not_blank_the_rejection_note(monkeypatch) -> None:
    """4.04 writes the dispute rating onto the same record minutes later. A
    transition that passed no note must leave the one that is there — erasing
    it would destroy the only written record of why the claim was refused."""
    no_signing(monkeypatch)
    dispute = a_dispute()
    rejected = asyncio.run(dispute_svc.reject(dispute.id, note="the SEO brief was delivered in full"))

    rated = asyncio.run(dispute_store.get_dispute_store().append_status(dispute.id, "rejected", rating_tx="tx_rating"))

    assert rated.note == rejected.note
    assert rated.rating_tx == "tx_rating"
    assert rated.resolved_at == rejected.resolved_at


def test_a_rejection_note_is_cleaned_and_bounded_before_it_is_stored(monkeypatch) -> None:
    """The store keeps a note EXACTLY as given — byte for byte, by design — so
    whatever this module leaves is precisely what an auditor reads. Bounding it
    and stripping the control characters is therefore this side's job, and it
    is the same treatment the buyer's `reason` gets: a paragraph survives,
    anything that forges structure does not."""
    no_signing(monkeypatch)
    messy = asyncio.run(dispute_svc.reject(a_dispute().id, note="checked\nthe \x00brief\x1b[31m in full"))

    assert messy.note is not None
    assert "\x00" not in messy.note and "\x1b" not in messy.note
    assert "\n" in messy.note and messy.note.startswith("checked")

    long_note = asyncio.run(dispute_svc.reject(a_dispute(step=1).id, note="x" * 5_000))

    assert long_note.note is not None
    assert len(long_note.note) <= dispute_svc.MAX_REASON_CHARS + len(" …[truncated]")


def test_a_rejection_reason_has_no_default() -> None:
    """MISSING is refused by the signature itself (story 4.06). A default of
    any kind — None, "" — would let a caller reject a dispute without telling
    the buyer why by simply saying nothing, which is the outcome the rule
    exists to make impossible."""
    note = inspect.signature(dispute_svc.reject).parameters["note"]

    assert note.kind is inspect.Parameter.KEYWORD_ONLY
    assert note.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "note",
    [None, "", "  \t \n ", "\x00\x1b\x07"],
    ids=["none", "empty", "whitespace", "control-characters-only"],
)
def test_a_rejection_without_a_usable_reason_is_refused_before_anything_is_written(
    monkeypatch, note: str | None
) -> None:
    """A rejection with no explanation is worse than no dispute system, so a
    reason that is empty AFTER cleaning — whitespace, or nothing but control
    characters — is no reason, and neither is a None from a caller that
    ignored the annotation. Each is refused with the one code the console can
    map to "say why", as the 422 the buyer's own missing `reason` carries.

    Refused FIRST, before the dispute is even read: the check is pure text
    handling and the one refusal an adjudicator can fix and resend, so the
    store is booby-trapped for the call — and the dispute is left exactly as
    the buyer opened it, still open and still rejectable."""
    no_signing(monkeypatch)
    dispute = a_dispute()
    store = dispute_store.get_dispute_store()
    as_opened = store._disputes[dispute.id]

    async def _must_not_be_read(dispute_id: str) -> None:
        raise AssertionError("the reason is checked before the dispute is read")

    monkeypatch.setattr(store, "get_dispute", _must_not_be_read)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.reject(dispute.id, note=cast(str, note)))

    assert refused.value.code == "rejection_reason_required"
    assert refused.value.status_code == 422
    assert refused.value.existing is None
    # Nothing written: the record is the very one the buyer opened.
    assert store._disputes[dispute.id] is as_opened
    assert as_opened.status == "open" and as_opened.note is None


# ── the dispute rating: after the credit, never instead of it ───


def test_the_rating_is_written_only_once_the_credit_is_recorded(monkeypatch, rater) -> None:
    """Story 4.04's ordering, made visible the way the claim's is above. The
    rating must find the dispute already `credited` in the store at the
    instant it is asked for: a rating written first would put a dispute
    consequence on an agent's record for a buyer who might never be paid."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)

    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    assert rater.stored_status == ["credited"]
    assert credited.refund_tx == "tx_credit"
    assert credited.rating_tx == "tx_rating"


def trap_the_refund(monkeypatch) -> None:
    """Booby-trap every way back into the refund: the claim, its release, the
    credit and the transfer beneath it. Installed from INSIDE a rating, so
    anything that reaches the refund after the rating has started fails."""

    async def _refund_touched(*args: Any, **kwargs: Any) -> None:
        raise SignedSomething("the rating path reached the refund")

    store = dispute_store.get_dispute_store()
    monkeypatch.setattr(store, "claim_refund", _refund_touched)
    monkeypatch.setattr(store, "release_refund_claim", _refund_touched)
    monkeypatch.setattr(refund_svc, "credit_refund", _refund_touched)
    no_signing(monkeypatch)


@pytest.mark.parametrize(
    ("unlanded", "rating_tx"),
    [("FAILED", None), ("TIMEOUT", "tx_rating_inflight"), ("REPLAY", None), ("raised", None)],
)
def test_a_rating_that_does_not_land_never_touches_the_refund(
    monkeypatch, rater, unlanded: str, rating_tx: str | None
) -> None:
    """D3. The buyer has been paid by the time the rating is asked for, and
    no answer the rating gets — a failure, a timeout, a collision, or an
    exception out of the attempt itself — may reverse, release or re-sign
    that. Nor may it be RAISED: a paid refund answered with an error is a
    refund the caller will think failed. The dispute stays `credited` with its
    refund hash, and only `rating_tx` says the consequence has not landed."""
    dispute = a_dispute()
    chain = settler(monkeypatch, LANDED)

    async def _rate_with_the_refund_trapped(credited: DisputeRecord, settlement: SettlementRecord) -> RatingOutcome:
        trap_the_refund(monkeypatch)
        if unlanded == "raised":
            raise RuntimeError("the RPC went away mid-rating")
        rater.script = [cast(RatingStatus, unlanded)]
        return await rater(credited, settlement)

    monkeypatch.setattr(dispute_rating, "submit_dispute_rating", _rate_with_the_refund_trapped)

    answered = asyncio.run(dispute_svc.uphold(dispute.id))

    assert answered.status == "credited"
    assert answered.refund_tx == "tx_credit"
    assert answered.rating_tx == rating_tx
    assert asyncio.run(dispute_svc.get_dispute(dispute.id)) == answered
    assert len(chain.calls) == 1


def test_an_open_or_rejected_dispute_is_never_rated(monkeypatch, rater, invalidated) -> None:
    """A dispute is a CLAIM until it is upheld AND paid, and a claim costs the
    agent nothing. Opening one never rates; rejecting one never rates; and an
    uphold of a rejected one is refused before anything could."""
    no_signing(monkeypatch)
    opened = a_dispute()
    rejected = asyncio.run(dispute_svc.reject(a_dispute(step=1).id, note="the SEO brief was delivered in full"))

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(rejected.id))

    assert refused.value.code == "dispute_rejected"
    assert rater.calls == 0
    assert invalidated == []
    for dispute_id in (opened.id, rejected.id):
        stored = asyncio.run(dispute_svc.get_dispute(dispute_id))
        assert stored is not None and stored.rating_tx is None


@pytest.mark.parametrize(("answer", "code"), [(REJECTED, "refund_failed"), (LOST, "refund_unconfirmed")])
def test_an_upheld_dispute_whose_credit_did_not_land_is_never_rated(
    monkeypatch, rater, answer: dict[str, Any], code: str
) -> None:
    """The rating follows the MONEY, not the decision. An uphold whose
    transfer failed leaves a dispute that is upheld and unpaid, and one whose
    transfer timed out leaves one that may or may not be paid — neither is
    `credited`, so neither is rated, and the repeat uphold a timeout refuses
    does not rate either."""
    dispute = a_dispute()
    settler(monkeypatch, answer)

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.uphold(dispute.id))
    assert refused.value.code == code

    if answer is LOST:
        with pytest.raises(DisputeError):
            asyncio.run(dispute_svc.uphold(dispute.id))

    assert rater.calls == 0


def test_a_repeat_uphold_lands_a_rating_that_failed_without_a_second_credit(monkeypatch, rater, invalidated) -> None:
    """The retry path in one test: the rating failed, the buyer was paid, and
    the dispute reads `credited` with no `rating_tx` — paid, not resolved.
    Upholding it again writes the rating and nothing else: the refund path is
    trapped for the whole of the second call."""
    dispute = a_dispute()
    chain = settler(monkeypatch, LANDED)
    rater.script = ["FAILED"]
    paid = asyncio.run(dispute_svc.uphold(dispute.id))
    assert paid.status == "credited" and paid.rating_tx is None
    assert invalidated == []

    trap_the_refund(monkeypatch)
    resolved = asyncio.run(dispute_svc.uphold(dispute.id))

    assert resolved.rating_tx == "tx_rating"
    assert resolved.refund_tx == paid.refund_tx == "tx_credit"
    assert resolved.resolved_at == paid.resolved_at  # a rating does not re-date the resolution
    assert invalidated == ["agt_writer"]
    assert len(chain.calls) == 1


def test_a_rating_with_no_settlement_to_weight_it_is_not_attempted(monkeypatch, rater, caplog) -> None:
    """The weight comes from the settled step's price (D2), so once the
    settlement is gone there is nothing to submit a rating with. The paid
    dispute is answered as it stands — still visibly unrated — and an ERROR
    names every id, because that consequence now needs a human."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    rater.script = ["FAILED"]
    paid = asyncio.run(dispute_svc.uphold(dispute.id))
    dispute_store.get_dispute_store()._settlements.clear()
    trap_the_refund(monkeypatch)
    caplog.clear()  # the first attempt's own FAILED line is not what is under test

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        again = asyncio.run(dispute_svc.uphold(dispute.id))

    assert again == paid and again.rating_tx is None
    assert rater.calls == 1  # the first attempt only
    logged = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and r.levelno == logging.ERROR]
    assert len(logged) == 1
    derived = dispute_rating.dispute_job_id(bytes.fromhex(JOB), 0).hex()
    for fact in (dispute.id, JOB, derived, "agt_writer", dispute.payer):
        assert fact in logged[0]


@pytest.mark.parametrize(
    ("setting", "blank", "named"),
    [
        ("reputation_enabled", False, "REPUTATION_ENABLED is false"),
        ("stellar_reputation_ledger", "", "STELLAR_REPUTATION_LEDGER is unset"),
        ("stellar_signing_key", "", "STELLAR_SIGNING_KEY is unset"),
    ],
)
def test_a_deployment_that_cannot_rate_submits_nothing_and_says_why(
    monkeypatch, rater, invalidated, caplog, setting: str, blank: object, named: str
) -> None:
    """Without the gate, a submit that cannot be signed raises, is classified
    TIMEOUT, and every uphold reports "unconfirmed" forever when the truth is
    "not configured". So nothing is submitted — not by the uphold that pays,
    nor by any repeat — and each one logs ERROR naming the missing setting
    beside every id, while the dispute stays visibly paid-but-unrated."""
    dispute = a_dispute()
    chain = settler(monkeypatch, LANDED)
    monkeypatch.setattr(settings, setting, blank)

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        paid = asyncio.run(dispute_svc.uphold(dispute.id))
        again = asyncio.run(dispute_svc.uphold(dispute.id))

    assert paid.status == "credited" and paid.refund_tx == "tx_credit"
    assert paid.rating_tx is None and again == paid
    assert rater.calls == 0
    assert invalidated == []
    assert len(chain.calls) == 1
    logged = [r.getMessage() for r in caplog.records if r.name == SVC_LOGGER and r.levelno == logging.ERROR]
    assert len(logged) == 2  # one per uphold: each is a paid dispute left unrated
    for fact in (named, dispute.id, JOB, "agt_writer", dispute.payer):
        assert fact in logged[0]
