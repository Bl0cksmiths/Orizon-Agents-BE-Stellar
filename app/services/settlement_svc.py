"""
Settlement evidence — the honest answer to "has this agent actually been paid?"

The operator dashboard shows an earnings figure, and that figure has to come
from the ledger's own record of money moving. It can never come from what this
backend believes about the runs it orchestrated: a run finalizes `complete`
whether or not its charge settled, so orchestration state is evidence that work
happened, not that anyone paid for it.

What this reads
---------------
`PaymentEscrow.charge` publishes `(("charged", agent_id), (receipt_id, auth_id,
amount, job_id))` after the transfer lands, and publishes nothing at all when
it fails — so the presence of a `charged` event IS the proof that value moved.
This module scans Soroban RPC's event history for that topic pair and turns
each hit into one entry a reader can look up on an explorer.

Why the payer has to be resolved separately
-------------------------------------------
The event carries no payer and no owner, and without them a charge is just an
amount. Every `charged` event the deployed escrow has emitted so far has
`payer == settler == owner_of(agent_id)` — the platform paying itself — because
`charge` ends in `usdc.transfer(&auth.payer, …)` while only the settler signs,
so the SAC's `from.require_auth()` can only ever be satisfied for the settler's
own funds (docs/evidence/2.04-reference-agent-runbook.md, "Settlement
position"). Rendering those as operator revenue would be the single most
misleading thing this dashboard could do, so every entry's payer is read back
from `authorization(auth_id)` and compared against the agent's owner and the
escrow's settler. Anything that resolves to one of our own keys — or that
cannot be resolved at all — is still reported, but never counted.

Why "no entries" is not "never paid"
------------------------------------
Soroban RPC keeps events for about seven days and then drops them, so a scan
can only ever answer "within the window this node still holds". That is why
`window_days` and `scanned_ledgers` travel with the result: the frontend says
"nothing in the last N days", not "never paid". And a scan that could not run
at all sets `unavailable` and returns no entries — zero is a real answer, and
it is never allowed to stand in for a failed lookup.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from pydantic import BaseModel
from stellar_sdk import scval
from stellar_sdk.soroban_rpc import EventFilter, EventFilterType, EventInfo, GetEventsResponse

from ..stellar import client as sc

logger = logging.getLogger(__name__)

# Soroban RPC keeps events for ~7 days — 604_800 s at the ~5 s ledger close
# time — and then drops them. Nothing older is knowable from events at any
# price, so this is the ceiling on what a scan can even claim to cover.
RETENTION_LEDGERS = 120_960

# A single getEvents call is capped by the node at ~10_000 ledgers, so covering
# the window means paging. Pages are explicit [start, end) ledger ranges rather
# than cursor continuations, because the range we asked for is exactly the
# range we are entitled to claim we scanned — which is what `scanned_ledgers`
# has to report.
LEDGERS_PER_PAGE = 10_000

# 13 pages cover the full retention window; 14 is that plus one page of slack
# for a node whose retention runs a little long. The cap exists because the
# walk runs inside the bounded worker pool app/main.py hands to
# asyncio.to_thread — an unbounded page walk would pin one of those threads
# behind a slow RPC and starve unrelated routes.
MAX_PAGES = 14

# Wall-clock cap on the whole page walk, for the same reason as MAX_PAGES:
# 14 pages against the 5 s read timeout in `_server()` is over a minute of
# pinned thread, which a dashboard poll must never be allowed to cost.
SCAN_BUDGET_SECONDS = 20.0

# Events per page. The filter already narrows to one contract, one topic and
# one agent, so this sits far above anything the deployed escrow produces; a
# page that fills it anyway means the range held more charges than we read, and
# that is reported as `truncated` rather than silently under-counted.
PAGE_EVENT_LIMIT = 200

# A full scan is a dozen-odd RPC round trips and the dashboard polls, so the
# whole answer is cached long enough to collapse a poll loop into one scan
# while staying inside a human's sense of "live". `unavailable` answers are
# cached the same way on purpose: a hard-down RPC must not be re-probed once
# per poll.
CACHE_TTL_SECONDS = 30.0

# An Authorization's payer is written once by `authorize` and never moves after
# it (only `spent` and `revoked` do), and both the escrow's settler and the
# asset its SAC wraps are fixed at deploy time. Values that cannot drift are
# cached far longer than a read that can.
IMMUTABLE_READ_TTL_SECONDS = 900.0

# What `payer` says when `authorization(auth_id)` could not be read. Chosen to
# be obviously not a G-address, so no client can mistake it for one.
UNKNOWN_PAYER = "unknown"

# The label used wherever the asset behind the amounts could not be established.
UNKNOWN_ASSET = "unknown"

CHARGED_TOPIC = "charged"


class SettlementEntry(BaseModel):
    """One `charged` event, attributed to the account that actually paid."""

    job_id: str  # hex, 16 bytes — the job the charge settled
    auth_id: str  # hex, 16 bytes — the authorization it was drawn from
    amount_stroops: int  # 7-decimal units of `SettlementEvidence.asset`
    ledger: int  # the ledger that closed the charge — the explorer anchor
    at: str | None  # ISO-8601 ledger close time, None if the node omitted it
    payer: str  # G… address, or UNKNOWN_PAYER when the authorization is unreadable
    # True when this is not third-party revenue: the agent's own owner paid, the
    # platform's settler paid, or the payer could not be established at all.
    self_payment: bool


class SettlementEvidence(BaseModel):
    """Everything the chain will say about one agent's earnings, and the limits
    of what was looked at while saying it."""

    agent_id: str
    asset: str  # what the escrow's SAC wraps; "native" (XLM) on testnet
    window_days: float  # the span actually scanned, so a client can say "in N days"
    scanned_ledgers: int  # ledgers actually covered, never the theoretical window
    entries: list[SettlementEntry]
    total_stroops: int  # sum of entries with self_payment False — verified revenue
    self_payment_stroops: int  # sum of the excluded ones: reported, not hidden
    truncated: bool  # the scan stopped before covering the whole window
    # Why no scan happened, in words a human can act on; None when one did. An
    # empty `entries` with this set to None means "nothing inside window_days".
    unavailable: str | None


def _describe(e: BaseException) -> str:
    """Compact "Type: message" description — bare type when there is no
    message, as asyncio.TimeoutError carries none. Mirrors the helper in
    registry_sync and external_binding."""
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _unavailable(agent_id: str, reason: str, asset: str = UNKNOWN_ASSET) -> SettlementEvidence:
    """An answer that admits it has no answer.

    Every field a caller might sum is zero AND `unavailable` says why, so a
    client can tell "this agent earned nothing" from "we could not find out" —
    the distinction the whole endpoint turns on.
    """
    return SettlementEvidence(
        agent_id=agent_id,
        asset=asset,
        window_days=0.0,
        scanned_ledgers=0,
        entries=[],
        total_stroops=0,
        self_payment_stroops=0,
        truncated=False,
        unavailable=reason,
    )


@dataclass(frozen=True)
class _Charge:
    """One `charged` event, decoded but not yet attributed to a payer."""

    job_id: str
    auth_id: str
    amount_stroops: int
    ledger: int
    at: str | None


def _decode_charged(event: EventInfo) -> _Charge | None:
    """Decode one `charged` event, or None if it is not one we understand.

    The filter already pins the contract and both topics, so a payload that
    does not unpack as `(receipt_id, auth_id, amount, job_id)` means the
    deployed ABI has moved under us. Such an event is skipped and logged rather
    than coerced: half a decoded event still has an amount, and putting a
    number of unknown provenance on an earnings dashboard is precisely the
    outcome this module exists to prevent. The rest of the scan stays readable,
    and the warning is what says the ABI drifted.
    """
    try:
        data = scval.to_native(event.value)
    except Exception as e:
        logger.warning("[settlement] undecodable charged event at ledger %s: %s", event.ledger, _describe(e))
        return None
    if not isinstance(data, (list, tuple)) or len(data) != 4:
        logger.warning("[settlement] charged event at ledger %s is not a 4-tuple payload", event.ledger)
        return None
    _receipt_id, auth_id, amount, job_id = data
    if not isinstance(auth_id, bytes) or not isinstance(job_id, bytes) or not isinstance(amount, int):
        logger.warning("[settlement] charged event at ledger %s has unexpected field types", event.ledger)
        return None
    return _Charge(
        job_id=job_id.hex(),
        auth_id=auth_id.hex(),
        amount_stroops=amount,
        # `ledger_close_at` is the only timestamp on the event and the node is
        # its source, so the entry carries the node's word for when this
        # happened rather than the moment we asked.
        ledger=event.ledger,
        at=event.ledger_close_at.isoformat() if event.ledger_close_at else None,
    )


@dataclass(frozen=True)
class _Scan:
    """The raw outcome of one page walk, and how much of the window it covers."""

    charges: list[_Charge]
    scanned_ledgers: int
    truncated: bool
    seconds_per_ledger: float


def _event_filter(escrow_id: str, agent_id: str) -> EventFilter:
    """Contract plus BOTH topics, so the node does the filtering.

    Matching `("charged", agent_id)` server-side is what keeps a full-window
    scan down to a handful of small pages. Filtering here instead would mean
    paging every `authd` and `revoked` event the escrow emitted for every other
    agent on the deployment, through a rate-limited public RPC.
    """
    return EventFilter(
        event_type=EventFilterType.CONTRACT,
        contract_ids=[escrow_id],
        topics=[[sc.sym(CHARGED_TOPIC).to_xdr(), sc.sym(agent_id).to_xdr()]],
    )


def _seconds_per_ledger(probe: GetEventsResponse) -> float:
    """The ledger close interval, measured from the node's own retention span.

    `window_days` is a client's licence to say "nothing in the last N days", so
    N is measured rather than assumed: a node keeping six days instead of
    seven, or a network running slow, both show up here instead of being
    papered over by a constant. Clamped to a plausible range because one absurd
    close time must not become an absurd claim.
    """
    ledgers = probe.latest_ledger - probe.oldest_ledger
    seconds = probe.latest_Ledger_close_time - probe.oldest_ledger_close_time
    if ledgers <= 0 or seconds <= 0:
        return 5.0
    return min(max(seconds / ledgers, 1.0), 30.0)


def _scan_sync(escrow_id: str, agent_id: str) -> _Scan:
    """Walk the retention window for this agent's `charged` events. Blocking.

    Two setup calls before the walk, deliberately. `getLatestLedger` gives the
    tip; a one-event `getEvents` probe then reports the node's OWN
    `oldestLedger` and close times. Starting from a hardcoded
    `latest - RETENTION_LEDGERS` instead would be a claim about retention we
    never verified, and the node rejects the request outright whenever its real
    retention runs even one ledger short of our constant. The probe's own
    events are discarded — the walk below covers the tip again — so nothing is
    counted twice.

    `_server()` is reused rather than a fresh SorobanServer: it carries the
    read timeout profile (5 s, no retry) that bounds this walk, and stellar-sdk
    already sends a User-Agent, which the public RPC answers 403 without.
    """
    server = sc._server()
    filters = [_event_filter(escrow_id, agent_id)]
    latest = server.get_latest_ledger().sequence
    probe = server.get_events(start_ledger=latest, filters=filters, limit=1)

    charges: list[_Charge] = []
    scanned = 0
    truncated = False
    pages = 0
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    cursor = max(probe.oldest_ledger, latest - RETENTION_LEDGERS + 1, 1)

    while cursor <= latest:
        if pages >= MAX_PAGES or time.monotonic() >= deadline:
            truncated = True
            break
        end = min(cursor + LEDGERS_PER_PAGE, latest + 1)
        try:
            page = server.get_events(start_ledger=cursor, end_ledger=end, filters=filters, limit=PAGE_EVENT_LIMIT)
        except Exception as e:
            # The FIRST page failing means nothing was scanned at all, which is
            # an `unavailable`, not a zero — let the caller say so. A later one
            # failing leaves real, already-decoded entries behind it, and
            # throwing those away to report a rounder number would be the same
            # lie pointing the other way.
            if scanned == 0:
                raise
            logger.warning("[settlement] page %d of %s failed: %s", pages + 1, agent_id, _describe(e))
            truncated = True
            break
        pages += 1
        scanned += end - cursor
        cursor = end
        charges.extend(c for c in (_decode_charged(ev) for ev in page.events) if c is not None)
        if len(page.events) >= PAGE_EVENT_LIMIT:
            # The node had at least as many charges in this range as we allowed
            # it to return, so what came back may be a subset of the range we
            # just claimed to have scanned. Saying so is the only honest move.
            truncated = True

    return _Scan(
        charges=charges,
        scanned_ledgers=scanned,
        truncated=truncated,
        seconds_per_ledger=_seconds_per_ledger(probe),
    )
