"""Edges of `sc.read_agent_records` the main suite leaves implicit, pinned before it is split up.

An entry that will not decode is logged and skipped without costing the rest
of its batch; a response with no entries list answers nothing; an RPC that
cannot be reached is logged by type and message. Faked at `sc._server`; nothing
reaches testnet.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from stellar_sdk.soroban_rpc import GetLedgerEntriesResponse, LedgerEntryResult
from test_agent_records import REGISTRY_ID, _Ledger, _record

from app.stellar import client as sc

LOGGER = "app.stellar.client"


def _install(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(sc, "_server", lambda **_kw: fake)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_an_entry_that_will_not_decode_is_logged_and_the_rest_are_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _Ledger({"fine": _record("fine"), "other": _record("other")})
    real = fake.get_ledger_entries

    def with_garbage(keys: list[Any]) -> GetLedgerEntriesResponse:
        answer = real(keys)
        garbage = LedgerEntryResult(key="AAAA", xdr="not xdr", lastModifiedLedgerSeq=1, liveUntilLedgerSeq=9)
        return GetLedgerEntriesResponse(entries=[garbage, *(answer.entries or [])], latestLedger=1)

    fake.get_ledger_entries = with_garbage  # type: ignore[method-assign]
    _install(monkeypatch, fake)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        records = sc.read_agent_records(REGISTRY_ID, ["fine", "other"])

    assert sorted(records) == ["fine", "other"]
    undecodable = [m for m in _warnings(caplog) if "undecodable agent record entry" in m]
    assert len(undecodable) == 1
    assert undecodable[0].startswith("[stellar.rpc] undecodable agent record entry: ")


def test_a_response_with_no_entries_list_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class Empty:
        def __init__(self) -> None:
            self.requests = 0

        def get_ledger_entries(self, keys: list[Any]) -> GetLedgerEntriesResponse:
            self.requests += 1
            return GetLedgerEntriesResponse(entries=None, latestLedger=1)

    fake = Empty()
    _install(monkeypatch, fake)

    assert sc.read_agent_records(REGISTRY_ID, ["a", "b"]) == {}
    assert fake.requests == 1  # an empty answer is an answer: not asked again


def test_a_batch_failing_twice_is_asked_exactly_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def __init__(self) -> None:
            self.requests = 0

        def get_ledger_entries(self, keys: list[Any]) -> GetLedgerEntriesResponse:
            self.requests += 1
            raise TimeoutError("read timed out")

    fake = Down()
    _install(monkeypatch, fake)

    assert sc.read_agent_records(REGISTRY_ID, ["a"]) == {}
    assert fake.requests == 2


def test_an_unreachable_rpc_is_logged_by_type_and_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def down(**_kw: Any) -> Any:
        raise ConnectionError("rpc down")

    monkeypatch.setattr(sc, "_server", down)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert sc.read_agent_records(REGISTRY_ID, ["a"]) == {}

    assert _warnings(caplog) == ["[stellar.rpc] agent records unreadable: ConnectionError: rpc down"]
