"""Money-affecting settlement failures must reach the server log, not only the
trace stream: state.traces is evicted after 200 tasks and lost on every restart,
so a charge that never landed — or one that landed with nothing sealed against
it — would otherwise leave no server-side record at all. The logs must also
never carry the signing key."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from stellar_sdk import Keypair

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan
from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

AUTH_ID_HEX = "ab" * 16
JOB_ID = b"\x01" * 16
SIGNING_SECRET = Keypair.random().secret
PAYER = Keypair.random().public_key


@pytest.fixture(autouse=True)
def clean_traces():
    yield
    for task_id in [t for t in state.traces if t.startswith("tsk_settle_")]:
        state.traces.pop(task_id, None)


def _plan() -> StoredPlan:
    return StoredPlan(
        id="pln_settle",
        intent="settle something",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id="agt_x",
                    agent_name="w.x",
                    rationale="r",
                    est_price_usdc=0.05,
                    est_eta_seconds=1.0,
                )
            ]
        ),
        total_usdc=0.05,
        total_eta=1.0,
    )


def _settle(task_id: str) -> tuple[str | None, str | None, bytes | None]:
    return asyncio.run(
        execution_svc._settle_onchain(
            task_id,
            time.monotonic(),
            _plan(),
            payer=PAYER,
            auth_id_hex=AUTH_ID_HEX,
            total_usdc=0.05,
        )
    )


def _errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.services.execution_svc" and r.levelno >= logging.ERROR]


def _use_fake_signer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "stellar_signing_key", SIGNING_SECRET)
    monkeypatch.setattr(sc, "_signer_keypair", lambda: Keypair.from_secret(SIGNING_SECRET))


def test_skipped_settlement_without_signing_key_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(settings, "stellar_signing_key", "")
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle("tsk_settle_nokey") == (None, None, None)

    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any("tsk_settle_nokey" in m and AUTH_ID_HEX in m and PAYER in m and "0.050000" in m for m in msgs), (
        f"unbilled settlement was not logged with its context: {msgs}"
    )


def test_total_above_charge_cap_never_invokes_charge_and_is_logged(monkeypatch, caplog):
    _use_fake_signer(monkeypatch)
    monkeypatch.setattr(settings, "max_charge_usdc", 0.01)

    async def no_invoke(contract_id, function_name, args):
        raise AssertionError("an over-cap total must never reach PaymentEscrow.charge")

    monkeypatch.setattr(sc, "invoke_with_server_key_async", no_invoke)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle("tsk_settle_cap") == (None, None, None)

    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any(
        "tsk_settle_cap" in m and "exceeds MAX_CHARGE_USDC" in m and AUTH_ID_HEX in m and PAYER in m and "0.050000" in m
        for m in msgs
    ), f"a refused over-cap charge was not logged with its context: {msgs}"


def test_charge_that_does_not_settle_is_logged(monkeypatch, caplog):
    _use_fake_signer(monkeypatch)

    async def fake_invoke(contract_id, function_name, args):
        assert function_name == "charge"
        return {"status": "FAILED", "hash": "chargehash123"}

    monkeypatch.setattr(sc, "invoke_with_server_key_async", fake_invoke)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle("tsk_settle_charge") == ("chargehash123", None, None)

    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any(
        "charge did not settle" in m
        and "status=FAILED" in m
        and "chargehash123" in m
        and AUTH_ID_HEX in m
        and PAYER in m
        for m in msgs
    ), f"a failed charge was not logged with its context: {msgs}"


@pytest.mark.parametrize("status", ["timeout", "", "PENDING"], ids=["timed-out", "missing", "unrecognised"])
def test_an_unconfirmed_charge_is_logged_as_undisputable_not_as_a_failure(monkeypatch, caplog, status):
    """`"timeout"` is the client's word for submitted and then lost track of —
    the same unknown `refund_svc.RefundStatus` names for a transfer, and the
    charge may settle two ledgers later.

    It used to be logged as a charge that "did not settle", which is precisely
    the thing nobody knows. The consequence is one-sided: no settlement row is
    written, so if the charge DOES land the buyer is charged and
    `issue_dispute_challenge` answers `unknown_job` until the 24-hour window
    expires. The operator's only route in is this line, so it has to name the
    job, the payer and the amount, and say the buyer cannot dispute it."""
    _use_fake_signer(monkeypatch)

    async def fake_invoke(contract_id, function_name, args):
        assert function_name == "charge"
        return {"status": status, "hash": "chargehash123"}

    monkeypatch.setattr(sc, "invoke_with_server_key_async", fake_invoke)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle("tsk_settle_unconfirmed") == ("chargehash123", None, None)

    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any(
        "UNCONFIRMED" in m
        and "MAY STILL SETTLE" in m
        and "NO WAY TO DISPUTE" in m
        and "chargehash123" in m
        and AUTH_ID_HEX in m
        and PAYER in m
        and "0.050000" in m
        for m in msgs
    ), f"an unconfirmed charge was not logged as one: {msgs}"
    # Never as a charge that did not settle: that is the claim the code cannot
    # make, and an operator who reads it stops reconciling.
    assert not any("did not settle" in m for m in msgs)

    # The buyer is told in their own terms, and the job id — what a dispute is
    # filed against — stays out of a world-readable trace.
    lines = [ln.msg for ln in state.traces["tsk_settle_unconfirmed"] if ln.level == "error"]
    assert any("unconfirmed" in m and "cannot be disputed" in m for m in lines), lines


def test_a_rejected_charge_is_still_logged_as_one_that_moved_nothing(monkeypatch, caplog):
    """The other half of the split. A FAILED charge was rejected by the ledger
    after simulation passed — nothing moved, there is nothing to reconcile, and
    calling it unconfirmed would send an operator hunting a payment that does
    not exist."""
    _use_fake_signer(monkeypatch)

    async def fake_invoke(contract_id, function_name, args):
        return {"status": "FAILED", "hash": "chargehash123"}

    monkeypatch.setattr(sc, "invoke_with_server_key_async", fake_invoke)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle("tsk_settle_rejected") == ("chargehash123", None, None)

    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any("did not settle" in m for m in msgs)
    assert not any("UNCONFIRMED" in m for m in msgs)


def test_seal_that_does_not_settle_after_a_charge_is_logged(monkeypatch, caplog):
    _use_fake_signer(monkeypatch)

    async def fake_invoke(contract_id, function_name, args):
        if function_name == "charge":
            return {"status": "SUCCESS", "hash": "chargehash123"}
        return {"status": "FAILED", "hash": "sealhash456"}

    monkeypatch.setattr(sc, "invoke_with_server_key_async", fake_invoke)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        charge_tx, proof_tx, job_id = _settle("tsk_settle_seal")

    assert (charge_tx, proof_tx) == ("chargehash123", "sealhash456")
    assert job_id is not None
    msgs = [r.getMessage() for r in _errors(caplog)]
    # Paid work left unattested: the charge tx and the job id have to be
    # recoverable from the log alone.
    assert any(
        "seal did not settle" in m
        and "status=FAILED" in m
        and "chargehash123" in m
        and job_id.hex() in m
        and AUTH_ID_HEX in m
        and PAYER in m
        for m in msgs
    ), f"a failed seal was not logged with its context: {msgs}"


def test_settlement_exception_is_logged_with_traceback(monkeypatch, caplog):
    _use_fake_signer(monkeypatch)

    def boom() -> Keypair:
        raise RuntimeError("soroban rpc unreachable")

    monkeypatch.setattr(sc, "_signer_keypair", boom)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        assert _settle("tsk_settle_raise") == (None, None, None)

    records = _errors(caplog)
    assert records, "a raising settlement was never logged"
    rec = records[0]
    msg = rec.getMessage()
    assert "tsk_settle_raise" in msg
    assert "soroban rpc unreachable" in msg
    assert AUTH_ID_HEX in msg and PAYER in msg
    assert rec.exc_info is not None


def test_cancellation_mid_settlement_is_logged_and_reraised(monkeypatch, caplog):
    """A deploy-triggered cancel landing between the charge submit and its
    confirmation must not bypass the reconstruction log: the charge may still
    settle on-chain, and CancelledError is a BaseException the generic handler
    never sees. Cancellation semantics are preserved — it re-raises."""
    _use_fake_signer(monkeypatch)

    async def cancelled_invoke(contract_id, function_name, args):
        raise asyncio.CancelledError

    monkeypatch.setattr(sc, "invoke_with_server_key_async", cancelled_invoke)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        with pytest.raises(asyncio.CancelledError):
            _settle("tsk_settle_cancel")

    msgs = [r.getMessage() for r in _errors(caplog)]
    assert any(
        "tsk_settle_cancel" in m and "cancelled" in m and AUTH_ID_HEX in m and PAYER in m and "0.050000" in m
        for m in msgs
    ), f"a cancelled settlement was never logged with its context: {msgs}"


def test_settlement_failure_trace_line_is_generic(monkeypatch):
    """Trace lines are world-readable when TASK_AUTH_REQUIRED is off — the raw
    exception text belongs in the server log, never in the trace stream."""
    _use_fake_signer(monkeypatch)

    def boom() -> Keypair:
        raise RuntimeError("soroban rpc unreachable")

    monkeypatch.setattr(sc, "_signer_keypair", boom)
    assert _settle("tsk_settle_trace") == (None, None, None)

    lines = state.traces["tsk_settle_trace"]
    errors = [ln for ln in lines if ln.level == "error"]
    assert any(ln.msg == "on-chain settlement failed" for ln in errors)
    assert not any("soroban rpc unreachable" in ln.msg for ln in lines)


def test_settlement_logs_never_carry_the_signing_key(monkeypatch, caplog):
    _use_fake_signer(monkeypatch)

    def boom() -> Keypair:
        raise RuntimeError("soroban rpc unreachable")

    monkeypatch.setattr(sc, "_signer_keypair", boom)
    formatter = logging.Formatter("%(message)s")
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        _settle("tsk_settle_secret")

    records = _errors(caplog)
    assert records
    for rec in records:
        # Message *and* rendered traceback — neither may leak the secret.
        assert SIGNING_SECRET not in formatter.format(rec)


def test_rating_submit_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", SIGNING_SECRET)

    async def fake_submit(agent_id, job_id, rating, weight, payer, kind="auto"):
        raise RuntimeError("sequence collision")

    monkeypatch.setattr(sc, "submit_rating_async", fake_submit)
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        asyncio.run(
            execution_svc._submit_ratings(
                "tsk_settle_rating",
                time.monotonic(),
                _plan(),
                {},
                payer=PAYER,
                job_id=JOB_ID,
            )
        )

    records = _errors(caplog)
    assert records, "a dropped reputation submit was never logged"
    msg = records[0].getMessage()
    assert "tsk_settle_rating" in msg
    assert "w.x" in msg and "agt_x" in msg
    assert "sequence collision" in msg
    assert JOB_ID.hex() in msg
    assert records[0].exc_info is not None
