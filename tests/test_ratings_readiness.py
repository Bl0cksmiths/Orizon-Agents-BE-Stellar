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
import time

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


def _signer_does_not_parse() -> str:
    raise RuntimeError("STELLAR_SIGNING_KEY must be an S… secret")


# Each status, set up the way a real deployment reaches it, and the exact
# `ratings` object the probe must report for it.
@pytest.mark.parametrize(
    ("status", "arrange", "expected"),
    [
        (
            "scorer",
            lambda mp: _chain_says(mp, scorer=SIGNER),
            {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER},
        ),
        (
            "not_scorer",
            lambda mp: _chain_says(mp, scorer=OTHER),
            {"writer": "not_scorer", "signer": SIGNER, "scorer": OTHER},
        ),
        (
            "not_scorer, nothing stored",
            lambda mp: _chain_says(mp, scorer=None),
            {"writer": "not_scorer", "signer": SIGNER, "scorer": None},
        ),
        (
            "unchecked",
            lambda mp: _chain_says(mp, error=ConnectionError("rpc down")),
            {"writer": "unchecked", "signer": SIGNER, "scorer": None},
        ),
        (
            "no_signer",
            lambda mp: mp.setattr(settings, "stellar_signing_key", ""),
            {"writer": "no_signer", "signer": None, "scorer": None},
        ),
        (
            "no_signer, key does not parse",
            lambda mp: mp.setattr(sc, "signer_public_key", _signer_does_not_parse),
            {"writer": "no_signer", "signer": None, "scorer": None},
        ),
        (
            "disabled",
            lambda mp: mp.setattr(settings, "reputation_enabled", False),
            {"writer": "disabled", "signer": None, "scorer": None},
        ),
    ],
)
def test_every_status_is_reported_and_none_moves_the_ready_verdict(client, monkeypatch, status, arrange, expected):
    """Informational, like `cold_start`: a deployment that cannot write
    ratings still serves every request, so no verdict here may turn a ready
    deployment into a 503 — least of all `not_scorer`, the one that matters."""
    arrange(monkeypatch)
    r = client.get("/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["ratings"] == expected


def test_a_healthy_writer_does_not_rescue_a_not_ready_deployment(client, monkeypatch):
    """The other direction of "never gates"."""
    _chain_says(monkeypatch, scorer=SIGNER)
    monkeypatch.setattr(settings, "openai_api_key", "")
    r = client.get("/readiness")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["ratings"] == {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER}


def test_a_stale_answer_is_served_and_refreshed_behind_the_probe(client, monkeypatch):
    """How the probe follows a set_scorer without a live read on its path:
    past the TTL it still answers at once with what it knows, starts one
    background read, and the next probe carries the new answer."""
    monkeypatch.setattr(
        rw, "_last_read", rw._ScorerRead(LEDGER, time.monotonic() - rw.SCORER_TTL_SECONDS - 1, scorer=OTHER)
    )
    reads: list[str] = []

    def _after_set_scorer(ledger: str) -> str:
        reads.append(ledger)
        return SIGNER

    monkeypatch.setattr(sc, "ledger_scorer", _after_set_scorer)

    assert client.get("/readiness").json()["ratings"]["writer"] == "not_scorer"
    deadline = time.monotonic() + 5
    while (rw._last_read is None or rw._last_read.scorer != SIGNER) and time.monotonic() < deadline:
        time.sleep(0.01)
    for _ in range(3):
        assert client.get("/readiness").json()["ratings"] == {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER}
    assert reads == [LEDGER]  # one read behind four probes
