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

from app import origin_lock
from app.config import Settings, settings

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


# ── who is locked out ───────────────────────────────────────────


def _scope(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()],
    }


@pytest.fixture
def frontend_token(monkeypatch):
    monkeypatch.setattr(settings, "frontend_proxy_token", TOKEN)
    return TOKEN


def test_the_frontend_token_lets_a_request_through(frontend_token):
    assert not origin_lock.locked_out(_scope("/api/agents", headers={"X-Frontend-Proxy-Token": frontend_token}))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Frontend-Proxy-Token": "wrong-" + "k" * 40},
        {"X-Frontend-Proxy-Token": ""},
        # The visitor header alone proves nothing: only the token does.
        {"X-Orizon-Client-Ip": "203.0.113.7"},
    ],
    ids=["absent", "wrong", "empty", "client-ip-only"],
)
def test_a_request_without_the_right_token_is_locked_out(frontend_token, headers):
    assert origin_lock.locked_out(_scope("/api/agents", headers=headers))


def test_with_no_token_configured_nothing_can_prove_it_is_the_frontend(monkeypatch):
    """An empty configured token matches nothing — not even an empty header."""
    monkeypatch.setattr(settings, "frontend_proxy_token", "")
    assert origin_lock.locked_out(_scope("/api/agents", headers={"X-Frontend-Proxy-Token": ""}))


@pytest.mark.parametrize("path", ["/", "/health", "/readiness", "/docs", "/redoc", "/openapi.json", "/api", "/apix"])
def test_paths_outside_api_are_never_locked(frontend_token, path):
    assert not origin_lock.locked_out(_scope(path))


def test_a_preflight_is_never_locked(frontend_token):
    assert not origin_lock.locked_out(
        _scope("/api/orchestrator/execute", method="OPTIONS", headers={"Access-Control-Request-Method": "POST"})
    )


def test_only_http_is_locked(frontend_token):
    assert not origin_lock.locked_out({"type": "lifespan"})
