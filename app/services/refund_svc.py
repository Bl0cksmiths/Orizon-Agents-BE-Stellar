"""Partial-credit refund — story 4.01, Option A (settler-funded platform credit).

The deployed `PaymentEscrow` has no refund entrypoint and never takes custody:
`charge` sends USDC payer → agent-owner directly, so there is nothing to reverse.
A dispute refund is therefore a **new transfer from the platform**, not a
clawback — the settler credits the buyer over the asset SAC, and the dispute is
recorded on-chain under a *derived* job id so it clears the ReputationLedger
replay guard (R12).

Honest trust model, disclosed in every artifact (SOW §3.8 standard):
  - the **platform funds** the credit — the disputed agent's only consequence is
    reputational, never a seizure of its funds;
  - the **platform adjudicates** the dispute — there is no on-chain arbitration
    in this sprint.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..stellar import client as sc
from .dispute_store import DisputeRecord

logger = logging.getLogger(__name__)

# Refund policy — the credited fraction of the DISPUTED STEP's settled charge.
# A dispute credits the buyer for the step that failed; 1.0 = the full step
# price. Stated up front so buyer and operator both know the terms in advance,
# rather than a case-by-case judgement (product rule).
DEFAULT_CREDITED_FRACTION = 1.0

# Rating written for an upheld dispute, on the same 0..100 scale the settler's
# synthetic rating uses — low, so a disputed agent's reputation reflects it.
DISPUTE_RATING = 10


class RefundRefused(Exception):
    """A refund that must NOT be signed, with a stable `code` the caller branches on.

    Every instance of this is money that did not move, raised before anything
    reaches the settler's key. Two codes today:

      - `nothing_to_credit` — the settlement says there is nothing to give back
        for this step (no such step, a step that never delivered, or an amount
        that computes to zero once D4's bounds are applied);
      - `refund_above_cap` — the amount is over `MAX_REFUND_USDC`. A refusal,
        never a clamp: quietly paying the ceiling would hide the mistaken uphold
        (or the bad settlement record) that the ceiling exists to catch.

    The code is what a caller maps to a response; `message` carries the numbers,
    for the operator who has to reconcile it afterwards.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _refuse(dispute: DisputeRecord, code: str, detail: str, amount_usdc: float) -> RefundRefused:
    """Log a refused credit on the money path, and build the error to raise.

    One helper so every refusal reaches the server log carrying the same four
    facts a reconciliation starts from — dispute, job, payer, amount — in the
    shape `execution_svc._settle_onchain` logs a skipped charge. ERROR rather
    than WARNING, for the same reason it is: an upheld dispute that cannot be
    paid is an operator's problem, and nothing else records it. Never a key,
    never an API key — only identifiers and the amount.
    """
    message = f"refusing to credit dispute {dispute.id}: {detail}"
    logger.error(
        "%s (code=%s, job %s, payer %s, %.7f USDC)",
        message,
        code,
        dispute.job_id_hex,
        dispute.payer,
        amount_usdc,
    )
    return RefundRefused(code, message)


def dispute_job_id(job_id: bytes) -> bytes:
    """Derive the dispute's job id from the settled job's id (R12).

    `ReputationLedger.submit` checks its replay guard on `Rated(agent_id,
    job_id)` before it reads `kind`, and the settler has already auto-rated the
    settled job under `job_id` — so a dispute rating on the same pair returns
    `Error::Replay`. A distinct but deterministic derived id lets the dispute be
    recorded on-chain, still linkable to the job it disputes.
    """
    return hashlib.sha256(job_id + b"dispute").digest()[:16]


def credited_amount_usdc(step_charged_usdc: float, fraction: float = DEFAULT_CREDITED_FRACTION) -> float:
    """The USDC credited back to the buyer for a disputed step, clamped to
    [0, the step charge]. `fraction` outside [0, 1] is clamped."""
    fraction = min(max(fraction, 0.0), 1.0)
    return round(max(step_charged_usdc, 0.0) * fraction, 7)


async def execute_refund(buyer: str, amount_usdc: float) -> dict[str, Any]:
    """Settler-funded platform credit: transfer `amount_usdc` from the settler
    to the buyer over the asset SAC. Returns the invoke result (incl. `hash`).

    A credit, never a clawback — the funds leave the platform wallet, so the
    settler must hold enough of the asset. The server signing key IS the
    settler, so the SAC `transfer(settler → buyer)` is authorised by that key.
    """
    settler = sc.signer_public_key()
    return await sc.invoke_with_server_key_async(
        sc.contract_ids().asset_sac,
        "transfer",
        [sc.addr(settler), sc.addr(buyer), sc.i128(sc.usdc_to_i128(amount_usdc))],
    )


async def record_dispute_rating(agent_id: str, job_id: bytes, buyer: str, weight_stroops: int) -> dict[str, Any]:
    """Record an upheld dispute on-chain as a low rating under the DERIVED job
    id (R12), so it lands despite the settled job's auto-rating already
    occupying `Rated(agent_id, job_id)`. Kept linkable to the disputed job."""
    return await sc.submit_rating_async(
        agent_id, dispute_job_id(job_id), DISPUTE_RATING, weight_stroops, buyer, "dispute"
    )
