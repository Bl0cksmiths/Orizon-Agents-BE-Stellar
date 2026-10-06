"""
Ramp models — the PHP ↔ USDCXLM orchestration layer over PDAX.

A *ramp* sequences several PDAX calls into one user-facing flow:

  on-ramp  (PHP → USDCXLM):  fiat deposit (bank/e-wallet) → buy USDC → crypto
                             withdraw USDCXLM to a Stellar address
  off-ramp (USDCXLM → PHP):  crypto deposit address → (agent sends USDC) →
                             sell USDC → fiat withdraw PHP to a bank account

Each ramp is tracked as a `RampRecord` with per-step `RampStage`s. The slow
steps (a human paying, or an on-chain transfer landing) are advanced by PDAX
settlement webhooks, not blocking calls.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .common import BoundedRequest

RampDirection = Literal["onramp", "offramp"]

# Lifecycle: quoted → awaiting_payment → funded → converting → settling →
# completed (or failed at any point).
RampStatus = Literal[
    "quoted",
    "awaiting_payment",
    "funded",
    "converting",
    "settling",
    "completed",
    "failed",
]


class RampEstimate(BaseModel):
    """Indicative conversion preview for a ramp (non-binding)."""

    direction: RampDirection
    php_amount: float
    usdc_amount: float
    price: float  # PHP per USDC
    quote_currency: str = "USDC"
    base_currency: str = "PHP"


class FundingQuote(BaseModel):
    """How many pesos to pay to fund a workflow priced in USDC. `php_to_pay`
    includes a safety buffer and is rounded up, so it always covers the target
    after spread, fees, and step rounding."""

    usdc_target: float
    php_to_pay: float
    php_base: float  # indicative PHP for exactly usdc_target (no buffer)
    buffer_bps: int
    price: float  # PHP per USDC


class RampStage(BaseModel):
    """One step within a ramp's lifecycle."""

    name: str
    status: str  # pending | success | failed
    detail: str = ""


def _positive_decimal_str(value: str, cap: str) -> str:
    """Validate an amount string: a finite positive decimal within a sane cap."""
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be a decimal number") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be a positive number")
    if amount > Decimal(cap):
        raise ValueError(f"amount exceeds the {cap} cap")
    return value


class OnRampRequest(BoundedRequest):
    """Start a PHP → USDCXLM ramp. The buyer pays PHP via a bank/e-wallet
    channel; the converted USDCXLM is delivered to `stellar_address`."""

    php_amount: str
    stellar_address: str = Field(..., pattern=r"^G[A-Z2-7]{55}$", description="Where USDCXLM is delivered")

    @field_validator("php_amount")
    @classmethod
    def _check_php_amount(cls, v: str) -> str:
        return _positive_decimal_str(v, "10000000")

    method: str = Field(..., max_length=64, description="Fiat deposit channel, e.g. instapay_upay_cashin")
    identifier: str = Field(..., max_length=128)
    sender_first_name: str = Field(..., max_length=200)
    sender_last_name: str = Field(..., max_length=200)
    sender_country_origin: str = "Philippines"
    source_of_funds: str = "Compensation"
    beneficiary_first_name: str = Field(..., max_length=200)
    beneficiary_last_name: str = Field(..., max_length=200)
    purpose: str = "Purchase of Goods"
    relationship_of_sender_to_beneficiary: str = "Myself"


class OffRampRequest(BoundedRequest):
    """Start a USDCXLM → PHP ramp. The agent sends USDCXLM to the returned
    deposit address; the converted PHP is paid out to the beneficiary bank."""

    usdc_amount: str
    identifier: str

    @field_validator("usdc_amount")
    @classmethod
    def _check_usdc_amount(cls, v: str) -> str:
        return _positive_decimal_str(v, "100000")

    beneficiary_bank_code: str = Field(..., max_length=200)
    beneficiary_account_name: str = Field(..., max_length=200)
    beneficiary_account_number: str = Field(..., max_length=200)
    fee_type: str = "Sender"
    method: str = "PAY-TO-ACCOUNT-NON-REAL-TIME"
    sender_first_name: str
    sender_last_name: str
    sender_country_origin: str = "Philippines"
    source_of_funds: str = "Business Income"
    beneficiary_first_name: str = Field(..., max_length=200)
    beneficiary_last_name: str = Field(..., max_length=200)
    purpose: str = "Business Transaction"
    relationship_of_sender_to_beneficiary: str = "Myself"


class RampRecord(BaseModel):
    """The full state of a ramp as it moves through its stages."""

    ramp_id: str
    direction: RampDirection
    status: RampStatus
    created_at: str
    php_amount: float = 0
    usdc_amount: float = 0
    price: float = 0
    stellar_address: str | None = None  # on-ramp delivery target
    deposit_address: str | None = None  # off-ramp USDCXLM deposit target
    deposit_tag: str | None = None
    identifier: str | None = None
    reference_number: str | None = None
    checkout_url: str | None = None
    order_id: int | None = None
    crypto_tx_id: int | None = None
    withdraw_request_id: str | None = None
    stages: list[RampStage] = Field(default_factory=list)
    error: str | None = None
