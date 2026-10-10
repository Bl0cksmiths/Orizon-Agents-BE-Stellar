"""The origin lock (app/origin_lock.py, ADR 0017): /api/* answers our frontend only.

Our Vercel frontend proves itself on every proxied /api request with
X-Frontend-Proxy-Token. Anything else that reaches Render directly walks past
the Vercel firewall, so the lock logs it (`log`) or refuses it (`enforce`) —
except the few callers that legitimately come straight here, each of which
carries an authentication of its own.

Settings are built with _env_file=None so the local .env can never leak into
an assertion.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

TOKEN = "frontend-proxy-token-" + "k" * 24


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── configuration ───────────────────────────────────────────────


def test_the_lock_logs_by_default():
    assert _settings().origin_lock_mode == "log"


@pytest.mark.parametrize("mode", ["off", "log", "enforce"])
def test_every_mode_boots_with_a_frontend_token(mode):
    assert _settings(origin_lock_mode=mode, frontend_proxy_token=TOKEN).origin_lock_mode == mode


@pytest.mark.parametrize("mode", ["off", "log"])
def test_a_lock_that_refuses_nobody_boots_without_a_token(mode):
    assert _settings(origin_lock_mode=mode).origin_lock_mode == mode


def test_an_enforced_lock_without_a_token_refuses_to_boot():
    """No token means nothing can prove it is our frontend: every /api call
    would be a 403, so the process refuses to start rather than lock itself out."""
    with pytest.raises(ValidationError, match="ORIGIN_LOCK_MODE=enforce needs FRONTEND_PROXY_TOKEN"):
        _settings(origin_lock_mode="enforce")


def test_an_unknown_mode_refuses_to_boot():
    with pytest.raises(ValidationError, match="origin_lock_mode"):
        _settings(origin_lock_mode="block")
