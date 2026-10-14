"""Edges of the charge-window scan the main suite leaves implicit, pinned before it is split up.

`_advance_sync` driven directly over a scripted RPC: a dense ledger whose rest
of range fails or is empty, the deadline passing while a full page is split, a
page that fails twice, and the events a walk skips (no agent, no decodable
charge, already held). Then `fetch_settlements`' own answers: the exact
unavailable reasons, the three truncation lines, the settler read only when a
charge needs it, and the totals it reports. Nothing reaches testnet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from stellar_sdk import scval
from test_charge_window import (
    BUYER,
    ESCROW_ID,
    LATEST,
    OLDEST,
    OWNER_A,
    REGISTRY_ID,
    SAC_ID,
    SECONDS_PER_LEDGER,
    _auth,
    _event,
    _Rpc,
    _Views,
)

from app.config import settings
from app.services import charge_window, settlement_svc, snapshot_store
from app.stellar import cache as rcache
from app.stellar import client as sc

LOGGER = "app.services.charge_window"
IN_WINDOW = LATEST - OLDEST + 1


class Clock:
    """`time.monotonic` for charge_window, moved only when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


class Rpc(_Rpc):
    """The main suite's RPC, plus pages that fail by start ledger and a hook on each page."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_from: dict[int, int] = {}  # start ledger → failures left
        self.before_page: Any = None

    def get_events(self, start_ledger: int | None = None, end_ledger: int | None = None, **kw: Any) -> Any:
        if end_ledger is not None and start_ledger is not None:
            if self.before_page is not None:
                self.before_page(start_ledger, end_ledger)
            if self.fail_from.get(start_ledger, 0) > 0:
                self.fail_from[start_ledger] -= 1
                self.pages.append((start_ledger, end_ledger))
                raise ConnectionError("rpc down")
        return super().get_events(start_ledger=start_ledger, end_ledger=end_ledger, **kw)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(charge_window, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


@pytest.fixture()
def rpc(monkeypatch: pytest.MonkeyPatch) -> Rpc:
    fake = Rpc()
    monkeypatch.setattr(sc, "_server", lambda **_kw: fake)
    return fake


@pytest.fixture()
def views(monkeypatch: pytest.MonkeyPatch, rpc: Rpc) -> Iterator[_Views]:
    fake = _Views()
    monkeypatch.setattr(settings, "stellar_payment_escrow", ESCROW_ID)
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC_ID)
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(sc, "simulate_read", fake)
    monkeypatch.setattr(snapshot_store, "_store", snapshot_store.InMemorySnapshotStore())
    charge_window.reset()
    rcache.clear()
    yield fake
    charge_window.reset()
    rcache.clear()


def _advance(held: Any = None, deadline: float = 2_000.0) -> charge_window._Advance:
    return charge_window._advance_sync(ESCROW_ID, held, deadline)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


# ── _advance_sync ───────────────────────────────────────────────────────────


def test_a_fresh_walk_anchors_the_window_and_reads_to_the_tip(clock: Clock, rpc: Rpc) -> None:
    rpc.events = [_event("ext_a", OLDEST + 5, auth=1, job=1)]

    advance = _advance()

    assert (advance.out_of_time, advance.ledgers_in_window) == (False, IN_WINDOW)
    index = advance.index
    assert index is not None
    assert (index.escrow_id, index.first_ledger, index.next_ledger, index.latest) == (
        ESCROW_ID,
        OLDEST,
        LATEST + 1,
        LATEST,
    )
    assert (index.lossy_ledger, index.seconds_per_ledger) == (0, SECONDS_PER_LEDGER)
    assert [(c.agent_id, c.charge.ledger) for c in index.charges] == [("ext_a", OLDEST + 5)]


def test_a_dense_ledger_whose_rest_fails_resumes_just_past_it(
    clock: Clock, rpc: Rpc, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(charge_window, "PAGE_EVENT_LIMIT", 2)
    dense = OLDEST + 100
    rpc.events = [_event("ext_a", dense, auth=i + 1, job=i + 1, index=i) for i in range(3)]
    rpc.events.append(_event("ext_a", dense + 50, auth=9, job=9))
    rpc.fail_from = {dense + 1: 2}

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        advance = _advance()

    index = advance.index
    assert index is not None
    assert index.lossy_ledger == dense and index.next_ledger == LATEST + 1
    assert [c.charge.ledger for c in index.charges] == [dense, dense, dense + 50]
    assert rpc.pages.count((dense + 1, OLDEST + 10_000)) == 2  # asked twice, then given up
    assert (dense + 1, dense + 1 + 10_000) in rpc.pages  # the walk resumes past the dense ledger
    assert _warnings(caplog) == [
        f"[charges] ledger {dense} holds more than 2 charges; the charges read from it are a subset",
        f"[charges] page [{OLDEST}, {OLDEST + 10_000}) failed after a dense ledger: rpc down",
    ]


def test_a_dense_ledger_that_ends_its_page_reads_nothing_more_of_it(
    clock: Clock, rpc: Rpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(charge_window, "PAGE_EVENT_LIMIT", 2)
    dense = OLDEST + 9_999
    rpc.events = [_event("ext_a", dense, auth=i + 1, job=i + 1, index=i) for i in range(3)]

    advance = _advance()

    assert advance.index is not None and advance.index.lossy_ledger == dense
    assert not any(start == dense + 1 and end == OLDEST + 10_000 for start, end in rpc.pages)
    assert (OLDEST + 10_000, OLDEST + 20_000) in rpc.pages


def test_the_deadline_passing_while_a_full_page_splits_stops_where_it_was(
    clock: Clock, rpc: Rpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(charge_window, "PAGE_EVENT_LIMIT", 2)
    rpc.events = [_event("ext_a", OLDEST + 10 + i, auth=i + 1, job=i + 1) for i in range(3)]

    def late(start: int, end: int) -> None:
        clock.now = 5_000.0

    rpc.before_page = late

    advance = _advance(deadline=2_000.0)

    assert advance.out_of_time is True
    assert advance.index is not None
    assert (advance.index.next_ledger, advance.index.charges) == (OLDEST, ())
    assert rpc.pages == [(OLDEST, OLDEST + 10_000)]


def test_a_walk_past_its_deadline_reads_no_page(clock: Clock, rpc: Rpc) -> None:
    advance = _advance(deadline=clock.now)

    assert advance.out_of_time is True
    assert advance.index is not None and advance.index.next_ledger == OLDEST
    assert rpc.pages == []


def test_a_page_that_fails_twice_ends_the_walk_there(clock: Clock, rpc: Rpc, caplog: pytest.LogCaptureFixture) -> None:
    rpc.fail_from = {OLDEST + 20_000: 2}

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        advance = _advance()

    assert advance.out_of_time is False
    assert advance.index is not None and advance.index.next_ledger == OLDEST + 20_000
    assert _warnings(caplog) == [
        f"[charges] page [{OLDEST + 20_000}, {OLDEST + 30_000}) failed: ConnectionError: rpc down"
    ]


def test_events_with_no_agent_no_charge_or_already_held_are_skipped(clock: Clock, rpc: Rpc) -> None:
    kept = _event("ext_a", OLDEST + 5, auth=1, job=1)
    no_agent = _event("ext_b", OLDEST + 6, auth=2, job=2)
    no_agent.topic = no_agent.topic[:1]
    not_a_symbol = _event("ext_c", OLDEST + 7, auth=3, job=3)
    not_a_symbol.topic = [not_a_symbol.topic[0], scval.to_uint32(7).to_xdr()]
    no_charge = _event("ext_d", OLDEST + 8, auth=4, job=4)
    no_charge.value = scval.to_uint32(1).to_xdr()
    fresh = _event("ext_e", OLDEST + 9, auth=5, job=5)
    rpc.events = [kept, no_agent, not_a_symbol, no_charge, fresh]
    held = charge_window._Index(
        ESCROW_ID,
        first_ledger=OLDEST,
        next_ledger=OLDEST,  # the held charge is read again by this walk
        charges=(charge_window._Indexed("ext_a", kept.id, settlement_svc._decode_charged(kept)),),
        seconds_per_ledger=1.0,
    )

    advance = _advance(held)

    assert advance.index is not None
    assert [(c.agent_id, c.event_id) for c in advance.index.charges] == [("ext_a", kept.id), ("ext_e", fresh.id)]


def test_a_held_index_for_another_escrow_is_not_resumed(clock: Clock, rpc: Rpc) -> None:
    held = charge_window._Index("C" + "Z" * 55, OLDEST, LATEST + 1, (), 1.0)

    advance = _advance(held)

    assert advance.index is not None and advance.index.escrow_id == ESCROW_ID
    assert rpc.pages[0] == (OLDEST, OLDEST + 10_000)


# ── fetch_settlements ───────────────────────────────────────────────────────


def _fetch(owners: dict[str, str], *, budget: float = 60.0) -> charge_window.WindowSettlements:
    rcache.clear()
    return asyncio.run(charge_window.fetch_settlements(owners, deadline=time.monotonic() + budget))


@pytest.mark.parametrize(
    ("unset", "reason"),
    [
        ("stellar_payment_escrow", "escrow contract not configured"),
        ("stellar_agent_registry", "agent registry not configured"),
    ],
)
def test_an_unconfigured_contract_is_unavailable_with_its_reason(
    views: _Views, monkeypatch: pytest.MonkeyPatch, unset: str, reason: str
) -> None:
    monkeypatch.setattr(settings, unset, "")
    window = _fetch({"ext_a": OWNER_A, "ext_b": OWNER_A})
    assert {a: e.unavailable for a, e in window.by_agent.items()} == {"ext_a": reason, "ext_b": reason}
    assert (window.ledgers_scanned, window.ledgers_in_window, window.out_of_time) == (0, 0, False)
    assert (window.charges, window.unattributed) == (0, 0)


def test_a_build_already_out_of_time_says_so_as_its_reason(views: _Views, rpc: Rpc) -> None:
    window = _fetch({"ext_a": OWNER_A}, budget=-1)
    assert window.by_agent["ext_a"].unavailable == "out of time before the event scan"
    assert window.out_of_time is True


def test_a_walk_that_returns_no_index_is_unreachable(views: _Views, rpc: Rpc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        charge_window,
        "_advance_sync",
        lambda *_a: charge_window._Advance(index=None, out_of_time=False, ledgers_in_window=9),
    )
    window = _fetch({"ext_a": OWNER_A})
    assert window.by_agent["ext_a"].unavailable == "soroban rpc unreachable"
    assert window.out_of_time is False
    assert charge_window._held.index is None


def test_no_charge_to_the_agents_asked_about_reads_no_settler(views: _Views, rpc: Rpc) -> None:
    rpc.events = [_event("someone_else", LATEST - 5, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER}

    window = _fetch({"ext_a": OWNER_A})

    assert "settler" not in views.reads and "authorization" not in views.reads
    assert (window.charges, window.unattributed, window.ledgers_in_window) == (0, 0, IN_WINDOW)
    assert window.complete is True


def test_the_totals_count_charges_and_those_with_no_payer(views: _Views, rpc: Rpc) -> None:
    rpc.events = [_event("ext_a", LATEST - 5, auth=1, job=1), _event("ext_b", LATEST - 4, auth=2, job=2)]
    views.payers = {_auth(1).hex(): BUYER, _auth(2).hex(): ConnectionError("rpc down")}

    window = _fetch({"ext_a": OWNER_A, "ext_b": OWNER_A})

    assert "settler" in views.reads
    assert (window.charges, window.unattributed, window.ledgers_scanned) == (2, 1, IN_WINDOW)
    evidence = window.by_agent["ext_a"]
    assert evidence.window_days == round(IN_WINDOW * SECONDS_PER_LEDGER / 86_400.0, 3)
    assert evidence.truncated is False


def test_a_lossy_window_is_logged_as_a_lossy_ledger(
    views: _Views, rpc: Rpc, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(charge_window, "PAGE_EVENT_LIMIT", 2)
    rpc.events = [_event("ext_a", LATEST - 7, auth=i + 1, job=i + 1, index=i) for i in range(3)]
    views.payers = {_auth(i + 1).hex(): BUYER for i in range(3)}

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        window = _fetch({"ext_a": OWNER_A})

    assert window.by_agent["ext_a"].truncated is True
    assert _warnings(caplog)[-1] == (
        f"[charges] window read to ledger {LATEST} of {LATEST} (lossy ledger) — settlements are a floor"
    )


def test_a_window_short_of_the_tip_is_logged_as_a_failed_read(
    views: _Views, rpc: Rpc, caplog: pytest.LogCaptureFixture
) -> None:
    rpc.fail_from = {OLDEST + 20_000: 2}

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        window = _fetch({"ext_a": OWNER_A})

    assert window.by_agent["ext_a"].truncated is True and window.out_of_time is False
    assert _warnings(caplog)[-1] == (
        f"[charges] window read to ledger {OLDEST + 19_999} of {LATEST} (a read failed) — settlements are a floor"
    )


def test_a_window_cut_short_by_the_deadline_is_logged_as_out_of_time(
    views: _Views, rpc: Rpc, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def short(escrow_id: str, held: Any, deadline: float) -> charge_window._Advance:
        index = charge_window._Index(escrow_id, OLDEST, OLDEST + 10, (), 5.0, latest=LATEST)
        return charge_window._Advance(index=index, out_of_time=True, ledgers_in_window=IN_WINDOW)

    monkeypatch.setattr(charge_window, "_advance_sync", short)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        window = _fetch({"ext_a": OWNER_A})

    assert window.out_of_time is True and window.ledgers_scanned == 10
    assert _warnings(caplog)[-1] == (
        f"[charges] window read to ledger {OLDEST + 9} of {LATEST} (out of time) — settlements are a floor"
    )
