"""
PDAX webhook models — registration request plus crypto/fiat event payloads.

POST /v1/config/webhook registers an endpoint for `event_type` ("crypto" or
"fiat"). PDAX then POSTs CryptoEvent / FiatEvent payloads to that URL as
transactions settle.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import BoundedRequest


class WebhookRegisterRequest(BoundedRequest):
    event_type: str = Field(..., description="crypto | fiat")
    webhook_endpoint: str


class WebhookRegistration(BaseModel):
    webhook_endpoint: str
    event_type: str


class CryptoEvent(BaseModel):
    """Inbound webhook payload for a crypto transaction."""

    identifier: str | None = None
    user_id: str
    reference_id: str | None = None
    request_id: str | None = None
    transaction_type: str = Field(..., description="WITHDRAWAL | DEPOSIT")
    transaction_hash: str | None = None
    amount: float
    fee_amount: float = 0
    asset: str
    asset_type: str = "crypto"
    network: str | None = None
    source_address: str | None = None
    source_address_tag: str | None = None
    destination_address: str | None = None
    destination_address_tag: str | None = None
    status: str = Field(..., description="completed | failed")


class FiatEvent(BaseModel):
    """Inbound webhook payload for a fiat transaction."""

    identifier: str | None = None
    user_id: str
    request_id: str | None = None
    reference_number: str | None = None
    amount: float
    asset: str = "PHP"
    asset_type: str = "FIAT"
    transaction_type: str = Field(..., description="WITHDRAWAL | DEPOSIT")
    status: str = Field(..., description="IN-PROGRESS | COMPLETED | FAILED")
    method: str | None = None
    fee: float = 0
