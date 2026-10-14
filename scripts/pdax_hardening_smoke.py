"""
Offline checks for the production-hardening layer.

Covers money normalization, the retry policy + backoff, the rate limiter,
fail-fast validation, webhook dedupe, and ramp settlement idempotency — none
of which need live PDAX.

Usage:
    cd ~/Websites-2026/orizon-agents-FE-Stellar/backend
    python3 scripts/pdax_hardening_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdax import ramp, validation, webhooks
from app.pdax.errors import PdaxError
from app.pdax.models.funding import FiatDepositRequest
from app.pdax.models.ramp import OnRampRequest
from app.pdax.models.webhooks import FiatEvent
from app.pdax.money import format_amount, quantize
from app.pdax.resilience import RateLimiter, is_retryable, with_retries


def check_money() -> None:
    assert format_amount(0.1 + 0.2) == "0.3"
    assert format_amount("17.179999999999998") == "17.18"
    assert str(quantize("17.18999", "0.01")) == "17.18"  # floors, never over-sends
    print("money: float noise erased + step floor OK")


def check_retry_policy() -> None:
    assert is_retryable(PdaxError("x", http_status=503))
    assert is_retryable(PdaxError("x", http_status=None))  # transport
    assert is_retryable(PdaxError("x", code="OT010032"))  # rate limit
    assert not is_retryable(PdaxError("x", http_status=400))
    print("retry policy: transient yes, 4xx no")


async def check_retry_runs() -> None:
    calls = {"n": 0}

    async def flaky(attempt: int) -> str:
        calls["n"] += 1
        if attempt < 3:
            raise PdaxError("temporary", http_status=503)
        return "ok"

    assert await with_retries(flaky, attempts=3, base_delay=0.001) == "ok"
    assert calls["n"] == 3, calls

    async def fatal(attempt: int) -> str:
        calls["n"] += 1
        raise PdaxError("bad input", http_status=400)

    before = calls["n"]
    try:
        await with_retries(fatal, attempts=3, base_delay=0.001)
        raise AssertionError("should have raised")
    except PdaxError:
        pass
    assert calls["n"] == before + 1, "4xx must not retry"
    print("retry runs: retried 503 to success, did not retry 400")


async def check_rate_limiter() -> None:
    limiter = RateLimiter(rate=1000.0, burst=2)
    for _ in range(5):
        await limiter.acquire()
    print("rate limiter: drained burst without error")


def check_validation() -> None:
    base = {
        "amount": "1000",
        "method": "instapay_upay_cashin",
        "identifier": "d1",
        "sender_first_name": "A",
        "sender_last_name": "B",
        "sender_country_origin": "Philippines",
        "source_of_funds": "Compensation",
        "beneficiary_first_name": "A",
        "beneficiary_last_name": "B",
        "purpose": "Purchase of Goods",
        "relationship_of_sender_to_beneficiary": "Myself",
    }
    validation.validate_fiat_deposit(FiatDepositRequest(**base))  # valid → no raise

    for bad in [
        {"method": "not_a_channel"},
        {"sender_country_origin": "Atlantis"},
        {"source_of_funds": "Robbery"},
        {"amount": "60000"},  # travel rule, no address/nid/dob
    ]:
        try:
            validation.validate_fiat_deposit(FiatDepositRequest(**{**base, **bad}))
            raise AssertionError(f"should reject {bad}")
        except PdaxError:
            pass
    # 60k with national id passes the travel rule
    validation.validate_fiat_deposit(
        FiatDepositRequest(**{**base, "amount": "60000", "sender_national_identity_number": "X123"})
    )
    print("validation: rejects bad method/country/source + enforces travel rule")


def check_webhook_dedupe() -> None:
    payload = {"identifier": "w1", "request_id": "r1", "status": "COMPLETED"}
    key = webhooks.event_key(payload)
    assert webhooks.claim_event(key) is True
    assert webhooks.claim_event(key) is False
    print("webhook dedupe: second delivery rejected")


class CountingClient:
    """Fake client that counts order + withdraw calls to prove idempotency."""

    def __init__(self) -> None:
        self.orders = 0
        self.withdraws = 0

    async def request(self, method, path, *, params=None, json=None, authenticated=True):
        if "v2/trade/price" in path or "v2/trade/quote" in path:
            q = {
                "quote_currency": "USDC",
                "base_currency": "PHP",
                "side": (json or params)["side"],
                "base_quantity": 17.18,
                "price": 58.2,
                "total_amount": 1000,
            }
            if "quote" in path:
                q |= {"quote_id": "q1", "expires_at": "z"}
            return {"data": q}
        if path.endswith("/v1/trade"):
            self.orders += 1
            return {
                "data": {
                    "order_id": 1,
                    "status": "successful",
                    "quote_currency": "USDC",
                    "base_currency": "PHP",
                    "side": json["side"],
                    "base_quantity": 17.18,
                    "price": 58.2,
                    "total_amount": 1000,
                }
            }
        if "fiat/deposit" in path:
            return {
                "request_id": "r",
                "identifier": json["identifier"],
                "reference_number": "ref",
                "amount": 1000,
                "method": json["method"],
                "payment_checkout_url": "u",
                "fee": 30,
                "status": "PENDING",
            }
        if "crypto/withdraw" in path:
            self.withdraws += 1
            return {
                "identifier": json["identifier"],
                "transaction_id": 9,
                "transaction_hash": "",
                "amount": json["amount"],
                "address": json["address"],
                "tag": None,
                "total": json["amount"],
                "fee": "0",
                "currency": "USDCXLM",
                "status": "IN PROGRESS",
            }
        raise AssertionError(path)


async def check_ramp_idempotency() -> None:
    client = CountingClient()
    rec = await ramp.start_onramp(
        client,
        OnRampRequest(
            php_amount="1000",
            stellar_address="G",
            method="instapay_upay_cashin",
            identifier="idem-1",
            sender_first_name="A",
            sender_last_name="B",
            beneficiary_first_name="A",
            beneficiary_last_name="B",
        ),
    )
    event = FiatEvent(user_id="u", identifier="idem-1", amount=1000, transaction_type="DEPOSIT", status="COMPLETED")
    first = await ramp.handle_event(client, event)
    second = await ramp.handle_event(client, event)  # duplicate delivery
    assert first and first.status == "completed"
    assert second and second.ramp_id == rec.ramp_id
    assert client.orders == 1 and client.withdraws == 1, (client.orders, client.withdraws)
    print("ramp idempotency: duplicate settlement did NOT double-execute")


async def main() -> None:
    check_money()
    check_retry_policy()
    await check_retry_runs()
    await check_rate_limiter()
    check_validation()
    check_webhook_dedupe()
    await check_ramp_idempotency()
    print("PDAX hardening smoke: ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
