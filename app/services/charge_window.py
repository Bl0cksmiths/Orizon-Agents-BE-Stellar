"""
The escrow's charges for the whole event window — read once, for every agent.

`settlement_svc.fetch_settlement` answers "has THIS agent been paid?" with a
scan filtered to the agent: an `owner_of` read, a ledger probe and thirteen
getEvents pages. Right for one operator's dashboard; ruinous for a report about
every agent. The adoption report ran it once per external agent — ~15 RPC
calls each, ~15,000 at 1,000 agents — and on 2026-10-06 its build overran its
15-minute budget twice and the endpoint answered 503 (D-091 at scale).

This module reads the `("charged", *)` topic of the configured escrow ONCE for
the RPC's event window and files each charge under the agent its second topic
names. Then it keeps what it read:

  - the index of charges and the ledger range it covers. The next build scans
    only the ledgers that closed since (one or two pages), and drops charges
    that aged out of the node's window;
  - every payer it has read (`authorization(auth_id).payer` is written once and
    never moves), so a charge's payer is read once, ever;
  - both, in Postgres beside the report (`snapshot_store`), so a restart
    resumes rather than starts over.

It is honest about falling short in the same terms `settlement_svc` is. A walk
that ran out of time, a page that failed, or one ledger holding more charges
than a page returns leaves the index `truncated`, and the next build picks up
where it stopped. A payer not read yet keeps its charge out of revenue
(`payer_unreadable`) until it is. And attribution is `settlement_svc`'s own
`_build_entries`, so a charge funded by the agent's owner or by the platform is
reported and never counted, exactly as on the operator dashboard.

Event order and paging. Pages are explicit `[start, end)` ledger ranges, as in
`settlement_svc`, because the range asked for is the range we may claim to have
read. A page that comes back full may hold more than it returned, so its range
is split in two and each half read again, down to a single ledger; a single
ledger that still overflows is the one thing that cannot be read whole, and is
reported (`truncated`) until it leaves the window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from stellar_sdk import scval
from stellar_sdk.soroban_rpc import EventFilter, EventFilterType, EventInfo

from ..config import settings
from ..stellar import client as sc
from . import settlement_svc, snapshot_store
from .settlement_svc import SettlementEvidence, _Charge

logger = logging.getLogger(__name__)

# Events per getEvents request. A range that returns this many is split and
# read again, so this bounds a response's size, not what can be read.
PAGE_EVENT_LIMIT = 1_000

# How many payer reads run at once. Each pins a thread of the bounded pool
# app/main.py hands to asyncio.to_thread, like every other chain read.
PAYER_CONCURRENCY = 4

# Ceiling on one payer read, so one stalled read costs its charge, not the
# build. Above the read profile's own 5 s transport timeout.
PAYER_READ_TIMEOUT_SECONDS = 15.0

# Ceiling on writing the index back to Postgres. Persistence is an
# optimisation of the next process's first build, never a reason to stall one.
SAVE_TIMEOUT_SECONDS = 10.0

# The row the index is kept under in `read_snapshots`, beside the report.
INDEX_NAME = "adoption_charge_index"
_INDEX_VERSION = 1


@dataclass(frozen=True)
class _Indexed:
    """One `charged` event, with the agent it paid and the node's event id."""

    agent_id: str
    event_id: str
    charge: _Charge


@dataclass(frozen=True)
class _Index:
    """Every charge the escrow emitted in `[first_ledger, next_ledger)`."""

    escrow_id: str
    first_ledger: int
    next_ledger: int  # the first ledger NOT read yet
    charges: tuple[_Indexed, ...]
    seconds_per_ledger: float
    # The newest ledger that held more charges than a page returns: until it
    # leaves the window, the charges read are a subset of the range covered.
    lossy_ledger: int = 0
    latest: int = 0  # the node's tip when this was read

    @property
    def scanned_ledgers(self) -> int:
        return max(0, self.next_ledger - self.first_ledger)

    @property
    def lossy(self) -> bool:
        return self.lossy_ledger >= self.first_ledger


@dataclass
class _Held:
    """What this process keeps between builds."""

    index: _Index | None = None
    payers: dict[str, str] = field(default_factory=dict)  # auth_id hex → payer, for `index.escrow_id`
    restored: bool = False
    saved: tuple[int, int, int] | None = None  # (first, next, payers) last written


_held = _Held()
_lock = asyncio.Lock()


def reset() -> None:
    """Forget the index and the payers (tests; a new process)."""
    global _held, _lock
    _held = _Held()
    _lock = asyncio.Lock()


@dataclass(frozen=True)
class WindowSettlements:
    """Each requested agent's settlement evidence, from one window scan."""

    by_agent: dict[str, SettlementEvidence]
    ledgers_scanned: int
    ledgers_in_window: int  # what the node holds, as of the scan; 0 when unknown
    # Some work was left for the next build because the deadline passed.
    out_of_time: bool
    charges: int  # charges to the requested agents found in the window
    unattributed: int  # of those, how many have no payer read yet (never revenue)

    @property
    def complete(self) -> bool:
        """Every read this needed was made: nothing truncated, unavailable,
        left for later, or missing a payer."""
        return (
            not self.out_of_time
            and self.unattributed == 0
            and all(e.unavailable is None and not e.truncated for e in self.by_agent.values())
        )


# ── the scan ──────────────────────────────────────────────────────────────
def _window_filter(escrow_id: str) -> EventFilter:
    """The escrow's `charged` events for ANY agent: the node matches the first
    topic and wildcards the second, so nothing else the escrow emits is paged."""
    return EventFilter(
        type=EventFilterType.CONTRACT,
        contractIds=[escrow_id],
        topics=[[sc.sym(settlement_svc.CHARGED_TOPIC).to_xdr(), "*"]],
    )


def _agent_of(event: EventInfo) -> str | None:
    """The agent a `charged` event paid: its second topic, a Symbol."""
    if len(event.topic) != 2:
        return None
    try:
        agent_id = scval.to_native(event.topic[1])
    except Exception:
        return None
    return agent_id if isinstance(agent_id, str) else None


class _Dense(Exception):
    """One ledger holds more charges than a page returns."""

    def __init__(self, ledger: int, events: list[EventInfo]) -> None:
        super().__init__(f"ledger {ledger} holds more than {PAGE_EVENT_LIMIT} charges")
        self.ledger = ledger
        self.events = events


def _read_range(server: Any, filters: list[EventFilter], start: int, end: int, deadline: float) -> list[EventInfo]:
    """Every event in `[start, end)`, splitting any range whose page came back
    full. Raises `_Dense` for a single ledger that overflows a page (carrying
    what it did return), TimeoutError past the deadline, and whatever the node
    raises."""
    page = _twice(
        lambda: server.get_events(start_ledger=start, end_ledger=end, filters=filters, limit=PAGE_EVENT_LIMIT),
        deadline,
    )
    events = [e for e in page.events if start <= e.ledger < end]
    if len(page.events) < PAGE_EVENT_LIMIT:
        return events
    if end - start <= 1:
        raise _Dense(start, events)
    if time.monotonic() >= deadline:
        raise TimeoutError("out of time splitting a full page")
    middle = (start + end) // 2
    return _read_range(server, filters, start, middle, deadline) + _read_range(server, filters, middle, end, deadline)


_T = TypeVar("_T")


def _twice(read: Callable[[], _T], deadline: float) -> _T:
    """`read()`, asked once more if it fails while there is time. The read
    profile does not retry (`sc._server()`), and one dropped request would
    otherwise end a build's scan where a second ask would have finished it."""
    try:
        return read()
    except Exception as e:
        if time.monotonic() >= deadline:
            raise
        logger.info("[charges] read failed, asking once more: %s: %s", type(e).__name__, e)
        return read()


@dataclass(frozen=True)
class _Advance:
    index: _Index | None
    out_of_time: bool
    ledgers_in_window: int


def _advance_sync(escrow_id: str, held: _Index | None, deadline: float) -> _Advance:
    """Bring `held` up to the node's tip, as far as the deadline allows. Blocking.

    Raises only when the node cannot be anchored at all (its tip or its
    retention): there is then nothing new to claim, and the caller decides
    what the index it already holds is worth.
    """
    server = sc._server()
    filters = [_window_filter(escrow_id)]
    latest = _twice(server.get_latest_ledger, deadline).sequence
    probe = _twice(lambda: server.get_events(start_ledger=latest, filters=filters, limit=1), deadline)
    window_start = max(probe.oldest_ledger, latest - settlement_svc.RETENTION_LEDGERS + 1, 1)
    seconds_per_ledger = settlement_svc._seconds_per_ledger(probe)
    in_window = latest - window_start + 1

    if held is not None and held.escrow_id == escrow_id and held.next_ledger >= window_start:
        first = max(held.first_ledger, window_start)
        charges = [c for c in held.charges if c.charge.ledger >= first]
        index = replace(held, first_ledger=first, charges=tuple(charges))
    else:
        index = _Index(escrow_id, window_start, window_start, (), seconds_per_ledger)
    index = replace(index, seconds_per_ledger=seconds_per_ledger, latest=latest)

    seen = {c.event_id for c in index.charges}
    found = list(index.charges)
    cursor = index.next_ledger
    lossy = index.lossy_ledger
    out_of_time = False
    while cursor <= latest:
        if time.monotonic() >= deadline:
            out_of_time = True
            break
        end = min(cursor + settlement_svc.LEDGERS_PER_PAGE, latest + 1)
        try:
            events = _read_range(server, filters, cursor, end, deadline)
        except _Dense as dense:
            logger.warning("[charges] %s; the charges read from it are a subset", dense)
            events, lossy = dense.events, max(lossy, dense.ledger)
            # The rest of the range is still readable: carry on past the ledger.
            try:
                events += (
                    _read_range(server, filters, dense.ledger + 1, end, deadline) if dense.ledger + 1 < end else []
                )
            except Exception as e:
                logger.warning("[charges] page [%d, %d) failed after a dense ledger: %s", cursor, end, e)
                end = dense.ledger + 1
        except TimeoutError:
            out_of_time = True
            break
        except Exception as e:
            logger.warning("[charges] page [%d, %d) failed: %s: %s", cursor, end, type(e).__name__, e)
            break
        for event in events:
            agent_id = _agent_of(event)
            charge = settlement_svc._decode_charged(event)
            if agent_id is None or charge is None or event.id in seen:
                continue
            seen.add(event.id)
            found.append(_Indexed(agent_id, event.id, charge))
        cursor = end

    index = replace(index, next_ledger=cursor, charges=tuple(found), lossy_ledger=lossy)
    return _Advance(index=index, out_of_time=out_of_time, ledgers_in_window=in_window)


# ── payers ────────────────────────────────────────────────────────────────
async def _resolve_payers(escrow_id: str, auth_ids: set[str], deadline: float) -> bool:
    """Read every payer in `auth_ids` not already held. True when some were
    left unread because the deadline passed. A failed read is retried by the
    next build: only a payer actually read is kept."""
    missing = sorted(a for a in auth_ids if a not in _held.payers)
    if not missing:
        return False
    gate = asyncio.Semaphore(PAYER_CONCURRENCY)
    skipped = False

    async def _one(auth_id: str) -> None:
        nonlocal skipped
        async with gate:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                skipped = True
                return
            try:
                payer = await asyncio.wait_for(
                    settlement_svc._read_payer(escrow_id, auth_id), timeout=min(remaining, PAYER_READ_TIMEOUT_SECONDS)
                )
            except TimeoutError:
                skipped = skipped or time.monotonic() >= deadline
                return
            if payer is not None:
                _held.payers[auth_id] = payer

    await asyncio.gather(*(_one(a) for a in missing))
    return skipped


# ── persistence ───────────────────────────────────────────────────────────
def _encode(index: _Index, payers: Mapping[str, str]) -> bytes:
    live = {c.charge.auth_id for c in index.charges}
    return json.dumps(
        {
            "v": _INDEX_VERSION,
            "escrow_id": index.escrow_id,
            "first_ledger": index.first_ledger,
            "next_ledger": index.next_ledger,
            "seconds_per_ledger": index.seconds_per_ledger,
            "lossy_ledger": index.lossy_ledger,
            "charges": [
                {
                    "agent_id": c.agent_id,
                    "event_id": c.event_id,
                    "job_id": c.charge.job_id,
                    "auth_id": c.charge.auth_id,
                    "amount_stroops": c.charge.amount_stroops,
                    "ledger": c.charge.ledger,
                    "tx_hash": c.charge.tx_hash,
                    "at": c.charge.at,
                }
                for c in index.charges
            ],
            # Only the payers of charges still in the window.
            "payers": {a: p for a, p in payers.items() if a in live},
        },
        separators=(",", ":"),
    ).encode()


def _decode(body: bytes, escrow_id: str) -> tuple[_Index, dict[str, str]] | None:
    """The stored index, or None when it is not one this process may resume:
    another version, another escrow, or anything malformed."""
    raw = json.loads(body)
    if not isinstance(raw, dict) or raw.get("v") != _INDEX_VERSION or raw.get("escrow_id") != escrow_id:
        return None
    charges = tuple(
        _Indexed(
            agent_id=str(c["agent_id"]),
            event_id=str(c["event_id"]),
            charge=_Charge(
                job_id=str(c["job_id"]),
                auth_id=str(c["auth_id"]),
                amount_stroops=int(c["amount_stroops"]),
                ledger=int(c["ledger"]),
                tx_hash=None if c["tx_hash"] is None else str(c["tx_hash"]),
                at=None if c["at"] is None else str(c["at"]),
            ),
        )
        for c in raw["charges"]
    )
    index = _Index(
        escrow_id=escrow_id,
        first_ledger=int(raw["first_ledger"]),
        next_ledger=int(raw["next_ledger"]),
        charges=charges,
        seconds_per_ledger=float(raw["seconds_per_ledger"]),
        lossy_ledger=int(raw.get("lossy_ledger", 0)),
    )
    if index.next_ledger < index.first_ledger:
        return None
    payers = {str(a): str(p) for a, p in dict(raw["payers"]).items()}
    return index, payers


async def _restore(escrow_id: str) -> None:
    """Resume from the index a previous process stored, once per process."""
    if _held.restored:
        return
    _held.restored = True
    try:
        stored = await asyncio.wait_for(
            snapshot_store.get_snapshot_store().load(INDEX_NAME, snapshot_store.deployment_scope()),
            timeout=SAVE_TIMEOUT_SECONDS,
        )
        decoded = _decode(stored.body, escrow_id) if stored is not None else None
    except Exception as e:
        logger.warning("[charges] stored index not restored: %s: %s", type(e).__name__, e)
        return
    if decoded is None or _held.index is not None:
        return
    _held.index, payers = decoded
    _held.payers.update(payers)
    _held.saved = (_held.index.first_ledger, _held.index.next_ledger, len(_held.payers))
    logger.info(
        "[charges] resumed from the stored index: ledgers [%d, %d), %d charges",
        _held.index.first_ledger,
        _held.index.next_ledger,
        len(_held.index.charges),
    )


async def _save() -> None:
    index = _held.index
    if index is None:
        return
    mark = (index.first_ledger, index.next_ledger, len(_held.payers))
    if mark == _held.saved:
        return
    try:
        await asyncio.wait_for(
            snapshot_store.get_snapshot_store().save(
                INDEX_NAME, snapshot_store.deployment_scope(), time.time(), _encode(index, _held.payers)
            ),
            timeout=SAVE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning("[charges] index not persisted: %s: %s", type(e).__name__, e)
        return
    _held.saved = mark


# ── the answer ────────────────────────────────────────────────────────────
def _unavailable_for(owners: Mapping[str, str], reason: str, *, out_of_time: bool = False) -> WindowSettlements:
    return WindowSettlements(
        by_agent={a: settlement_svc._unavailable(a, reason) for a in owners},
        ledgers_scanned=0,
        ledgers_in_window=0,
        out_of_time=out_of_time,
        charges=0,
        unattributed=0,
    )


async def fetch_settlements(owners: Mapping[str, str], *, deadline: float) -> WindowSettlements:
    """Settlement evidence for every agent in `owners` (agent id → owner), from
    one scan of the escrow's window. Never raises; never runs past `deadline`
    by more than one in-flight read.

    Each agent's evidence is what `settlement_svc.fetch_settlement` would say
    of it, attributed with the same rule against the owner given here — which
    is the on-chain owner: the registry's `Agent.owner` is set at registration
    and has no setter.
    """
    escrow_id = settings.stellar_payment_escrow
    if not escrow_id:
        return _unavailable_for(owners, "escrow contract not configured")
    if not settings.stellar_agent_registry:
        return _unavailable_for(owners, "agent registry not configured")

    async with _lock:
        await _restore(escrow_id)
        held = _held.index if _held.index is not None and _held.index.escrow_id == escrow_id else None
        if held is None:
            _held.payers.clear()
        out_of_time = False
        in_window = 0
        reached_tip = False
        if time.monotonic() >= deadline:
            out_of_time = True
        else:
            try:
                advance = await asyncio.to_thread(_advance_sync, escrow_id, held, deadline)
            except Exception as e:
                logger.warning("[charges] the event window could not be read: %s: %s", type(e).__name__, e)
            else:
                _held.index = held = advance.index
                out_of_time = advance.out_of_time
                in_window = advance.ledgers_in_window
                reached_tip = held is not None and held.next_ledger > held.latest
        if held is None:
            reason = "out of time before the event scan" if out_of_time else "soroban rpc unreachable"
            return _unavailable_for(owners, reason, out_of_time=out_of_time)

        by_agent: dict[str, list[_Charge]] = {}
        for indexed in held.charges:
            if indexed.agent_id in owners:
                by_agent.setdefault(indexed.agent_id, []).append(indexed.charge)
        needed = {c.auth_id for charges in by_agent.values() for c in charges}
        if await _resolve_payers(escrow_id, needed, deadline):
            out_of_time = True
        await _save()
        payers = {a: _held.payers[a] for a in needed if a in _held.payers}

    settler = await settlement_svc._read_settler(escrow_id) if needed else None
    platform = settlement_svc._platform_keys()
    asset = await settlement_svc._read_asset(settings.stellar_asset_sac)
    truncated = not reached_tip or held.lossy
    window_days = round(held.scanned_ledgers * held.seconds_per_ledger / 86_400.0, 3)
    evidence: dict[str, SettlementEvidence] = {}
    for agent_id, owner in owners.items():
        entries, total, excluded = settlement_svc._build_entries(
            by_agent.get(agent_id, []), payers, owner, settler, platform
        )
        evidence[agent_id] = SettlementEvidence(
            agent_id=agent_id,
            asset=asset,
            window_days=window_days,
            scanned_ledgers=held.scanned_ledgers,
            entries=entries,
            total_stroops=total,
            self_payment_stroops=excluded,
            truncated=truncated,
            unavailable=None,
        )
    charges = sum(len(c) for c in by_agent.values())
    unattributed = sum(1 for cs in by_agent.values() for c in cs if c.auth_id not in payers)
    if truncated:
        logger.warning(
            "[charges] window read to ledger %d of %d (%s) — settlements are a floor",
            held.next_ledger - 1,
            held.latest,
            "out of time" if out_of_time else "lossy ledger" if held.lossy else "a read failed",
        )
    return WindowSettlements(
        by_agent=evidence,
        ledgers_scanned=held.scanned_ledgers,
        ledgers_in_window=in_window,
        out_of_time=out_of_time,
        charges=charges,
        unattributed=unattributed,
    )
