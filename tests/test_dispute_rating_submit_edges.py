"""Edges of `submit_dispute_rating` the main suite leaves implicit, pinned before it is split up.

The exact lines a SUCCESS, a hashless answer and a status-less answer log, and
the `credited unrecorded` a dispute with no recorded credit carries on every
line. Hermetic: the stellar client is monkeypatched, nothing reaches testnet.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from test_dispute_rating_submit import DERIVED, DISPUTE_ID, JOB, PAYER, WEIGHT, _dispute, _fake_submit, _rate

from app.services.dispute_rating import DISPUTE_RATING

LOGGER = "app.services.dispute_rating"

FACTS = (
    f"agent agt_writer, job {JOB}, derived {DERIVED}, rating {DISPUTE_RATING}, weight {WEIGHT}, "
    f"payer {PAYER}, credited unrecorded, refund tx_refund"
)


def _messages(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER]


def test_a_success_logs_one_info_line_with_every_fact(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": "tx_ok"})
    with caplog.at_level(logging.INFO, logger=LOGGER):
        outcome = _rate(_dispute())
    assert (outcome.status, outcome.tx_hash, outcome.reason) == ("SUCCESS", "tx_ok", None)
    assert _messages(caplog) == [(logging.INFO, f"dispute {DISPUTE_ID}: rating written — tx tx_ok ({FACTS})")]


@pytest.mark.parametrize("raw_hash", ["", 123, None])
def test_a_success_whose_hash_is_not_a_hash_is_a_timeout_with_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw_hash: Any
) -> None:
    _fake_submit(monkeypatch, {"status": "SUCCESS", "hash": raw_hash})
    with caplog.at_level(logging.INFO, logger=LOGGER):
        outcome = _rate(_dispute())
    assert (outcome.status, outcome.tx_hash) == ("TIMEOUT", None)
    assert _messages(caplog) == [
        (
            logging.ERROR,
            f"dispute {DISPUTE_ID}: rating unconfirmed and MAY STILL LAND — a retry cannot double it, the replay "
            f"guard refuses a second: status=SUCCESS hash=None ({FACTS})",
        )
    ]


@pytest.mark.parametrize("raw", [{"hash": "maybe_tx"}, {"status": None, "hash": "maybe_tx"}, {"status": ""}])
def test_a_missing_status_is_logged_as_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: dict[str, Any]
) -> None:
    _fake_submit(monkeypatch, raw)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        outcome = _rate(_dispute())
    assert outcome.status == "TIMEOUT"
    [(level, message)] = _messages(caplog)
    assert level == logging.ERROR
    assert f"status=missing hash={raw.get('hash')} ({FACTS})" in message


def test_a_failed_transaction_without_a_hash_keeps_none_and_its_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_submit(monkeypatch, {"status": "FAILED"})
    with caplog.at_level(logging.INFO, logger=LOGGER):
        outcome = _rate(_dispute())
    assert (outcome.status, outcome.tx_hash, outcome.reason) == ("FAILED", None, "the ledger failed the transaction")
    assert _messages(caplog) == [
        (
            logging.ERROR,
            f"dispute {DISPUTE_ID}: rating transaction failed on the ledger, nothing was written — hash=None ({FACTS})",
        )
    ]
