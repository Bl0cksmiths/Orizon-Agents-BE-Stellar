"""
PDAX trade models — indicative price, firm quote (v1/v2), and orders.

Indicative prices are non-binding. Firm quotes carry a `quote_id` that expires
in 15 seconds and can be accepted via POST /trade (an order). v2 price/quote add
an explicit `currency` + `quantity` (receive-side) on top of v1's `base_quantity`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import BoundedRequest, Side


class IndicativePriceParams(BaseModel):
    """Query for GET /v1/trade/price."""

    quote_currency: str = Field(..., description="Crypto asset, e.g. USDC")
    base_currency: str = Field("PHP", description="PHP asset")
    side: Side
    base_quantity: str


class IndicativePriceV2Params(BaseModel):
    """Query for GET /v2/trade/price (adds receive-side currency + quantity)."""

    side: Side
    quote_currency: str
    base_currency: str = "PHP"
    currency: str = Field(..., description="Currency you want to receive")
    quantity: str


class FirmQuoteRequest(BoundedRequest):
    """Body for POST /v1/trade/quote."""

    quote_currency: str
    base_currency: str = "PHP"
    side: Side
    base_quantity: str


class FirmQuoteV2Request(BoundedRequest):
    """Body for POST /v2/trade/quote."""

    side: Side
    quote_currency: str
    base_currency: str = "PHP"
    currency: str
    quantity: str


class Quote(BaseModel):
    """Indicative or firm quote payload (firm adds quote_id + expires_at)."""

    quote_id: str | None = None
    expires_at: str | None = None
    quote_currency: str
    base_currency: str
    side: Side
    base_quantity: float
    price: float
    total_amount: float


class OrderRequest(BoundedRequest):
    """Body for POST /v1/trade — accept a firm quote."""

    quote_id: str
    side: Side
    idempotency_id: str = Field(..., description="Client-generated UUIDv4")


class Order(BaseModel):
    """Order result from POST /trade and GET /orders/{id}."""

    order_id: int
    status: str
    quote_currency: str
    base_currency: str
    side: Side
    base_quantity: float
    price: float
    total_amount: float
    created_at: str | None = None
    updated_at: str | None = None


class OrdersResponse(BaseModel):
    """Envelope for the order-history list route."""

    orders: list[Order]
