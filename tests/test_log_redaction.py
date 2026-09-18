"""Secret redaction for log text (`app/security.py`).

A library can log a provider's error verbatim — agno does, at ERROR — so the
guarantee that no credential reaches a log line has to hold for text this
codebase never wrote. These tests pin both halves of the mask: this
deployment's own configured values, and secret-shaped tokens from anywhere.

Keys are generated per test run, never copied from a real account.
"""

from __future__ import annotations

import pytest

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
