"""Can this deployment write ratings? — the verdict, and how it is kept.

The testnet ReputationLedger held zero ratings and nobody could say why: the
config gate in `_submit_ratings` returned in silence, and a signing key that
is not the ledger's Scorer reverts every rating while /readiness reported the
signer "configured". `rating_writer` answers the question with a closed set —
disabled, no_signer, scorer, not_scorer, unchecked — and these pin each
answer, the cache that keeps the one chain read cheap, and the bound that
keeps it from hanging. The chain is always a stub: nothing here reaches the
network, and an unstubbed read fails the test.
"""

from __future__ import annotations

import asyncio

import pytest
from stellar_sdk import Keypair, StrKey

from app.config import settings
from app.services import rating_writer as rw
from app.stellar import client as sc

SIGNER = Keypair.from_raw_ed25519_seed(b"\x0a" * 32).public_key
OTHER = Keypair.from_raw_ed25519_seed(b"\x0b" * 32).public_key
LEDGER = StrKey.encode_contract(b"\x0c" * 32)


class Chain:
    """Stands in for `sc.ledger_scorer`: answers as told and counts reads."""

    def __init__(self, scorer: str | None = None, error: Exception | None = None) -> None:
        self.scorer = scorer
        self.error = error
        self.reads: list[str] = []

    def __call__(self, ledger: str) -> str | None:
        self.reads.append(ledger)
        if self.error is not None:
            raise self.error
        return self.scorer


@pytest.fixture(autouse=True)
def fresh_writer(monkeypatch):
    """No cached read, no task, no skip history — and no way to the network."""
    monkeypatch.setattr(rw, "_last_read", None)
    monkeypatch.setattr(rw, "_read_task", None)
    monkeypatch.setattr(rw, "_report_task", None)
    monkeypatch.setattr(rw, "_skip_warned_at", {})
    monkeypatch.setattr(rw, "_skips_unreported", {})

    def _unstubbed(ledger: str) -> str | None:
        raise AssertionError("a test reached the chain without stubbing it")

    monkeypatch.setattr(sc, "ledger_scorer", _unstubbed)


def configure(monkeypatch, *, enabled: bool = True, ledger: str = LEDGER, key: bool = True) -> None:
    """A deployment whose key signs as SIGNER, unless told otherwise."""
    monkeypatch.setattr(settings, "reputation_enabled", enabled)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", ledger)
    # Presence is all the config gate reads; the public key comes from the
    # stubbed keypair below, so no real secret is ever parsed here.
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present" if key else "")
    monkeypatch.setattr(sc, "signer_public_key", lambda: SIGNER)


def on_chain(monkeypatch, chain: Chain) -> Chain:
    monkeypatch.setattr(sc, "ledger_scorer", chain)
    return chain


# ── config decides: no chain read at all ────────────────────────


def test_reputation_switched_off_is_disabled(monkeypatch):
    configure(monkeypatch, enabled=False)
    v = asyncio.run(rw.check())
    assert v.status == "disabled"
    assert v.gap is not None and "REPUTATION_ENABLED" in v.gap.problem
    assert (v.signer, v.scorer) == (None, None)


def test_no_ledger_is_disabled(monkeypatch):
    configure(monkeypatch, ledger="")
    v = asyncio.run(rw.check())
    assert v.status == "disabled"
    assert v.gap is not None and "STELLAR_REPUTATION_LEDGER" in v.gap.problem


def test_no_signing_key_is_no_signer(monkeypatch):
    configure(monkeypatch, key=False)
    v = asyncio.run(rw.check())
    assert v.status == "no_signer"
    assert v.gap is not None and v.gap.problem == "STELLAR_SIGNING_KEY is unset"
    assert (v.signer, v.scorer) == (None, None)


def test_a_key_that_does_not_parse_is_no_signer_and_never_quoted(monkeypatch):
    """`_signer_keypair`'s message quotes stellar_sdk's, which quotes the
    seed. The verdict must carry none of it — not in the gap, not anywhere."""
    configure(monkeypatch)
    secret_ish = "SBADSEEDTHATMUSTNEVERAPPEAR"

    def _bad_key() -> str:
        raise RuntimeError(f"STELLAR_SIGNING_KEY must be an S… secret ({secret_ish})")

    monkeypatch.setattr(sc, "signer_public_key", _bad_key)
    v = asyncio.run(rw.check())
    assert v.status == "no_signer"
    assert v.gap is not None and "STELLAR_SIGNING_KEY is set but" in v.gap.problem
    assert secret_ish not in repr(v)


def test_the_config_gate_names_the_first_gap_an_operator_would_fix(monkeypatch):
    """The order `_submit_ratings` gates in, so every surface names the same
    setting for the same deployment."""
    configure(monkeypatch, enabled=False, ledger="", key=False)
    assert rw.config_gap() is rw._REPUTATION_OFF
    monkeypatch.setattr(settings, "reputation_enabled", True)
    assert rw.config_gap() is rw._NO_LEDGER
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    assert rw.config_gap() is rw._NO_KEY
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present")
    assert rw.config_gap() is None
