"""/readiness says whether this deployment can write ratings — and nothing more.

`signer: configured` is true of a key that is not the ledger's Scorer, which
is exactly the deployment whose every rating reverts with Unauthorized. The
`ratings` object answers the real question on demand, because the boot line
that also answers it is gone with the instance that wrote it. It is
informational: no status moves `ready` or the status code, and the probe never
waits on the chain for it. Nothing here reaches the network.
"""

from __future__ import annotations

import asyncio

import pytest
from stellar_sdk import Keypair

from app.config import settings
from app.services import rating_writer as rw
from app.stellar import client as sc

SIGNER = Keypair.from_raw_ed25519_seed(b"\x0a" * 32).public_key
OTHER = Keypair.from_raw_ed25519_seed(b"\x0b" * 32).public_key
LEDGER = "C" + "A" * 55


@pytest.fixture(autouse=True)
def ready_deployment(client, monkeypatch):
    """A deployment /readiness calls ready, with a key that signs as SIGNER.

    Takes `client` first so the app boots on the hermetic config — reputation
    disabled, no boot-time read — and each test decides what the chain says.
    """
    for field in (
        "stellar_agent_registry",
        "stellar_payment_escrow",
        "stellar_attestation_registry",
        "stellar_asset_sac",
    ):
        monkeypatch.setattr(settings, field, "C" + "A" * 55)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(settings, "stellar_admin_address", "G" + "A" * 55)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "pdax_username", "")
    monkeypatch.setattr(settings, "pdax_password", "")
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "reputation_prior_bps", 7000)
    monkeypatch.setattr(settings, "reputation_prior_weight_usdc", 12.0)
    monkeypatch.setattr(settings, "reputation_floor_bps", 5500)
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present")
    monkeypatch.setattr(sc, "signer_public_key", lambda: SIGNER)
    monkeypatch.setattr(rw, "_last_read", None)
    monkeypatch.setattr(rw, "_read_task", None)
    monkeypatch.setattr(rw, "_report_task", None)

    def _unstubbed(ledger: str) -> str | None:
        raise AssertionError("a test reached the chain without stubbing it")

    monkeypatch.setattr(sc, "ledger_scorer", _unstubbed)


def _chain_says(monkeypatch, *, scorer: str | None = None, error: Exception | None = None) -> None:
    """Resolve the one chain read the verdict needs, before the probe asks."""

    def _read(ledger: str) -> str | None:
        if error is not None:
            raise error
        return scorer

    monkeypatch.setattr(sc, "ledger_scorer", _read)
    asyncio.run(rw.check())


def test_a_scorer_deployment_reports_the_full_payload(client, monkeypatch):
    """Exact equality, like the hardening suite's: a field on an
    unauthenticated route is reviewed, never accreted."""
    _chain_says(monkeypatch, scorer=SIGNER)
    r = client.get("/readiness")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "llm": "ok",
        "stellar": "configured",
        "signer": "configured",
        "pdax": "unconfigured",
        "cold_start": {"routable": True, "lower_bound_bps": 5677, "floor_bps": 5500, "margin_bps": 177},
        "ratings": {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER},
    }
