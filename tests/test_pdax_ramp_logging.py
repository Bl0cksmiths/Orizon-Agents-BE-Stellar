"""Ramp observability: the money-moving path must be reconstructable from the
log alone. Ramp records live in RAM only, so a restart leaves the log as the
sole evidence of what a ramp did — these tests assert the evidence exists and
that it never carries beneficiary PII."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.pdax import ramp, ramp_store
from app.pdax.errors import PdaxError
from app.pdax.models.ramp import OffRampRequest, RampRecord

STORE_LOGGER = "app.pdax.ramp_store"
RAMP_LOGGER = "app.pdax.ramp"

VALID_G = "G" + "A" * 55

# Beneficiary details that must never reach a log line.
ACCOUNT_NUMBER = "1234567890"
ACCOUNT_NAME = "Ada Lovelace"

OFFRAMP_REQ = dict(
    usdc_amount="10",
    identifier="off-9",
    beneficiary_bank_code="BAUBPPH",
    beneficiary_account_name=ACCOUNT_NAME,
    beneficiary_account_number=ACCOUNT_NUMBER,
    sender_first_name="Ada",
    sender_last_name="Lovelace",
    beneficiary_first_name="Ada",
    beneficiary_last_name="Lovelace",
)


@pytest.fixture(autouse=True)
def clean_ramp_state():
    ramp_store._ramps.clear()
    ramp_store._locks.clear()
    ramp._PAYOUTS.clear()
    yield
    ramp_store._ramps.clear()
    ramp_store._locks.clear()
    ramp._PAYOUTS.clear()


def _rec(ramp_id: str, status: str = "completed") -> RampRecord:
    return RampRecord(
        ramp_id=ramp_id,
        direction="onramp",
        status=status,
        created_at="t",
        identifier=f"id-{ramp_id}",
    )


def _messages(caplog, logger_name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


class _FailingClient:
    """Every PDAX call fails with the same typed upstream error."""

    def __init__(self, error: PdaxError) -> None:
        self.error = error

    async def request(self, method, path, *, params=None, json=None, authenticated=True):
        raise self.error


class _WithdrawFailsClient:
    """Sells the USDC successfully, then fails the payout — the worst case:
    the pesos exist and never reached the beneficiary."""

    def __init__(self) -> None:
        self.error = PdaxError("Bank rejected the account.", code="PAP0010", http_status=400)

    async def request(self, method, path, *, params=None, json=None, authenticated=True):
        if path.endswith("v1/crypto/deposit"):
            return {"data": {"currency": "USDCXLM", "address": "GDEPOSIT", "tag": None}}
        if path.endswith("v1/fiat/withdraw"):
            raise self.error
        return {
            "data": {
                "quote_id": "q1",
                "order_id": 77,
                "status": "filled",
                "quote_currency": "USDC",
                "base_currency": "PHP",
                "side": "sell",
                "base_quantity": 10.0,
                "price": 58.0,
                "total_amount": 580.0,
            }
        }


def _onramp_record() -> RampRecord:
    return RampRecord(
        ramp_id="r9",
        direction="onramp",
        status="awaiting_payment",
        created_at="t",
        php_amount=500.0,
        identifier="on-9",
        stellar_address=VALID_G,
    )


def test_add_stage_logs_the_transition(caplog):
    record = _rec("r1")
    with caplog.at_level(logging.INFO, logger=STORE_LOGGER):
        ramp_store.add_stage(record, "buy_usdc", "success", "order 77")
    lines = _messages(caplog, STORE_LOGGER)
    assert any(
        "ramp_id=r1" in m and "stage=buy_usdc" in m and "status=success" in m and "order 77" in m for m in lines
    ), lines


def test_add_stage_logs_direction_for_correlation(caplog):
    record = _rec("r2")
    with caplog.at_level(logging.INFO, logger=STORE_LOGGER):
        ramp_store.add_stage(record, "estimate", "failed", "pdax_error")
    assert any("direction=onramp" in m for m in _messages(caplog, STORE_LOGGER))


def test_evicting_an_in_flight_ramp_warns(caplog, monkeypatch):
    """A live ramp pushed out by the retention cap can never be advanced or
    reconciled again — it must not vanish silently."""
    monkeypatch.setattr(ramp_store, "_MAX_RAMPS", 1)
    ramp_store.save(_rec("r0", "awaiting_payment"))
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        ramp_store.save(_rec("r1", "completed"))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("evicted in-flight ramp" in m and "ramp_id=r0" in m for m in warnings), warnings


def test_evicting_a_terminal_ramp_does_not_warn(caplog, monkeypatch):
    monkeypatch.setattr(ramp_store, "_MAX_RAMPS", 1)
    ramp_store.save(_rec("r0", "completed"))
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        ramp_store.save(_rec("r1", "completed"))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_failed_conversion_logs_ramp_stage_amount_and_upstream_code(caplog):
    """The gap this closes: a failed conversion used to leave nothing in the
    log naming the ramp, the stage, the amount, or the reason."""
    record = _onramp_record()
    client = _FailingClient(PdaxError("Insufficient balance.", code="OT010006", http_status=400))
    with caplog.at_level(logging.ERROR, logger=RAMP_LOGGER):
        asyncio.run(ramp.advance_onramp(client, record))
    errors = [r for r in caplog.records if r.name == RAMP_LOGGER and r.levelno == logging.ERROR]
    assert errors, "a failed conversion must log at error"
    line = errors[0].getMessage()
    assert "ramp_id=r9" in line
    assert "direction=onramp" in line
    assert "stage=buy_usdc" in line
    assert "php=500" in line
    assert "identifier=on-9" in line
    assert "OT010006" in line  # the structured upstream code
    assert "Insufficient balance." in line  # ...and the raw upstream text, log-side only


def test_failed_payout_after_a_successful_sell_is_logged(caplog):
    """place_order succeeded, the payout did not: the customer's money is
    parked in the PDAX account. This must be the loudest line in the log."""

    async def run():
        record = await ramp.start_offramp(_WithdrawFailsClient(), OffRampRequest(**OFFRAMP_REQ))
        with caplog.at_level(logging.ERROR, logger=RAMP_LOGGER):
            return await ramp.advance_offramp(_WithdrawFailsClient(), record)

    advanced = asyncio.run(run())
    assert advanced.status == "failed"
    errors = _messages(caplog, RAMP_LOGGER)
    assert any("stage=fiat_withdraw" in m and "PAP0010" in m and f"ramp_id={advanced.ramp_id}" in m for m in errors)


def test_logs_never_carry_beneficiary_pii(caplog):
    """Logs are held to the same standard as the API responses, which mask
    account numbers — no account number, account name, or party name may
    appear anywhere in the ramp's log output."""

    async def run():
        with caplog.at_level(logging.DEBUG):
            record = await ramp.start_offramp(_WithdrawFailsClient(), OffRampRequest(**OFFRAMP_REQ))
            await ramp.advance_offramp(_WithdrawFailsClient(), record)

    asyncio.run(run())
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert emitted.strip(), "expected the off-ramp to log something at all"
    assert ACCOUNT_NUMBER not in emitted
    assert ACCOUNT_NAME not in emitted
    assert "Lovelace" not in emitted


def test_missing_payout_details_is_logged(caplog):
    """USDC arrived but the beneficiary stash is gone (restart/eviction) —
    funds received with nowhere to send them."""

    async def run():
        record = await ramp.start_offramp(_WithdrawFailsClient(), OffRampRequest(**OFFRAMP_REQ))
        ramp._PAYOUTS.clear()
        with caplog.at_level(logging.ERROR, logger=RAMP_LOGGER):
            return await ramp.advance_offramp(_WithdrawFailsClient(), record)

    advanced = asyncio.run(run())
    assert advanced.status == "failed"
    assert any("payout details missing" in m for m in _messages(caplog, RAMP_LOGGER))


def test_interrupted_advance_is_logged_with_traceback(caplog, monkeypatch):
    async def boom(client, req):
        raise RuntimeError("interrupted mid-advance")

    monkeypatch.setattr(ramp.trade, "firm_quote_v2", boom)
    record = _onramp_record()
    ramp_store.save(record)

    async def run():
        client = _FailingClient(PdaxError("x"))
        with caplog.at_level(logging.ERROR, logger=RAMP_LOGGER):
            with pytest.raises(RuntimeError):
                await ramp._advance_guarded(client, record, ramp.advance_onramp)

    asyncio.run(run())
    records = [r for r in caplog.records if r.name == RAMP_LOGGER and r.levelno == logging.ERROR]
    assert any("ramp advance interrupted" in r.getMessage() and "cause=RuntimeError" in r.getMessage() for r in records)
    assert any(r.exc_info for r in records), "the escaping exception must be logged with its traceback"
