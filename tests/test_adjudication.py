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
  5. a rejected dispute is never payable.

Hermetic: the in-memory dispute store, an in-process challenge table and real
ed25519 keys, with the settler's SAC transfer stubbed at `execute_refund`. No
chain, no network, no database. Every process singleton these paths touch — the
store, the challenge table, app state and the trace bus — is reset per test.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.services import dispute_store, dispute_svc, refund_svc
from app.services import external_binding as eb
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep
from app.services.dispute_svc import dispute_message
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
def _fresh_state():
    """Every singleton these paths reach, reset around each test."""
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


def test_a_credited_dispute_is_answered_without_consulting_the_claim(monkeypatch) -> None:
    """And it must not need the claim to reach that answer. The claim WOULD
    also refuse — a credited dispute is not `upheld` — but making the
    idempotency of a paid dispute depend on a lock in another table means a
    lock that was dropped, expired or never taken becomes a second payment.
    Two independent answers to "has this been paid", and this test is what
    stops the redundant-looking one being tidied away."""
    dispute = a_dispute()
    settler(monkeypatch, LANDED)
    credited = asyncio.run(dispute_svc.uphold(dispute.id))

    async def _must_not_be_asked(dispute_id: str) -> None:
        raise SignedSomething("a credited dispute must be answered from its own status")

    monkeypatch.setattr(dispute_store.get_dispute_store(), "claim_refund", _must_not_be_asked)
    no_signing(monkeypatch)

    assert asyncio.run(dispute_svc.uphold(dispute.id)) == credited


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
