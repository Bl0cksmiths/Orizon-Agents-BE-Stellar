"""Settlement evidence — the tests that stop the operator dashboard lying.

The endpoint answers "has this agent actually been paid?", so what these tests
pin is not arithmetic but ATTRIBUTION and HONESTY ABOUT IGNORANCE:

  - a charge funded by a third party is revenue;
  - a charge funded by the agent's own owner, or by the platform's settler, is
    NOT — and is still reported, because hiding it is its own kind of lie;
  - a charge whose payer could not be read is excluded on the same principle,
    without taking the entries around it down with it;
  - a scan that could not run says so in `unavailable` and never returns a zero
    that reads as "this agent earned nothing";
  - `scanned_ledgers` is what was actually read, and a walk that stopped early
    admits it in `truncated`.

Hermetic: `sc._server` and `sc.simulate_read` are monkeypatched everywhere, and
nothing here touches the network. There is no pytest-asyncio, so async entry
points are driven with a bare `asyncio.run` per the repo idiom.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from stellar_sdk import scval
from stellar_sdk.soroban_rpc import EventInfo, GetEventsResponse

from app.config import settings
from app.services import settlement_svc as svc
from app.stellar import cache as rcache
from app.stellar import client as sc

AGENT_ID = "ext_agent"
ESCROW_ID = "CA" + "A" * 54
REGISTRY_ID = "CB" + "B" * 54
SAC_ID = "CC" + "C" * 54

OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
SETTLER = "GDUKMGUGDZQK6YHYA5Z6AY2G4XDSZPSZ3SW5UN3ARVMO6QSRDWP5YLEX"
BUYER = "GCEZWKCA5VLDNRLN3RPRJMRZOX3Z6G5CHCGSNFHEYVXM3XOJMDS674JZ"

# The stub's node history. Close times are always exactly 5 s apart whatever
# range a test asks for, because `window_days` is derived from the span the
# node reports rather than from a constant — so the stub has to report a
# self-consistent one or the days it produces mean nothing.
OLDEST_LEDGER = 1_000_000
LATEST_LEDGER = 1_000_100
OLDEST_CLOSE = 1_700_000_000
SECONDS_PER_LEDGER = 5

# Exactly what sc.simulate_read raises when the chain ANSWERED and the host
# function failed — `owner_of` panics on an id the registry does not hold.
CONTRACT_ERROR = RuntimeError("simulate failed: HostError: Error(Contract, #2)")


def _auth_id(n: int) -> bytes:
    return bytes([n]) * 16


def _job_id(n: int) -> bytes:
    return bytes([0x80 | n]) * 16


def _charged_event(ledger: int, auth_id: bytes, job_id: bytes, amount: int) -> EventInfo:
    """A real `charged` EventInfo, encoded exactly as PaymentEscrow emits it:
    topics `("charged", agent_id)`, data `(receipt_id, auth_id, amount, job_id)`
    — note the payload carries NO payer and NO owner, which is the whole reason
    this service has to resolve them itself."""
    value = scval.to_vec(
        [
            scval.to_bytes(b"\xee" * 16),
            scval.to_bytes(auth_id),
            scval.to_int128(amount),
            scval.to_bytes(job_id),
        ]
    )
    return EventInfo(
        type="contract",
        ledger=ledger,
        ledgerClosedAt="2026-09-16T08:57:00Z",
        contractId=ESCROW_ID,
        id=f"{ledger}-0",
        topic=[scval.to_symbol(svc.CHARGED_TOPIC).to_xdr(), scval.to_symbol(AGENT_ID).to_xdr()],
        value=value.to_xdr(),
        inSuccessfulContractCall=True,
        operationIndex=0,
        transactionIndex=0,
        txHash="ab" * 32,
    )


class _FakeRpc:
    """A SorobanServer stand-in that serves events out of a ledger range.

    Records the [start, end) of every page it is asked for, which is what lets
    the paging tests assert that `scanned_ledgers` reports the ledgers actually
    requested rather than the theoretical retention window.
    """

    def __init__(
        self,
        *,
        events: list[EventInfo] | None = None,
        latest: int = LATEST_LEDGER,
        oldest: int = OLDEST_LEDGER,
        page_errors: dict[int, BaseException] | None = None,
        latest_error: BaseException | None = None,
        page_delay: float = 0.0,
    ) -> None:
        self.events = events or []
        self.latest = latest
        self.oldest = oldest
        self.page_errors = page_errors or {}
        self.page_delay = page_delay
        self.latest_error = latest_error
        self.pages: list[tuple[int, int]] = []
        self.latest_close = OLDEST_CLOSE + (latest - oldest) * SECONDS_PER_LEDGER

    def get_latest_ledger(self) -> Any:
        if self.latest_error is not None:
            raise self.latest_error
        return SimpleNamespace(sequence=self.latest)

    def get_events(
        self,
        start_ledger: int | None = None,
        end_ledger: int | None = None,
        filters: Any = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> GetEventsResponse:
        if end_ledger is None:
            # The one-event probe: its job is to report the node's own retention
            # bounds, and its events are deliberately discarded by the scanner.
            return self._response([])
        self.pages.append((start_ledger or 0, end_ledger))
        if self.page_delay:
            time.sleep(self.page_delay)
        error = self.page_errors.get(len(self.pages))
        if error is not None:
            raise error
        hits = [e for e in self.events if start_ledger is not None and start_ledger <= e.ledger < end_ledger]
        return self._response(hits[:limit] if limit else hits)

    def _response(self, events: list[EventInfo]) -> GetEventsResponse:
        return GetEventsResponse(
            events=events,
            latestLedger=self.latest,
            oldestLedger=self.oldest,
            latestLedgerCloseTime=self.latest_close,
            oldestLedgerCloseTime=OLDEST_CLOSE,
            cursor="cursor",
        )


def _reader(
    *,
    owner: Any = OWNER,
    settler: Any = SETTLER,
    asset: Any = "native",
    payers: dict[str, str] | None = None,
    auth_errors: dict[str, BaseException] | None = None,
) -> Any:
    """A fake `sc.simulate_read` covering every contract read the service makes.

    Any value may be an exception instance, which is raised instead of
    returned; `auth_errors` fails the `authorization` read for specific auth
    ids (hex) while leaving the others readable.
    """
    payers = payers or {}
    auth_errors = auth_errors or {}

    def fake_read(contract_id: str, function_name: str, args: Any = None, source: Any = None) -> Any:
        if function_name == "list_ids":
            # Not the service's read — the 1.02 registry-sync loop's. Served
            # (as an empty registry) rather than refused, so the loop finds
            # nothing to do instead of retrying against the real network.
            return []
        if function_name == "owner_of":
            if isinstance(owner, BaseException):
                raise owner
            return owner
        if function_name == "settler":
            if isinstance(settler, BaseException):
                raise settler
            return settler
        if function_name == "name":
            if isinstance(asset, BaseException):
                raise asset
            return asset
        if function_name == "authorization":
            auth_hex = bytes(scval.to_native(args[0])).hex()
            if auth_hex in auth_errors:
                raise auth_errors[auth_hex]
            return {
                "payer": payers[auth_hex],
                "agent_id": AGENT_ID,
                "max_amount": 1_000_000,
                "spent": 1,
                "expires_at": 0,
                "revoked": False,
            }
        raise AssertionError(f"unexpected read: {function_name}")

    return fake_read


@pytest.fixture(autouse=True)
def configured(hermetic_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the service at fake contract ids and give it an empty cache.

    conftest blanks the registry id and never touches the escrow or SAC ids, so
    a gitignored .env could otherwise leave this suite reading real testnet
    contracts. All three are pinned here and restored afterwards.
    """
    rcache.clear()
    # Putting a registry id back re-arms the 1.02 sync loop, which every
    # TestClient starts from lifespan and which reads `list_ids` the instant it
    # does — before any test body has had a chance to patch anything. That read
    # went to the real testnet RPC. This fixture is autouse, so installing a
    # stand-in reader here puts it in place BEFORE the `client` fixture runs;
    # tests that need their own reader override it afterwards.
    monkeypatch.setattr(sc, "simulate_read", _reader())
    saved = (settings.stellar_payment_escrow, settings.stellar_asset_sac)
    settings.stellar_agent_registry = REGISTRY_ID
    settings.stellar_payment_escrow = ESCROW_ID
    settings.stellar_asset_sac = SAC_ID
    yield settings
    settings.stellar_payment_escrow, settings.stellar_asset_sac = saved
    rcache.clear()


def _run(
    monkeypatch: pytest.MonkeyPatch,
    rpc: _FakeRpc,
    reader: Any,
    agent_id: str = AGENT_ID,
) -> svc.SettlementEvidence:
    monkeypatch.setattr(sc, "_server", lambda **_kw: rpc)
    monkeypatch.setattr(sc, "simulate_read", reader)
    return asyncio.run(svc.fetch_settlement(agent_id))


def test_a_third_party_payment_is_counted_as_revenue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one case the dashboard may show as earnings: someone who is neither
    the agent's owner nor the platform funded the charge."""
    rpc = _FakeRpc(events=[_charged_event(1_000_050, _auth_id(1), _job_id(1), 120_000)])
    reader = _reader(payers={_auth_id(1).hex(): BUYER})

    result = _run(monkeypatch, rpc, reader)

    assert result.unavailable is None
    assert result.asset == "native"  # testnet's SAC wraps XLM — never USDC
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert (entry.payer, entry.self_payment) == (BUYER, False)
    assert (entry.auth_id, entry.job_id) == (_auth_id(1).hex(), _job_id(1).hex())
    assert (entry.amount_stroops, entry.ledger) == (120_000, 1_000_050)
    assert entry.at == "2026-09-16T08:57:00+00:00"
    assert (result.total_stroops, result.self_payment_stroops) == (120_000, 0)


def test_a_charge_the_owner_paid_is_excluded_but_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """`payer == owner_of(agent_id)` is the agent's owner moving their own
    money. It is not revenue, and it is not hidden either — the amount lands in
    `self_payment_stroops` so a reader can see what was taken out."""
    rpc = _FakeRpc(events=[_charged_event(1_000_050, _auth_id(1), _job_id(1), 500_000)])
    reader = _reader(payers={_auth_id(1).hex(): OWNER})

    result = _run(monkeypatch, rpc, reader)

    assert result.unavailable is None
    assert [(e.payer, e.self_payment) for e in result.entries] == [(OWNER, True)]
    assert (result.total_stroops, result.self_payment_stroops) == (0, 500_000)


def test_a_charge_the_settler_paid_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform paying itself — every `charged` event the deployed escrow
    has produced so far, because `charge` can only move the settler's funds.
    The settler is not the owner here, so only the settler comparison catches
    it."""
    rpc = _FakeRpc(events=[_charged_event(1_000_050, _auth_id(1), _job_id(1), 90_000)])
    reader = _reader(payers={_auth_id(1).hex(): SETTLER})

    result = _run(monkeypatch, rpc, reader)

    assert [(e.payer, e.self_payment) for e in result.entries] == [(SETTLER, True)]
    assert (result.total_stroops, result.self_payment_stroops) == (0, 90_000)


def test_zero_revenue_is_returned_as_zero_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window with nothing in it is a real answer: no entries, no totals, and
    `unavailable` left None so the client knows the chain WAS read. The span it
    was read over travels with it, which is what lets the frontend say "nothing
    in the last N days" instead of "never paid"."""
    rpc = _FakeRpc()
    result = _run(monkeypatch, rpc, _reader())

    assert (result.entries, result.total_stroops, result.self_payment_stroops) == ([], 0, 0)
    assert result.unavailable is None
    assert result.truncated is False
    assert result.scanned_ledgers == LATEST_LEDGER - OLDEST_LEDGER + 1
    assert result.window_days > 0


def test_an_unreachable_rpc_is_unavailable_and_never_a_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The distinction the whole endpoint turns on. A zero that came from a
    failed lookup reads on a dashboard as "this agent has earned nothing",
    which is a claim about the chain we did not make."""
    rpc = _FakeRpc(latest_error=ConnectionError("rpc unreachable"))

    result = _run(monkeypatch, rpc, _reader())

    assert result.unavailable == "soroban rpc unreachable"
    assert (result.entries, result.total_stroops, result.self_payment_stroops) == ([], 0, 0)
    assert result.scanned_ledgers == 0
    # The asset was readable, so it is still reported: an unavailable answer
    # need not throw away the facts it did establish.
    assert result.asset == "native"


def test_the_first_page_failing_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was scanned, so there is nothing to report — the same fact as an
    unreachable node, reached one round trip later."""
    rpc = _FakeRpc(page_errors={1: ConnectionError("rpc unreachable")})

    result = _run(monkeypatch, rpc, _reader())

    assert result.unavailable == "soroban rpc unreachable"
    assert result.entries == []


def test_an_unconfigured_escrow_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No escrow id means no events to read, which is a configuration fact and
    not an earnings fact."""
    settings.stellar_payment_escrow = ""
    result = _run(monkeypatch, _FakeRpc(), _reader())

    assert result.unavailable == "escrow contract not configured"
    assert result.total_stroops == 0


def test_an_unconfigured_registry_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the registry the owner cannot be resolved, so no charge could be
    told apart from a self-payment — refuse rather than guess."""
    settings.stellar_agent_registry = ""
    result = _run(monkeypatch, _FakeRpc(), _reader())

    assert result.unavailable == "agent registry not configured"


def test_an_unreadable_owner_is_unavailable_rather_than_unclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable chain is not the same statement as "the platform paid for
    all of it".
    Returning the charges with every one marked self_payment would be a
    different false statement, so the scan does not even run."""
    rpc = _FakeRpc(events=[_charged_event(1_000_050, _auth_id(1), _job_id(1), 120_000)])
    reader = _reader(owner=ConnectionError("rpc unreachable"))

    result = _run(monkeypatch, rpc, reader)

    assert result.unavailable == "agent owner unreadable"
    assert result.entries == []
    assert rpc.pages == []  # the expensive walk never started


def test_an_agent_that_is_not_registered_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chain answered and `owner_of` panicked: no such agent. A different
    fact from an unreadable chain, and it gets a different sentence."""
    result = _run(monkeypatch, _FakeRpc(), _reader(owner=CONTRACT_ERROR))

    assert result.unavailable == "agent not found in the registry"


def test_an_unreadable_asset_is_never_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testnet's SAC wraps XLM, so a default of "USDC" would relabel every
    amount on the dashboard. When the SAC cannot be read the unit says so."""
    rpc = _FakeRpc(events=[_charged_event(1_000_050, _auth_id(1), _job_id(1), 120_000)])
    reader = _reader(asset=ConnectionError("rpc unreachable"), payers={_auth_id(1).hex(): BUYER})

    result = _run(monkeypatch, rpc, reader)

    assert result.asset == "unknown"
    # The amounts are still true — only their label is unknown.
    assert result.total_stroops == 120_000


def test_the_window_is_paged_and_scanned_ledgers_reports_what_was_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window wider than one getEvents call is walked in explicit [start, end)
    pages, and `scanned_ledgers` is the sum of those ranges — never the
    theoretical retention window."""
    latest = OLDEST_LEDGER + 25_000
    rpc = _FakeRpc(
        latest=latest,
        events=[
            _charged_event(OLDEST_LEDGER + 5, _auth_id(1), _job_id(1), 10_000),
            _charged_event(OLDEST_LEDGER + 15_000, _auth_id(2), _job_id(2), 20_000),
            _charged_event(OLDEST_LEDGER + 24_999, _auth_id(3), _job_id(3), 30_000),
        ],
    )
    reader = _reader(payers={_auth_id(n).hex(): BUYER for n in (1, 2, 3)})

    result = _run(monkeypatch, rpc, reader)

    assert rpc.pages == [
        (OLDEST_LEDGER, OLDEST_LEDGER + 10_000),
        (OLDEST_LEDGER + 10_000, OLDEST_LEDGER + 20_000),
        (OLDEST_LEDGER + 20_000, latest + 1),
    ]
    # One entry per page, oldest first — the walk collects across pages rather
    # than stopping at the first that answered.
    assert [e.amount_stroops for e in result.entries] == [10_000, 20_000, 30_000]
    assert result.scanned_ledgers == 25_001
    assert result.truncated is False
    assert result.total_stroops == 60_000


def test_the_scan_never_reaches_past_the_retention_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node holding more history than RPC retention does not widen the claim:
    the walk starts one retention window back from the tip, and the whole
    window fits inside the page cap with room to spare."""
    latest = OLDEST_LEDGER + 500_000
    rpc = _FakeRpc(latest=latest)

    result = _run(monkeypatch, rpc, _reader())

    assert rpc.pages[0][0] == latest - svc.RETENTION_LEDGERS + 1
    assert rpc.pages[-1][1] == latest + 1
    assert len(rpc.pages) <= svc.MAX_PAGES
    assert result.scanned_ledgers == svc.RETENTION_LEDGERS
    assert result.truncated is False
    assert result.window_days == 7.0  # 120_960 ledgers × 5 s, measured not assumed


def test_the_page_cap_truncates_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping early is allowed; pretending the window was covered is not."""
    monkeypatch.setattr(svc, "MAX_PAGES", 2)
    rpc = _FakeRpc(latest=OLDEST_LEDGER + 25_000)

    result = _run(monkeypatch, rpc, _reader())

    assert len(rpc.pages) == 2
    assert result.truncated is True
    assert result.scanned_ledgers == 20_000  # what was read, not the 25_001 asked of it
    assert result.unavailable is None  # a scan DID happen — it just did not finish


def test_the_time_budget_truncates_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The walk runs on a shared worker thread, so it is bounded by wall clock
    as well as by page count — and says so when the clock is what stopped it."""
    monkeypatch.setattr(svc, "SCAN_BUDGET_SECONDS", 0.01)
    rpc = _FakeRpc(latest=OLDEST_LEDGER + 25_000, page_delay=0.05)

    result = _run(monkeypatch, rpc, _reader())

    assert len(rpc.pages) == 1
    assert result.truncated is True
    assert result.scanned_ledgers == 10_000


def test_a_saturated_page_is_reported_as_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page that fills its event limit may be a subset of the range it
    covers, so the entries cannot be presented as the complete set even though
    every ledger in the window was requested."""
    monkeypatch.setattr(svc, "PAGE_EVENT_LIMIT", 1)
    rpc = _FakeRpc(
        events=[
            _charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000),
            _charged_event(1_000_020, _auth_id(2), _job_id(2), 20_000),
        ]
    )
    reader = _reader(payers={_auth_id(n).hex(): BUYER for n in (1, 2)})

    result = _run(monkeypatch, rpc, reader)

    assert len(result.entries) == 1
    assert result.truncated is True
    assert result.scanned_ledgers == LATEST_LEDGER - OLDEST_LEDGER + 1


def test_a_later_page_failing_keeps_what_was_already_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries already decoded are real. Throwing them away to report a rounder
    number would be the same lie as inventing them, pointing the other way."""
    rpc = _FakeRpc(
        latest=OLDEST_LEDGER + 25_000,
        events=[_charged_event(OLDEST_LEDGER + 5, _auth_id(1), _job_id(1), 10_000)],
        page_errors={2: ConnectionError("rpc went away")},
    )
    reader = _reader(payers={_auth_id(1).hex(): BUYER})

    result = _run(monkeypatch, rpc, reader)

    assert result.unavailable is None
    assert result.truncated is True
    assert result.scanned_ledgers == 10_000
    assert result.total_stroops == 10_000


def test_one_unreadable_authorization_does_not_blank_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payer that could not be read costs its OWN entry its revenue and
    nothing else. The unreadable one is still listed, labelled "unknown" rather
    than given a plausible G-address, and its amount is reported among the
    exclusions so the gap is visible instead of silently missing."""
    rpc = _FakeRpc(
        events=[
            _charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000),
            _charged_event(1_000_020, _auth_id(2), _job_id(2), 20_000),
            _charged_event(1_000_030, _auth_id(3), _job_id(3), 30_000),
        ]
    )
    reader = _reader(
        payers={_auth_id(1).hex(): BUYER, _auth_id(3).hex(): BUYER},
        auth_errors={_auth_id(2).hex(): ConnectionError("rpc unreachable")},
    )

    result = _run(monkeypatch, rpc, reader)

    assert [(e.amount_stroops, e.payer, e.self_payment) for e in result.entries] == [
        (10_000, BUYER, False),
        (20_000, "unknown", True),
        (30_000, BUYER, False),
    ]
    assert (result.total_stroops, result.self_payment_stroops) == (40_000, 20_000)
    assert result.unavailable is None


def test_the_authorization_behind_several_charges_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charges deduplicate onto their authorization before the reads go out —
    the per-entry fan-out is the only part of this request that grows with
    on-chain history."""
    rpc = _FakeRpc(
        events=[
            _charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000),
            _charged_event(1_000_020, _auth_id(1), _job_id(2), 20_000),
        ]
    )
    inner = _reader(payers={_auth_id(1).hex(): BUYER})
    calls: list[str] = []

    def counting(contract_id: str, function_name: str, args: Any = None, source: Any = None) -> Any:
        calls.append(function_name)
        return inner(contract_id, function_name, args, source)

    result = _run(monkeypatch, rpc, counting)

    assert calls.count("authorization") == 1
    assert result.total_stroops == 30_000


def test_a_repeat_lookup_is_served_from_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full scan is a dozen-odd round trips and the dashboard polls, so the
    second look inside the TTL must not re-walk the window."""
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    monkeypatch.setattr(sc, "_server", lambda **_kw: rpc)
    monkeypatch.setattr(sc, "simulate_read", _reader(payers={_auth_id(1).hex(): BUYER}))

    async def twice() -> tuple[svc.SettlementEvidence, svc.SettlementEvidence]:
        return await svc.fetch_settlement(AGENT_ID), await svc.fetch_settlement(AGENT_ID)

    first, second = asyncio.run(twice())

    assert first == second
    assert len(rpc.pages) == 1


def test_the_route_returns_the_frozen_payload(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """The response shape the operator dashboard was built against. Every field
    is load-bearing — `window_days` and `unavailable` are what stop an empty
    list reading as "never paid" — so the key set is pinned here."""
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    monkeypatch.setattr(sc, "_server", lambda **_kw: rpc)
    monkeypatch.setattr(sc, "simulate_read", _reader(payers={_auth_id(1).hex(): BUYER}))

    response = client.get(f"/api/stellar/settlement/{AGENT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "agent_id",
        "asset",
        "window_days",
        "scanned_ledgers",
        "entries",
        "total_stroops",
        "self_payment_stroops",
        "truncated",
        "unavailable",
    }
    assert set(body["entries"][0]) == {
        "job_id",
        "auth_id",
        "amount_stroops",
        "ledger",
        "tx_hash",
        "at",
        "payer",
        "self_payment",
        "exclusion",
    }
    assert (body["agent_id"], body["total_stroops"], body["unavailable"]) == (AGENT_ID, 10_000, None)
    # Revenue carries no exclusion reason, which is the half of the contract a
    # client is most likely to get backwards.
    assert (body["entries"][0]["self_payment"], body["entries"][0]["exclusion"]) == (False, None)


def test_the_route_answers_200_when_the_chain_cannot_be_read(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Never a 5xx: a dashboard that gets an error back has an empty state
    indistinguishable from "this agent has never been paid", which is the one
    thing this endpoint exists to prevent it from saying."""
    monkeypatch.setattr(sc, "_server", lambda **_kw: _FakeRpc(latest_error=ConnectionError("down")))
    monkeypatch.setattr(sc, "simulate_read", _reader())

    response = client.get(f"/api/stellar/settlement/{AGENT_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["unavailable"] == "soroban rpc unreachable"
    assert (body["entries"], body["total_stroops"]) == ([], 0)


def _event_with_value(value_xdr: str) -> EventInfo:
    """A `charged` event carrying an arbitrary payload — for the cases where
    the deployed ABI no longer matches what this service decodes."""
    return EventInfo(
        type="contract",
        ledger=1_000_010,
        ledgerClosedAt="2026-09-16T08:57:00Z",
        contractId=ESCROW_ID,
        id="1000010-0",
        topic=[scval.to_symbol(svc.CHARGED_TOPIC).to_xdr(), scval.to_symbol(AGENT_ID).to_xdr()],
        value=value_xdr,
        inSuccessfulContractCall=True,
        operationIndex=0,
        transactionIndex=0,
        txHash="cd" * 32,
    )


@pytest.mark.parametrize(
    "value_xdr",
    [
        "not-valid-xdr-at-all",
        scval.to_vec([scval.to_bytes(b"\x01" * 16), scval.to_int128(5), scval.to_bytes(b"\x02" * 16)]).to_xdr(),
        scval.to_vec(
            [
                scval.to_bytes(b"\xee" * 16),
                scval.to_symbol("not_an_auth_id"),
                scval.to_int128(5),
                scval.to_bytes(b"\x02" * 16),
            ]
        ).to_xdr(),
    ],
    ids=["undecodable", "wrong_arity", "wrong_types"],
)
def test_a_charged_event_we_cannot_decode_is_skipped(monkeypatch: pytest.MonkeyPatch, value_xdr: str) -> None:
    """If the escrow's ABI moves under us, the amount inside an event we no
    longer understand is a number of unknown provenance. It is dropped, and the
    charges around it still report — half a decoded event must never reach an
    earnings figure."""
    rpc = _FakeRpc(
        events=[
            _event_with_value(value_xdr),
            _charged_event(1_000_020, _auth_id(1), _job_id(1), 10_000),
        ]
    )
    reader = _reader(payers={_auth_id(1).hex(): BUYER})

    result = _run(monkeypatch, rpc, reader)

    assert [e.amount_stroops for e in result.entries] == [10_000]
    assert result.total_stroops == 10_000


def test_an_unreadable_settler_costs_every_charge_its_revenue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the settler we cannot rule out that the platform funded a
    charge, and "we could not check" must not render as "verified". The entry
    keeps the payer that WAS established, so the exclusion is legible rather
    than mysterious."""
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    reader = _reader(settler=ConnectionError("rpc unreachable"), payers={_auth_id(1).hex(): BUYER})

    result = _run(monkeypatch, rpc, reader)

    assert [(e.payer, e.self_payment) for e in result.entries] == [(BUYER, True)]
    assert (result.total_stroops, result.self_payment_stroops) == (0, 10_000)


def test_a_payer_budget_timeout_excludes_every_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The authorization reads are the only fan-out that scales with on-chain
    history, so they run against a budget. Blowing it leaves every payer
    unknown — and an unknown payer is not revenue."""
    monkeypatch.setattr(svc, "PAYER_BUDGET_SECONDS", 0)
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    reader = _reader(payers={_auth_id(1).hex(): BUYER})

    result = _run(monkeypatch, rpc, reader)

    assert [(e.payer, e.self_payment) for e in result.entries] == [("unknown", True)]
    assert (result.total_stroops, result.self_payment_stroops) == (0, 10_000)


def test_an_unconfigured_sac_reports_an_unknown_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SAC id means nothing to ask what the amounts are denominated in.
    Saying so beats defaulting to a unit the deployment may not use."""
    settings.stellar_asset_sac = ""
    result = _run(monkeypatch, _FakeRpc(), _reader())

    assert result.asset == "unknown"
    assert result.unavailable is None


def test_a_degenerate_ledger_span_falls_back_to_the_default_close_time() -> None:
    """`window_days` is measured from the node's own retention span. A node
    reporting a span it cannot have measured (zero ledgers, or clocks that ran
    backwards) falls back to the nominal 5 s close time rather than producing
    an absurd number of days."""
    degenerate = GetEventsResponse(
        events=[],
        latestLedger=OLDEST_LEDGER,
        oldestLedger=OLDEST_LEDGER,
        latestLedgerCloseTime=OLDEST_CLOSE,
        oldestLedgerCloseTime=OLDEST_CLOSE,
        cursor="cursor",
    )

    assert svc._seconds_per_ledger(degenerate) == 5.0


@pytest.mark.parametrize(
    ("payer", "settler", "expected"),
    [
        (None, SETTLER, "payer_unreadable"),
        (OWNER, SETTLER, "owner"),
        (SETTLER, SETTLER, "settler"),
        (BUYER, None, "settler_unreadable"),
        (BUYER, SETTLER, None),
    ],
    ids=["payer_unreadable", "owner", "settler", "settler_unreadable", "revenue"],
)
def test_every_exclusion_names_the_rule_that_applied(
    monkeypatch: pytest.MonkeyPatch,
    payer: str | None,
    settler: str | None,
    expected: str | None,
) -> None:
    """The four exclusions are four different sentences to the operator reading
    them, and `self_payment` alone cannot tell them apart. `payer=None` here
    means the authorization read failed; `settler=None` means `settler()` did.
    """
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    reader = _reader(
        settler=ConnectionError("rpc unreachable") if settler is None else settler,
        payers={} if payer is None else {_auth_id(1).hex(): payer},
        auth_errors={_auth_id(1).hex(): ConnectionError("rpc unreachable")} if payer is None else None,
    )

    entry = _run(monkeypatch, rpc, reader).entries[0]

    assert entry.exclusion == expected
    assert entry.self_payment is (expected is not None)


def test_the_owner_rule_outranks_the_settler_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live case: `owner_of(orizon_batch)` IS the escrow's settler, so both
    rules match every historical charge and only precedence decides what the
    dashboard says. "Your own owner account funded this" is the more precise
    statement about THIS agent — and for an external operator, whose owner is
    their own wallet and not ours, it is the only true one."""
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    reader = _reader(owner=OWNER, settler=OWNER, payers={_auth_id(1).hex(): OWNER})

    entry = _run(monkeypatch, rpc, reader).entries[0]

    assert entry.exclusion == "owner"


def test_an_unreadable_payer_outranks_an_unreadable_settler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both lookups failed. "We could not read the payer" is the more specific
    fact — reporting "we could not rule out the platform" would describe a
    comparison that never ran, because there was no account to compare."""
    rpc = _FakeRpc(events=[_charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000)])
    reader = _reader(
        settler=ConnectionError("rpc unreachable"),
        auth_errors={_auth_id(1).hex(): ConnectionError("rpc unreachable")},
    )

    entry = _run(monkeypatch, rpc, reader).entries[0]

    assert (entry.exclusion, entry.payer) == ("payer_unreadable", "unknown")


def test_an_exclusion_is_absent_exactly_when_the_charge_is_revenue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant a client is entitled to rely on: `exclusion is None` if
    and only if `self_payment` is False, and `total_stroops` is the sum of
    exactly those entries."""
    rpc = _FakeRpc(
        events=[
            _charged_event(1_000_010, _auth_id(1), _job_id(1), 10_000),  # revenue
            _charged_event(1_000_020, _auth_id(2), _job_id(2), 20_000),  # owner
            _charged_event(1_000_030, _auth_id(3), _job_id(3), 30_000),  # settler
            _charged_event(1_000_040, _auth_id(4), _job_id(4), 40_000),  # payer unreadable
        ]
    )
    reader = _reader(
        payers={_auth_id(1).hex(): BUYER, _auth_id(2).hex(): OWNER, _auth_id(3).hex(): SETTLER},
        auth_errors={_auth_id(4).hex(): ConnectionError("rpc unreachable")},
    )

    result = _run(monkeypatch, rpc, reader)

    assert [e.exclusion for e in result.entries] == [None, "owner", "settler", "payer_unreadable"]
    for entry in result.entries:
        assert (entry.exclusion is None) is (entry.self_payment is False)
    assert result.total_stroops == sum(e.amount_stroops for e in result.entries if e.exclusion is None)
    assert result.self_payment_stroops == sum(e.amount_stroops for e in result.entries if e.exclusion is not None)


def test_the_exclusion_vocabulary_is_closed() -> None:
    """The frontend keys its copy off these four strings, so the set is part of
    the contract: adding a fifth rule without a sentence to go with it would
    render as a blank reason next to a withheld amount."""
    assert set(get_args(svc.Exclusion)) == {"payer_unreadable", "owner", "settler", "settler_unreadable"}
