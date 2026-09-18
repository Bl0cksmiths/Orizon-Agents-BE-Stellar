"""The ratings verdict is said once at boot, in words an operator can act on.

A free-tier instance boots on every wake, and its logs go with it — so the
boot line is the one moment the deployment volunteers whether its paid runs
will be rated. These pin that line for every status: its level (INFO only
when ratings can land), that it names what to fix, that it stays one line and
carries no secret, and that it runs on the real boot path without holding the
boot behind the chain read it needs. Nothing here reaches the network.
"""

from __future__ import annotations

import logging

import pytest
from stellar_sdk import Keypair, StrKey

from app.config import settings
from app.services import rating_writer as rw

WRITER_LOG = "app.services.rating_writer"
SIGNER = Keypair.from_raw_ed25519_seed(b"\x0a" * 32).public_key
OTHER = Keypair.from_raw_ed25519_seed(b"\x0b" * 32).public_key
LEDGER = StrKey.encode_contract(b"\x0c" * 32)


@pytest.fixture(autouse=True)
def configured_ledger(monkeypatch):
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(settings, "stellar_network", "testnet")


def _line(caplog, v: rw.WriterVerdict) -> logging.LogRecord:
    with caplog.at_level(logging.DEBUG, logger=WRITER_LOG):
        rw.report(v)
    records = [r for r in caplog.records if r.name == WRITER_LOG]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert "\n" not in records[0].getMessage()
    return records[0]


# ── one line per status ─────────────────────────────────────────


def test_a_signer_that_is_the_scorer_says_so_at_info(caplog):
    """The healthy case speaks too: a check heard only on failure cannot be
    told apart from one that never ran."""
    record = _line(caplog, rw.WriterVerdict("scorer", signer=SIGNER, scorer=SIGNER))
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert SIGNER in message and LEDGER in message
    assert "wallet-authorized" in message


def test_a_signer_that_is_not_the_scorer_names_both_addresses_and_the_fix(caplog):
    record = _line(caplog, rw.WriterVerdict("not_scorer", signer=SIGNER, scorer=OTHER))
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert SIGNER in message and OTHER in message and LEDGER in message
    assert "Unauthorized" in message
    assert f"set_scorer({SIGNER})" in message


def test_a_ledger_with_no_scorer_points_at_the_contract_id_and_network(caplog):
    record = _line(caplog, rw.WriterVerdict("not_scorer", signer=SIGNER, scorer=None))
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert LEDGER in message
    assert "stores no Scorer" in message
    assert "STELLAR_NETWORK=testnet" in message


def test_an_unread_scorer_is_a_warning_that_does_not_guess(caplog):
    record = _line(caplog, rw.WriterVerdict("unchecked", signer=SIGNER, read_error="timed out after 8s"))
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "timed out after 8s" in message
    assert "unknown" in message
    assert SIGNER in message


def test_no_signer_says_paid_runs_will_not_be_rated(caplog):
    record = _line(caplog, rw.WriterVerdict("no_signer", gap=rw._NO_KEY))
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "STELLAR_SIGNING_KEY is unset" in message
    assert "will not be rated" in message


def test_disabled_says_paid_runs_will_not_be_rated(caplog):
    record = _line(caplog, rw.WriterVerdict("disabled", gap=rw._REPUTATION_OFF))
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "REPUTATION_ENABLED is false" in message
    assert "will not be rated" in message
