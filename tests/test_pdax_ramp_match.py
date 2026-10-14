"""`ramp._match`, the settlement-event matcher, pinned case by case.

Which ramp and which advance an event resolves to, and which non-matches are
dropped quietly (never-acted-on events) and which are warned about (a completed
deposit that belongs to no ramp is unclaimed money). Records are put straight
into the in-process ramp store; no PDAX client is involved.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.pdax import ramp, ramp_store
from app.pdax.models.ramp import RampRecord
from app.pdax.models.webhooks import CryptoEvent, FiatEvent

ADDRESS = "GDEPOSITADDRESS"
OTHER_ADDRESS = "GSOMEWHEREELSE"


@pytest.fixture(autouse=True)
def clean_ramp_state() -> Any:
    ramp_store._ramps.clear()
    ramp_store._locks.clear()
    yield
    ramp_store._ramps.clear()
    ramp_store._locks.clear()


def _record(ramp_id: str, direction: str, **fields: Any) -> RampRecord:
    record = RampRecord(
        ramp_id=ramp_id, direction=direction, status="awaiting_payment", created_at="2026-10-15T00:00:00Z", **fields
    )
    return ramp_store.save(record)


def _fiat(**overrides: Any) -> FiatEvent:
    fields: dict[str, Any] = {
        "identifier": "on-1",
        "user_id": "u1",
        "amount": 500.0,
        "transaction_type": "DEPOSIT",
        "status": "COMPLETED",
    }
    return FiatEvent(**{**fields, **overrides})


def _crypto(**overrides: Any) -> CryptoEvent:
    fields: dict[str, Any] = {
        "identifier": "dep-1",
        "user_id": "u1",
        "amount": 10.0,
        "asset": "USDCXLM",
        "transaction_type": "DEPOSIT",
        "status": "completed",
        "destination_address": ADDRESS,
    }
    return CryptoEvent(**{**fields, **overrides})


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "app.pdax.ramp" and r.levelno >= logging.WARNING]


def _match(caplog: pytest.LogCaptureFixture, event: CryptoEvent | FiatEvent) -> tuple[Any, Any, list[str]]:
    with caplog.at_level(logging.WARNING, logger="app.pdax.ramp"):
        record, advance = ramp._match(event)
    return record, advance, _warnings(caplog)


# ── fiat: an on-ramp, matched by identifier ──────────────────────────────


@pytest.mark.parametrize("transaction_type", ["deposit", "Bank DEPOSIT", "DEPOSIT"])
def test_a_completed_fiat_deposit_matches_its_onramp(caplog: pytest.LogCaptureFixture, transaction_type: str) -> None:
    onramp = _record("r_on", "onramp", identifier="on-1")
    record, advance, warned = _match(caplog, _fiat(transaction_type=transaction_type, status="completed"))
    assert (record, advance, warned) == (onramp, ramp.advance_onramp, [])


@pytest.mark.parametrize(
    "event", [_fiat(transaction_type="WITHDRAWAL"), _fiat(status="IN-PROGRESS"), _fiat(status="FAILED")]
)
def test_a_fiat_event_never_acted_on_is_dropped_quietly(caplog: pytest.LogCaptureFixture, event: FiatEvent) -> None:
    _record("r_on", "onramp", identifier="on-1")
    assert _match(caplog, event) == (None, None, [])


@pytest.mark.parametrize("identifier", [None, "", "unknown-id"])
def test_a_completed_fiat_deposit_with_no_ramp_is_warned(
    caplog: pytest.LogCaptureFixture, identifier: str | None
) -> None:
    _record("r_on", "onramp", identifier="on-1")
    record, advance, warned = _match(caplog, _fiat(identifier=identifier))
    assert (record, advance) == (None, None)
    assert warned == [
        "unmatched settlement event: kind=fiat type=DEPOSIT status=COMPLETED amount=500.0 asset=PHP "
        f"identifier={identifier} target=None"
    ]


def test_a_fiat_deposit_naming_an_offramp_is_warned_not_matched(caplog: pytest.LogCaptureFixture) -> None:
    _record("r_off", "offramp", identifier="on-1", deposit_address=ADDRESS)
    record, advance, warned = _match(caplog, _fiat())
    assert (record, advance) == (None, None)
    assert len(warned) == 1 and "kind=fiat" in warned[0]


# ── crypto: an off-ramp, matched by deposit address and tag ──────────────


@pytest.mark.parametrize("asset", ["USDCXLM", "usdcxlm"])
def test_a_completed_usdc_deposit_matches_its_offramp(caplog: pytest.LogCaptureFixture, asset: str) -> None:
    offramp = _record("r_off", "offramp", deposit_address=ADDRESS)
    record, advance, warned = _match(caplog, _crypto(asset=asset, status="COMPLETED", transaction_type="deposit"))
    assert (record, advance, warned) == (offramp, ramp.advance_offramp, [])


@pytest.mark.parametrize("event", [_crypto(transaction_type="WITHDRAWAL"), _crypto(status="failed")])
def test_a_crypto_event_never_acted_on_is_dropped_quietly(caplog: pytest.LogCaptureFixture, event: CryptoEvent) -> None:
    _record("r_off", "offramp", deposit_address=ADDRESS)
    assert _match(caplog, event) == (None, None, [])


def test_a_deposit_of_another_asset_is_warned_and_never_matched(caplog: pytest.LogCaptureFixture) -> None:
    _record("r_off", "offramp", deposit_address=ADDRESS)
    record, advance, warned = _match(caplog, _crypto(asset="BTC"))
    assert (record, advance) == (None, None)
    assert warned == [
        "unmatched settlement event: kind=crypto type=DEPOSIT status=completed amount=10.0 asset=BTC "
        f"identifier=dep-1 target={ADDRESS}"
    ]


def test_the_first_offramp_on_the_address_wins_when_the_event_has_no_tag(caplog: pytest.LogCaptureFixture) -> None:
    first = _record("r_a", "offramp", deposit_address=ADDRESS, deposit_tag="tag-a")
    _record("r_b", "offramp", deposit_address=ADDRESS, deposit_tag="tag-b")
    assert _match(caplog, _crypto(destination_address_tag="")) == (first, ramp.advance_offramp, [])


def test_a_tagged_event_matches_only_the_ramp_holding_its_tag(caplog: pytest.LogCaptureFixture) -> None:
    _record("r_a", "offramp", deposit_address=ADDRESS, deposit_tag="tag-a")
    second = _record("r_b", "offramp", deposit_address=ADDRESS, deposit_tag="tag-b")
    assert _match(caplog, _crypto(destination_address_tag="tag-b")) == (second, ramp.advance_offramp, [])


@pytest.mark.parametrize(
    ("records", "event"),
    [
        ([("r_on", "onramp", {"deposit_address": ADDRESS})], _crypto()),
        ([("r_off", "offramp", {"deposit_address": None})], _crypto(destination_address=None)),
        ([("r_off", "offramp", {"deposit_address": ""})], _crypto(destination_address="")),
        ([("r_off", "offramp", {"deposit_address": OTHER_ADDRESS})], _crypto()),
        (
            [("r_off", "offramp", {"deposit_address": ADDRESS, "deposit_tag": "tag-a"})],
            _crypto(destination_address_tag="tag-z"),
        ),
        ([], _crypto()),
    ],
)
def test_a_usdc_deposit_no_offramp_claims_is_warned(
    caplog: pytest.LogCaptureFixture, records: list[tuple[str, str, dict[str, Any]]], event: CryptoEvent
) -> None:
    for ramp_id, direction, fields in records:
        _record(ramp_id, direction, **fields)
    record, advance, warned = _match(caplog, event)
    assert (record, advance) == (None, None)
    assert len(warned) == 1 and "kind=crypto" in warned[0] and f"target={event.destination_address}" in warned[0]
