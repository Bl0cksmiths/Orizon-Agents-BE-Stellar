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

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Literal

from ..config import settings
from ..stellar import client as sc
from .dispute_store import DisputeRecord, SettlementRecord

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


# What a submitted refund transfer is known to have done. Three values because
# the money question has exactly three answers, and collapsing any two of them
# costs a buyer their credit or pays it twice.
RefundStatus = Literal["SUCCESS", "FAILED", "TIMEOUT"]


@dataclass(frozen=True)
class RefundOutcome:
    """The result of one settler-funded credit, as the caller must treat it.

      - `SUCCESS` — the transfer landed and `tx_hash` is its receipt. Record it
        on the dispute and close it.
      - `FAILED`  — it definitively did not move funds. Nothing was paid, so the
        refund claim may be released and the dispute left upheld.
      - `TIMEOUT` — **the transfer MAY STILL LAND.** The submission is on the
        network and only the network knows; `tx_hash` is the in-flight hash when
        the client returned one. It must NEVER be retried automatically and the
        claim must NEVER be released (D3): either would credit the buyer a
        second time the moment the first submission settles. The claim stays,
        the dispute stays in `crediting`, and a human reconciles it.

    Frozen: an outcome is a record of what happened, not a variable.
    """

    status: RefundStatus
    tx_hash: str | None
    amount_usdc: float


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


def creditable_for(
    settlement: SettlementRecord,
    dispute: DisputeRecord,
    fraction: float = DEFAULT_CREDITED_FRACTION,
) -> float:
    """The USDC to credit for `dispute`, bounded by what actually settled (D4).

    `min(dispute.creditable_usdc, step.price_usdc × fraction, settlement.settled_usdc)`
    over the step the dispute names, rounded to the ledger's 7 decimals.

    **The settlement is consulted, never the plan**, and that is the whole point
    of this function. `step.price_usdc` ORIGINATES AS AN ESTIMATE — the planner
    priced the step before it ran, `dispute_svc` froze the buyer's creditable
    figure from it at opening time, and nothing on that path ever asked the
    chain what moved. `settlement.settled_usdc` is what `PaymentEscrow.charge`
    ACTUALLY MOVED: `_settle_onchain` floors the total to dust and rounds it to
    7 decimals, so the two genuinely differ. Only the minimum of the two is safe
    to pay — this is a settler-funded credit out of the platform's own wallet
    (see the module docstring), so crediting an estimate that ran above the
    charge pays the buyer money the platform never took.

    Each of the three bounds says something the others do not:

      - `dispute.creditable_usdc` is the PROMISE the buyer was shown when they
        opened the dispute, frozen then so a later policy change cannot rewrite
        it. Never pay more than was promised.
      - `step.price_usdc × fraction` is the policy share of that one step under
        the fraction in force NOW, so lowering `DISPUTE_CREDITED_FRACTION`
        applies to disputes already open (raising it cannot, because the promise
        above still caps it).
      - `settlement.settled_usdc` is the hard ceiling of what ever came out of
        the buyer's escrow for the whole workflow.

    Every clamp that actually bites is logged at WARNING with both numbers,
    because a clamp means two records disagree about money. Taking the smaller
    number silently is exactly how an overpayment — or a buyer quietly credited
    less than they were promised — becomes invisible.

    Raises `RefundRefused("nothing_to_credit")` when the settlement has no such
    step, when the step never delivered (it was not part of what the buyer paid
    for, so there is nothing to give back), or when the bounds compute to zero.
    """
    step = settlement.step(dispute.step_index)
    if step is None:
        raise _refuse(
            dispute,
            "nothing_to_credit",
            f"settlement {settlement.job_id_hex} has no step {dispute.step_index}",
            0.0,
        )
    if not step.delivered:
        raise _refuse(
            dispute,
            "nothing_to_credit",
            f"step {dispute.step_index} ({step.agent_id}) never delivered, so it was never paid for",
            0.0,
        )

    # Start from the promise and clamp downwards, so the running value is always
    # the smallest bound seen so far and each log line names the pair that moved
    # it. `credited_amount_usdc` applies the fraction, so this path and the one
    # `dispute_svc` used to write `creditable_usdc` share one rounding rule.
    amount = max(dispute.creditable_usdc, 0.0)
    step_credit = credited_amount_usdc(step.price_usdc, fraction)
    if step_credit < amount:
        logger.warning(
            "dispute %s: credit clamped by the step price — %.7f USDC promised at open time, "
            "%.7f USDC creditable from step %d now (job %s, payer %s)",
            dispute.id,
            amount,
            step_credit,
            dispute.step_index,
            dispute.job_id_hex,
            dispute.payer,
        )
        amount = step_credit
    if settlement.settled_usdc < amount:
        logger.warning(
            "dispute %s: credit clamped by the settled total — %.7f USDC computed for step %d, "
            "%.7f USDC ever settled on-chain for the workflow (job %s, payer %s)",
            dispute.id,
            amount,
            dispute.step_index,
            settlement.settled_usdc,
            dispute.job_id_hex,
            dispute.payer,
        )
        amount = settlement.settled_usdc

    amount = round(amount, 7)
    if amount <= 0:
        raise _refuse(
            dispute,
            "nothing_to_credit",
            f"the bounds compute to {amount:.7f} USDC for step {dispute.step_index}",
            amount,
        )

    # D5, and it is enforced HERE, in the function that produces the number,
    # rather than in the transfer wrapper. `creditable_for` is the only place in
    # the service that computes a refund amount, so checking at the point of
    # production means no amount exists that has not been through the ceiling —
    # whereas a check at the call site guards that one call site, and a call
    # site is the thing a later caller most easily writes a second copy of.
    # `credit_refund` re-checks what it is handed as a cheap second gate, but
    # this is the one that has to hold.
    if amount > settings.max_refund_usdc:
        raise _refuse(
            dispute,
            "refund_above_cap",
            f"{amount:.7f} USDC exceeds MAX_REFUND_USDC={settings.max_refund_usdc:.7f}",
            amount,
        )
    return amount


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


async def credit_refund(dispute: DisputeRecord, amount_usdc: float) -> RefundOutcome:
    """Sign and submit the credit for `dispute`, as a typed three-way outcome.

    A wrapper over `execute_refund`, which keeps the signature the 4.01 spike
    script and its tests call it with. What this adds is the one distinction a
    caller must not get wrong, because `sc.invoke_with_server_key_async` NEVER
    RAISES on failure — it returns a dict, and a caller that only looks for a
    hash cannot tell a transfer that failed from one still in flight.

    The mapping, in the order it is decided:

      - `status == "SUCCESS"` with a hash → SUCCESS. The credit landed.
      - `status == "FAILED"` → FAILED. The ledger rejected it, so no funds
        moved; this is the ONLY answer that says that.
      - anything else → TIMEOUT. `"timeout"` is the client's own word for
        "submitted, then lost track of it", and the leftovers land here on
        purpose: an unrecognised status, or a SUCCESS with no hash, is a
        transfer whose fate is unknown, which is the same hazard by another
        name. An exception is mapped here too — it can be raised before the
        submission or after it, and nothing in the dict distinguishes those.

    **TIMEOUT MEANS THE TRANSFER MAY STILL LAND, so it must NEVER be retried
    automatically and the refund claim must NEVER be released** (D3). Releasing
    a claim says "nothing was signed"; after a timeout something was, and the
    retry it unlocks credits the buyer twice the moment the first submission
    settles. The dispute stays in `crediting` and a human reconciles it from the
    ERROR line this logs.

    `amount_usdc` must have come from `creditable_for`; the two guards below
    re-check it rather than trust the caller, so a hand-computed or stale amount
    cannot reach the settler's key either.
    """
    if amount_usdc <= 0:
        raise _refuse(dispute, "nothing_to_credit", f"{amount_usdc:.7f} USDC is not payable", amount_usdc)
    if amount_usdc > settings.max_refund_usdc:
        raise _refuse(
            dispute,
            "refund_above_cap",
            f"{amount_usdc:.7f} USDC exceeds MAX_REFUND_USDC={settings.max_refund_usdc:.7f}",
            amount_usdc,
        )

    try:
        raw = await execute_refund(dispute.payer, amount_usdc)
    except asyncio.CancelledError:
        # A shutdown cancel (main.py's drain window) can land between the submit
        # and its confirmation, exactly like `_settle_onchain`'s — and
        # CancelledError is a BaseException the handler below never sees. Log
        # for reconstruction first, then let the cancellation propagate: the
        # claim is still held, which is the correct state for a transfer nobody
        # can account for.
        logger.error(
            "dispute %s: refund transfer cancelled mid-flight and MAY HAVE LANDED — do not retry "
            "(job %s, payer %s, %.7f USDC)",
            dispute.id,
            dispute.job_id_hex,
            dispute.payer,
            amount_usdc,
        )
        raise
    except Exception as e:
        logger.error(
            "dispute %s: refund transfer raised and MAY HAVE LANDED — do not retry: %s (job %s, payer %s, %.7f USDC)",
            dispute.id,
            e,
            dispute.job_id_hex,
            dispute.payer,
            amount_usdc,
            exc_info=True,
        )
        return RefundOutcome("TIMEOUT", None, amount_usdc)

    raw_hash = raw.get("hash")
    tx_hash = raw_hash if isinstance(raw_hash, str) and raw_hash else None
    status = str(raw.get("status") or "")

    if status == "SUCCESS" and tx_hash:
        logger.info(
            "dispute %s credited %.7f USDC to %s — tx %s (job %s)",
            dispute.id,
            amount_usdc,
            dispute.payer,
            tx_hash,
            dispute.job_id_hex,
        )
        return RefundOutcome("SUCCESS", tx_hash, amount_usdc)

    if status.upper() == "FAILED":
        logger.error(
            "dispute %s: refund transfer did not settle — status=%s hash=%s, no funds moved "
            "(job %s, payer %s, %.7f USDC)",
            dispute.id,
            status,
            tx_hash,
            dispute.job_id_hex,
            dispute.payer,
            amount_usdc,
        )
        return RefundOutcome("FAILED", tx_hash, amount_usdc)

    logger.error(
        "dispute %s: refund transfer unconfirmed and MAY STILL LAND — do not retry, reconcile by hand: "
        "status=%s hash=%s (job %s, payer %s, %.7f USDC)",
        dispute.id,
        status or "missing",
        tx_hash,
        dispute.job_id_hex,
        dispute.payer,
        amount_usdc,
    )
    return RefundOutcome("TIMEOUT", tx_hash, amount_usdc)


async def record_dispute_rating(agent_id: str, job_id: bytes, buyer: str, weight_stroops: int) -> dict[str, Any]:
    """Record an upheld dispute on-chain as a low rating under the DERIVED job
    id (R12), so it lands despite the settled job's auto-rating already
    occupying `Rated(agent_id, job_id)`. Kept linkable to the disputed job."""
    return await sc.submit_rating_async(
        agent_id, dispute_job_id(job_id), DISPUTE_RATING, weight_stroops, buyer, "dispute"
    )
