"""Secret redaction for log text (`app/security.py`).

A library can log a provider's error verbatim — agno does, at ERROR — so the
guarantee that no credential reaches a log line has to hold for text this
codebase never wrote. These tests pin both halves of the mask: this
deployment's own configured values, and secret-shaped tokens from anywhere.

Keys are generated per test run, never copied from a real account.
"""

from __future__ import annotations

import pytest
from stellar_sdk import Keypair, StrKey

from app import security
from app.config import settings


@pytest.fixture
def secrets_configured(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "openai_api_key": "sk-proj-" + "a1B2c3D4" * 4,
        "pdax_password": "correct-horse-battery-staple",
        "database_url": "postgresql://orizon:s3cr3t-pa55word@db.internal:5432/orizon",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return values


def test_a_configured_secret_is_masked_wherever_it_appears(secrets_configured: dict[str, str]) -> None:
    text = f"login failed for pdax with {secrets_configured['pdax_password']} (retrying)"

    assert security.redact_secrets(text) == "login failed for pdax with [redacted] (retrying)"


def test_secret_shaped_tokens_are_masked_even_when_they_are_not_ours() -> None:
    # A provider echoing back a rejected key, or a seed pasted into an error:
    # neither is this deployment's configured value, so only the shape pass
    # can catch it.
    foreign_seed = Keypair.random().secret
    text = f"auth rejected for sk-live-{'Zz9_' * 6} while signing with {foreign_seed}"

    masked = security.redact_secrets(text)

    assert masked == "auth rejected for [redacted] while signing with [redacted]"


def test_public_identifiers_are_never_mistaken_for_secrets() -> None:
    # The service logs these constantly; masking any of them would blind the
    # logs to exactly the addresses and hashes an incident is traced by.
    public_key = Keypair.random().public_key
    contract_id = StrKey.encode_contract(bytes(range(32)))
    tx_hash = "ab" * 32
    text = f"submitted by {public_key} to {contract_id} in tx {tx_hash}"

    assert security.redact_secrets(text) == text


def test_a_secret_embedded_in_a_longer_one_is_masked_whole(
    secrets_configured: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The database password is also configured on its own. Masked shortest
    # first, the URL would log as "postgresql://orizon:[redacted]@db..." —
    # host and user leaked, and the "whole URL is secret" rule broken.
    monkeypatch.setattr(settings, "api_key", "s3cr3t-pa55word")
    text = f"pool failed: {secrets_configured['database_url']}"

    assert security.redact_secrets(text) == "pool failed: [redacted]"
