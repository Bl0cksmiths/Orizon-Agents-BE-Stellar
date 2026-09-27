"""Shared test setup — force a hermetic, offline configuration before the app
is imported so tests never touch OpenAI, PDAX, or the real signing key."""

from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.stellar import client as _stellar_client


class _NoLiveRpc(RuntimeError):
    """Raised by the hermetic suite's Soroban server stand-in."""


def _no_live_rpc(*, submit: bool = False):
    raise _NoLiveRpc("hermetic suite: no live Soroban RPC — patch the call under test")


@pytest.fixture(autouse=True)
def hermetic_settings():
    """Neutralize anything secret/live that .env may have provided, and
    restore every setting mutated by a test."""
    saved = {
        "stellar_signing_key": settings.stellar_signing_key,
        "api_key": settings.api_key,
        "max_charge_usdc": settings.max_charge_usdc,
        "stellar_reputation_ledger": settings.stellar_reputation_ledger,
        "stellar_agent_registry": settings.stellar_agent_registry,
        "stellar_admin_address": settings.stellar_admin_address,
    }
    settings.stellar_signing_key = ""
    settings.api_key = ""
    # Reads need a source address to build a simulation envelope, and the
    # default is "" — so without this the suite only passes on a machine whose
    # gitignored .env happens to supply one, and fails in CI with
    # "no source address; set STELLAR_ADMIN_ADDRESS". Pinning a known-valid
    # public key here keeps the suite hermetic; it is an identifier, not a
    # secret, and nothing in the suite reaches the network.
    settings.stellar_admin_address = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
    # A live ledger id in .env would make reputation reads hit testnet RPC —
    # tests must stay offline, so force the prior-fallback path.
    settings.stellar_reputation_ledger = ""
    # Same reasoning for the agent registry: a live id in .env would let the
    # 1.02 registry-sync loop (started by lifespan, which every TestClient
    # runs) fire real testnet RPC from inside the hermetic suite.
    settings.stellar_agent_registry = ""
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


@pytest.fixture(autouse=True)
def no_live_rpc(monkeypatch):
    """Every Soroban call in the suite fails fast instead of reaching testnet.

    Blanking the ledger and registry ids above keeps the DEFAULT paths offline,
    but a test that arms a ledger id to reach the on-chain branch — and every
    background read lifespan starts (the ratings writer's scorer check, the
    reputation pre-warm) — would otherwise dial the real RPC whenever a test
    forgot to patch the one call it exercises. Such a test passes on a laptop
    with a network and on nothing else, and what it asserts is testnet's state
    that day. `_server` is the single constructor every read and write goes
    through, so replacing it here closes all of them; a test that means to
    drive the client patches `_server` (or the call above it) itself, which
    overrides this.
    """
    monkeypatch.setattr(_stellar_client, "_server", _no_live_rpc)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
