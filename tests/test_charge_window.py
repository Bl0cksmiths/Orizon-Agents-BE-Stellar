"""One scan of the escrow's `charged` events, for every agent at once.

The adoption report used to call `settlement_svc.fetch_settlement` once per
external agent: an `owner_of` read, a ledger probe and thirteen getEvents
pages EACH. At 1,000 external agents that is ~15,000 RPC calls, and the build
never finished inside its budget (D-091 at scale). `charge_window` reads the
`("charged", *)` topic for the window ONCE and attributes each charge to its
agent, and keeps what it read, so the next build only reads the ledgers that
closed since.

Pinned here: the RPC cost does not grow with the number of agents; the
attribution is `settlement_svc`'s own rule (owner- and platform-funded charges
are never revenue); the window claimed is the window read; and every way the
scan can fall short — out of time, a page that failed, a range too dense to
read whole, a payer not yet read — is reported as such and resumed, never
dressed up as a complete zero.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from stellar_sdk import scval
from stellar_sdk.soroban_rpc import EventInfo, GetEventsResponse

from app.config import settings
from app.services import charge_window, settlement_svc, snapshot_store
from app.stellar import cache as rcache
from app.stellar import client as sc

ESCROW_ID = "CA" + "A" * 54
REGISTRY_ID = "CB" + "B" * 54
SAC_ID = "CC" + "C" * 54

OWNER_A = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
OWNER_B = "GCEZWKCA5VLDNRLN3RPRJMRZOX3Z6G5CHCGSNFHEYVXM3XOJMDS674JZ"
SETTLER = "GDUKMGUGDZQK6YHYA5Z6AY2G4XDSZPSZ3SW5UN3ARVMO6QSRDWP5YLEX"
BUYER = "GDJHP2I6NRCWYZTB3ZOXRE74V4M4EGXRYORGNPTGQ6BVNJNSSJO4PKXJ"

OLDEST = 1_000_000
LATEST = OLDEST + 120_000  # 120,001 ledgers held: 13 pages of 10,000
CLOSE = 1_790_000_000
SECONDS_PER_LEDGER = 5


def _auth(n: int) -> bytes:
    return bytes([n]) * 16


def _event(agent_id: str, ledger: int, auth: int, job: int, *, index: int = 0) -> EventInfo:
    value = scval.to_vec(
        [
            scval.to_bytes(b"\xee" * 16),
            scval.to_bytes(_auth(auth)),
            scval.to_int128(100_000),
            scval.to_bytes(bytes([0x80 | job]) * 16),
        ]
    )
    return EventInfo(
        type="contract",
        ledger=ledger,
        ledgerClosedAt="2026-10-05T08:57:00Z",
        contractId=ESCROW_ID,
        id=f"{ledger:019d}-{index:010d}",
        topic=[scval.to_symbol("charged").to_xdr(), scval.to_symbol(agent_id).to_xdr()],
        value=value.to_xdr(),
        inSuccessfulContractCall=True,
        operationIndex=0,
        transactionIndex=index,
        txHash=f"{ledger:032x}{index:032x}",
    )


class _Rpc:
    """getLatestLedger + getEvents over a list of events, recording each page."""

    def __init__(self, events: list[EventInfo] | None = None) -> None:
        self.events = events or []
        self.latest = LATEST
        self.oldest = OLDEST
        self.pages: list[tuple[int, int]] = []
        self.filters: list[Any] = []
        self.page_errors: dict[int, BaseException] = {}
        self.anchor_error: BaseException | None = None
        self.on_page: Any = None

    def get_latest_ledger(self) -> Any:
        if self.anchor_error is not None:
            raise self.anchor_error
        return SimpleNamespace(sequence=self.latest)

    def get_events(
        self,
        start_ledger: int | None = None,
        end_ledger: int | None = None,
        filters: Any = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> GetEventsResponse:
        self.filters.append(filters)
        if end_ledger is None:  # the retention probe
            return self._response([])
        assert start_ledger is not None and cursor is None
        self.pages.append((start_ledger, end_ledger))
        if self.on_page is not None:
            self.on_page(len(self.pages))
        error = self.page_errors.get(len(self.pages))
        if error is not None:
            raise error
        hits = sorted((e for e in self.events if start_ledger <= e.ledger < end_ledger), key=lambda e: e.id)
        return self._response(hits[:limit] if limit else hits)

    def _response(self, events: list[EventInfo]) -> GetEventsResponse:
        return GetEventsResponse(
            events=events,
            latestLedger=self.latest,
            oldestLedger=self.oldest,
            latestLedgerCloseTime=CLOSE + (self.latest - self.oldest) * SECONDS_PER_LEDGER,
            oldestLedgerCloseTime=CLOSE,
            cursor="",
        )


class _Views:
    """`sc.simulate_read` for the views the attribution reads."""

    def __init__(self) -> None:
        self.payers: dict[str, Any] = {}
        self.reads: list[str] = []

    def __call__(self, contract_id: str, function_name: str, args: Any = None, source: Any = None, **_kw: Any) -> Any:
        self.reads.append(function_name)
        if function_name == "authorization":
            payer = self.payers[bytes(scval.to_native(args[0])).hex()]
            if isinstance(payer, BaseException):
                raise payer
            return {"payer": payer}
        if function_name == "settler":
            return SETTLER
        if function_name == "name":
            return "native"
        raise AssertionError(f"unexpected read {function_name}")


@pytest.fixture()
def chain(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Rpc, _Views]]:
    rpc, views = _Rpc(), _Views()
    monkeypatch.setattr(settings, "stellar_payment_escrow", ESCROW_ID)
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC_ID)
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(sc, "_server", lambda **_kw: rpc)
    monkeypatch.setattr(sc, "simulate_read", views)
    monkeypatch.setattr(snapshot_store, "_store", snapshot_store.InMemorySnapshotStore())
    charge_window.reset()
    rcache.clear()
    yield rpc, views
    charge_window.reset()
    rcache.clear()


def _fetch(owners: dict[str, str], *, budget: float = 60.0) -> charge_window.WindowSettlements:
    rcache.clear()  # what a later build sees: only what charge_window itself kept
    return asyncio.run(charge_window.fetch_settlements(owners, deadline=time.monotonic() + budget))


def _revenue(window: charge_window.WindowSettlements, agent_id: str) -> list[str]:
    return [e.job_id for e in window.by_agent[agent_id].entries if not e.self_payment]


# ── one scan for everyone ───────────────────────────────────────────────────
def test_one_scan_answers_every_agent_and_costs_the_same_for_one_or_a_thousand(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_7", OLDEST + 50_000, auth=1, job=1), _event("ext_9", LATEST - 3, auth=2, job=2)]
    views.payers = {_auth(1).hex(): BUYER, _auth(2).hex(): BUYER}

    many = _fetch({f"ext_{i}": OWNER_A for i in range(1_000)})
    pages_for_many = len(rpc.filters)
    payer_reads = views.reads.count("authorization")
    charge_window.reset()
    snapshot_store._store = snapshot_store.InMemorySnapshotStore()  # a cold start, nothing stored
    rpc.filters.clear()
    _fetch({"ext_7": OWNER_A})

    assert len(rpc.filters) == pages_for_many == 14  # the probe and 13 pages, whatever the agent count
    assert len(many.by_agent) == 1_000
    assert _revenue(many, "ext_7") == [(bytes([0x81]) * 16).hex()]
    assert _revenue(many, "ext_9") == [(bytes([0x82]) * 16).hex()]
    quiet = many.by_agent["ext_3"]
    assert (quiet.entries, quiet.truncated, quiet.unavailable) == ([], False, None)
    assert quiet.scanned_ledgers == 120_001
    assert quiet.window_days == pytest.approx(120_001 * SECONDS_PER_LEDGER / 86_400, abs=1e-3)
    assert many.complete is True
    assert payer_reads == 2  # only the charges, never per agent


def test_the_scan_filters_on_the_topic_wildcard_server_side(chain: Any) -> None:
    rpc, _views = chain

    _fetch({"ext_a": OWNER_A})

    (topic,) = rpc.filters[-1][0].topics
    assert topic == [scval.to_symbol("charged").to_xdr(), "*"]
    assert rpc.filters[-1][0].contract_ids == [ESCROW_ID]


def test_attribution_is_settlement_svcs_own_rule(chain: Any) -> None:
    """Owner-funded and settler-funded charges are reported, never revenue."""
    rpc, views = chain
    rpc.events = [
        _event("ext_a", LATEST - 30, auth=1, job=1),
        _event("ext_a", LATEST - 20, auth=2, job=2),
        _event("ext_a", LATEST - 10, auth=3, job=3),
    ]
    views.payers = {_auth(1).hex(): OWNER_A, _auth(2).hex(): SETTLER, _auth(3).hex(): BUYER}

    evidence = _fetch({"ext_a": OWNER_A}).by_agent["ext_a"]

    assert [(e.payer, e.exclusion) for e in evidence.entries] == [
        (OWNER_A, "owner"),
        (SETTLER, "settler"),
        (BUYER, None),
    ]
    assert evidence.total_stroops == 100_000


def test_an_agent_not_asked_about_costs_no_payer_read(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("team_agent", LATEST - 5, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER}

    window = _fetch({"ext_a": OWNER_A})

    assert set(window.by_agent) == {"ext_a"}
    assert "authorization" not in views.reads


# ── incremental ─────────────────────────────────────────────────────────────
def test_the_next_build_reads_only_the_ledgers_closed_since(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", OLDEST + 1_000, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER, _auth(2).hex(): BUYER}
    _fetch({"ext_a": OWNER_A})
    rpc.pages.clear()
    views.reads.clear()

    rpc.latest += 60
    rpc.oldest += 60
    rpc.events.append(_event("ext_a", LATEST + 30, auth=2, job=2))
    again = _fetch({"ext_a": OWNER_A})

    assert rpc.pages == [(LATEST + 1, LATEST + 61)]
    assert len(_revenue(again, "ext_a")) == 2
    assert views.reads.count("authorization") == 1  # the old payer is kept, never re-read
    assert again.by_agent["ext_a"].scanned_ledgers == 120_001
    assert again.complete is True


def test_a_charge_that_ages_out_of_the_window_is_dropped(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", OLDEST + 10, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER}
    assert len(_revenue(_fetch({"ext_a": OWNER_A}), "ext_a")) == 1

    rpc.latest += 100
    rpc.oldest += 100
    later = _fetch({"ext_a": OWNER_A})

    assert later.by_agent["ext_a"].entries == []


def test_a_held_index_from_before_the_window_is_rescanned_from_scratch(chain: Any) -> None:
    rpc, _views = chain
    _fetch({"ext_a": OWNER_A})
    rpc.latest += 500_000
    rpc.oldest += 500_000
    rpc.pages.clear()

    again = _fetch({"ext_a": OWNER_A})

    assert rpc.pages[0][0] == rpc.oldest
    assert again.complete is True


# ── honest about falling short ──────────────────────────────────────────────
def test_a_full_page_is_split_until_every_charge_is_read(chain: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rpc, views = chain
    monkeypatch.setattr(charge_window, "PAGE_EVENT_LIMIT", 2)
    rpc.events = [_event("ext_a", LATEST - 100 + i, auth=i + 1, job=i + 1) for i in range(5)]
    views.payers = {_auth(i + 1).hex(): BUYER for i in range(5)}

    window = _fetch({"ext_a": OWNER_A})

    assert len(_revenue(window, "ext_a")) == 5
    assert window.complete is True and window.by_agent["ext_a"].truncated is False


def test_one_ledger_denser_than_a_page_is_reported_truncated(chain: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rpc, views = chain
    monkeypatch.setattr(charge_window, "PAGE_EVENT_LIMIT", 2)
    rpc.events = [_event("ext_a", LATEST - 7, auth=i + 1, job=i + 1, index=i) for i in range(3)]
    views.payers = {_auth(i + 1).hex(): BUYER for i in range(3)}

    window = _fetch({"ext_a": OWNER_A})

    assert window.by_agent["ext_a"].truncated is True
    assert window.complete is False


def test_out_of_time_mid_walk_keeps_what_it_read_and_the_next_build_resumes(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", OLDEST + 5, auth=1, job=1), _event("ext_a", LATEST - 5, auth=2, job=2)]
    views.payers = {_auth(1).hex(): BUYER, _auth(2).hex(): BUYER}
    rpc.on_page = lambda n: time.sleep(0.05) if n == 4 else None

    first = _fetch({"ext_a": OWNER_A}, budget=0.04)

    assert first.out_of_time is True and first.complete is False
    evidence = first.by_agent["ext_a"]
    assert evidence.truncated is True
    assert evidence.scanned_ledgers == 40_000
    # The charge it reached is kept; its payer waits for the next build, and
    # until then it is not revenue.
    assert [e.exclusion for e in evidence.entries] == ["payer_unreadable"]
    assert first.unattributed == 1

    rpc.on_page = None
    rpc.pages.clear()
    second = _fetch({"ext_a": OWNER_A})

    assert rpc.pages[0] == (OLDEST + 40_000, OLDEST + 50_000)
    assert second.complete is True
    assert len(_revenue(second, "ext_a")) == 2


def test_a_page_that_fails_is_resumed_from_not_restarted(chain: Any) -> None:
    rpc, _views = chain
    rpc.page_errors = {3: ConnectionError("rpc down")}

    first = _fetch({"ext_a": OWNER_A})

    assert first.complete is False and first.out_of_time is False
    assert first.by_agent["ext_a"].truncated is True
    assert first.by_agent["ext_a"].scanned_ledgers == 20_000
    rpc.page_errors = {}
    rpc.pages.clear()
    assert _fetch({"ext_a": OWNER_A}).complete is True
    assert rpc.pages[0][0] == OLDEST + 20_000


def test_no_anchor_and_nothing_held_is_unavailable_never_zero(chain: Any) -> None:
    rpc, _views = chain
    rpc.anchor_error = ConnectionError("rpc down")

    window = _fetch({"ext_a": OWNER_A})

    evidence = window.by_agent["ext_a"]
    assert evidence.unavailable == "soroban rpc unreachable"
    assert (evidence.entries, evidence.scanned_ledgers) == ([], 0)
    assert window.complete is False


def test_no_anchor_with_an_index_held_serves_it_as_truncated(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", LATEST - 5, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER}
    _fetch({"ext_a": OWNER_A})
    rpc.anchor_error = ConnectionError("rpc down")

    window = _fetch({"ext_a": OWNER_A})

    evidence = window.by_agent["ext_a"]
    assert evidence.unavailable is None and evidence.truncated is True
    assert len(_revenue(window, "ext_a")) == 1
    assert window.complete is False


def test_a_payer_not_read_is_not_revenue_and_is_retried(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", LATEST - 5, auth=1, job=1)]
    views.payers = {_auth(1).hex(): ConnectionError("rpc down")}

    first = _fetch({"ext_a": OWNER_A})

    assert [e.exclusion for e in first.by_agent["ext_a"].entries] == ["payer_unreadable"]
    assert (first.charges, first.unattributed) == (1, 1)
    assert first.complete is False
    views.payers = {_auth(1).hex(): BUYER}
    second = _fetch({"ext_a": OWNER_A})
    assert len(_revenue(second, "ext_a")) == 1
    assert (second.charges, second.unattributed, second.complete) == (1, 0, True)


def test_nothing_read_before_the_deadline_is_out_of_time_never_zero(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", LATEST - 5, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER}

    window = asyncio.run(charge_window.fetch_settlements({"ext_a": OWNER_A}, deadline=time.monotonic() - 1))

    assert window.out_of_time is True and window.complete is False
    assert window.by_agent["ext_a"].unavailable is not None
    assert rpc.pages == [] and "authorization" not in views.reads


@pytest.mark.parametrize("unset", ["stellar_payment_escrow", "stellar_agent_registry"])
def test_an_unconfigured_contract_is_unavailable(chain: Any, monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    monkeypatch.setattr(settings, unset, "")

    window = _fetch({"ext_a": OWNER_A})

    assert window.by_agent["ext_a"].unavailable is not None
    assert window.complete is False


# ── a restart resumes ───────────────────────────────────────────────────────
def test_a_restart_resumes_from_the_persisted_index(chain: Any) -> None:
    rpc, views = chain
    rpc.events = [_event("ext_a", OLDEST + 5, auth=1, job=1)]
    views.payers = {_auth(1).hex(): BUYER}
    _fetch({"ext_a": OWNER_A})
    stored = asyncio.run(snapshot_store._store.load(charge_window.INDEX_NAME, snapshot_store.deployment_scope()))
    assert stored is not None and json.loads(stored.body)["next_ledger"] == LATEST + 1

    charge_window.reset()  # a new process: nothing in memory
    rpc.latest += 10
    rpc.pages.clear()
    views.reads.clear()
    window = _fetch({"ext_a": OWNER_A})

    assert rpc.pages == [(LATEST + 1, LATEST + 11)]
    assert "authorization" not in views.reads  # the payer came back with the index
    assert len(_revenue(window, "ext_a")) == 1
    assert window.complete is True


def test_a_stored_index_for_another_escrow_is_ignored(chain: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rpc, _views = chain
    _fetch({"ext_a": OWNER_A})
    charge_window.reset()
    monkeypatch.setattr(settings, "stellar_payment_escrow", "CD" + "D" * 54)
    monkeypatch.setattr(snapshot_store, "deployment_scope", lambda: "same-scope-by-mistake")
    asyncio.run(
        snapshot_store._store.save(
            charge_window.INDEX_NAME,
            "same-scope-by-mistake",
            time.time(),
            json.dumps(
                {
                    "v": 1,
                    "escrow_id": ESCROW_ID,  # not the escrow now configured
                    "first_ledger": OLDEST,
                    "next_ledger": LATEST + 1,
                    "seconds_per_ledger": 5.0,
                    "lossy_ledger": 0,
                    "charges": [],
                    "payers": {},
                }
            ).encode(),
        )
    )
    rpc.pages.clear()

    _fetch({"ext_a": OWNER_A})

    assert rpc.pages[0][0] == OLDEST  # scanned from scratch


def test_a_stored_index_that_does_not_decode_is_ignored(chain: Any) -> None:
    rpc, _views = chain
    asyncio.run(
        snapshot_store._store.save(charge_window.INDEX_NAME, snapshot_store.deployment_scope(), time.time(), b"{nope")
    )
    window = _fetch({"ext_a": OWNER_A})

    assert window.complete is True
    assert rpc.pages[0][0] == OLDEST


def test_the_window_scan_is_not_the_per_agent_scan(chain: Any) -> None:
    """The operator dashboard's per-agent read is untouched by this module."""
    assert settlement_svc.fetch_settlement is not charge_window.fetch_settlements
