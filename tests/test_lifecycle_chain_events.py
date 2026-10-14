"""Characterization of `ChainReader.events`: its getEvents paging and how each raw event is read.

The hermetic world answers every scan in one page, so the paging rules are
pinned here against a scripted RPC: what each request carries, where the next
cursor comes from, when the scan stops, and how one raw event becomes a
`ChainEvent`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from stellar_sdk import scval

from scripts.lifecycle.chain import _EVENT_PAGE, _EVENT_PAGES, ChainEvent, ChainReader
from scripts.lifecycle.config import TESTNET_PASSPHRASE
from scripts.lifecycle.retry import RetryPolicy

RPC = "https://rpc.events.test"
CONTRACT = "CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI"
RATED = str(scval.to_symbol("rated").to_xdr())
AGENT = str(scval.to_symbol("ext_agent").to_xdr())
SEVEN = str(scval.to_uint32(7).to_xdr())


def _raw(n: int, tx: str = "aa", **overrides: Any) -> dict[str, Any]:
    event = {
        "contractId": CONTRACT,
        "txHash": tx,
        "ledger": 100 + n,
        "topic": [RATED, AGENT],
        "value": SEVEN,
        "inSuccessfulContractCall": True,
        "pagingToken": f"tok-{n}",
        "id": f"id-{n}",
    }
    event.update(overrides)
    return event


def _reader(pages: list[dict[str, Any]]) -> tuple[ChainReader, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    queue = list(pages)

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload["params"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": queue.pop(0)})

    reader = ChainReader(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        rpc_url=RPC,
        horizon_url="https://horizon.events.test",
        passphrase=TESTNET_PASSPHRASE,
        retry=RetryPolicy(attempts=1, sleep=lambda _s: None),
    )
    return reader, sent


FILTERS = [{"type": "contract", "contractIds": [CONTRACT]}]


def test_one_short_page_is_one_request_from_the_start_ledger() -> None:
    reader, sent = _reader([{"events": [_raw(1)], "cursor": "c1"}])
    events = reader.events(CONTRACT, 50)
    assert sent == [{"startLedger": 50, "filters": FILTERS, "pagination": {"limit": _EVENT_PAGE}}]
    assert events == [
        ChainEvent(
            contract_id=CONTRACT,
            tx_hash="aa",
            ledger=101,
            topics=["rated", "ext_agent"],
            value=7,
            successful=True,
        )
    ]


def test_a_full_page_pages_on_by_the_answers_cursor() -> None:
    full = [_raw(n) for n in range(_EVENT_PAGE)]
    reader, sent = _reader([{"events": full, "cursor": "c1"}, {"events": [_raw(999)], "cursor": "c2"}])
    events = reader.events(CONTRACT, 50)
    assert len(events) == _EVENT_PAGE + 1
    assert sent[1] == {"filters": FILTERS, "pagination": {"limit": _EVENT_PAGE, "cursor": "c1"}}


def test_without_a_cursor_the_last_events_paging_token_or_id_pages_on() -> None:
    full = [_raw(n) for n in range(_EVENT_PAGE)]
    no_token = [*full[:-1], _raw(_EVENT_PAGE - 1, pagingToken=None)]
    reader, sent = _reader([{"events": full}, {"events": no_token, "cursor": ""}, {"events": []}])
    reader.events(CONTRACT, 50)
    assert [p["pagination"].get("cursor") for p in sent] == [None, f"tok-{_EVENT_PAGE - 1}", f"id-{_EVENT_PAGE - 1}"]


def test_a_full_page_with_no_cursor_at_all_stops() -> None:
    full = [_raw(n, pagingToken=None, id=None) for n in range(_EVENT_PAGE)]
    reader, sent = _reader([{"events": full, "cursor": None}])
    assert len(reader.events(CONTRACT, 50)) == _EVENT_PAGE
    assert len(sent) == 1


def test_an_empty_page_stops() -> None:
    reader, sent = _reader([{"events": None}])
    assert reader.events(CONTRACT, 50) == []
    assert len(sent) == 1


def test_the_scan_is_bounded_by_the_page_budget() -> None:
    full = [_raw(n) for n in range(_EVENT_PAGE)]
    reader, sent = _reader([{"events": full, "cursor": f"c{i}"} for i in range(_EVENT_PAGES + 1)])
    assert len(reader.events(CONTRACT, 50)) == _EVENT_PAGE * _EVENT_PAGES
    assert len(sent) == _EVENT_PAGES


def test_only_the_named_transactions_events_are_kept() -> None:
    reader, _ = _reader([{"events": [_raw(1, tx="aa"), _raw(2, tx="bb"), _raw(3, tx="aa")]}])
    assert [e.ledger for e in reader.events(CONTRACT, 50, tx_hash="aa")] == [101, 103]


def test_a_raw_events_missing_fields_read_plainly() -> None:
    raw = {"txHash": "cc", "inSuccessfulContractCall": False}
    reader, _ = _reader([{"events": [raw, _raw(2, value="", ledger=None, topic=None)]}])
    assert reader.events(CONTRACT, 50) == [
        ChainEvent(contract_id="None", tx_hash="cc", ledger=0, topics=[], value=None, successful=False),
        ChainEvent(contract_id=CONTRACT, tx_hash="aa", ledger=0, topics=[], value=None, successful=True),
    ]
