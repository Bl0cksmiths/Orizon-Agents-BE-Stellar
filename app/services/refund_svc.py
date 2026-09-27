"""Partial-credit refund — story 4.01, Option A (settler-funded platform credit).

The deployed `PaymentEscrow` has no refund entrypoint and never takes custody:
`charge` sends USDC payer → agent-owner directly, so there is nothing to reverse.
A dispute refund is therefore a **new transfer from the platform**, not a
clawback — the settler credits the buyer over the asset SAC.

Money only. The dispute's reputation consequence is `dispute_rating`'s (story
4.04), written under a *derived* job id once the credit has landed, and kept
apart on purpose: an unconfirmed refund must never be retried, an unconfirmed
rating always safely can be, and a failed rating must never touch the credit.

Honest trust model, disclosed in every artifact (SOW §3.8 standard):
  - the **platform funds** the credit — the disputed agent's only consequence is
    reputational, never a seizure of its funds;
  - the **platform adjudicates** the dispute — there is no on-chain arbitration
    in this sprint.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
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

# The ledger's unit: USDC has 7 decimals on Stellar (`sc.usdc_to_i128`).
_STROOPS_PER_USDC = 10_000_000


class RefundRefused(Exception):
    """A refund that must NOT be signed, with a stable `code` the caller branches on.

    Every instance of this is money that did not move, raised before anything
    reaches the settler's key. Three codes today:

      - `nothing_to_credit` — the settlement says there is nothing to give back
        for this step (no such step, a step that never delivered, or an amount
        that computes to zero once D4's bounds are applied);
      - `refund_above_cap` — the amount is over `MAX_REFUND_USDC`. A refusal,
        never a clamp: quietly paying the ceiling would hide the mistaken uphold
        (or the bad settlement record) that the ceiling exists to catch;
      - `refund_amount_invalid` — the amount, or the `MAX_REFUND_USDC` it is
        bounded by, is not a usable number, so no bound in this module can say
        anything about it. Kept apart from the two above because it is neither
        a judgement about this dispute nor a ceiling an operator raised: it
        means a figure on the records or in the environment is not a quantity
        of money, and the fix is to that, not to the dispute.

    The code is what a caller maps to a response; `message` carries the numbers,
    for the operator who has to reconcile it afterwards.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ConfigGap:
    """A setting whose absence stops this deployment paying any credit."""

    # For operators: names the setting and what is wrong with it.
    problem: str
    # For the adjudicator's refusal, which a person reads: names no setting.
    reason: str


_NO_KEY = ConfigGap("STELLAR_SIGNING_KEY is unset", "there is no settler to pay a credit from")
_NO_SAC = ConfigGap("STELLAR_ASSET_SAC is unset", "there is no asset contract to pay a credit over")


def config_gap() -> ConfigGap | None:
    """The first missing setting that stops credits, or None when both are set.

    `rating_writer.config_gap`'s twin, for the other half of an uphold, and
    deliberately the same shape: presence only, in the order an operator would
    fix them, asked before anything is claimed or signed.

    It exists because the credit was the one money path with no such check, and
    the absence was not merely untidy. `execute_refund` reads the settler
    through `sc.signer_public_key`, which RAISES on an empty key BEFORE it
    submits anything. `credit_refund` now reads that raise — a
    `NotSubmittedError`, like a key that is present but will not parse — as
    FAILED and hands the claim back; before it did, an unconfigured deployment
    looked exactly like a transfer lost on the network and wedged the dispute
    in `crediting`. Asking here is still better than finding out there: the
    adjudicator is refused before anything is claimed, with a reason that
    names the gap. That difference is knowable here, and without touching the
    key at all.

    DISPUTE_REFUNDS_ENABLED is deliberately NOT among these. It is the
    operator's switch over the platform's wallet, `dispute_svc.uphold` refuses
    on it first and with its own code, and a second opinion about it here could
    only ever disagree with that one.

    Presence, never the value — and `.strip()`, because a setting whose whole
    value is whitespace is one somebody meant to set and did not.
    """
    if not settings.stellar_signing_key.strip():
        return _NO_KEY
    if not settings.stellar_asset_sac.strip():
        return _NO_SAC
    return None


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


def _refuse_above_cap(dispute: DisputeRecord, amount_usdc: float) -> None:
    """Hold `amount_usdc` to `MAX_REFUND_USDC`, failing CLOSED on a cap that is no cap.

    The ceiling is a `>`, and a `>` against NaN is false for every amount while
    nothing is greater than inf — so a cap that is not a finite number waves
    every credit through, which is exactly the failure a ceiling exists to
    prevent (QA D-054: `MAX_REFUND_USDC=nan` let a 50 USDC credit through at
    50.0). `Settings()` refuses to boot on such a value, but settings are also
    assigned outside it — by tests, by tooling, by anything that sets an
    attribute — and a cap check that trusts its caller to have validated the
    cap is the check that failed open. So the cap's own usability is asked
    first, here, and a cap at or below zero is refused the same way: it is
    not a ceiling anyone meant, and comparing against it would report every
    credit as "above the cap" when the fault is the setting.

    `refund_amount_invalid` rather than `refund_above_cap`: the amount has not
    been judged against a ceiling, because there is no ceiling to judge it
    against, and the fix is to the environment rather than to the dispute.
    """
    cap = settings.max_refund_usdc
    if not (math.isfinite(cap) and cap > 0):
        raise _refuse(
            dispute,
            "refund_amount_invalid",
            f"MAX_REFUND_USDC={cap} is not a finite amount above zero, so no credit can be held to it",
            amount_usdc,
        )
    if amount_usdc > cap:
        raise _refuse(
            dispute,
            "refund_above_cap",
            f"{amount_usdc:.7f} USDC exceeds MAX_REFUND_USDC={cap:.7f}",
            amount_usdc,
        )


def credited_amount_usdc(step_charged_usdc: float, fraction: float = DEFAULT_CREDITED_FRACTION) -> float:
    """The USDC credited back to the buyer for a disputed step, clamped to
    [0, the step charge]. `fraction` outside [0, 1] is clamped."""
    fraction = min(max(fraction, 0.0), 1.0)
    return round(max(step_charged_usdc, 0.0) * fraction, 7)


def creditable_for(
    settlement: SettlementRecord,
    dispute: DisputeRecord,
    fraction: float = DEFAULT_CREDITED_FRACTION,
    *,
    credited_elsewhere_usdc: Sequence[float] = (),
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
        the buyer's escrow for the whole workflow — and it is a ceiling on the
        workflow's credits TOGETHER, so it is applied net of
        `credited_elsewhere_usdc`: what the job's other disputes have been
        paid, or may yet be. Counted in stroops, because that is the unit the
        charge moved: each step's credit is rounded to 7 decimals on its own,
        and without the net bound the rounding alone let a workflow's credits
        add up to a stroop or two more than its charge.

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
    # The two bounds are asked to BE numbers before either is compared, for
    # the reason the amount is below and the ceiling is in `_refuse_above_cap`:
    # each clamp is a `<`, a NaN bound is never less than anything, and an inf
    # one never less than a finite promise — so a bound that is not a finite
    # number is skipped without a word, and the credit is paid as if the
    # settlement had never been consulted (QA D-054's NaN `settled_usdc`).
    # The amount check below cannot catch it: the promise it is left holding
    # is a perfectly good number, just an unbounded one.
    if not (math.isfinite(step_credit) and math.isfinite(settlement.settled_usdc)):
        raise _refuse(
            dispute,
            "refund_amount_invalid",
            f"step {dispute.step_index} credits {step_credit} USDC and the workflow settled "
            f"{settlement.settled_usdc} USDC, and a bound that is not a finite number bounds nothing",
            amount,
        )
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

    # Asked BEFORE either bound below, because NaN defeats both by
    # construction: every guard on this path is a `<` or a `>`, and every
    # comparison against NaN is false, so a NaN clears the floor, clears the
    # ceiling, and arrives at `usdc_to_i128` — which raises AFTER the claim has
    # been taken, and `credit_refund` can only read a raise as "may still have
    # landed". A wedged dispute holding a claim for a transfer that never
    # existed is the cost, so the one test a non-number cannot pass is made
    # here. Infinities go the same way: an unbounded credit is precisely what
    # the ceiling exists to stop, and it cannot stop one it cannot compare.
    # The open door is `DISPUTE_CREDITED_FRACTION`, whose NaN survives
    # `credited_amount_usdc`'s min(max(…)) untouched and is frozen onto the
    # dispute as the promise it was opened with.
    if not math.isfinite(amount):
        raise _refuse(
            dispute,
            "refund_amount_invalid",
            f"the bounds compute to {amount} USDC for step {dispute.step_index}, which is not an amount of money",
            amount,
        )
    if credited_elsewhere_usdc:
        if not all(math.isfinite(c) for c in credited_elsewhere_usdc):
            raise _refuse(
                dispute,
                "refund_amount_invalid",
                f"another dispute of job {dispute.job_id_hex} records a credit that is not a number "
                f"({list(credited_elsewhere_usdc)}), so what is left of the settled total is unknown",
                amount,
            )
        left_stroops = sc.usdc_to_i128(settlement.settled_usdc) - sum(
            sc.usdc_to_i128(c) for c in credited_elsewhere_usdc
        )
        if sc.usdc_to_i128(amount) > left_stroops:
            left = max(left_stroops, 0) / _STROOPS_PER_USDC
            logger.warning(
                "dispute %s: credit clamped by what is left of the settled total — %.7f USDC computed for step %d, "
                "%.7f USDC settled for the workflow, %.7f USDC already credited or in flight on its other "
                "disputes, %.7f USDC left (job %s, payer %s)",
                dispute.id,
                amount,
                dispute.step_index,
                settlement.settled_usdc,
                sum(credited_elsewhere_usdc),
                left,
                dispute.job_id_hex,
                dispute.payer,
            )
            amount = left
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
    _refuse_above_cap(dispute, amount)
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
    caller must not get wrong. `sc.invoke_with_server_key_async` answers a
    transfer that reached the ledger with a dict — and a caller that only looks
    for a hash cannot tell one that failed from one still in flight — and it
    RAISES for everything else: `sc.NotSubmittedError` when the write failed
    before anything was sent, and any other exception when it may have been.

    The mapping, in the order it is decided:

      - `sc.NotSubmittedError` → FAILED. The client raises it only ahead of
        `sendTransaction` — the signing key, the source account, the build,
        the simulation (a settler holding too little USDC is refused here),
        the signature — or for a send the RPC refused outright. No transaction
        exists anywhere after one of these, so nothing can land later.
      - any other exception → TIMEOUT. It may have been raised after the send.
        When it is the client's `sc.InFlightError`, its `tx_hash` — the signed
        transaction's hash — is the outcome's hash.
      - `status == "SUCCESS"` with a hash → SUCCESS. The credit landed.
      - `status == "FAILED"` → FAILED. The ledger rejected it, so no funds
        moved; of the dict's answers this is the ONLY one that says that.
        Matched EXACTLY, the way SUCCESS is above and the way `dispute_rating`
        matches both of its own: this is the branch that RELEASES the refund
        claim, and case-folding it would widen a door that says "nothing was
        signed" on the strength of a word the client wrote and this module
        did not. A
        client that ever answered `failed` falls through to TIMEOUT instead,
        which holds the claim — the buyer is paid late rather than twice.
      - anything else → TIMEOUT. `"timeout"` is the client's own word for
        "submitted, then lost track of it", and the leftovers land here on
        purpose: an unrecognised status, or a SUCCESS with no hash, is a
        transfer whose fate is unknown, which is the same hazard by another
        name.

    **TIMEOUT MEANS THE TRANSFER MAY STILL LAND, so it must NEVER be retried
    automatically and the refund claim must NEVER be released** (D3). Releasing
    a claim says "nothing was signed"; after a timeout something was, and the
    retry it unlocks credits the buyer twice the moment the first submission
    settles. The dispute stays in `crediting` and a human reconciles it from the
    ERROR line this logs.

    `amount_usdc` must have come from `creditable_for`; the three guards below
    re-check it rather than trust the caller, so a hand-computed or stale amount
    cannot reach the settler's key either — and the finiteness one leads, for
    the reason `creditable_for`'s does: the other two are comparisons, and a
    comparison cannot refuse a NaN.
    """
    if not math.isfinite(amount_usdc):
        raise _refuse(
            dispute,
            "refund_amount_invalid",
            f"{amount_usdc} USDC is not an amount of money",
            amount_usdc,
        )
    if amount_usdc <= 0:
        raise _refuse(dispute, "nothing_to_credit", f"{amount_usdc:.7f} USDC is not payable", amount_usdc)
    _refuse_above_cap(dispute, amount_usdc)

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
    except sc.NotSubmittedError as e:
        # FAILED, and so the claim is released — which is safe for exactly the
        # reason `dispute_rating` already treats this type as a refusal:
        # `NotSubmittedError` is PROOF that nothing was sent, not a guess. The
        # client raises it only before `sendTransaction`, or when the RPC
        # refused the send and holds nothing of it. Filing it as TIMEOUT, as
        # the catch-all below would, parked the dispute in `crediting` over a
        # transfer that never existed — an under-funded settler or an RPC
        # outage at `load_account` wedged every uphold it met, and only a hand
        # edit could pay the buyer. Anything else still falls through to the
        # catch-all, because only this type carries that proof.
        logger.error(
            "dispute %s: refund transfer was refused before it was sent — no funds moved: %s "
            "(job %s, payer %s, %.7f USDC)",
            dispute.id,
            e,
            dispute.job_id_hex,
            dispute.payer,
            amount_usdc,
        )
        return RefundOutcome("FAILED", None, amount_usdc)
    except Exception as e:
        # The client raises `InFlightError` for a failure after the transfer
        # was signed and sent, carrying the signed transaction's hash; keep it,
        # so the dispute records what a reconciliation asks the ledger about.
        in_flight = e.tx_hash if isinstance(e, sc.InFlightError) else None
        logger.error(
            "dispute %s: refund transfer raised and MAY HAVE LANDED — do not retry: %s "
            "(hash %s, job %s, payer %s, %.7f USDC)",
            dispute.id,
            e,
            in_flight,
            dispute.job_id_hex,
            dispute.payer,
            amount_usdc,
            exc_info=True,
        )
        return RefundOutcome("TIMEOUT", in_flight, amount_usdc)

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

    if status == "FAILED":
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
