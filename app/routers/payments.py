from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, Response

from .. import money
from ..schemas import X402Request, X402Response

router = APIRouter(tags=["payments"])


@router.post("/payments/x402", response_model=X402Response)
async def x402(
    req: X402Request,
    response: Response,
    x_orizon_payment: str | None = Header(default=None),
) -> X402Response:
    """Simulated HTTP 402 flow.

    First call (no payment header): respond 402 with a challenge.
    Second call with any non-empty X-Orizon-Payment header: respond 200 + receipt.
    """
    if not x_orizon_payment:
        response.status_code = 402
        # The amount exact and the token the asset the escrow actually moves
        # (native XLM on testnet) — never an assumed "USDC" (ADR 0015).
        amount = money.format_amount(money.to_stroops(req.amount_usdc))
        response.headers["X-Orizon-Payment-Required"] = (
            f"amount={amount};agent={req.agent_id};token={money.asset_code()}"
        )
        return X402Response(status="402")

    return X402Response(status="paid", receipt="0x" + secrets.token_hex(10))
