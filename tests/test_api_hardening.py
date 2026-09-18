"""Probes, auth guard, charge validation, and the rate limiter."""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.config import settings
from app.security import RateLimitMiddleware

VALID_G = "G" + "A" * 55
CHARGE_BODY = {
    "auth_id_hex": "ab" * 16,
    "amount_usdc": 1.0,
    "job_id_hex": "cd" * 16,
}


def test_health_probe(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


_CONTRACT_ID_FIELDS = (
    "stellar_agent_registry",
    "stellar_payment_escrow",
    "stellar_attestation_registry",
    "stellar_asset_sac",
    "stellar_reputation_ledger",
)


def _configure_stellar(monkeypatch) -> None:
    for field in _CONTRACT_ID_FIELDS:
        monkeypatch.setattr(settings, field, "C" + "A" * 55)
    # Reads need a source account as much as they need contract ids, so a
    # fully-configured Stellar setup includes the admin address.
    monkeypatch.setattr(settings, "stellar_admin_address", VALID_G)


def _pin_shipped_reputation(monkeypatch) -> None:
    # The cold-start numbers are config, so a developer's .env would otherwise
    # decide what the exact-equality assertions below expect.
    monkeypatch.setattr(settings, "reputation_prior_bps", 7000)
    monkeypatch.setattr(settings, "reputation_prior_weight_usdc", 12.0)
    monkeypatch.setattr(settings, "reputation_floor_bps", 5500)


def test_readiness_ready_without_signing_key(client, monkeypatch):
    """Read-only deployments are legitimate: an absent signer is reported
    but never fails readiness (config.py doctrine)."""
    _configure_stellar(monkeypatch)
    _pin_shipped_reputation(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "pdax_username", "")
    monkeypatch.setattr(settings, "pdax_password", "")
    r = client.get("/readiness")
    assert r.status_code == 200
    # Exact equality on purpose: a field added to this probe has to be added
    # here too, so nothing reaches an unauthenticated route unreviewed.
    assert r.json() == {
        "status": "ready",
        "llm": "ok",
        "stellar": "configured",
        "signer": "absent",
        "pdax": "unconfigured",
        # 5677 - 5500: the shipped margin that keeps open registration real.
        "cold_start": {"routable": True, "lower_bound_bps": 5677, "floor_bps": 5500, "margin_bps": 177},
    }


def test_readiness_reports_a_floor_that_locks_newcomers_out_and_stays_ready(client, monkeypatch):
    """The hostile config: a floor raised past the prior's bound. Nothing
    errors — every newcomer just misses the floor forever — so the probe has
    to say so. It says so without failing: the process serves correctly, and
    a curated network that hires only rated agents is a policy, not an
    outage (the startup check's own doctrine, app/main.py)."""
    _configure_stellar(monkeypatch)
    _pin_shipped_reputation(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "pdax_username", "")
    monkeypatch.setattr(settings, "pdax_password", "")
    monkeypatch.setattr(settings, "reputation_floor_bps", 6000)
    r = client.get("/readiness")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "llm": "ok",
        "stellar": "configured",
        "signer": "absent",
        "pdax": "unconfigured",
        "cold_start": {"routable": False, "lower_bound_bps": 5677, "floor_bps": 6000, "margin_bps": -323},
    }


def test_readiness_names_a_lockout_nobody_touched_the_floor_for(client, monkeypatch):
    """The least visible way in: trimming the prior's evidence mass. The
    newcomer's displayed score is still 3.5/5 and the floor never moved, yet
    the bound sinks below it — so the bound itself has to be on the probe."""
    _pin_shipped_reputation(monkeypatch)
    monkeypatch.setattr(settings, "reputation_prior_weight_usdc", 4.0)
    assert client.get("/readiness").json()["cold_start"] == {
        "routable": False,
        "lower_bound_bps": 4709,
        "floor_bps": 5500,
        "margin_bps": -791,
    }


def test_readiness_503_when_llm_key_missing(client, monkeypatch):
    _configure_stellar(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")
    r = client.get("/readiness")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["llm"] == "missing_key"


def test_readiness_503_when_stellar_incomplete(client, monkeypatch):
    _configure_stellar(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "stellar_agent_registry", "")
    r = client.get("/readiness")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["stellar"] == "incomplete"


def test_readiness_503_when_admin_address_missing(client, monkeypatch):
    """Without STELLAR_ADMIN_ADDRESS every simulate_read raises outright
    ("no source address"), so the probe must not report a healthy Stellar."""
    _configure_stellar(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "stellar_admin_address", "")
    r = client.get("/readiness")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["stellar"] == "incomplete"


def test_readiness_reports_configured_signer_and_pdax(client, monkeypatch):
    _configure_stellar(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "stellar_signing_key", "S" + "A" * 55)
    monkeypatch.setattr(settings, "pdax_username", "ops@example.com")
    monkeypatch.setattr(settings, "pdax_password", "pw")
    body = client.get("/readiness").json()
    assert body["signer"] == "configured"
    assert body["pdax"] == "configured"


def test_attestation_path_rejects_out_of_bounds_job_id(client):
    """The job_id path param is bounded at the router edge, so oversized or
    non-hex ids are a 422 — no RPC round-trip, no stack trace."""
    for job_id in (
        "ab" * 512,  # far past 16 bytes
        "abc",  # too short
        "zz" * 16,  # right length, not hex
        "../../etc/passwd",
    ):
        r = client.get(f"/api/stellar/attestation/{job_id}")
        assert r.status_code in (404, 422), (job_id, r.status_code)


def test_attestation_path_accepts_a_well_formed_job_id(client, monkeypatch):
    """The bound rejects garbage without rejecting real ids: a valid one gets
    past validation and reaches the (here stubbed-offline) RPC layer."""
    from app.stellar import client as sc

    def _offline(*_args, **_kwargs):
        raise RuntimeError("rpc unreachable")

    monkeypatch.setattr(sc, "simulate_read", _offline)
    r = client.get(f"/api/stellar/attestation/{'ab' * 16}")
    assert r.status_code == 400
    assert r.json()["detail"] == "attestation_read_failed"


def test_charge_rejects_negative_amount(client):
    r = client.post(
        "/api/stellar/server/charge",
        json={**CHARGE_BODY, "amount_usdc": -5},
    )
    assert r.status_code == 422


def test_charge_rejects_malformed_hex_ids(client):
    r = client.post(
        "/api/stellar/server/charge",
        json={**CHARGE_BODY, "auth_id_hex": "not-hex"},
    )
    assert r.status_code == 422


def test_charge_unavailable_without_signing_key(client):
    r = client.post("/api/stellar/server/charge", json=CHARGE_BODY)
    assert r.status_code == 503


def test_charge_enforces_server_side_cap(client, hermetic_settings):
    from stellar_sdk import Keypair

    hermetic_settings.stellar_signing_key = Keypair.random().secret
    hermetic_settings.max_charge_usdc = 100.0
    r = client.post(
        "/api/stellar/server/charge",
        json={**CHARGE_BODY, "amount_usdc": 500.0},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "amount_exceeds_charge_cap"


def test_signing_routes_require_api_key_when_configured(client, hermetic_settings):
    hermetic_settings.api_key = "secret-key"
    r = client.post("/api/stellar/server/charge", json=CHARGE_BODY)
    assert r.status_code == 401
    r = client.post(
        "/api/stellar/server/charge",
        json=CHARGE_BODY,
        headers={"X-API-Key": "secret-key"},
    )
    # Past the guard now — fails later on the (unset) signing key instead.
    assert r.status_code == 503


def test_execute_rejects_bad_payer_address(client):
    r = client.post(
        "/api/orchestrator/execute",
        json={"plan_id": "nope", "payer": "not-an-address"},
    )
    assert r.status_code == 422


def test_register_agent_rejects_bad_owner(client):
    r = client.post(
        "/api/stellar/build/register-agent",
        json={
            "owner": "invalid",
            "agent_id": "a1",
            "name": "Agent",
            "skills": ["code"],
            "price_usdc": 0.1,
        },
    )
    assert r.status_code == 422


def test_rate_limiter_throttles_after_limit():
    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/hit", ok)])
    limited = TestClient(RateLimitMiddleware(inner, limit=3, window_seconds=60))

    for _ in range(3):
        assert limited.get("/hit").status_code == 200
    blocked = limited.get("/hit")
    assert blocked.status_code == 429
    assert "retry-after" in {k.lower() for k in blocked.headers}
    # The frontend backs off on this hint (lib/use-polling.ts), so it has to
    # be a positive whole number of seconds inside the window, not a 0 that
    # would invite an immediate retry.
    retry_after = int(blocked.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert blocked.headers["x-ratelimit-remaining"] == "0"
    assert blocked.json()["error"]["code"] == "rate_limited"


def test_default_budget_seats_a_realistic_number_of_dashboard_tabs():
    """The budget is currently whole-service, not per visitor (client_key()
    resolves to a constant until TRUSTED_PROXY_HOPS is tuned), so the default
    is pinned against the console's real cost: an open dashboard tab polls two
    endpoints every 5 s = 24 req/min. 120/min seated five tabs, which the live
    product exceeds routinely."""
    tab_requests_per_minute = 2 * (60 // 5)
    assert tab_requests_per_minute == 24
    assert settings.rate_limit_per_minute // tab_requests_per_minute >= 50


def test_rate_limiter_exempts_health():
    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/health", ok)])
    limited = TestClient(RateLimitMiddleware(inner, limit=1, window_seconds=60))
    for _ in range(5):
        assert limited.get("/health").status_code == 200


def test_rate_limiter_exempts_every_probe_path():
    """All four exempt paths stay exempt, at any budget: the proxied
    `/api/health` especially, since an uptime monitor polling it through the
    frontend would otherwise spend the shared budget on every visitor's
    behalf."""
    from app.security import EXEMPT_PATHS

    assert EXEMPT_PATHS == frozenset({"/", "/health", "/readiness", "/api/health"})

    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route(path, ok) for path in sorted(EXEMPT_PATHS)])
    limited = TestClient(RateLimitMiddleware(inner, limit=1, window_seconds=60))
    for path in sorted(EXEMPT_PATHS):
        for _ in range(4):
            r = limited.get(path)
            assert r.status_code == 200, (path, r.status_code)
            assert "x-ratelimit-limit" not in r.headers, path


def test_rate_limit_headers_on_allowed_response(client):
    r = client.get("/api/agents")
    assert r.status_code == 200
    assert r.headers["x-ratelimit-limit"] == str(settings.rate_limit_per_minute)
    assert 0 <= int(r.headers["x-ratelimit-remaining"]) < settings.rate_limit_per_minute


def test_rate_limit_headers_absent_on_exempt_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "x-ratelimit-limit" not in r.headers
    assert "x-ratelimit-remaining" not in r.headers


def test_cors_allows_project_preview_origins(client):
    for origin in (
        "https://orizon-agents-fe-stellar.vercel.app",
        "https://orizon-agents-fe-stellar-git-update-2-team.vercel.app",
    ):
        r = client.options(
            "/api/agents",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == origin


def test_cors_preflight_allows_task_token_header(client):
    """The frontend sends X-Task-Token for per-task reads; a cross-origin
    caller must get it back from the preflight or the request never fires."""
    origin = "https://orizon-agents-fe-stellar.vercel.app"
    r = client.options(
        "/api/tasks",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-task-token",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    allowed = {h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")}
    assert "x-task-token" in allowed
    # The headers already relied on stay allowed.
    assert {"content-type", "authorization", "x-api-key"} <= allowed


def test_cors_preflight_rejects_unknown_header(client):
    """Guard against the allow-list quietly becoming a wildcard."""
    r = client.options(
        "/api/tasks",
        headers={
            "Origin": "https://orizon-agents-fe-stellar.vercel.app",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-not-a-real-header",
        },
    )
    assert r.status_code == 400


def test_cors_rejects_foreign_vercel_origins(client):
    for origin in (
        "https://evil.vercel.app",
        "https://orizon-agents-fe-stellar.vercel.app.evil.com",
        "http://orizon-agents-fe-stellar.vercel.app",
    ):
        r = client.options(
            "/api/agents",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in r.headers, origin


def test_docs_exposed_by_default(client):
    # docs_enabled defaults on (public demo); flipping it off is applied at
    # app construction, so only the default is testable here.
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_openapi_documents_error_envelope(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["contact"]["name"] == "Orizon Agents"
    # pydantic's AnyUrl normalizes a bare origin with a trailing slash
    assert spec["info"]["contact"]["url"].rstrip("/") == "https://orizons.xyz"
    assert spec["servers"][0]["url"] == "https://orizon-agents-be-stellar.onrender.com"
    assert "ErrorEnvelope" in spec["components"]["schemas"]
    assert set(spec["components"]["schemas"]["ErrorBody"]["properties"]) == {"code", "message", "request_id"}
    # include_router merges the shared error responses into every operation.
    for path, method in (("/api/agents", "get"), ("/api/orchestrator/decompose", "post")):
        responses = spec["paths"][path][method]["responses"]
        for status in ("429", "500"):
            ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("ErrorEnvelope"), (path, status)


def test_access_log_redacts_token_query_value(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="app.security"):
        r = client.get("/api/agents?token=supersecret&foo=1")
    assert r.status_code == 200
    lines = [
        rec.getMessage() for rec in caplog.records if rec.name == "app.security" and "/api/agents" in rec.getMessage()
    ]
    assert lines, "expected an access log line"
    joined = "\n".join(lines)
    assert "supersecret" not in joined
    assert "token=***" in joined
    assert "foo=1" in joined


def test_redacted_target_masks_every_token_param():
    from app.security import _redacted_target

    scope = {"path": "/api/trace/stream", "query_string": b"token=abc&access_token=def&x=1"}
    assert _redacted_target(scope) == "/api/trace/stream?token=***&access_token=***&x=1"
    assert _redacted_target({"path": "/api/agents", "query_string": b""}) == "/api/agents"


def test_security_headers_on_api_response(client):
    r = client.get("/api/agents")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"


def test_500_handler_allows_preview_origin():
    from app.main import app

    if not any(getattr(r, "path", None) == "/boom-cors-test" for r in app.routes):

        @app.get("/boom-cors-test", include_in_schema=False)
        async def boom() -> None:
            raise RuntimeError("boom")

    preview = "https://orizon-agents-fe-stellar-git-update-2-team.vercel.app"
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom-cors-test", headers={"Origin": preview})
        assert r.status_code == 500
        # The hand-rolled CORS in the 500 handler must apply the same regex
        # as the middleware, or preview deployments can't read the envelope.
        assert r.headers["access-control-allow-origin"] == preview
        foreign = c.get("/boom-cors-test", headers={"Origin": "https://evil.vercel.app"})
        assert foreign.status_code == 500
        assert "access-control-allow-origin" not in foreign.headers


def test_security_headers_on_429():
    from app.main import SecurityHeadersMiddleware

    async def ok(request):
        return PlainTextResponse("ok")

    # Same composition the app registers: the header middleware wrapping the
    # limiter, so its 429 short-circuits pick up the hardening headers.
    inner = Starlette(routes=[Route("/hit", ok)])
    limited = TestClient(SecurityHeadersMiddleware(RateLimitMiddleware(inner, limit=1, window_seconds=60)))

    assert limited.get("/hit").status_code == 200
    blocked = limited.get("/hit")
    assert blocked.status_code == 429
    assert blocked.headers["x-content-type-options"] == "nosniff"
    assert blocked.headers["referrer-policy"] == "no-referrer"
    assert blocked.headers["x-frame-options"] == "DENY"


def test_app_orders_header_middleware_outside_limiter():
    from app.main import app

    names = [m.cls.__name__ for m in app.user_middleware]  # outermost first
    assert names.index("SecurityHeadersMiddleware") < names.index("RateLimitMiddleware")
    assert names.index("RateLimitMiddleware") < names.index("BodyLimitMiddleware")
    # Request-id context is outermost, so 413/429 short-circuits carry it.
    assert names.index("RequestContextMiddleware") == 0
