"""Ramp orchestration against a fake PdaxClient: full on/off-ramp lifecycle,
duplicate-webhook idempotency, a re-entered ramp never repeating an
irreversible stage it already completed, off-ramp deposits reconciled on
amount, asset and tag before anything is sold, unmatched settlement events
being warned about rather than dropped, failed ramps exposing a code instead
of upstream text, and off-ramp payout PII dropped once the advance step
finishes (success or failure) or the payment window expires."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.pdax import ramp, ramp_store
from app.pdax.errors import PdaxError
from app.pdax.models.ramp import OffRampRequest, OnRampRequest
from app.pdax.models.webhooks import CryptoEvent, FiatEvent

VALID_G = "G" + "A" * 55

ONRAMP_REQ = dict(
    php_amount="500",
    stellar_address=VALID_G,
    method="instapay_upay_cashin",
    identifier="on-1",
    sender_first_name="Ada",
    sender_last_name="Lovelace",
    beneficiary_first_name="Ada",
    beneficiary_last_name="Lovelace",
)

OFFRAMP_REQ = dict(
    usdc_amount="10",
    identifier="off-1",
    beneficiary_bank_code="BAUBPPH",
    beneficiary_account_name="Ada Lovelace",
    beneficiary_account_number="1234567890",
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


def _quote(side: str) -> dict:
    return {
        "quote_id": "q1",
        "quote_currency": "USDC",
        "base_currency": "PHP",
        "side": side,
        "base_quantity": 10.0,
        "price": 58.0,
        "total_amount": 580.0,
    }


def _order(side: str) -> dict:
    return {
        "order_id": 77,
        "status": "filled",
        "quote_currency": "USDC",
        "base_currency": "PHP",
        "side": side,
        "base_quantity": 10.0,
        "price": 58.0,
        "total_amount": 580.0,
    }


class FakePdaxClient:
    """Dispatches client.request calls to canned responses by path suffix."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def request(self, method, path, *, params=None, json=None, authenticated=True):
        self.calls.append(path)
        for suffix, resp in self.responses.items():
            if path.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected PDAX call: {method} {path}")


def _offramp_client() -> FakePdaxClient:
    return FakePdaxClient(
        {
            "v2/trade/price": {"data": _quote("sell")},
            "v1/crypto/deposit": {"data": {"currency": "USDCXLM", "address": "GDEPOSIT", "tag": None}},
            "v2/trade/quote": {"data": _quote("sell")},
            "v1/trade": {"data": _order("sell")},
            "v1/fiat/withdraw": {
                "data": {
                    "request_id": "wr1",
                    "identifier": "off-1-payout",
                    "amount": 580.0,
                    "method": "PAY-TO-ACCOUNT-NON-REAL-TIME",
                    "status": "COMPLETED",
                }
            },
        }
    )


def test_offramp_payout_popped_after_completion():
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        assert record.status == "awaiting_payment"
        assert record.ramp_id in ramp._PAYOUTS  # PII held while in flight
        advanced = await ramp.advance_offramp(client, record)
        assert advanced.status == "completed"
        assert advanced.withdraw_request_id == "wr1"
        assert ramp._PAYOUTS == {}  # ...and dropped once settled

    asyncio.run(run())


def test_offramp_payout_popped_even_on_failure():
    client = _offramp_client()
    client.responses["v2/trade/quote"] = PdaxError("Insufficient balance.", code="OT010006", http_status=400)

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        advanced = await ramp.advance_offramp(client, record)
        assert advanced.status == "failed"
        assert ramp._PAYOUTS == {}  # dropped on the failure path too

    asyncio.run(run())


def _onramp_client() -> FakePdaxClient:
    return FakePdaxClient(
        {
            "v2/trade/price": {"data": _quote("buy")},
            "v1/fiat/deposit": {
                "request_id": "rq1",
                "identifier": "on-1",
                "reference_number": "RF1",
                "amount": 500.0,
                "method": "instapay_upay_cashin",
                "payment_checkout_url": "https://pay.example/checkout",
                "fee": 0.0,
            },
            "v2/trade/quote": {"data": _quote("buy")},
            "v1/trade": {"data": _order("buy")},
            "v1/crypto/withdraw": {
                "identifier": "on-1-out",
                "transaction_id": 555,
                "amount": "10",
                "address": VALID_G,
                "total": "10",
                "fee": "0",
                "currency": "USDCXLM",
            },
        }
    )


def test_onramp_lifecycle_happy_path():
    client = _onramp_client()

    async def run():
        record = await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        assert record.status == "awaiting_payment"
        assert record.checkout_url == "https://pay.example/checkout"
        event = FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        advanced = await ramp.handle_event(client, event)
        assert advanced is not None
        assert advanced.status == "completed"
        assert advanced.order_id == 77
        assert advanced.crypto_tx_id == 555
        stage_names = [s.name for s in advanced.stages]
        assert "buy_usdc" in stage_names
        assert "withdraw_usdcxlm" in stage_names

    asyncio.run(run())


def test_duplicate_webhook_does_not_double_apply():
    client = _onramp_client()

    async def run():
        await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        event = FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        first = await ramp.handle_event(client, event)
        assert first.status == "completed"
        calls_after_first = len(client.calls)
        # A retried delivery matches the ramp but must not re-run the buy or
        # the withdrawal — the record comes back untouched.
        second = await ramp.handle_event(client, event)
        assert second is first
        assert second.status == "completed"
        assert second.order_id == 77
        assert len(client.calls) == calls_after_first

    asyncio.run(run())


def test_offramp_lifecycle_via_webhook_event():
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        event = CryptoEvent(
            user_id="u1",
            transaction_type="DEPOSIT",
            amount=10.0,
            asset="USDCXLM",
            destination_address=record.deposit_address,
            status="completed",
        )
        advanced = await ramp.handle_event(client, event)
        assert advanced is not None
        assert advanced.status == "completed"
        # Duplicate crypto-deposit event: no further PDAX calls either.
        calls_after_first = len(client.calls)
        again = await ramp.handle_event(client, event)
        assert again.status == "completed"
        assert len(client.calls) == calls_after_first

    asyncio.run(run())


def _flaky_once_firm_quote(monkeypatch) -> dict:
    """Patch trade.firm_quote_v2 to die once with a non-PdaxError (simulating a
    bug or cancelled request escaping the advance step), then work normally."""
    calls = {"n": 0}
    real = ramp.trade.firm_quote_v2

    async def flaky(client, req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("interrupted mid-advance")
        return await real(client, req)

    monkeypatch.setattr(ramp.trade, "firm_quote_v2", flaky)
    return calls


def test_reconcile_recovers_onramp_after_interrupted_advance(monkeypatch):
    client = _onramp_client()
    client.responses["v1/fiat/transactions"] = {
        "data": [
            {
                "request_id": "rq1",
                "identifier": "on-1",
                "transaction_id": 1,
                "amount": "500",
                "method": "instapay_upay_cashin",
                "mode": "CashIn",
                "reference_number": "RF1",
                "status": "COMPLETED",
            }
        ]
    }
    _flaky_once_firm_quote(monkeypatch)

    async def run():
        record = await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        event = FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        with pytest.raises(RuntimeError):
            await ramp.handle_event(client, event)
        # The interrupted advance rolled the ramp back to a recoverable state
        # instead of stranding it in funded/converting.
        assert record.status == "awaiting_payment"
        assert any(s.name == "advance" and s.status == "failed" for s in record.stages)
        recovered = await ramp.reconcile(client, record.ramp_id)
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.order_id == 77

    asyncio.run(run())


def test_retried_webhook_recovers_offramp_and_keeps_payout(monkeypatch):
    client = _offramp_client()
    _flaky_once_firm_quote(monkeypatch)

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        event = CryptoEvent(
            user_id="u1",
            transaction_type="DEPOSIT",
            amount=10.0,
            asset="USDCXLM",
            destination_address=record.deposit_address,
            status="completed",
        )
        with pytest.raises(RuntimeError):
            await ramp.handle_event(client, event)
        assert record.status == "awaiting_payment"
        # Payout PII must survive the interruption — the ramp is still live.
        assert record.ramp_id in ramp._PAYOUTS
        recovered = await ramp.handle_event(client, event)
        assert recovered is not None
        assert recovered.status == "completed"
        assert ramp._PAYOUTS == {}  # ...and is dropped once settled

    asyncio.run(run())


def _flaky_once(monkeypatch, module, name: str) -> dict:
    """Patch a payout call to die once with a non-PdaxError, so the advance is
    interrupted AFTER the trade has settled — the state `_advance_guarded`
    rolls back to `awaiting_payment` and a retried webhook re-enters."""
    calls = {"n": 0}
    real = getattr(module, name)

    async def flaky(client, req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("interrupted after the trade")
        return await real(client, req)

    monkeypatch.setattr(module, name, flaky)
    return calls


def test_onramp_reentry_after_a_placed_order_does_not_buy_again(monkeypatch):
    """The buy is irreversible and `place_order` carries a fresh idempotency id
    every call, so a re-entered on-ramp must resume at the payout instead of
    converting the buyer's pesos a second time."""
    client = _onramp_client()
    _flaky_once(monkeypatch, ramp.withdrawals, "crypto_out")

    async def run():
        record = await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        event = FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        with pytest.raises(RuntimeError):
            await ramp.handle_event(client, event)
        # Re-enterable, with the buy already settled on the record.
        assert record.status == "awaiting_payment"
        assert record.order_id == 77
        recovered = await ramp.handle_event(client, event)
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.crypto_tx_id == 555
        assert client.calls.count("pdax-institution/v1/trade") == 1
        assert client.calls.count("pdax-institution/v2/trade/quote") == 1

    asyncio.run(run())


def test_onramp_reentry_after_a_sent_withdrawal_does_not_send_again():
    """The other irreversible on-ramp stage: a recorded transaction id means the
    USDCXLM already left, so a re-entry completes the ramp rather than
    delivering twice."""
    client = _onramp_client()

    async def run():
        record = await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        record.order_id, record.usdc_amount, record.crypto_tx_id = 77, 10.0, 555
        calls_before = len(client.calls)
        advanced = await ramp.advance_onramp(client, record)
        assert advanced.status == "completed"
        assert len(client.calls) == calls_before

    asyncio.run(run())


def test_offramp_reentry_after_a_placed_order_does_not_sell_again(monkeypatch):
    """Mirror of the on-ramp case: the sell already settled, so a retried
    webhook must not sell the customer's USDC a second time."""
    client = _offramp_client()
    _flaky_once(monkeypatch, ramp.withdrawals, "fiat_withdraw")

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        event = CryptoEvent(
            user_id="u1",
            transaction_type="DEPOSIT",
            amount=10.0,
            asset="USDCXLM",
            destination_address=record.deposit_address,
            status="completed",
        )
        with pytest.raises(RuntimeError):
            await ramp.handle_event(client, event)
        assert record.status == "awaiting_payment"
        assert record.order_id == 77
        recovered = await ramp.handle_event(client, event)
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.withdraw_request_id == "wr1"
        assert client.calls.count("pdax-institution/v1/trade") == 1
        assert client.calls.count("pdax-institution/v2/trade/quote") == 1

    asyncio.run(run())


def test_malformed_quote_envelope_fails_ramp_cleanly():
    """A PDAX body missing the data envelope must land the ramp in "failed"
    via a PdaxError — never escape as a bare KeyError (a 500 and a ramp
    frozen in "converting")."""
    client = _onramp_client()
    client.responses["v2/trade/quote"] = {"detail": "upstream maintenance"}

    async def run():
        record = await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        assert record.status == "awaiting_payment"
        event = FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        advanced = await ramp.handle_event(client, event)
        assert advanced is not None
        assert advanced.status == "failed"
        # A code, never the upstream text — the record is returned verbatim by
        # GET /api/pdax/ramp/{ramp_id}.
        assert advanced.error == "bad_upstream_shape"
        assert [s.detail for s in advanced.stages if s.status == "failed"] == ["bad_upstream_shape"]

    asyncio.run(run())


def test_malformed_quote_fields_fail_ramp_cleanly():
    """Enveloped but field-mangled quote payloads (ValidationError) fold into
    the same failed-ramp path instead of a 500."""
    client = _onramp_client()
    client.responses["v2/trade/quote"] = {"data": {"quote_id": "q1"}}

    async def run():
        record = await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
        event = FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        advanced = await ramp.handle_event(client, event)
        assert advanced is not None
        assert advanced.status == "failed"
        assert record.status == "failed"
        assert advanced.error == "bad_upstream_shape"

    asyncio.run(run())


# The account named in the upstream text is a marker no generated value can
# contain: ramp ids are lowercase hex and timestamps are digits, so a bare
# number like "9912" turned up inside a random ramp id about once in a few
# hundred runs. "q", "x" and "z" are not hex digits, and the lowercase,
# hyphenated form is not a Stellar address either.
UPSTREAM_ACCOUNT = "acct-qzx-upstream"
UPSTREAM_TEXT = f"Insufficient balance in institutional account {UPSTREAM_ACCOUNT}."


def _failing_onramp_client() -> FakePdaxClient:
    client = _onramp_client()
    client.responses["v2/trade/quote"] = PdaxError(UPSTREAM_TEXT, code="OT010006", http_status=400)
    return client


async def _run_failing_onramp(client) -> object:
    await ramp.start_onramp(client, OnRampRequest(**ONRAMP_REQ))
    return await ramp.handle_event(
        client,
        FiatEvent(
            identifier="on-1",
            user_id="u1",
            amount=500.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        ),
    )


def test_failed_ramp_record_carries_a_code_not_upstream_text(caplog):
    """GET /api/pdax/ramp/{ramp_id} returns the record verbatim, so the record
    owes the client the same thing `_fail` does: a stable snake_case code, and
    never the upstream message."""
    client = _failing_onramp_client()
    with caplog.at_level(logging.ERROR, logger="app.pdax.ramp"):
        advanced = asyncio.run(_run_failing_onramp(client))
    assert advanced.status == "failed"
    assert advanced.error == "insufficient_balance"
    failed_details = [s.detail for s in advanced.stages if s.status == "failed"]
    assert failed_details == ["insufficient_balance"]
    assert all(UPSTREAM_TEXT not in (s.detail or "") for s in advanced.stages)
    # The raw text is not lost — it lives in the server-side log line only.
    assert any(UPSTREAM_TEXT in r.getMessage() for r in caplog.records)


def test_ramp_status_route_never_returns_upstream_text(client):
    """End-to-end: the same doctrine holds through the HTTP surface."""
    fake = _failing_onramp_client()
    record = asyncio.run(_run_failing_onramp(fake))
    r = client.get(f"/api/pdax/ramp/{record.ramp_id}")
    assert r.status_code == 200
    assert UPSTREAM_ACCOUNT not in r.text
    assert "Insufficient balance" not in r.text
    assert r.json()["error"] == "insufficient_balance"


def test_unmatched_event_is_warned_not_silently_dropped(caplog):
    """A completed deposit matching no ramp means money arrived that nothing
    will act on — the process may simply have restarted since the ramp was
    created. It advances nothing, but it must never be silent."""
    client = _onramp_client()

    async def run():
        event = FiatEvent(
            identifier="unknown-id",
            user_id="u1",
            amount=1.0,
            transaction_type="DEPOSIT",
            status="COMPLETED",
        )
        with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
            assert await ramp.handle_event(client, event) is None
        assert client.calls == []

    asyncio.run(run())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "unmatched settlement event" in m and "identifier=unknown-id" in m and "amount=1.0" in m and "type=DEPOSIT" in m
        for m in warnings
    ), warnings


def test_unmatched_crypto_deposit_warns_with_destination(caplog):
    client = _offramp_client()
    event = CryptoEvent(
        user_id="u1",
        transaction_type="DEPOSIT",
        amount=10.0,
        asset="USDCXLM",
        destination_address="GNOBODY",
        status="completed",
    )
    with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
        assert asyncio.run(ramp.handle_event(client, event)) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unmatched settlement event" in m and "target=GNOBODY" in m for m in warnings), warnings


def test_events_we_never_act_on_stay_quiet(caplog):
    """Withdrawals and non-final statuses are not unclaimed money — they must
    not add noise the unmatched warning then has to stand out in."""
    client = _onramp_client()
    withdrawal = FiatEvent(
        identifier="unknown-id",
        user_id="u1",
        amount=1.0,
        transaction_type="WITHDRAWAL",
        status="COMPLETED",
    )
    pending = FiatEvent(
        identifier="unknown-id",
        user_id="u1",
        amount=1.0,
        transaction_type="DEPOSIT",
        status="IN-PROGRESS",
    )

    async def run():
        assert await ramp.handle_event(client, withdrawal) is None
        assert await ramp.handle_event(client, pending) is None

    with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
        asyncio.run(run())
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_abandoned_offramp_payout_details_expire(caplog):
    """An off-ramp whose agent never sends the USDC must not hold the
    beneficiary's account number for the life of the process — it is only
    popped on terminal advance or after 500 newer ramps evict it."""
    client = _offramp_client()

    async def run():
        return await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))

    record = asyncio.run(run())
    assert record.ramp_id in ramp._PAYOUTS
    # Inside the payment window the ramp may still settle: PII stays.
    ramp.expire_payouts(now=time.monotonic() + ramp.PAYOUT_STASH_TTL_SECONDS - 1)
    assert record.ramp_id in ramp._PAYOUTS
    with caplog.at_level(logging.INFO, logger="app.pdax.ramp"):
        ramp.expire_payouts(now=time.monotonic() + ramp.PAYOUT_STASH_TTL_SECONDS + 1)
    assert ramp._PAYOUTS == {}
    assert any("dropped abandoned off-ramp payout details" in r.getMessage() for r in caplog.records)


def test_starting_an_offramp_sweeps_stale_payouts(monkeypatch):
    """The sweep is event-driven (no background task), so a later off-ramp
    clears an abandoned one."""
    client = _offramp_client()
    monkeypatch.setattr(ramp, "PAYOUT_STASH_TTL_SECONDS", -1.0)

    async def run():
        first = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        second = await ramp.start_offramp(client, OffRampRequest(**{**OFFRAMP_REQ, "identifier": "off-2"}))
        return first, second

    first, second = asyncio.run(run())
    assert first.ramp_id not in ramp._PAYOUTS
    assert list(ramp._PAYOUTS) == [second.ramp_id]


def test_expired_payout_fails_the_advance_rather_than_paying_a_stale_beneficiary(monkeypatch):
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        monkeypatch.setattr(ramp, "PAYOUT_STASH_TTL_SECONDS", -1.0)
        return await ramp.advance_offramp(client, record)

    advanced = asyncio.run(run())
    assert advanced.status == "failed"
    assert advanced.error == "missing_payout_details"
    assert "v1/fiat/withdraw" not in client.calls


def test_oversized_payout_field_fails_the_ramp_not_the_process():
    """OffRampRequest leaves some fields unbounded while FiatWithdrawRequest
    bounds them, so the withdrawal body can fail local validation. That must
    land as a failed stage, not a ValidationError 500 on a funded ramp."""
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**{**OFFRAMP_REQ, "sender_first_name": "A" * 300}))
        return await ramp.advance_offramp(client, record)

    advanced = asyncio.run(run())
    assert advanced.status == "failed"
    assert advanced.error == "invalid_withdraw_request"
    assert "v1/fiat/withdraw" not in client.calls  # never left the process


def test_advance_offramp_without_payout_fails_cleanly():
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        ramp._PAYOUTS.clear()  # simulate a restart losing the payout stash
        advanced = await ramp.advance_offramp(client, record)
        assert advanced.status == "failed"
        assert advanced.error == "missing_payout_details"

    asyncio.run(run())


def _deposit_event(
    address: str | None, amount: float, *, asset: str = "USDCXLM", tag: str | None = None
) -> CryptoEvent:
    return CryptoEvent(
        user_id="u1",
        transaction_type="DEPOSIT",
        amount=amount,
        asset=asset,
        destination_address=address,
        destination_address_tag=tag,
        status="completed",
    )


def _traded(client: FakePdaxClient) -> bool:
    return "pdax-institution/v1/trade" in client.calls


def test_underfunded_offramp_deposit_never_sells(caplog):
    """The declared amount is what the caller asked to sell; the deposit is what
    actually arrived. Selling the declared 10 USDC against a 1 USDC deposit pays
    the beneficiary out of the institutional balance."""
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        return await ramp.handle_event(client, _deposit_event(record.deposit_address, 1.0))

    with caplog.at_level(logging.ERROR, logger="app.pdax.ramp"):
        advanced = asyncio.run(run())
    assert advanced is not None
    assert advanced.status == "failed"
    assert advanced.error == "underfunded_deposit"
    assert [s.detail for s in advanced.stages if s.status == "failed"] == ["underfunded_deposit"]
    assert not _traded(client)
    assert "pdax-institution/v1/fiat/withdraw" not in client.calls
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("stage=reconcile_deposit" in m and "code=underfunded_deposit" in m for m in errors), errors


def test_deposit_matching_within_float_noise_still_settles(caplog):
    """Amounts are floats here and formatted through money.format_amount
    everywhere else — a deposit that matches must not read as a shortfall on
    binary representation noise."""
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        return await ramp.handle_event(client, _deposit_event(record.deposit_address, 10.000000000000002))

    with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
        advanced = asyncio.run(run())
    assert advanced is not None
    assert advanced.status == "completed"
    assert advanced.withdraw_request_id == "wr1"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_over_deposit_settles_the_declared_amount_and_is_warned_about(caplog):
    """An over-deposit covers the sell, so it proceeds — but the excess is
    parked in the institutional account and has to be returned by hand."""
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        return await ramp.handle_event(client, _deposit_event(record.deposit_address, 25.0))

    with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
        advanced = asyncio.run(run())
    assert advanced is not None
    assert advanced.status == "completed"
    assert advanced.usdc_amount == 10.0  # only the declared amount was sold
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "deposit exceeds the declared amount" in m and "declared=10" in m and "deposited=25" in m for m in warnings
    ), warnings


def test_wrong_asset_deposit_does_not_match_an_offramp(caplog):
    """An off-ramp sells USDC: some other token landing on the deposit address
    must not buy a peso payout."""
    client = _offramp_client()

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        assert await ramp.handle_event(client, _deposit_event(record.deposit_address, 10.0, asset="XLM")) is None
        assert record.status == "awaiting_payment"

    with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
        asyncio.run(run())
    assert not _traded(client)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unmatched settlement event" in m and "asset=XLM" in m for m in warnings), warnings


SHARED_ADDRESS = "GSHAREDCUSTODIAL"


def _tagged_deposit_address(tag: str) -> dict:
    return {"data": {"currency": "USDCXLM", "address": SHARED_ADDRESS, "tag": tag}}


def test_tagged_deposit_matches_the_ramp_that_owns_the_tag():
    """PDAX may issue one shared custodial address plus a per-deposit tag, so
    matching on the address alone would pay customer A out of B's deposit."""
    client = _offramp_client()
    client.responses["v1/crypto/deposit"] = _tagged_deposit_address("tag-a")

    async def run():
        first = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        client.responses["v1/crypto/deposit"] = _tagged_deposit_address("tag-b")
        second = await ramp.start_offramp(client, OffRampRequest(**{**OFFRAMP_REQ, "identifier": "off-2"}))
        advanced = await ramp.handle_event(client, _deposit_event(SHARED_ADDRESS, 10.0, tag="tag-b"))
        assert advanced is not None
        assert advanced.ramp_id == second.ramp_id
        assert advanced.status == "completed"
        # The older ramp on the same address is untouched.
        assert first.status == "awaiting_payment"

    asyncio.run(run())


def test_untagged_event_falls_back_to_the_address_match():
    """Not every deposit event carries a tag; the address match must still
    settle the ramp rather than stranding the customer's USDC."""
    client = _offramp_client()
    client.responses["v1/crypto/deposit"] = _tagged_deposit_address("tag-a")

    async def run():
        record = await ramp.start_offramp(client, OffRampRequest(**OFFRAMP_REQ))
        assert record.deposit_tag == "tag-a"
        advanced = await ramp.handle_event(client, _deposit_event(SHARED_ADDRESS, 10.0))
        assert advanced is not None
        assert advanced.status == "completed"

    asyncio.run(run())
