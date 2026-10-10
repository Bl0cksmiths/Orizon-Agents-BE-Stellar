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
from app.main import app
from tests.route_inventory import concrete, operator_guard

TOKEN = "frontend-proxy-token-" + "k" * 24
OPERATOR_KEY = "operator-key-" + "o" * 32


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


# ── the allowlist ───────────────────────────────────────────────

OPEN = [e for e in origin_lock.EXEMPTIONS if not e.keyed]
KEYED = [e for e in origin_lock.EXEMPTIONS if e.keyed]


def _ids(exemptions):
    return [f"{e.method} {e.template}" for e in exemptions]


@pytest.fixture
def operator_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", OPERATOR_KEY)
    return OPERATOR_KEY


def test_the_open_allowlist_is_the_webhook_and_the_proxied_probe():
    assert {(e.method, e.template) for e in OPEN} == {
        ("POST", "/api/pdax/webhooks/receive"),
        ("GET", "/api/health"),
    }


@pytest.mark.parametrize("exemption", OPEN, ids=_ids(OPEN))
def test_an_open_route_needs_no_token(frontend_token, exemption):
    assert not origin_lock.locked_out(_scope(concrete(exemption.template), exemption.method))


@pytest.mark.parametrize("exemption", KEYED, ids=_ids(KEYED))
def test_a_keyed_route_is_open_to_a_caller_with_the_operator_key(frontend_token, operator_key, exemption):
    scope = _scope(concrete(exemption.template), exemption.method, {"X-API-Key": operator_key})
    assert not origin_lock.locked_out(scope)


@pytest.mark.parametrize("key", [None, "", "wrong-" + "o" * 40], ids=["absent", "empty", "wrong"])
@pytest.mark.parametrize("exemption", KEYED, ids=_ids(KEYED))
def test_a_keyed_route_without_the_operator_key_is_locked(frontend_token, operator_key, exemption, key):
    headers = {} if key is None else {"X-API-Key": key}
    assert origin_lock.locked_out(_scope(concrete(exemption.template), exemption.method, headers))


@pytest.mark.parametrize("exemption", KEYED, ids=_ids(KEYED))
def test_with_no_operator_key_configured_a_keyed_route_stays_locked(frontend_token, exemption):
    """`require_api_key` waves everyone through while API_KEY is empty, so the
    lock must not: an empty key opens nothing, whatever header is sent."""
    settings.api_key = ""  # hermetic_settings restores it
    scope = _scope(concrete(exemption.template), exemption.method, {"X-API-Key": ""})
    assert origin_lock.locked_out(scope)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # The right path, the wrong method.
        ("GET", "/api/pdax/webhooks/receive"),
        ("POST", "/api/health"),
        # A template is exact: no prefix match, no extra or missing segment.
        ("POST", "/api/pdax/webhooks/receive/extra"),
        ("POST", "/api/pdax/webhooks"),
        ("GET", "/api/healthz"),
        ("POST", "/api/disputes/a/b/uphold"),
        ("POST", "/api/disputes//uphold"),
        # A neighbour of an exempt route is not exempt.
        ("POST", "/api/pdax/webhooks/register"),
    ],
)
def test_a_near_miss_of_the_allowlist_is_locked(frontend_token, method, path):
    assert origin_lock.locked_out(_scope(path, method))


# Reads that take the operator key as an optional ELEVATION, not as their
# guard: anyone holding the task's read token (or nothing, for a binding's
# public half) reads them, and the key only widens what they see. They are the
# console's own reads, so they stay behind the lock; an operator reading one
# goes through orizons.xyz, which forwards X-API-Key untouched. The OpenAPI
# cannot tell an optional header from a guard's, so they are named here.
OPTIONAL_KEY_READS = {
    ("GET", "/api/agents/{agent_id}/binding"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/tasks/{task_id}/artifact"),
    ("GET", "/api/tasks/{task_id}/disputes"),
    ("GET", "/api/disputes/{dispute_id}"),
    ("GET", "/api/trace/{task_id}"),
    ("GET", "/api/trace/{task_id}/stream"),
}


def _openapi_key_operations() -> set[tuple[str, str]]:
    """Every /api operation that takes the operator key, guarded or optional, read off the OpenAPI."""
    spec = app.openapi()
    return {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        if path.startswith(origin_lock.API_PREFIX)
        for method, operation in item.items()
        if isinstance(operation, dict) and operator_guard(operation) is not None
    }


def test_the_keyed_allowlist_is_exactly_the_operator_keyed_routes():
    """Drift guard: every route behind an operator key is reachable with it,
    and nothing else rides on that exemption. A new keyed route, or a key
    taken off one, fails here until EXEMPTIONS (or OPTIONAL_KEY_READS) says so."""
    assert {(e.method, e.template) for e in KEYED} == _openapi_key_operations() - OPTIONAL_KEY_READS


def test_the_optional_key_reads_are_reads_and_still_take_the_key():
    assert OPTIONAL_KEY_READS <= _openapi_key_operations()
    assert {method for method, _ in OPTIONAL_KEY_READS} == {"GET"}
