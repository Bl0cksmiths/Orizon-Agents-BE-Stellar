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
from app.security import security_headers
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


# ── the counter ─────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_the_counter_keeps_a_total_and_the_last_hour():
    clock = _Clock()
    counted = origin_lock.LockStats(clock)
    for _ in range(3):
        counted.record()
    clock.now += 30 * 60
    counted.record()
    assert (counted.total, counted.last_hour()) == (4, 4)
    clock.now += 45 * 60  # the first three are now 75 minutes old
    assert (counted.total, counted.last_hour()) == (4, 1)
    clock.now += 60 * 60
    assert (counted.total, counted.last_hour()) == (4, 0)


def test_the_counter_stays_small_however_long_it_runs():
    clock = _Clock()
    counted = origin_lock.LockStats(clock)
    for _ in range(24 * 60):  # a day of one refusal a minute
        counted.record()
        clock.now += 60
    assert counted.total == 24 * 60
    assert len(counted._buckets) <= 61
    clock.now -= 60  # back to the minute of the last refusal
    assert counted.last_hour() == 60


def test_the_counter_resets():
    counted = origin_lock.LockStats(_Clock())
    counted.record()
    counted.reset()
    assert (counted.total, counted.last_hour()) == (0, 0)


# ── what the log names, and how often ───────────────────────────


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("/api/agents", "/api/agents"),
        ("/api/agents/agt_01h8", "/api/agents/{agent_id}"),
        # A literal route is not mistaken for a template's parameter.
        ("/api/agents/bind/endpoint-check", "/api/agents/bind/endpoint-check"),
        ("/api/tasks/t_secret/artifact", "/api/tasks/{task_id}/artifact"),
        ("/api/no-such-route", origin_lock.UNROUTED),
        ("/api/agents/a/b/c/d", origin_lock.UNROUTED),
    ],
)
def test_a_path_is_logged_as_its_route_template(path, template):
    assert origin_lock.RouteTemplates().resolve(app, path) == template


def test_unreadable_templates_log_every_path_as_unrouted(caplog):
    class _Broken:
        def openapi(self):
            raise RuntimeError("schema failed")

    with caplog.at_level("ERROR", logger="app.origin_lock"):
        assert origin_lock.RouteTemplates().resolve(_Broken(), "/api/agents") == origin_lock.UNROUTED
    assert "could not read the route templates" in caplog.text


def test_the_coalescer_logs_once_per_template_per_window_and_counts_the_rest():
    clock = _Clock()
    coalescer = origin_lock.WarningCoalescer(60.0, clock)
    assert coalescer.admit("/api/agents") == 0
    assert [coalescer.admit("/api/agents") for _ in range(5)] == [None] * 5
    # Another template has a window of its own.
    assert coalescer.admit("/api/tasks") == 0
    clock.now += 60
    assert coalescer.admit("/api/agents") == 5
    assert coalescer.admit("/api/agents") is None


# ── the middleware, through the app ─────────────────────────────

LOGGER = "app.origin_lock"


@pytest.fixture(autouse=True)
def fresh_lock_state():
    """The counter and the log window are process-wide; every test starts from zero."""
    origin_lock.stats.reset()
    origin_lock.log_coalescer.reset()
    yield
    origin_lock.stats.reset()
    origin_lock.log_coalescer.reset()


@pytest.fixture
def mode(monkeypatch, frontend_token):
    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "origin_lock_mode", value)

    return _set


def _lock_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelname == "WARNING"]


def test_off_serves_everyone_and_counts_nothing(client, mode, caplog):
    mode("off")
    assert client.get("/api/agents").status_code == 200
    assert origin_lock.stats.total == 0
    assert _lock_lines(caplog) == []


def test_log_serves_a_direct_call_but_counts_and_logs_it(client, mode, caplog):
    mode("log")
    with caplog.at_level("WARNING", logger=LOGGER):
        response = client.get("/api/agents/agt_01h8?token=never-logged")
    assert response.status_code != 403
    assert origin_lock.stats.total == 1
    (line,) = _lock_lines(caplog)
    assert line.startswith("origin lock would refuse GET /api/agents/{agent_id}: not from the frontend [")
    # The template, never the path or its query string.
    assert "agt_01h8" not in line and "never-logged" not in line
    assert f"[{response.headers['x-request-id']}]" in line


def test_log_coalesces_a_flood_into_one_line_per_route(client, mode, caplog):
    mode("log")
    with caplog.at_level("WARNING", logger=LOGGER):
        for _ in range(20):
            client.get("/api/agents")
        client.get("/api/flow/default")
    assert origin_lock.stats.total == 21
    assert len(_lock_lines(caplog)) == 2


def test_log_does_not_count_the_frontend(client, mode, frontend_token):
    mode("log")
    assert client.get("/api/agents", headers={"X-Frontend-Proxy-Token": frontend_token}).status_code == 200
    assert origin_lock.stats.total == 0


def test_enforce_refuses_a_direct_call_in_the_error_envelope(client, mode, caplog):
    mode("enforce")
    with caplog.at_level("WARNING", logger=LOGGER):
        response = client.get("/api/agents", headers={"X-Frontend-Proxy-Token": "forged-" + "x" * 40})
    assert response.status_code == 403
    request_id = response.headers["x-request-id"]
    assert response.json() == {
        "detail": "origin_forbidden",
        "error": {
            "code": "origin_forbidden",
            "message": "This API is only available through orizons.xyz.",
            "request_id": request_id,
        },
    }
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    # Inside the hardening headers, so the refusal is stamped like any response.
    for name, value in security_headers("/api/agents"):
        assert response.headers[name.decode()] == value.decode()
    # Outside the rate limiter: a refusal spends nobody's budget.
    assert "x-ratelimit-remaining" not in response.headers
    # Nothing of what was sent comes back, in the body or the log.
    assert "forged" not in response.text
    assert origin_lock.stats.total == 1
    (line,) = _lock_lines(caplog)
    assert line.startswith("origin lock refused GET /api/agents: not from the frontend")
    assert "forged" not in caplog.text


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_enforce_refuses_without_reading_the_route(client, mode, method):
    """A refused request never reaches routing: an unknown route is 403, not 404."""
    mode("enforce")
    response = client.request(method, "/api/no-such-route")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_forbidden"


def test_enforce_serves_the_frontend(client, mode, frontend_token):
    mode("enforce")
    response = client.get("/api/agents", headers={"X-Frontend-Proxy-Token": frontend_token})
    assert response.status_code == 200
    assert origin_lock.stats.total == 0


@pytest.mark.parametrize("path", ["/", "/health", "/readiness", "/docs", "/openapi.json"])
def test_enforce_leaves_paths_outside_api_alone(client, mode, path):
    mode("enforce")
    assert client.get(path).status_code != 403
    assert origin_lock.stats.total == 0


def test_enforce_lets_a_cors_preflight_through(client, mode):
    mode("enforce")
    response = client.options(
        "/api/orchestrator/execute",
        headers={
            "Origin": "https://orizon-agents-fe-stellar.vercel.app",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://orizon-agents-fe-stellar.vercel.app"
    assert origin_lock.stats.total == 0


def test_a_refusal_carries_cors_for_an_allowed_origin(client, mode):
    """So the console's browser can read the 403 rather than see an opaque CORS failure."""
    mode("enforce")
    origin = "https://orizon-agents-fe-stellar.vercel.app"
    response = client.get("/api/agents", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == origin


def test_enforce_leaves_the_proxied_probe_open(client, mode):
    mode("enforce")
    assert client.get("/api/health").status_code == 200


def test_enforce_leaves_the_pdax_webhook_to_its_signature(client, mode):
    """PDAX cannot hold our token: the webhook reaches its own HMAC check, which refuses an unsigned delivery."""
    mode("enforce")
    response = client.post("/api/pdax/webhooks/receive", content=b"{}")
    assert response.status_code != 403
    assert response.json()["error"]["code"] != "origin_forbidden"
    assert origin_lock.stats.total == 0


def test_enforce_leaves_a_keyed_route_to_the_operator_with_the_key(client, mode, operator_key):
    mode("enforce")
    response = client.post("/api/disputes/dsp_0000000000000000/uphold", headers={"X-API-Key": operator_key}, json={})
    # Past the lock: whatever the route answers, it is the route's answer.
    assert response.status_code != 403
    assert origin_lock.stats.total == 0


def test_enforce_refuses_a_keyed_route_without_the_key(client, mode, operator_key):
    mode("enforce")
    response = client.post("/api/disputes/dsp_0000000000000000/uphold", headers={"X-API-Key": "wrong-" + "o" * 40})
    assert response.status_code == 403
    assert "wrong-" not in response.text
