"""The ratings verdict is said once at boot, in words an operator can act on.

A free-tier instance boots on every wake, and its logs go with it — so the
boot line is the one moment the deployment volunteers whether its paid runs
will be rated. These pin that line for every status: its level (INFO only
when ratings can land), that it names what to fix, that it stays one line and
carries no secret, and that it runs on the real boot path without holding the
boot behind the chain read it needs. Nothing here reaches the network.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from stellar_sdk import Keypair, StrKey

from app.config import settings
from app.services import rating_writer as rw
from app.stellar import client as sc

WRITER_LOG = "app.services.rating_writer"
SIGNER = Keypair.from_raw_ed25519_seed(b"\x0a" * 32).public_key
OTHER = Keypair.from_raw_ed25519_seed(b"\x0b" * 32).public_key
LEDGER = StrKey.encode_contract(b"\x0c" * 32)


@pytest.fixture(autouse=True)
def configured_ledger(monkeypatch):
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(settings, "stellar_network", "testnet")
    # A fresh writer each test, and an unstubbed chain read fails loudly.
    monkeypatch.setattr(rw, "_last_read", None)
    monkeypatch.setattr(rw, "_read_task", None)
    monkeypatch.setattr(rw, "_report_task", None)

    def _unstubbed(ledger: str) -> str | None:
        raise AssertionError("a test reached the chain without stubbing it")

    monkeypatch.setattr(sc, "ledger_scorer", _unstubbed)


def _signs_as(monkeypatch, signer: str) -> None:
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present")
    monkeypatch.setattr(sc, "signer_public_key", lambda: signer)


def _hanging_chain(monkeypatch) -> threading.Event:
    """A chain read that blocks until the returned event is set."""
    release = threading.Event()

    def _hung(ledger: str) -> str | None:
        release.wait(5)
        return SIGNER

    monkeypatch.setattr(sc, "ledger_scorer", _hung)
    return release


def _writer_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == WRITER_LOG]


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


# ── in the background, and gone at shutdown ─────────────────────


def test_start_checks_and_reports_without_being_awaited(monkeypatch, caplog):
    _signs_as(monkeypatch, SIGNER)
    monkeypatch.setattr(sc, "ledger_scorer", lambda ledger: SIGNER)

    async def _boot():
        rw.start()
        task = rw._report_task
        assert task is not None and not task.done()  # start() returned first
        await task

    with caplog.at_level(logging.DEBUG, logger=WRITER_LOG):
        asyncio.run(_boot())
    [record] = _writer_records(caplog)
    assert record.levelno == logging.INFO
    assert "ratings writer ok" in record.getMessage()


def test_stop_cancels_a_check_still_waiting_on_the_chain(monkeypatch, caplog):
    _signs_as(monkeypatch, SIGNER)
    release = _hanging_chain(monkeypatch)

    async def _boot_then_shut_down():
        rw.start()
        report_task = rw._report_task
        await asyncio.sleep(0.05)  # the read is now in flight
        await rw.stop()
        release.set()  # let the abandoned worker thread finish
        return report_task

    with caplog.at_level(logging.DEBUG, logger=WRITER_LOG):
        report_task = asyncio.run(_boot_then_shut_down())
    assert report_task is not None and report_task.cancelled()
    assert rw._report_task is None and rw._read_task is None
    assert _writer_records(caplog) == []


def test_a_startup_check_that_dies_is_logged_not_lost(monkeypatch, caplog):
    """Nothing awaits the task, so without its done-callback an exception
    inside it would vanish without a line anywhere."""
    monkeypatch.setattr(settings, "reputation_enabled", False)

    def _boom(v: rw.WriterVerdict) -> None:
        raise RuntimeError("report broke")

    monkeypatch.setattr(rw, "report", _boom)

    async def _boot():
        rw.start()
        task = rw._report_task
        assert task is not None
        await asyncio.wait({task})
        await asyncio.sleep(0)  # let the done-callback run

    with caplog.at_level(logging.ERROR, logger=WRITER_LOG):
        asyncio.run(_boot())
    assert any("startup check died" in r.getMessage() for r in _writer_records(caplog))
