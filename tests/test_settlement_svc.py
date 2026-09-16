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
from types import SimpleNamespace
from typing import Any

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

# A 101-ledger node history whose close times are exactly 5 s apart, so
# `window_days` comes out of measured data rather than a constant.
OLDEST_LEDGER = 1_000_000
LATEST_LEDGER = 1_000_100
OLDEST_CLOSE = 1_700_000_000
LATEST_CLOSE = OLDEST_CLOSE + (LATEST_LEDGER - OLDEST_LEDGER) * 5

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
    ) -> None:
        self.events = events or []
        self.latest = latest
        self.oldest = oldest
        self.page_errors = page_errors or {}
        self.latest_error = latest_error
        self.pages: list[tuple[int, int]] = []

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
            latestLedgerCloseTime=LATEST_CLOSE,
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
def configured(hermetic_settings: Any) -> Any:
    """Point the service at fake contract ids and give it an empty cache.

    conftest blanks the registry id and never touches the escrow or SAC ids, so
    a gitignored .env could otherwise leave this suite reading real testnet
    contracts. All three are pinned here and restored afterwards.
    """
    rcache.clear()
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
