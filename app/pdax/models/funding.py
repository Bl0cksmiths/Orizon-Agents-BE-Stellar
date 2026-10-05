"""
PDAX funding models — crypto deposit address and fiat deposit (cash-in).

Crypto deposit returns a wallet address (+ tag/memo) for a currency such as
USDCXLM. Fiat deposit carries full sender/beneficiary travel-rule data and
returns a `payment_checkout_url` to complete the cash-in.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import BoundedRequest


class CryptoDepositAddress(BaseModel):
    """Payload for GET /v1/crypto/deposit?currency=USDCXLM."""

    currency: str
    address: str
    tag: str | None = None


class FiatDepositRequest(BoundedRequest):
    """Body for POST /v1/fiat/deposit. Many sender fields are conditionally
    required for amounts ≥ 50,000 PHP (travel rule); kept optional here and
    enforced by the service layer."""

    amount: str = Field(..., max_length=32)
    method: str = Field(..., max_length=64, description="See FIAT_DEPOSIT_METHODS")
    identifier: str = Field(..., max_length=128, description="Unique client identifier")
    currency: str = Field("PHP", description="PHP only")

    # Sender
    sender_first_name: str = Field(..., max_length=200)
    sender_middle_name: str = "n.a."
    sender_last_name: str = Field(..., max_length=200)
    sender_country_origin: str = Field(..., max_length=100)
    sender_address_line_one: str | None = None
    sender_address_line_two: str | None = None
    sender_city: str | None = None
    sender_province: str | None = None
    sender_country: str | None = None
    sender_zip_code: str | None = None
    sender_phone_number: str | None = None
    sender_nationality: str | None = None
    sender_national_identity_number: str | None = None
    sender_dob: str | None = Field(default=None, description="mm-dd-yyyy")
    sender_place_of_birth: str | None = None
    source_of_funds: str
    sender_email: str | None = None

    # Beneficiary
    beneficiary_first_name: str
    beneficiary_middle_name: str = "n.a."
    beneficiary_last_name: str
    beneficiary_sex: str | None = None
    beneficiary_nationality: str | None = None
    beneficiary_dob: str | None = None
    beneficiary_address_line_one: str | None = None
    beneficiary_address_line_two: str | None = None
    beneficiary_barangay: str | None = None
    beneficiary_city: str | None = None
    beneficiary_province: str | None = None
    beneficiary_country: str | None = None
    beneficiary_zip_code: str | None = None
    beneficiary_government_issued_id: str | None = None
    beneficiary_phone_number: str | None = None

    purpose: str
    relationship_of_sender_to_beneficiary: str
    nature_of_business: str | None = None


class FiatDepositResult(BaseModel):
    """Response from POST /v1/fiat/deposit."""

    request_id: str
    identifier: str
    reference_number: str
    amount: float
    method: str
    payment_checkout_url: str
    fee: float
    status: str = "PENDING"
