"""Story 4.03 — the money arithmetic and the transfer (app/services/refund_svc.py).

This is the highest blast radius in the sprint: a settler-funded credit leaves
the PLATFORM's wallet, so every bound that decides the amount has to be pinned,
and every one of them has to be audible in the log when it bites. Three
properties are asserted here rather than left as claims:

  - D4 — the credit is `min(promise, step price × fraction, settled total)`,
    computed from the SETTLEMENT and never from the plan, and each clamp that
    actually reduces the amount is logged with both numbers;
  - D5 — an amount over `MAX_REFUND_USDC` is REFUSED before anything is signed,
    which is asserted by making the stellar client explode if it is ever reached;
  - D3 — SUCCESS, FAILED and TIMEOUT are three distinct outcomes, and anything
    whose fate is unknown lands on TIMEOUT (never FAILED, which would invite a
    retry that credits the buyer twice).

Hermetic: dataclass records built in-process, the stellar client monkeypatched.
No chain, no network, no database.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.config import settings
from app.services import refund_svc
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep
from app.services.refund_svc import RefundRefused

JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 16 bytes of job id, as hex
TASK = "tsk_disputed"
PAYER = Keypair.random().public_key
SIGNING_SECRET = Keypair.random().secret

STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id="agt_seo", agent_name="SEO Brief", price_usdc=0.07, delivered=True),
    SettlementStep(step_index=2, agent_id="agt_dead", agent_name="Failed", price_usdc=0.09, delivered=False),
)


def _settlement(*, settled_usdc: float = 0.12, steps: tuple[SettlementStep, ...] = STEPS) -> SettlementRecord:
    """A settlement as `execution_svc` records one when a workflow seals."""
    return SettlementRecord(
        task_id=TASK,
        payer=PAYER,
        auth_id_hex="ab" * 16,
        job_id_hex=JOB,
        charge_tx="tx_charge",
        proof_tx="tx_proof",
        settled_usdc=settled_usdc,
        steps=steps,
        settled_at=1_700_000_000.0,
        window_closes_at=1_700_086_400.0,
    )


def _dispute(*, step_index: int = 0, creditable_usdc: float = 0.05) -> DisputeRecord:
    """An upheld dispute, carrying the credit the buyer was promised at open time."""
    return DisputeRecord(
        id="dsp_deadbeefdeadbeef",
        job_id_hex=JOB,
        task_id=TASK,
        step_index=step_index,
        agent_id="agt_writer",
        payer=PAYER,
        reason="the draft was empty",
        status="upheld",
        charged_usdc=0.05,
        creditable_usdc=creditable_usdc,
        opened_at=1_700_000_100.0,
    )


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.services.refund_svc" and r.levelno == level]


def test_credit_is_the_promise_when_no_bound_bites(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        assert refund_svc.creditable_for(_settlement(), _dispute()) == 0.05
    assert _records(caplog, logging.WARNING) == [], "nothing was clamped, so nothing may be logged as clamped"


def test_the_step_price_clamps_a_stale_promise_and_says_so(caplog) -> None:
    """The dispute was opened promising more than the step ever cost. The step
    price wins, and the pair of numbers has to be in the log."""
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        amount = refund_svc.creditable_for(_settlement(), _dispute(creditable_usdc=0.09))

    assert amount == 0.05
    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any(
        "clamped by the step price" in m and "0.0900000" in m and "0.0500000" in m and JOB in m and PAYER in m
        for m in msgs
    ), f"the step-price clamp was not logged with both numbers: {msgs}"


def test_the_settled_total_clamps_the_step_price_and_says_so(caplog) -> None:
    """The charge floors and rounds, so what settled can be below the step's
    own price — and then it, not the price, is the ceiling."""
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        amount = refund_svc.creditable_for(_settlement(settled_usdc=0.01), _dispute())

    assert amount == 0.01
    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any(
        "clamped by the settled total" in m and "0.0500000" in m and "0.0100000" in m and JOB in m and PAYER in m
        for m in msgs
    ), f"the settled-total clamp was not logged with both numbers: {msgs}"


def test_a_lowered_fraction_clamps_the_step_bound(caplog) -> None:
    """Lowering DISPUTE_CREDITED_FRACTION applies to disputes already open —
    raising it cannot, because the promise frozen at open time still caps it."""
    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        assert refund_svc.creditable_for(_settlement(), _dispute(), 0.5) == 0.025
        assert refund_svc.creditable_for(_settlement(), _dispute(), 2.0) == 0.05  # fraction clamped to 1.0

    msgs = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert any("clamped by the step price" in m and "0.0250000" in m for m in msgs), (
        f"the halved step bound was not logged: {msgs}"
    )


def test_the_amount_comes_from_the_settlement_not_the_plan_estimate() -> None:
    """The guard against paying an estimate: a settlement whose steps priced
    high but which settled low credits the settled figure, every time."""
    generous = (SettlementStep(step_index=0, agent_id="agt_writer", agent_name="C", price_usdc=0.9, delivered=True),)
    amount = refund_svc.creditable_for(_settlement(settled_usdc=0.02, steps=generous), _dispute(creditable_usdc=0.9))
    assert amount == 0.02


def _no_signing(monkeypatch) -> None:
    """Make the settler's key explode if anything reaches it. A refusal that is
    logged but still signs is not a refusal, and only this catches that."""

    async def _exploding_invoke(contract_id: str, function_name: str, args: list) -> dict:
        raise AssertionError("a refused refund must never reach the stellar client")

    monkeypatch.setattr(sc, "invoke_with_server_key_async", _exploding_invoke)


def test_a_step_the_settlement_does_not_have_refuses(monkeypatch, caplog) -> None:
    _no_signing(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="app.services.refund_svc"):
        with pytest.raises(RefundRefused) as exc:
            refund_svc.creditable_for(_settlement(), _dispute(step_index=7))

    assert exc.value.code == "nothing_to_credit"
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any("dsp_deadbeefdeadbeef" in m and JOB in m and PAYER in m and "nothing_to_credit" in m for m in msgs), (
        f"the refusal was not logged with its context: {msgs}"
    )


def test_a_step_that_never_delivered_refuses(monkeypatch) -> None:
    """An undelivered step was never part of what the buyer paid for, so there
    is nothing to give back — whatever the dispute was opened promising."""
    _no_signing(monkeypatch)
    with pytest.raises(RefundRefused) as exc:
        refund_svc.creditable_for(_settlement(), _dispute(step_index=2, creditable_usdc=0.09))
    assert exc.value.code == "nothing_to_credit"


def test_a_credit_that_computes_to_zero_refuses(monkeypatch) -> None:
    _no_signing(monkeypatch)
    with pytest.raises(RefundRefused) as exc:
        refund_svc.creditable_for(_settlement(), _dispute(creditable_usdc=0.0))
    assert exc.value.code == "nothing_to_credit"

    # Zero from the other direction: a workflow that settled for nothing.
    with pytest.raises(RefundRefused) as exc:
        refund_svc.creditable_for(_settlement(settled_usdc=0.0), _dispute())
    assert exc.value.code == "nothing_to_credit"


def test_an_amount_over_the_cap_refuses_before_anything_is_signed(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "max_refund_usdc", 0.01)
    _no_signing(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="app.services.refund_svc"):
        with pytest.raises(RefundRefused) as exc:
            refund_svc.creditable_for(_settlement(), _dispute())

    assert exc.value.code == "refund_above_cap"
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(
        "exceeds MAX_REFUND_USDC" in m
        and "0.0500000" in m
        and "0.0100000" in m
        and "dsp_deadbeefdeadbeef" in m
        and JOB in m
        and PAYER in m
        for m in msgs
    ), f"the refused over-cap credit was not logged with its context: {msgs}"


def test_a_hand_rolled_over_cap_amount_never_reaches_the_signer(monkeypatch) -> None:
    """The ceiling lives in `creditable_for`, but a caller that computed its own
    number must not be able to walk around it either."""
    monkeypatch.setattr(settings, "max_refund_usdc", 0.01)
    _no_signing(monkeypatch)

    with pytest.raises(RefundRefused) as exc:
        asyncio.run(refund_svc.credit_refund(_dispute(), 5.0))
    assert exc.value.code == "refund_above_cap"


def test_a_zero_amount_never_reaches_the_signer(monkeypatch) -> None:
    _no_signing(monkeypatch)
    with pytest.raises(RefundRefused) as exc:
        asyncio.run(refund_svc.credit_refund(_dispute(), 0.0))
    assert exc.value.code == "nothing_to_credit"


def _fake_transfer(monkeypatch, outcome: dict | BaseException) -> list[dict]:
    """Stand in for the SAC transfer, recording what was submitted.

    Patched at the stellar client, not at `execute_refund`, so the wrapper is
    exercised through the real transfer builder — which is what makes the
    "never reached the signer" assertions elsewhere in this file mean anything.
    """
    calls: list[dict] = []

    async def _invoke(contract_id: str, function_name: str, args: list) -> dict:
        calls.append({"contract": contract_id, "fn": function_name, "args": args})
        # BaseException, not Exception: a cancel is one of the cases under test.
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sc, "signer_public_key", lambda: "GSETTLER")
    monkeypatch.setattr(sc, "invoke_with_server_key_async", _invoke)
    monkeypatch.setattr(sc, "addr", lambda a: ("addr", a))
    monkeypatch.setattr(sc, "i128", lambda v: ("i128", v))  # usdc_to_i128 stays real
    return calls


def test_a_settled_transfer_is_a_success_outcome(monkeypatch) -> None:
    calls = _fake_transfer(monkeypatch, {"status": "SUCCESS", "hash": "refund_tx"})

    outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.tx_hash, outcome.amount_usdc) == ("SUCCESS", "refund_tx", 0.05)
    # settler -> the disputing payer, in stroops: the credit, not a clawback.
    assert calls[0]["fn"] == "transfer"
    assert calls[0]["args"] == [("addr", "GSETTLER"), ("addr", PAYER), ("i128", 500_000)]


def test_a_rejected_transfer_is_a_failed_outcome(monkeypatch, caplog) -> None:
    """FAILED is the only answer that says no funds moved, so it is the only
    one a caller may release the refund claim on."""
    _fake_transfer(monkeypatch, {"status": "FAILED", "hash": "refund_tx"})

    with caplog.at_level(logging.ERROR, logger="app.services.refund_svc"):
        outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.tx_hash) == ("FAILED", "refund_tx")
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(
        "did not settle" in m and "no funds moved" in m and JOB in m and PAYER in m and "0.0500000" in m for m in msgs
    ), f"a failed credit was not logged with its context: {msgs}"


def test_a_timed_out_transfer_is_a_timeout_outcome_and_keeps_its_hash(monkeypatch, caplog) -> None:
    """The double-credit hazard: submitted, unconfirmed, may still land. The
    in-flight hash has to survive into the log and the outcome, because manual
    reconciliation starts from it."""
    _fake_transfer(monkeypatch, {"status": "timeout", "hash": "inflight_tx"})

    with caplog.at_level(logging.ERROR, logger="app.services.refund_svc"):
        outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.tx_hash) == ("TIMEOUT", "inflight_tx")
    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(
        "do not retry" in m
        and "inflight_tx" in m
        and "dsp_deadbeefdeadbeef" in m
        and JOB in m
        and PAYER in m
        and "0.0500000" in m
        for m in msgs
    ), f"an unconfirmed credit was not logged for reconciliation: {msgs}"


def test_a_success_without_a_hash_is_a_timeout(monkeypatch) -> None:
    """No receipt means no proof it landed and no way to check later — the
    unknown bucket, never FAILED, which would invite a second credit."""
    _fake_transfer(monkeypatch, {"status": "SUCCESS"})
    assert asyncio.run(refund_svc.credit_refund(_dispute(), 0.05)).status == "TIMEOUT"


def test_an_unrecognised_status_is_a_timeout(monkeypatch) -> None:
    _fake_transfer(monkeypatch, {"status": "NOT_FOUND", "hash": "maybe_tx"})
    assert asyncio.run(refund_svc.credit_refund(_dispute(), 0.05)).status == "TIMEOUT"


@pytest.mark.parametrize("status", ["failed", "Failed", "FAILED "])
def test_only_the_exact_word_failed_releases_a_claim(monkeypatch, status: str) -> None:
    """FAILED is matched exactly, the way SUCCESS is.

    The two branches disagreed: SUCCESS was compared verbatim while FAILED was
    case-folded first, which made the door that says "nothing was signed" —
    the only one a caller may release the refund claim through — wider than
    the door that says the credit landed. A status this module did not agree
    on is a transfer whose fate is unknown, so it belongs in TIMEOUT, which
    holds the claim and pays the buyer late rather than twice.
    """
    _fake_transfer(monkeypatch, {"status": status, "hash": "maybe_tx"})

    outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.tx_hash) == ("TIMEOUT", "maybe_tx")


def test_a_raising_transfer_is_a_timeout_and_is_logged(monkeypatch, caplog) -> None:
    """A raise can happen either side of the submission and the exception does
    not say which, so it is treated as in-flight: logged, never retried."""
    _fake_transfer(monkeypatch, RuntimeError("soroban rpc unreachable"))

    with caplog.at_level(logging.ERROR, logger="app.services.refund_svc"):
        outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.tx_hash) == ("TIMEOUT", None)
    records = _records(caplog, logging.ERROR)
    assert records, "a raising credit was never logged"
    assert any(
        "do not retry" in r.getMessage() and "soroban rpc unreachable" in r.getMessage() and r.exc_info is not None
        for r in records
    ), f"the raise was not logged with its traceback: {[r.getMessage() for r in records]}"


def test_refund_logs_never_carry_the_signing_key(monkeypatch, caplog) -> None:
    """Every line this module writes is about money, and the settler's key is
    the thing that moves it — message and rendered traceback alike."""
    monkeypatch.setattr(settings, "stellar_signing_key", SIGNING_SECRET)
    _fake_transfer(monkeypatch, RuntimeError("soroban rpc unreachable"))
    formatter = logging.Formatter("%(message)s")

    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        amount = refund_svc.creditable_for(_settlement(settled_usdc=0.01), _dispute(creditable_usdc=0.09))
        asyncio.run(refund_svc.credit_refund(_dispute(), amount))
        with pytest.raises(RefundRefused):
            refund_svc.creditable_for(_settlement(), _dispute(step_index=7))

    records = [r for r in caplog.records if r.name == "app.services.refund_svc"]
    assert records
    for rec in records:
        assert SIGNING_SECRET not in formatter.format(rec)


def test_cancellation_mid_transfer_is_logged_and_reraised(monkeypatch, caplog) -> None:
    """A deploy-triggered cancel can land between the submit and its
    confirmation, and CancelledError is a BaseException the generic handler
    never sees. The reconstruction line must fire anyway; cancellation
    semantics are preserved by re-raising, which keeps the refund claim held."""
    _fake_transfer(monkeypatch, asyncio.CancelledError())

    with caplog.at_level(logging.ERROR, logger="app.services.refund_svc"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    msgs = [r.getMessage() for r in _records(caplog, logging.ERROR)]
    assert any(
        "cancelled mid-flight" in m
        and "do not retry" in m
        and "dsp_deadbeefdeadbeef" in m
        and JOB in m
        and PAYER in m
        and "0.0500000" in m
        for m in msgs
    ), f"a cancelled credit was never logged with its context: {msgs}"
