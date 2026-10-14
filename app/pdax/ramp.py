"""
Ramp orchestration — PHP ↔ USDCXLM over PDAX.

Composes the trade / funding / withdrawals primitives into two flows and tracks
them as `RampRecord`s:

  start_onramp  → fiat deposit (returns a checkout URL the buyer pays)
  advance_onramp (on FiatEvent DEPOSIT COMPLETED) → buy USDC → withdraw USDCXLM
  start_offramp → returns a USDCXLM deposit address
  advance_offramp (on CryptoEvent DEPOSIT completed) → sell USDC → fiat withdraw

`handle_event` is the webhook entry point: it matches an inbound settlement
event to a waiting ramp and advances it. The relevant Stellar asset is USDCXLM.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from pydantic import ValidationError

from ..config import settings
from . import funding, money, ramp_store, trade, transactions, withdrawals
from .client import PdaxClient
from .errors import PdaxError, orizon_code
from .models.funding import FiatDepositRequest
from .models.ramp import (
    FundingQuote,
    OffRampRequest,
    OnRampRequest,
    RampDirection,
    RampEstimate,
    RampRecord,
)
from .models.trade import FirmQuoteV2Request, IndicativePriceV2Params, OrderRequest
from .models.webhooks import CryptoEvent, FiatEvent
from .models.withdrawals import CryptoOutRequest, FiatWithdrawRequest

USDC = "USDC"
USDCXLM = "USDCXLM"
PHP = "PHP"

logger = logging.getLogger(__name__)

# Codes raised by our own adapters rather than by PDAX (trade._unwrap, the
# quote guard below, the withdraw-request guard, the deposit reconciliation
# guard). They are already stable snake_case identifiers carrying no upstream
# text, so they reach the client unmapped; everything else goes through the
# ORIZON_CODES table.
_INTERNAL_CODES = frozenset({"bad_upstream_shape", "invalid_quote", "invalid_withdraw_request", "underfunded_deposit"})


def _num(x: object) -> str:
    """PDAX amounts are strings; format via Decimal so a float like 17.18 never
    serializes as 17.179999999999998 and trips step validation."""
    return money.format_amount(x)


def _client_code(e: PdaxError) -> str:
    """The stable snake_case code a client may see for a failed ramp stage —
    never the upstream message, per the doctrine in routers/pdax.py `_fail`."""
    code = str(e.code) if e.code is not None else ""
    if code in _INTERNAL_CODES:
        return code
    return orizon_code(e.code)


def _log_failure(record: RampRecord, stage: str, e: PdaxError) -> str:
    """Write the one line that makes a failed conversion diagnosable, and
    return the client-safe code to store on the ramp.

    A ramp moves real money: `place_order` can succeed (the PHP is already
    converted) and the payout leg fail straight after, leaving funds parked in
    the PDAX institutional account. Without this the record is marked "failed"
    in RAM only and nothing answers "which ramp, how much, why" once the
    process restarts — so every failure path names the ramp, the stage, the
    direction, both amounts, and the upstream code/message/request id.

    This is also the *only* place the raw upstream message is written down:
    the record itself keeps the mapped code, since GET /api/pdax/ramp/{id}
    returns it verbatim to the client.

    Deliberately absent: beneficiary account numbers and account names, and
    sender/beneficiary names. The API already masks those (see
    `_mask_account_numbers` in routers/pdax.py) and the log is held to the
    same standard.
    """
    code = _client_code(e)
    logger.error(
        "ramp stage failed: ramp_id=%s direction=%s stage=%s code=%s php=%s usdc=%s identifier=%s upstream=%s",
        record.ramp_id,
        record.direction,
        stage,
        code,
        _num(record.php_amount),
        _num(record.usdc_amount),
        record.identifier,
        e.to_dict(),
    )
    return code


async def estimate(
    client: PdaxClient, direction: RampDirection, amount: str, currency: str | None = None
) -> RampEstimate:
    """Indicative conversion preview. `currency` denominates `amount` (per the
    firm-quote v2 sample); default is PHP for on-ramp, USDC for off-ramp. Pass
    `currency="USDC"` on an on-ramp to price a *target* USDC amount (e.g. a
    workflow cost) → `php_amount` is the pesos needed."""
    if direction == "onramp":
        params = IndicativePriceV2Params(
            side="buy",
            quote_currency=USDC,
            base_currency=PHP,
            currency=currency or PHP,
            quantity=amount,
        )
        q = await trade.indicative_price_v2(client, params)
        return RampEstimate(
            direction="onramp",
            php_amount=q.total_amount,
            usdc_amount=q.base_quantity,
            price=q.price,
        )
    params = IndicativePriceV2Params(
        side="sell",
        quote_currency=USDC,
        base_currency=PHP,
        currency=currency or USDC,
        quantity=amount,
    )
    q = await trade.indicative_price_v2(client, params)
    return RampEstimate(
        direction="offramp",
        php_amount=q.total_amount,
        usdc_amount=q.base_quantity,
        price=q.price,
    )


async def funding_quote(client: PdaxClient, usdc_target: str) -> FundingQuote:
    """Pesos a buyer must pay to end up with at least `usdc_target` USDC.

    Prices off a PHP-denominated reference quote (currency=PHP — the shape PDAX's
    v2 sample uses) rather than the tiny USDC target, which would trip PDAX's
    per-trade minimum. Adds the safety buffer, rounds up, and floors at the PDAX
    fiat-deposit minimum, so the amount is always payable and always covers the
    workflow (any excess stays as USDC in the buyer's wallet)."""
    ref = await estimate(client, "onramp", settings.pdax_ramp_quote_reference_php, currency=PHP)
    price = ref.price or 0.0
    buffer_bps = max(0, settings.pdax_ramp_buffer_bps)
    php_needed = float(usdc_target) * price
    buffered = php_needed * (1 + buffer_bps / 10_000)
    php_to_pay = max(
        float(settings.pdax_ramp_min_php),
        float(money.quantize_up(buffered, "1")),
    )
    return FundingQuote(
        usdc_target=float(usdc_target),
        php_to_pay=php_to_pay,
        php_base=php_needed,
        buffer_bps=buffer_bps,
        price=price,
    )


async def start_onramp(client: PdaxClient, req: OnRampRequest) -> RampRecord:
    """Create the fiat deposit and a ramp record awaiting the buyer's payment."""
    record = RampRecord(
        ramp_id=ramp_store.new_id(),
        direction="onramp",
        status="quoted",
        created_at=ramp_store.now_iso(),
        php_amount=float(req.php_amount),
        stellar_address=req.stellar_address,
        identifier=req.identifier,
    )
    # Best-effort indicative preview (non-fatal if PDAX is unreachable).
    try:
        est = await estimate(client, "onramp", req.php_amount)
        record.usdc_amount, record.price = est.usdc_amount, est.price
        ramp_store.add_stage(record, "estimate", "success", f"{est.usdc_amount} USDC @ {est.price}")
    except PdaxError as e:
        code = _log_failure(record, "estimate", e)
        ramp_store.add_stage(record, "estimate", "failed", code)

    deposit = FiatDepositRequest(
        amount=req.php_amount,
        method=req.method,
        identifier=req.identifier,
        currency=PHP,
        sender_first_name=req.sender_first_name,
        sender_last_name=req.sender_last_name,
        sender_country_origin=req.sender_country_origin,
        source_of_funds=req.source_of_funds,
        beneficiary_first_name=req.beneficiary_first_name,
        beneficiary_last_name=req.beneficiary_last_name,
        purpose=req.purpose,
        relationship_of_sender_to_beneficiary=req.relationship_of_sender_to_beneficiary,
    )
    try:
        result = await funding.fiat_deposit(client, deposit)
        record.checkout_url = result.payment_checkout_url
        record.reference_number = result.reference_number
        record.status = "awaiting_payment"
        ramp_store.add_stage(record, "fiat_deposit", "success", result.reference_number)
    except PdaxError as e:
        record.status = "failed"
        record.error = _log_failure(record, "fiat_deposit", e)
        ramp_store.add_stage(record, "fiat_deposit", "failed", record.error)
    return ramp_store.save(record)


async def advance_onramp(client: PdaxClient, record: RampRecord) -> RampRecord:
    """PHP has landed — buy USDC then withdraw USDCXLM to the Stellar address."""
    record.status = "converting"
    if record.order_id is None:
        try:
            quote = await trade.firm_quote_v2(
                client,
                FirmQuoteV2Request(
                    side="buy",
                    quote_currency=USDC,
                    base_currency=PHP,
                    currency=PHP,
                    quantity=_num(record.php_amount),
                ),
            )
            quote_id = quote.quote_id
            if not quote_id:
                raise PdaxError("firm quote response carried no quote_id", code="invalid_quote")
            order = await trade.place_order(
                client, OrderRequest(quote_id=quote_id, side="buy", idempotency_id=str(uuid.uuid4()))
            )
            record.order_id = order.order_id
            record.usdc_amount = order.base_quantity
            record.price = order.price
            ramp_store.add_stage(record, "buy_usdc", "success", f"order {order.order_id}")
        except PdaxError as e:
            record.status, record.error = "failed", _log_failure(record, "buy_usdc", e)
            ramp_store.add_stage(record, "buy_usdc", "failed", record.error)
            return ramp_store.save(record)
    else:
        # Re-entry: `_advance_guarded` rolls an interrupted advance back to
        # `awaiting_payment`, so a retried webhook runs this again with the buy
        # already settled. `place_order` sends a fresh idempotency id on every
        # call, so re-quoting here would convert the buyer's pesos a SECOND
        # time — resume at the payout instead.
        ramp_store.add_stage(record, "buy_usdc", "success", f"order {record.order_id} already placed")

    record.status = "settling"
    if record.crypto_tx_id is not None:
        # Same reasoning one stage on: the USDCXLM already left the account and
        # an on-chain send cannot be recalled, so a re-entry completes the ramp
        # rather than delivering twice.
        record.status = "completed"
        ramp_store.add_stage(record, "withdraw_usdcxlm", "success", f"{record.crypto_tx_id} already sent")
        return ramp_store.save(record)
    try:
        out = await withdrawals.crypto_out(
            client,
            CryptoOutRequest(
                identifier=f"{record.identifier}-out",
                currency=USDCXLM,
                amount=_num(record.usdc_amount),
                address=record.stellar_address or "",
            ),
        )
        record.crypto_tx_id = out.transaction_id
        record.status = "completed"
        ramp_store.add_stage(record, "withdraw_usdcxlm", "success", str(out.transaction_id))
    except PdaxError as e:
        # The PHP is already converted at this point — the USDC exists and did
        # not reach the buyer. Loudest failure in the system.
        record.status, record.error = "failed", _log_failure(record, "withdraw_usdcxlm", e)
        ramp_store.add_stage(record, "withdraw_usdcxlm", "failed", record.error)
    return ramp_store.save(record)


async def start_offramp(client: PdaxClient, req: OffRampRequest) -> RampRecord:
    """Return a USDCXLM deposit address; the agent sends USDC to it next."""
    record = RampRecord(
        ramp_id=ramp_store.new_id(),
        direction="offramp",
        status="quoted",
        created_at=ramp_store.now_iso(),
        usdc_amount=float(req.usdc_amount),
        identifier=req.identifier,
    )
    try:
        est = await estimate(client, "offramp", req.usdc_amount)
        record.php_amount, record.price = est.php_amount, est.price
        ramp_store.add_stage(record, "estimate", "success", f"{est.php_amount} PHP @ {est.price}")
    except PdaxError as e:
        code = _log_failure(record, "estimate", e)
        ramp_store.add_stage(record, "estimate", "failed", code)

    try:
        addr = await funding.crypto_deposit_address(client, USDCXLM)
        record.deposit_address = addr.address
        record.deposit_tag = addr.tag
        record.status = "awaiting_payment"
        ramp_store.add_stage(record, "deposit_address", "success", addr.address)
    except PdaxError as e:
        record.status, record.error = "failed", _log_failure(record, "deposit_address", e)
        ramp_store.add_stage(record, "deposit_address", "failed", record.error)
    # Stash the beneficiary payout details for the advance step.
    _stash_payout(record.ramp_id, req)
    return ramp_store.save(record)


async def advance_offramp(client: PdaxClient, record: RampRecord) -> RampRecord:
    """USDC has arrived — sell it for PHP then withdraw to the bank account."""
    expire_payouts()
    stash = _PAYOUTS.get(record.ramp_id)
    payout = stash.request if stash else None
    if payout is None:
        # USDC has landed but the beneficiary details are gone (process
        # restart, or eviction): the customer's crypto is in the PDAX account
        # with nowhere to pay it out to. Operator action required.
        logger.error(
            "ramp stage failed: ramp_id=%s direction=offramp stage=fiat_withdraw usdc=%s identifier=%s "
            "upstream=payout details missing — funds received with no beneficiary on file",
            record.ramp_id,
            _num(record.usdc_amount),
            record.identifier,
        )
        record.status, record.error = "failed", "missing_payout_details"
        ramp_store.add_stage(record, "fiat_withdraw", "failed", record.error)
        return ramp_store.save(record)

    try:
        record.status = "converting"
        if record.order_id is None:
            try:
                quote = await trade.firm_quote_v2(
                    client,
                    FirmQuoteV2Request(
                        side="sell",
                        quote_currency=USDC,
                        base_currency=PHP,
                        currency=USDC,
                        quantity=_num(record.usdc_amount),
                    ),
                )
                quote_id = quote.quote_id
                if not quote_id:
                    raise PdaxError("firm quote response carried no quote_id", code="invalid_quote")
                order = await trade.place_order(
                    client, OrderRequest(quote_id=quote_id, side="sell", idempotency_id=str(uuid.uuid4()))
                )
                record.order_id = order.order_id
                record.php_amount = order.total_amount
                record.price = order.price
                ramp_store.add_stage(record, "sell_usdc", "success", f"order {order.order_id}")
            except PdaxError as e:
                record.status, record.error = "failed", _log_failure(record, "sell_usdc", e)
                ramp_store.add_stage(record, "sell_usdc", "failed", record.error)
                return ramp_store.save(record)
        else:
            # Re-entry after an interrupted advance (see advance_onramp): the
            # sell already settled and `place_order` is idempotent only against
            # its own per-call uuid, so re-quoting here would sell the
            # customer's USDC twice. Resume at the payout, which is keyed on the
            # stable "-payout" identifier and so is deduplicated by PDAX.
            ramp_store.add_stage(record, "sell_usdc", "success", f"order {record.order_id} already placed")

        record.status = "settling"
        try:
            # FiatWithdrawRequest bounds its fields; some of the OffRampRequest
            # fields feeding it do not, and the derived "-payout" identifier is
            # longer than the one the caller sent. A local ValidationError here
            # would escape as a 500 and strand a funded ramp, so fold it into
            # the same failed-stage path as any upstream rejection.
            try:
                withdraw_request = FiatWithdrawRequest(
                    identifier=f"{payout.identifier}-payout",
                    amount=_num(record.php_amount),
                    currency=PHP,
                    method=payout.method,
                    fee_type=payout.fee_type,
                    sender_first_name=payout.sender_first_name,
                    sender_last_name=payout.sender_last_name,
                    sender_country_origin=payout.sender_country_origin,
                    source_of_funds=payout.source_of_funds,
                    beneficiary_first_name=payout.beneficiary_first_name,
                    beneficiary_last_name=payout.beneficiary_last_name,
                    beneficiary_bank_code=payout.beneficiary_bank_code,
                    beneficiary_account_name=payout.beneficiary_account_name,
                    beneficiary_account_number=payout.beneficiary_account_number,
                    purpose=payout.purpose,
                    relationship_of_sender_to_beneficiary=payout.relationship_of_sender_to_beneficiary,
                )
            except ValidationError as e:
                raise PdaxError(
                    "payout details do not fit a fiat withdrawal request",
                    code="invalid_withdraw_request",
                ) from e
            result = await withdrawals.fiat_withdraw(client, withdraw_request)
            record.withdraw_request_id = result.request_id
            record.status = "completed" if result.status != "FAILED" else "failed"
            ramp_store.add_stage(record, "fiat_withdraw", "success", result.status)
        except PdaxError as e:
            # The USDC is already sold — the pesos exist and did not reach the
            # beneficiary. Loudest failure on this side.
            record.status, record.error = "failed", _log_failure(record, "fiat_withdraw", e)
            ramp_store.add_stage(record, "fiat_withdraw", "failed", record.error)
        return ramp_store.save(record)
    finally:
        # Payout PII (names, bank code, account number) lives only while the
        # ramp is in flight — drop it as soon as the advance step reaches a
        # terminal state (completed or failed). An unexpected escape leaves it
        # in place so a reconcile re-entry can still pay out; the TTL sweep
        # (expire_payouts) and the store's eviction path both still drop it if
        # the ramp never resumes.
        if record.status in ("completed", "failed"):
            _PAYOUTS.pop(record.ramp_id, None)


def _warn_unmatched(event: CryptoEvent | FiatEvent, target: str | None) -> None:
    """A settled deposit that belongs to no tracked ramp: money moved upstream
    and nothing here will act on it.

    Ramps are process-local (see ramp_store), so a buyer who pays half an hour
    after starting an on-ramp can settle against a restarted process and land
    here. This line is then the only record that the money arrived — it names
    the identifier, event type and amount so the ramp can be settled by hand.
    """
    logger.warning(
        "unmatched settlement event: kind=%s type=%s status=%s amount=%s asset=%s identifier=%s target=%s",
        "fiat" if isinstance(event, FiatEvent) else "crypto",
        event.transaction_type,
        event.status,
        event.amount,
        event.asset,
        event.identifier,
        target,
    )


def _deposit_covers_declared(record: RampRecord, deposited: float) -> bool:
    """Reconcile what actually ARRIVED against what an off-ramp declared, and
    fail the ramp if the deposit falls short.

    `start_offramp` records the amount the caller *asked* to sell, and both the
    sell and the peso payout are sized from it. The only authoritative figure
    is what the customer actually sent, so without this a ramp declared at
    100,000 USDC and funded with 1 sells 100,000 out of the institutional
    balance and pays the beneficiary the full proceeds.

    Amounts are compared through `money.format_amount` like every other amount
    in this module, so a deposit that matches to the cent can never read as a
    shortfall on binary-float noise (10.000000000000002 vs 10.0).

    An over-deposit PROCEEDS and settles only the declared amount: the deposit
    covers the sell, so no payout can exceed what arrived — the conservative
    invariant here — and failing would strand the whole deposit rather than
    just the excess. The excess stays in the institutional account and is
    warned about so it can be returned by hand.
    """
    declared = money.to_decimal(_num(record.usdc_amount))
    arrived = money.to_decimal(_num(deposited))
    if arrived < declared:
        e = PdaxError(
            f"deposit of {_num(deposited)} USDC does not cover the declared {_num(record.usdc_amount)} USDC",
            code="underfunded_deposit",
        )
        record.status, record.error = "failed", _log_failure(record, "reconcile_deposit", e)
        ramp_store.add_stage(record, "reconcile_deposit", "failed", record.error)
        return False
    if arrived > declared:
        logger.warning(
            "off-ramp deposit exceeds the declared amount: ramp_id=%s declared=%s deposited=%s identifier=%s",
            record.ramp_id,
            _num(record.usdc_amount),
            _num(deposited),
            record.identifier,
        )
    return True


# What a settlement event resolves to: its ramp and the advance that settles
# it, or (None, None) when it belongs to none.
_Matched = tuple[RampRecord | None, Callable[[PdaxClient, RampRecord], Awaitable[RampRecord]] | None]


def _match_fiat(event: FiatEvent) -> _Matched:
    """An on-ramp, by the identifier a completed fiat deposit carries."""
    if "DEPOSIT" not in event.transaction_type.upper():
        return None, None
    if str(event.status).upper() != "COMPLETED":
        return None, None
    record = ramp_store.find_by_identifier(event.identifier) if event.identifier else None
    if record and record.direction == "onramp":
        return record, advance_onramp
    _warn_unmatched(event, None)
    return None, None


def _match(event: CryptoEvent | FiatEvent) -> _Matched:
    """Find the ramp + advance function a settlement event belongs to.

    Events we were never going to act on (withdrawals, non-final statuses) are
    dropped quietly; a *completed deposit* with no matching ramp is warned
    about, because that one means unclaimed funds.
    """
    if isinstance(event, FiatEvent):
        return _match_fiat(event)

    # CryptoEvent — match an off-ramp by its USDCXLM deposit address (+ tag).
    if "DEPOSIT" not in event.transaction_type.upper():
        return None, None
    if str(event.status).lower() != "completed":
        return None, None
    if str(event.asset).upper() != USDCXLM:
        # An off-ramp sells USDC: a deposit of some unrelated token must never
        # match one, or an arbitrary asset sent to a tracked address buys a
        # peso payout. It is still unclaimed money, so it is warned about.
        _warn_unmatched(event, event.destination_address)
        return None, None
    # PDAX may hand out one shared custodial address plus a per-deposit tag, in
    # which case the tag — not the address — is what identifies the ramp, and
    # matching on the address alone would pay customer A out of customer B's
    # deposit. Events that carry no tag fall back to the address match.
    tag = event.destination_address_tag
    for record in ramp_store.list_all():
        if (
            record.direction == "offramp"
            and record.deposit_address
            and record.deposit_address == event.destination_address
            and (not tag or record.deposit_tag == tag)
        ):
            return record, advance_offramp
    _warn_unmatched(event, event.destination_address)
    return None, None


async def _advance_guarded(
    client: PdaxClient,
    record: RampRecord,
    advance: Callable[[PdaxClient, RampRecord], Awaitable[RampRecord]],
) -> RampRecord:
    """Flip a funded ramp forward through `advance` — as one recoverable unit.

    Expected PDAX failures are caught inside advance_* and land the ramp in a
    terminal "failed" state. Anything that escapes instead (a bug, a cancelled
    request) would otherwise strand the ramp mid-flight in funded/converting/
    settling — states neither reconcile nor a retried webhook re-enters. Roll
    the status back to `awaiting_payment` so the ramp stays recoverable.

    Must be called with the per-ramp lock held.
    """
    record.status = "funded"
    try:
        return await advance(client, record)
    except BaseException as e:
        logger.error(
            "ramp advance interrupted: ramp_id=%s direction=%s php=%s usdc=%s identifier=%s cause=%s",
            record.ramp_id,
            record.direction,
            _num(record.php_amount),
            _num(record.usdc_amount),
            record.identifier,
            type(e).__name__,
            exc_info=e,
        )
        record.status = "awaiting_payment"
        # Stage details reach the client verbatim, so record the stable code —
        # the exception type and traceback are in the log line above.
        ramp_store.add_stage(record, "advance", "failed", "advance_interrupted")
        ramp_store.save(record)
        raise


async def reconcile(client: PdaxClient, ramp_id: str) -> RampRecord | None:
    """Actively check PDAX for settlement of a waiting ramp and advance it — a
    fallback for when the webhook hasn't been delivered (e.g. staging redirect
    flows). Safe to poll: a ramp already past `awaiting_payment` is untouched."""
    record = ramp_store.get(ramp_id)
    if record is None or record.status != "awaiting_payment":
        return record
    async with ramp_store.lock_for(record.ramp_id):
        if record.status != "awaiting_payment":
            return record
        if record.direction == "onramp" and record.identifier:
            txns = await transactions.fiat_transactions(client, identifier=record.identifier, mode="CashIn")
            if any(str(t.status).upper() == "COMPLETED" for t in txns):
                return await _advance_guarded(client, record, advance_onramp)
        elif record.direction == "offramp" and record.deposit_address:
            crypto_txns = await transactions.crypto_transactions(client, type="crypto_in")
            for t in crypto_txns:
                if t.receiver_wallet_address == record.deposit_address and str(t.status).lower() == "completed":
                    return await _advance_guarded(client, record, advance_offramp)
        return record


async def handle_event(client: PdaxClient, event: CryptoEvent | FiatEvent) -> RampRecord | None:
    """Webhook entry point — match a settlement event to a waiting ramp and
    advance it under a per-ramp lock. Idempotent: a ramp already past
    `awaiting_payment` is returned untouched instead of advancing twice."""
    record, advance = _match(event)
    if record is None or advance is None:
        return None
    async with ramp_store.lock_for(record.ramp_id):
        if record.status != "awaiting_payment":
            return record
        # A matched CryptoEvent is always an off-ramp deposit (see _match): what
        # it says arrived is the only figure the sell may be sized against.
        if isinstance(event, CryptoEvent) and not _deposit_covers_declared(record, event.amount):
            return ramp_store.save(record)
        return await _advance_guarded(client, record, advance)


# How long an off-ramp may sit awaiting its USDC before the stashed payout
# details are treated as abandoned. Sized to the ramp's payment window: the
# agent sends USDC immediately after `start_offramp` returns the deposit
# address, so an hour is generous, and a ramp that quiet is not settling
# unattended anyway.
PAYOUT_STASH_TTL_SECONDS = 3600.0


class _PayoutStash(NamedTuple):
    """A stashed `OffRampRequest` plus when it was stashed (monotonic clock)."""

    created_at: float
    request: OffRampRequest


# Beneficiary payout details for in-flight off-ramps (kept beside the store).
# This is the most sensitive structure in the process — full bank account
# number, account name and both parties' names — so it is swept on every
# touch rather than only on terminal advance / store eviction.
_PAYOUTS: dict[str, _PayoutStash] = {}


def _stash_payout(ramp_id: str, req: OffRampRequest) -> None:
    expire_payouts()
    _PAYOUTS[ramp_id] = _PayoutStash(time.monotonic(), req)


def expire_payouts(*, now: float | None = None) -> None:
    """Drop beneficiary PII for off-ramps that were never funded.

    An off-ramp whose agent never sends the USDC is otherwise held for the
    life of the process — the record is only popped on terminal advance or on
    store eviction, which takes 500 newer ramps. `now` is injectable so the
    expiry is deterministic in tests, as elsewhere in this package.
    """
    cutoff = (time.monotonic() if now is None else now) - PAYOUT_STASH_TTL_SECONDS
    stale = [rid for rid, stash in _PAYOUTS.items() if stash.created_at < cutoff]
    for ramp_id in stale:
        del _PAYOUTS[ramp_id]
        logger.info("dropped abandoned off-ramp payout details: ramp_id=%s", ramp_id)
