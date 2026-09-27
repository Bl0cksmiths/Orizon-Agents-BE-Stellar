"""The refund reconcile sweep: settle claims parked in `crediting` from the chain.

A refund whose submission timed out is parked in `crediting` with its claim
held and its in-flight hash on the record (D3), and until now only an operator
running docs/disputes.md's manual procedure could move it. This automates
exactly what that procedure asks of a human who has a hash to look up, and
nothing more:

  - the transfer LANDED -> record `credited`, with the hash and the amount the
    transaction moved (read off the transaction, never off an estimate);
  - it FAILED -> nothing moved, so the claim is released and the dispute is
    `upheld` and payable again;
  - it is NOT FOUND -> released only once the LEDGER's clock has passed the
    last moment the transaction could have been valid, plus a margin, AND the
    RPC's history still covers every ledger it could have landed in. Before
    that, it is left alone.

Everything else is left for a human, and said so in the log: a claim with no
hash (nothing to look up — the procedure's second step, reading the settler's
history, is a judgement this does not make), an RPC that errored or timed out,
a hash older than the RPC's history, a transaction that is not this dispute's
refund, an amount that disagrees with the one on record, a claim over a
dispute that is not `crediting`.

Two properties hold every write here, and neither is a read in Python:

  - every write is a compare-and-set on `crediting` AND on the very hash the
    chain was asked about (`expected_refund_tx`), so two sweeps — or a sweep
    and anything else — can record a landed credit exactly once, and a verdict
    about one transfer can never close or release a claim that has since been
    taken again over another;
  - nothing is signed or submitted. The sweep reads the chain and writes the
    record, which is why the RATING a manual reconcile is followed by is left
    to the next uphold (see `_record_landed`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from stellar_sdk import Address, TransactionEnvelope, scval
from stellar_sdk.operation import InvokeHostFunction
from stellar_sdk.xdr import HostFunctionType, SCValType

from ..config import settings
from ..stellar import client as sc
from . import refund_svc
from .dispute_store import DisputeRecord, RefundClaim, get_dispute_store

logger = logging.getLogger(__name__)

# The refund transaction's validity window: `_send_server_signed` builds it
# with `.set_timeout(30)`, so its maxTime is the builder's clock plus 30. A
# test holds the two numbers together.
REFUND_TX_TIMEOUT_SECONDS = 30

# How far past the latest possible maxTime the ledger must be before a hash the
# RPC cannot find is read as "can never land". Generous on purpose: the cost of
# waiting is a buyer paid two minutes later, and the cost of being early is a
# second transfer out of the platform's wallet.
EXPIRY_MARGIN_SECONDS = 120

# A claim younger than this is not looked at. An uphold that is still running
# holds its claim for up to about a minute (submit plus a 30 s poll); the
# sweep is for what is left over after that, never a second opinion on a
# payout still in progress.
MIN_CLAIM_AGE_SECONDS = 300

# The RPC's oldest retained ledger must close at least this long before the
# claim was taken for NOT_FOUND to mean anything: the transaction cannot have
# landed before its claim existed, and the margin covers the ledger's clock
# and ours disagreeing by a little.
HISTORY_MARGIN_SECONDS = 60

# The whole wait for one lookup. The client's read profile already bounds the
# HTTP call at 5 s; this bounds the worker thread's queue as well, so one
# stuck lookup cannot hold the pass — and the pass cannot hold the loop.
LOOKUP_TIMEOUT_SECONDS = 20.0

_STROOPS_PER_USDC = 10_000_000

Action = Literal[
    "credited",  # SUCCESS, recorded
    "released",  # FAILED, claim handed back
    "expired_released",  # NOT_FOUND past maxTime + margin, claim handed back
    "already_settled",  # somebody recorded the same verdict first
    "pending",  # NOT_FOUND, but the transaction may still land
    "young",  # claim too recent to look at
    "no_hash",  # nothing to look up: a human reads the settler's history
    "history_gap",  # older than the RPC's history: a human
    "rpc_error",  # the chain could not be asked: try again next pass
    "not_this_refund",  # the hash is not this dispute's refund transfer: a human
    "amount_mismatch",  # the chain and the record disagree on the amount: a human
    "no_expiry_bound",  # nothing on record dates the submission: a human
    "not_crediting",  # a claim over a dispute not in `crediting`: a human
    "missing",  # a claim over a dispute that cannot be read: a human
    "lost_race",  # the record moved under the verdict: a human
    "error",  # the pass failed on this claim: try again next pass
]


@dataclass(frozen=True)
class Decision:
    """What one pass did with one claim, and why — the unit every log line is."""

    dispute_id: str
    action: Action
    claim_age_s: float
    tx_hash: str | None = None
    chain: str | None = None  # the RPC's word: SUCCESS, FAILED, NOT_FOUND
    detail: str = ""


@dataclass(frozen=True)
class SweepReport:
    """One pass: when it ran and what it did, counted by action."""

    started_at: float
    finished_at: float
    outcomes: dict[str, int] = field(default_factory=dict)
    # Why the pass did nothing at all (refunds not configured, the store down).
    skipped: str | None = None


@dataclass(frozen=True)
class _Transfer:
    """The one SAC `transfer` a refund transaction invokes, as it was signed."""

    contract: str
    source: str
    to: str
    stroops: int


def _refund_transfer(envelope_xdr: str) -> _Transfer | None:
    """The refund transfer in `envelope_xdr`, or None if it is not one.

    A refund is a plain v1 envelope with exactly one InvokeHostFunction that
    calls `transfer(from, to, amount)` on a contract. Anything else — a fee
    bump, a second operation, another function, arguments of the wrong shape —
    is not something this sweep may reason about.
    """
    try:
        envelope = TransactionEnvelope.from_xdr(envelope_xdr, sc.network_passphrase())
    except Exception:
        return None
    tx = envelope.transaction
    if len(tx.operations) != 1 or not isinstance(tx.operations[0], InvokeHostFunction):
        return None
    host_function = tx.operations[0].host_function
    if host_function.type != HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT:
        return None
    call = host_function.invoke_contract
    if call is None or call.function_name.sc_symbol != b"transfer" or len(call.args) != 3:
        return None
    source, to, amount = call.args
    if source.type != SCValType.SCV_ADDRESS or to.type != SCValType.SCV_ADDRESS or amount.type != SCValType.SCV_I128:
        return None
    return _Transfer(
        contract=Address.from_xdr_sc_address(call.contract_address).address,
        source=scval.from_address(source).address,
        to=scval.from_address(to).address,
        stroops=scval.from_int128(amount),
    )


def _log(decision: Decision) -> None:
    """One structured line per decision: dispute, claim age, hash, the chain's
    answer, the action. The level says who has to act: INFO when nobody,
    WARNING when a human must look, ERROR when a record and the chain disagree,
    CRITICAL when money may have landed on a record that no longer says so."""
    level = {
        "credited": logging.INFO,
        "released": logging.INFO,
        "expired_released": logging.INFO,
        "already_settled": logging.INFO,
        "pending": logging.INFO,
        "young": logging.DEBUG,
        "rpc_error": logging.WARNING,
        "error": logging.ERROR,
        "no_hash": logging.WARNING,
        "history_gap": logging.ERROR,
        "not_this_refund": logging.ERROR,
        "amount_mismatch": logging.ERROR,
        "no_expiry_bound": logging.WARNING,
        "not_crediting": logging.ERROR,
        "missing": logging.ERROR,
        "lost_race": logging.CRITICAL,
    }[decision.action]
    logger.log(
        level,
        "refund reconcile: dispute=%s claim_age_s=%.0f tx=%s chain=%s action=%s%s",
        decision.dispute_id,
        decision.claim_age_s,
        decision.tx_hash or "-",
        decision.chain or "-",
        decision.action,
        f" — {decision.detail}" if decision.detail else "",
    )


async def _lookup(tx_hash: str) -> sc.LedgerTransaction:
    """`sc.get_transaction` off the event loop, bounded as a whole."""
    return await asyncio.wait_for(asyncio.to_thread(sc.get_transaction, tx_hash), timeout=LOOKUP_TIMEOUT_SECONDS)


def _identify(
    dispute: DisputeRecord, found: sc.LedgerTransaction, *, settler: str, asset_sac: str
) -> tuple[_Transfer | None, str]:
    """The refund transfer the chain holds under this dispute's hash, or why not.

    Every check is against the chain's copy of the transaction: that it hashes
    to what was asked, calls `transfer` on the asset SAC, from the settler, to
    this dispute's payer, for an amount that agrees with the one on record
    when one is. A hash that answers anything else is not evidence about this
    dispute, whatever its status.
    """
    if not found.envelope_xdr:
        return None, "the RPC answered with no envelope"
    try:
        hashed = TransactionEnvelope.from_xdr(found.envelope_xdr, sc.network_passphrase()).hash_hex()
    except Exception:
        return None, "the envelope does not decode"
    if hashed != found.tx_hash:
        return None, f"the envelope hashes to {hashed}, not the hash asked about"
    transfer = _refund_transfer(found.envelope_xdr)
    if transfer is None:
        return None, "the transaction is not a single SAC transfer"
    if transfer.contract != asset_sac:
        return None, f"the transfer is over {transfer.contract}, not the asset SAC"
    if transfer.source != settler:
        return None, f"the transfer is from {transfer.source}, not the settler"
    if transfer.to != dispute.payer:
        return None, f"the transfer is to {transfer.to}, not this dispute's payer"
    if transfer.stroops <= 0:
        return None, f"the transfer moves {transfer.stroops} stroops"
    return transfer, ""


async def _record_landed(dispute: DisputeRecord, tx_hash: str, stroops: int, base: dict[str, Any]) -> Decision:
    """A SUCCESS: record `credited` with what the transaction moved, exactly once.

    The RATING that follows a credit is deliberately NOT written here, and the
    next uphold writes it. Rating signs and submits a transaction with the
    server key; this sweep signs nothing, and keeping it that way keeps it a
    reader of the chain and a writer of the record only — a background loop
    that could sign would be a different thing to switch on. It also matches
    the manual procedure, where recording the credit and upholding again are
    two steps, and an uphold of a `credited` dispute signs no transfer and
    rates it alone. A `credited` dispute with no `rating_tx` already reads, on
    the receipt and to `uphold`, as paid and not yet fully resolved.
    """
    store = get_dispute_store()
    amount_usdc = stroops / _STROOPS_PER_USDC
    written = await store.append_status(
        dispute.id,
        "credited",
        refund_tx=tx_hash,
        credited_usdc=amount_usdc,
        expected_status="crediting",
        expected_refund_tx=tx_hash,
    )
    if written is not None:
        return Decision(
            **base,
            action="credited",
            detail=f"{amount_usdc:.7f} USDC landed; the agent's rating is left to the next uphold of this dispute",
        )
    return await _after_a_lost_write(dispute.id, tx_hash, base, landed=True)


async def _release(dispute: DisputeRecord, tx_hash: str, action: Action, base: dict[str, Any], why: str) -> Decision:
    """Nothing moved and nothing can: hand the claim back, gated on this hash."""
    released = await get_dispute_store().release_refund_claim(dispute.id, expected_refund_tx=tx_hash)
    if released is not None:
        return Decision(**base, action=action, detail=f"{why}; the dispute is upheld and payable again")
    return await _after_a_lost_write(dispute.id, tx_hash, base, landed=False)


async def _after_a_lost_write(dispute_id: str, tx_hash: str, base: dict[str, Any], *, landed: bool) -> Decision:
    """The compare-and-set refused: say what the record says now.

    The same verdict already on record is somebody else getting there first —
    a second sweep, an operator — and nothing is wrong. Anything else means
    the dispute moved under the verdict; nothing was written over it, and a
    human decides.
    """
    now = await get_dispute_store().get_dispute(dispute_id)
    if landed and now is not None and now.status == "credited" and now.refund_tx == tx_hash:
        return Decision(**base, action="already_settled", detail="this credit was already recorded")
    if not landed and now is not None and now.status == "upheld":
        return Decision(**base, action="already_settled", detail="the claim was already handed back")
    state = "missing" if now is None else f"{now.status}, refund tx {now.refund_tx or '-'}"
    what = "the transfer LANDED" if landed else "the transfer moved nothing"
    return Decision(
        **base,
        action="lost_race",
        detail=f"{what}, but the dispute moved under the verdict (now {state}) — nothing was overwritten;"
        " reconcile it by hand",
    )


async def reconcile_claim(claim: RefundClaim, *, now: float, settler: str, asset_sac: str) -> Decision:
    """Decide one claim from the chain, act on it, and return what was done.

    `now` is this process's clock and is used for ONE thing, the claim's age.
    Whether a transaction can still land is decided by the ledger's clock,
    from the same RPC answer the transaction's absence came in.
    """
    age = now - claim.claimed_at
    if age < MIN_CLAIM_AGE_SECONDS:
        return Decision(claim.dispute_id, "young", age)

    dispute = await get_dispute_store().get_dispute(claim.dispute_id)
    if dispute is None:
        return Decision(claim.dispute_id, "missing", age, detail="a refund claim over a dispute that cannot be read")
    if dispute.status != "crediting":
        # The queue was read before this dispute was; a claim settled in
        # between (by another pass, or an operator) is simply gone now.
        if all(held.dispute_id != dispute.id for held in await get_dispute_store().list_refund_claims()):
            return Decision(
                dispute.id,
                "already_settled",
                age,
                dispute.refund_tx,
                detail=f"the claim was settled while this pass ran (now {dispute.status})",
            )
        # The wedge the store describes: a claim held over a dispute that is not
        # mid-payout. Blocking rather than forgetting is the store's rule; the
        # way out is a human reading the chain.
        return Decision(
            dispute.id,
            "not_crediting",
            age,
            dispute.refund_tx,
            detail=f"the claim is held over a dispute in {dispute.status}",
        )
    tx_hash = dispute.refund_tx
    if not tx_hash:
        return Decision(
            dispute.id,
            "no_hash",
            age,
            detail=f"no in-flight hash on record — read the settler's history for a transfer to {dispute.payer}"
            f" of about {(dispute.inflight_usdc or dispute.creditable_usdc):.7f} USDC (docs/disputes.md)",
        )

    try:
        found = await _lookup(tx_hash)
    except Exception as exc:
        return Decision(
            dispute.id, "rpc_error", age, tx_hash, detail=f"{type(exc).__name__}: {exc} — nothing was changed"
        )
    base: dict[str, Any] = {"dispute_id": dispute.id, "claim_age_s": age, "tx_hash": tx_hash, "chain": found.status}

    if found.status in ("SUCCESS", "FAILED"):
        transfer, why_not = _identify(dispute, found, settler=settler, asset_sac=asset_sac)
        if transfer is None:
            return Decision(**base, action="not_this_refund", detail=why_not)
        if dispute.inflight_usdc is not None and sc.usdc_to_i128(dispute.inflight_usdc) != transfer.stroops:
            return Decision(
                **base,
                action="amount_mismatch",
                detail=f"the chain's transfer is {transfer.stroops} stroops, the record's in-flight amount"
                f" {sc.usdc_to_i128(dispute.inflight_usdc)}",
            )
        if found.status == "SUCCESS":
            return await _record_landed(dispute, tx_hash, transfer.stroops, base)
        return await _release(dispute, tx_hash, "released", base, "the ledger answered FAILED, so no funds moved")

    if found.status != "NOT_FOUND":
        return Decision(**base, action="rpc_error", detail="an answer this sweep does not know — nothing was changed")

    # NOT_FOUND is evidence only inside the window the RPC answers for. Its
    # oldest ledger must predate the claim: a transaction cannot land before
    # its claim existed, so a history that starts earlier covers every ledger
    # it could be in. One that starts later may simply have forgotten it —
    # which is how a refund that landed reads once it is a week old.
    if found.oldest_ledger_close_time > claim.claimed_at - HISTORY_MARGIN_SECONDS:
        return Decision(
            **base,
            action="history_gap",
            detail=f"the RPC's history starts at ledger {found.oldest_ledger} (closed"
            f" {found.oldest_ledger_close_time}), after this claim was taken ({claim.claimed_at:.0f}) — NOT_FOUND"
            " cannot tell 'never landed' from 'forgotten'; read the payer's account by hand",
        )
    # The latest moment the transaction could be valid. Its maxTime is the
    # builder's clock plus the timeout, and the builder ran BEFORE the row
    # that recorded this hash was written — so that row's `updated_at` (or any
    # later one's) plus the timeout is at or past the real maxTime. It is our
    # clock's number, and so is maxTime: the network compares both with the
    # ledger's close time, and so does this.
    if dispute.updated_at is None:
        return Decision(**base, action="no_expiry_bound", detail="nothing on record dates the submission")
    can_land_until = dispute.updated_at + REFUND_TX_TIMEOUT_SECONDS
    if found.latest_ledger_close_time <= can_land_until + EXPIRY_MARGIN_SECONDS:
        return Decision(
            **base,
            action="pending",
            detail=f"the ledger closed at {found.latest_ledger_close_time}; the transaction may land until"
            f" {can_land_until:.0f}, and is left alone until {can_land_until + EXPIRY_MARGIN_SECONDS:.0f}",
        )
    return await _release(
        dispute,
        tx_hash,
        "expired_released",
        base,
        f"not on the ledger, and ledger {found.latest_ledger} closed at {found.latest_ledger_close_time}, past its"
        f" last valid moment {can_land_until:.0f} + {EXPIRY_MARGIN_SECONDS}s — it can never land",
    )


_run_lock = asyncio.Lock()
_last: SweepReport | None = None
_task: asyncio.Task[None] | None = None


async def sweep_once() -> SweepReport:
    """One pass over the claim queue. Never raises; one pass at a time.

    A pass that finds another already running returns at once rather than
    queueing behind it: the running one is already doing this work.
    """
    global _last
    started = time.time()
    if _run_lock.locked():
        return SweepReport(started, time.time(), skipped="a pass is already running")
    async with _run_lock:
        report = await _pass(started)
        _last = report
        return report


async def _pass(started: float) -> SweepReport:
    gap = refund_svc.config_gap()
    if gap is not None:
        logger.warning("refund reconcile: pass skipped — %s", gap.problem)
        return SweepReport(started, time.time(), skipped=gap.problem)
    try:
        settler = sc.signer_public_key()
        asset_sac = sc.contract_ids().asset_sac
        claims = await get_dispute_store().list_refund_claims()
    except Exception as exc:
        logger.error("refund reconcile: pass failed before any claim was read: %s", exc, exc_info=True)
        return SweepReport(started, time.time(), skipped=f"{type(exc).__name__}")
    counts: Counter[str] = Counter()
    for claim in claims:
        try:
            decision = await reconcile_claim(claim, now=time.time(), settler=settler, asset_sac=asset_sac)
        except Exception as exc:
            decision = Decision(
                claim.dispute_id, "error", time.time() - claim.claimed_at, detail=f"{type(exc).__name__}: {exc}"
            )
        _log(decision)
        counts[decision.action] += 1
    report = SweepReport(started, time.time(), dict(sorted(counts.items())))
    logger.info(
        "refund reconcile: pass over %d claim(s) in %.1fs — %s",
        len(claims),
        report.finished_at - started,
        ", ".join(f"{k}={v}" for k, v in report.outcomes.items()) or "nothing held",
    )
    return report


async def _loop() -> None:
    while True:
        await sweep_once()
        await asyncio.sleep(settings.refund_reconcile_interval_seconds)


def _on_task_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("refund reconcile loop died: %s", exc, exc_info=exc)


def start() -> bool:
    """Start the sweep if this deployment asked for it; returns whether it runs.

    Both switches must be on. The sweep's own is off by default; the refund
    switch is the one it depends on, because releasing a claim makes a
    dispute payable again, which only means something where credits are paid.
    """
    global _task
    if not settings.refund_reconcile_enabled:
        return False
    if not settings.dispute_refunds_enabled:
        logger.warning(
            "refund reconcile: REFUND_RECONCILE_ENABLED is on but DISPUTE_REFUNDS_ENABLED is off — the sweep is"
            " NOT started; claims held in crediting stay with a human"
        )
        return False
    if _task is not None and not _task.done():
        return True
    _task = asyncio.create_task(_loop())
    _task.add_done_callback(_on_task_done)
    logger.info(
        "refund reconcile: sweep started, every %.0fs, looking at claims older than %ds",
        settings.refund_reconcile_interval_seconds,
        MIN_CLAIM_AGE_SECONDS,
    )
    return True


async def stop() -> None:
    """Cancel the loop and wait for it to unwind (shutdown path)."""
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None


def status() -> dict[str, Any]:
    """The sweep as /readiness reports it: whether it runs, and its last pass."""
    return {
        "enabled": settings.refund_reconcile_enabled and settings.dispute_refunds_enabled,
        "running": _task is not None and not _task.done(),
        "last_run_at": None if _last is None else _last.finished_at,
        "last_skipped": None if _last is None else _last.skipped,
        "last_outcomes": {} if _last is None else dict(_last.outcomes),
    }
