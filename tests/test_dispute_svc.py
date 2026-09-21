"""The rules that decide whether a dispute may exist (app/services/dispute_svc.py).

Story 4.02, ADR 0002. Every credit story 4.03 pays starts as a record written
here, so this file is the one that has to be paranoid: one test per rule, plus
the two properties that hold across all of them — a dispute writes NOTHING
on-chain, and a caller who cannot prove they are the buyer learns nothing about
the workflow beyond the fact that it settled (which the escrow's `charged` event
already says in public).

Hermetic: the in-memory dispute store, an in-process challenge table, and real
ed25519 keys. No chain, no network, no database. The store singleton and the
challenge table are reset between tests so nothing leaks from one rule to the
next.
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
from app.services import external_binding as eb
from app.services.dispute_store import SettlementRecord, SettlementStep
from app.services.dispute_svc import dispute_message

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_seo", agent_name="SEO Brief", price_usdc=0.07, delivered=True),
)


@pytest.fixture(autouse=True)
def _fresh_state():
    """A store and a challenge table per test — both are process singletons."""
    dispute_store._store = None
    eb._challenges.clear()
    yield
    dispute_store._store = None
    eb._challenges.clear()


def _sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def _seed(
    payer: str,
    *,
    job: str = JOB,
    task: str = TASK,
    steps: tuple[SettlementStep, ...] = STEPS,
    settled_usdc: float = 0.12,
    window_seconds: float = 3600.0,
) -> SettlementRecord:
    """Record a settlement the way `execution_svc` will when a workflow seals."""
    now = time.time()
    record = SettlementRecord(
        task_id=task,
        payer=payer,
        auth_id_hex="ab" * 16,
        job_id_hex=job,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=settled_usdc,
        steps=steps,
        settled_at=now,
        window_closes_at=now + window_seconds,
    )
    asyncio.run(dispute_store.get_dispute_store().record_settlement(record))
    return record


def _open(
    payer: Keypair,
    *,
    job: str = JOB,
    step: int = 0,
    reason: str = "the draft ignored half the brief",
    nonce: str | None = None,
    signature: str | None = None,
    claimed_payer: str | None = None,
):
    """The whole flow a router performs: mint a challenge, sign it, open."""
    if nonce is None:
        nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(job, step))
    if signature is None:
        signature = _sign(payer, dispute_message(job, step, nonce))
    return asyncio.run(
        dispute_svc.open_dispute(
            job_id_hex=job,
            step_index=step,
            reason=reason,
            payer=claimed_payer or payer.public_key,
            nonce=nonce,
            signature_b64=signature,
        )
    )


# ── the happy path ──────────────────────────────────────────────


def test_the_payer_opens_a_dispute_against_a_settled_step() -> None:
    payer = Keypair.random()
    _seed(payer.public_key)

    record = _open(payer, step=1)

    assert record.status == "open"
    assert record.id.startswith("dsp_")
    assert record.job_id_hex == JOB
    assert record.task_id == TASK
    assert record.step_index == 1
    assert record.agent_id == "agt_seo"  # the agent that delivered THAT step
    assert record.payer == payer.public_key
    assert record.reason == "the draft ignored half the brief"
    # The step's own settled price, and the full credit the default policy
    # promises for it — not the workflow's 0.12 total.
    assert record.charged_usdc == 0.07
    assert record.creditable_usdc == 0.07
    assert record.resolved_at is None and record.refund_tx is None and record.rating_tx is None
    assert record.opened_at <= time.time()


def test_the_credit_follows_the_configured_fraction(monkeypatch) -> None:
    """`creditable_usdc` is frozen at opening time from the policy in force,
    so a later change to the setting cannot rewrite what a buyer was shown."""
    payer = Keypair.random()
    _seed(payer.public_key)
    monkeypatch.setattr(settings, "dispute_credited_fraction", 0.5)

    record = _open(payer, step=1)

    assert record.creditable_usdc == refund_svc.credited_amount_usdc(0.07, 0.5) == 0.035
    monkeypatch.setattr(settings, "dispute_credited_fraction", 1.0)
    assert asyncio.run(dispute_svc.get_dispute(record.id)).creditable_usdc == 0.035


def test_opening_a_dispute_writes_nothing_on_chain(monkeypatch) -> None:
    """The boundary ADR 0002 draws: a dispute is a CLAIM. 4.03 pays the credit
    and 4.04 writes the rating; 4.02 must not submit a transaction, must not
    read the chain, and must not touch reputation — so every way out of this
    process is booby-trapped and the happy path still runs."""

    def _boom(*args, **kwargs):
        raise AssertionError("story 4.02 must not touch the chain")

    for name in (
        "simulate_read",
        "invoke_with_server_key",
        "invoke_with_server_key_async",
        "submit_rating",
        "submit_rating_async",
        "submit_signed_xdr",
        "submit_signed_xdr_async",
        "build_invoke_xdr",
        "signer_public_key",
        "_server",
    ):
        monkeypatch.setattr(sc, name, _boom)
    monkeypatch.setattr(refund_svc, "execute_refund", _boom)
    monkeypatch.setattr(refund_svc, "record_dispute_rating", _boom)

    payer = Keypair.random()
    _seed(payer.public_key)

    assert _open(payer).status == "open"


def test_a_successful_dispute_consumes_its_challenge() -> None:
    """Single use, end to end: the proof that opened this dispute cannot be
    replayed, and the record is what the buyer is answered with afterwards."""
    payer = Keypair.random()
    _seed(payer.public_key)
    nonce, _ = asyncio.run(dispute_svc.issue_dispute_challenge(JOB, 0))

    record = _open(payer, nonce=nonce)

    assert eb.dispute_challenge_is_live(JOB, 0, nonce) is False
    assert asyncio.run(dispute_svc.get_dispute(record.id)) == record


# ── the read surface four other lanes code against ──────────────


def test_the_read_surface_answers_by_id_and_by_task() -> None:
    payer = Keypair.random()
    settlement = _seed(payer.public_key)

    first = _open(payer, step=0)
    second = _open(payer, step=1)

    assert asyncio.run(dispute_svc.get_dispute(first.id)) == first
    assert asyncio.run(dispute_svc.get_dispute("dsp_nosuchdispute")) is None
    assert asyncio.run(dispute_svc.list_for_task(TASK)) == (first, second)
    assert asyncio.run(dispute_svc.list_for_task("tsk_other")) == ()
    assert asyncio.run(dispute_svc.settlement_for_task(TASK)) == settlement
    assert asyncio.run(dispute_svc.settlement_for_task("tsk_other")) is None
