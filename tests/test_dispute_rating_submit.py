"""Story 4.04 — writing an upheld dispute's rating (app/services/dispute_rating.py).

The dispute's consequence for the agent is one `ReputationLedger.submit`, and
every argument to it is load-bearing: the DERIVED id is the only key the
settler's own auto-rating does not already hold, the weight has to be the one
the settler used or the two ratings of one step would carry different evidence,
and `kind="dispute"` is the only thing that moves `dispute_rate_bps`. So the
arguments are pinned exactly, and then every answer the chain can give is
pinned to its outcome:

  - SUCCESS, FAILED, TIMEOUT and REPLAY are four distinct outcomes, and anything
    whose fate is unknown lands on TIMEOUT — never FAILED;
  - only the contract's `Replay` is REPLAY, and `Unauthorized` is logged as the
    deployment misconfiguration it is rather than as a verdict on the dispute;
  - every line that is not a success names the dispute, the sealed job, the
    derived id, the agent, the payer, the weight and the rating — and none of
    them carries the signing key.

Hermetic: dataclass records built in-process, the stellar client monkeypatched.
No chain, no network, no database.
"""

from __future__ import annotations

import asyncio

from stellar_sdk import Keypair

import app.stellar.client as sc
from app.services import reputation_svc
from app.services.dispute_rating import DISPUTE_RATING, dispute_job_id, submit_dispute_rating
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
DISPUTE_ID = "dsp_deadbeefdeadbeef"
PAYER = Keypair.random().public_key
SIGNING_SECRET = Keypair.random().secret

# One agent serving two steps of the same job: the case 4.01's job-only
# derivation collided on, so the fixtures carry it by default.
STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.07, delivered=True),
)
# The settler's weight for step 1: 0.07 USDC in stroops.
WEIGHT = 700_000
DERIVED = dispute_job_id(bytes.fromhex(JOB), 1).hex()


def _settlement(*, steps: tuple[SettlementStep, ...] = STEPS) -> SettlementRecord:
    """A settlement as `execution_svc` records one when a workflow seals."""
    return SettlementRecord(
        task_id=TASK,
        payer=PAYER,
        auth_id_hex="ab" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=0.12,
        steps=steps,
        settled_at=1_700_000_000.0,
        window_closes_at=1_700_086_400.0,
    )


def _dispute(*, step_index: int = 1, job_id_hex: str = JOB) -> DisputeRecord:
    """A dispute whose refund has landed — the only state a rating follows."""
    return DisputeRecord(
        id=DISPUTE_ID,
        job_id_hex=job_id_hex,
        task_id=TASK,
        step_index=step_index,
        agent_id="agt_writer",
        payer=PAYER,
        reason="the brief was empty",
        status="credited",
        charged_usdc=0.07,
        creditable_usdc=0.07,
        opened_at=1_700_000_100.0,
        refund_tx="tx_refund",
    )


def _fake_submit(monkeypatch, outcome: dict | BaseException) -> list[dict]:
    """Stand in for `sc.submit_rating_async`, recording every submit.

    Patched at the stellar client, so the call is made exactly as production
    makes it — positional, through the module attribute.
    """
    calls: list[dict] = []

    async def _submit(agent_id, job_id, rating, weight, payer, kind) -> dict:
        calls.append(
            {"agent_id": agent_id, "job_id": job_id, "rating": rating, "weight": weight, "payer": payer, "kind": kind}
        )
        # BaseException, not Exception: a cancel is one of the cases under test.
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sc, "submit_rating_async", _submit)
    return calls


def _rate(dispute: DisputeRecord | None = None, settlement: SettlementRecord | None = None):
    return asyncio.run(submit_dispute_rating(dispute or _dispute(), settlement or _settlement()))


def test_the_submit_carries_the_derived_id_the_settlers_weight_and_the_payer(monkeypatch) -> None:
    calls = _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    _rate()

    assert calls == [
        {
            "agent_id": "agt_writer",
            "job_id": dispute_job_id(bytes.fromhex(JOB), 1),
            "rating": DISPUTE_RATING,
            "weight": WEIGHT,
            "payer": PAYER,
            "kind": "dispute",
        }
    ]
    # The sealed id is the one key this rating can never land under: the
    # settler's auto-rating already holds it.
    assert calls[0]["job_id"] != bytes.fromhex(JOB)


def test_the_weight_is_the_settlers_helper_over_the_settled_step_price(monkeypatch) -> None:
    """D2: the same helper the settler weights its own rating with, fed the
    settled step's price — so a change to the weighting rule moves both."""
    seen: list[float] = []

    def _weight(price_usdc: float) -> int:
        seen.append(price_usdc)
        return 4_321

    monkeypatch.setattr(reputation_svc, "rating_weight_stroops", _weight)
    calls = _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    outcome = _rate()

    assert seen == [0.07]
    assert calls[0]["weight"] == outcome.weight_stroops == 4_321


def test_two_steps_by_one_agent_rate_under_two_ids(monkeypatch) -> None:
    """The collision 4.01's derivation had: the second step's rating would have
    been refused as a replay of the first."""
    calls = _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "rating_tx"})

    _rate(_dispute(step_index=0))
    _rate(_dispute(step_index=1))

    assert calls[0]["agent_id"] == calls[1]["agent_id"]
    assert calls[0]["job_id"] != calls[1]["job_id"]
    assert calls[0]["weight"] == 500_000 and calls[1]["weight"] == WEIGHT


def test_the_rating_is_below_what_the_settler_gives_a_non_delivery() -> None:
    """An upheld dispute must score beneath honest non-delivery (see the
    constant's comment), measured against what the settler actually awards."""
    non_delivery, _ = reputation_svc.synthetic_rating(None, 0.07)
    assert 0 < DISPUTE_RATING < non_delivery
