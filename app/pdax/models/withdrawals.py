"""
PDAX withdrawal models — fiat withdraw, user-info upload, and crypto out.

Fiat withdrawals route through a bank/e-wallet (`beneficiary_bank_code`) using
PAY-TO-ACCOUNT-REAL-TIME or NON-REAL-TIME; PDAX picks the channel and reports
attempts in `retry_methods`. Crypto out sends a token to an external address;
amounts ≥ 50,000 PHP require beneficiary VASP data (travel rule).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import BoundedRequest

# Field bounds, matching the ramp models (app/pdax/models/ramp.py): 200 for a
# name-or-address-sized field, 64 for an enum-ish channel token. Without them
# these fields are capped only by the 1 MiB body limit.
_TEXT = 200
_TOKEN = 64
# The ramp derives this identifier as f"{offramp.identifier}-payout", so leave
# headroom above the 128 an OnRampRequest identifier allows.
_IDENTIFIER = 160
# Free text: a payment instruction legitimately runs longer than a name.
_INSTRUCTIONS = 500


class FiatWithdrawRequest(BoundedRequest):
    """Body for POST /v1/fiat/withdraw."""

    identifier: str = Field(..., max_length=_IDENTIFIER)
    amount: str = Field(..., max_length=_TOKEN)
    currency: str = Field(default="PHP", max_length=_TOKEN, description="PHP only")
    method: str = Field(..., max_length=_TOKEN, description="PAY-TO-ACCOUNT-REAL-TIME | -NON-REAL-TIME")
    fee_type: str = Field(..., max_length=_TOKEN, description="Sender | Beneficiary")

    # Sender
    sender_first_name: str = Field(..., max_length=_TEXT)
    sender_middle_name: str = Field(default="n.a.", max_length=_TEXT)
    sender_last_name: str = Field(..., max_length=_TEXT)
    sender_country_origin: str = Field(..., max_length=_TEXT)
    sender_address_line_one: str | None = Field(default=None, max_length=_TEXT)
    sender_address_line_two: str | None = Field(default=None, max_length=_TEXT)
    sender_city: str | None = Field(default=None, max_length=_TEXT)
    sender_province: str | None = Field(default=None, max_length=_TEXT)
    sender_country: str | None = Field(default=None, max_length=_TEXT)
    sender_zip_code: str | None = Field(default=None, max_length=_TOKEN)
    sender_phone_number: str | None = Field(default=None, max_length=_TOKEN)
    sender_nationality: str | None = Field(default=None, max_length=_TEXT)
    sender_national_identity_number: str | None = Field(default=None, max_length=_TEXT)
    sender_dob: str | None = Field(default=None, max_length=_TOKEN)
    sender_place_of_birth: str | None = Field(default=None, max_length=_TEXT)
    source_of_funds: str = Field(..., max_length=_TEXT)
    sender_email: str | None = Field(default=None, max_length=_TEXT)

    # Beneficiary
    beneficiary_first_name: str = Field(..., max_length=_TEXT)
    beneficiary_middle_name: str = Field(default="n.a.", max_length=_TEXT)
    beneficiary_last_name: str = Field(..., max_length=_TEXT)
    beneficiary_sex: str | None = Field(default=None, max_length=_TOKEN)
    beneficiary_nationality: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_dob: str | None = Field(default=None, max_length=_TOKEN)
    beneficiary_bank_code: str = Field(..., max_length=_TEXT)
    beneficiary_account_name: str = Field(..., max_length=_TEXT)
    beneficiary_account_number: str = Field(..., max_length=_TEXT)
    beneficiary_address_line_one: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_address_line_two: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_barangay: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_city: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_province: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_country: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_zip_code: str | None = Field(default=None, max_length=_TOKEN)
    beneficiary_government_issued_id: str | None = Field(default=None, max_length=_TEXT)
    beneficiary_phone_number: str | None = Field(default=None, max_length=_TOKEN)

    purpose: str = Field(..., max_length=_TEXT)
    relationship_of_sender_to_beneficiary: str = Field(..., max_length=_TEXT)
    nature_of_business: str | None = Field(default=None, max_length=_TEXT)
    instructions: str | None = Field(default=None, max_length=_INSTRUCTIONS)


class RetryMethod(BaseModel):
    """One attempted payment channel within a fiat withdrawal."""

    request_id: str
    channel: str
    status: str
    fail_reason: str = ""
    time: str | None = None


class FiatWithdrawResult(BaseModel):
    """Response from POST /v1/fiat/withdraw."""

    request_id: str | None = None
    identifier: str
    reference_number: str | None = None
    amount: float
    method: str
    retry_methods: list[RetryMethod] = Field(default_factory=list)
    status: str = "PENDING"
    fee: float = 0


class CryptoOutRequest(BoundedRequest):
    """Body for POST /v1/crypto/withdraw."""

    identifier: str
    currency: str = Field(..., description="Token symbol, e.g. USDCXLM")
    amount: str
    address: str
    tag: str | None = ""
    beneficiary_first_name: str | None = None
    beneficiary_last_name: str | None = None
    beneficiary_exchange: str | None = None
    send_to_self: str | None = "false"
    beneficiary_wallet: str | None = "false"


class CryptoOutResult(BaseModel):
    """Response from POST /v1/crypto/withdraw."""

    identifier: str
    transaction_id: int
    transaction_hash: str = ""
    amount: str
    address: str
    tag: str | None = None
    total: str
    fee: str
    currency: str
    status: str = "IN PROGRESS"
    created_at: str | None = None
