"""CORS on every response the middleware stack can produce.

A browser hides any cross-origin response that lacks Access-Control-Allow-Origin
from the page that asked for it: the console sees an opaque network failure
instead of the 403, 429 or 500 envelope it knows how to explain. So the
header has to reach every response a layer of the stack makes on its own —
the limiter's 429, the origin lock's 403, the 500 handler's envelope — and a
compressed one, not only those the router produced.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import RateLimitMiddleware

ALLOWED = "https://orizon-agents-fe-stellar.vercel.app"
FOREIGN = "https://evil.vercel.app"


def _vary(response) -> set[str]:
    return {part.strip().lower() for part in response.headers.get("vary", "").split(",") if part.strip()}


# ── preflights ────────────────────────────────────────────────────────────


def test_an_allowed_origin_preflight_is_answered(client):
    r = client.options("/api/agents", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"})

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    allowed = {m.strip() for m in r.headers["access-control-allow-methods"].split(",")}
    assert {"GET", "POST", "DELETE", "OPTIONS"} <= allowed
    assert "origin" in _vary(r)


def test_a_foreign_origin_preflight_gets_no_allow_origin(client):
    r = client.options("/api/agents", headers={"Origin": FOREIGN, "Access-Control-Request-Method": "GET"})

    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_a_preflight_is_answered_before_the_request_context(client, caplog):
    """The one guarantee CORS-outermost gives up, pinned so it is changed on
    purpose: CORS answers a preflight itself, so it gets no X-Request-ID and no
    access-log line. A browser never shows a preflight's headers to script, so
    there is no client that could have quoted the id (app/main.py)."""
    with caplog.at_level("INFO", logger="app.security"):
        r = client.options("/api/agents", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"})
        client.get("/api/agents", headers={"Origin": ALLOWED})

    access = [
        rec.getMessage() for rec in caplog.records if rec.name == "app.security" and "/api/agents" in rec.getMessage()
    ]
    assert r.status_code == 200
    assert "x-request-id" not in r.headers
    assert [line for line in access if line.startswith("GET ")], "the GET beside it is logged"
    assert not [line for line in access if line.startswith("OPTIONS ")]


# ── responses the router produced ─────────────────────────────────────────


def test_an_allowed_origin_get_carries_cors_and_a_request_id(client):
    r = client.get("/api/agents", headers={"Origin": ALLOWED})

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["x-request-id"]
    # Script can read the registry headers only because they are exposed.
    assert "x-total-count" in r.headers["access-control-expose-headers"].lower()


def test_a_foreign_origin_get_carries_no_allow_origin(client):
    r = client.get("/api/agents", headers={"Origin": FOREIGN})

    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert r.headers["x-request-id"]


def test_a_compressed_response_keeps_its_cors_headers(client):
    """GZip and CORS both edit Vary; neither may drop the other's entry, or a
    shared cache serves one origin's (or one encoding's) copy to another."""
    r = client.get("/api/agents", headers={"Origin": ALLOWED, "Accept-Encoding": "gzip"})

    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert {"accept-encoding", "origin"} <= _vary(r)
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["x-request-id"]
    # httpx already decoded it; the payload is the list, intact.
    assert isinstance(r.json(), list)


def test_a_small_response_is_left_uncompressed(client):
    r = client.get("/health", headers={"Origin": ALLOWED, "Accept-Encoding": "gzip"})

    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.headers["access-control-allow-origin"] == ALLOWED


def test_the_compressed_bytes_are_real_gzip():
    """httpx decodes transparently, so read the raw stream to prove the body
    on the wire is what Content-Encoding claims."""
    with TestClient(app) as c, c.stream("GET", "/api/agents", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert r.headers["content-encoding"] == "gzip"
    assert gzip.decompress(raw).startswith(b"[")


# ── responses a middleware made on its own ────────────────────────────────


def _global_limiter() -> RateLimitMiddleware:
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "the app registers no RateLimitMiddleware"
    return node


@pytest.fixture
def one_request_a_minute(client, monkeypatch) -> Iterator[None]:
    limiter = _global_limiter()
    limiter._hits.clear()
    monkeypatch.setattr(limiter, "limit", 1)
    yield
    limiter._hits.clear()


def test_the_limiters_429_carries_cors_and_a_request_id(client, one_request_a_minute):
    assert client.get("/api/agents", headers={"Origin": ALLOWED}).status_code == 200

    r = client.get("/api/agents", headers={"Origin": ALLOWED})

    assert r.status_code == 429
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["x-request-id"] == r.json()["error"]["request_id"]
    assert int(r.headers["retry-after"]) >= 1


def test_the_origin_locks_403_carries_cors_and_a_request_id(client, monkeypatch):
    monkeypatch.setattr(settings, "frontend_proxy_token", "frontend-proxy-token-" + "k" * 24)
    monkeypatch.setattr(settings, "origin_lock_mode", "enforce")

    r = client.get("/api/agents", headers={"Origin": ALLOWED})

    assert r.status_code == 403
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["x-request-id"]


def test_an_unhandled_500_carries_cors_and_a_request_id():
    if not any(getattr(route, "path", None) == "/boom-sonar-cors" for route in app.routes):

        @app.get("/boom-sonar-cors", include_in_schema=False)
        async def boom() -> None:
            raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom-sonar-cors", headers={"Origin": ALLOWED, "X-Request-ID": "req-sonar-500"})
        foreign = c.get("/boom-sonar-cors", headers={"Origin": FOREIGN})

    assert r.status_code == 500
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "origin" in _vary(r)
    assert r.headers["x-request-id"] == "req-sonar-500"
    assert r.json()["error"]["request_id"] == "req-sonar-500"
    assert foreign.status_code == 500
    assert "access-control-allow-origin" not in foreign.headers


# ── the order that makes the above hold ───────────────────────────────────


def test_the_middleware_order_is_pinned():
    """Outermost first. CORS outermost is what puts its header on everything
    the layers inside it make; app/main.py says why each of the rest sits
    where it does. Starlette's ServerErrorMiddleware still wraps all of these,
    which is why the 500 handler stamps CORS by hand."""
    assert [m.cls.__name__ for m in app.user_middleware] == [
        "CORSMiddleware",
        "RequestContextMiddleware",
        "GZipMiddleware",
        "SecurityHeadersMiddleware",
        "OriginLockMiddleware",
        "RateLimitMiddleware",
        "RouteRateLimitMiddleware",
        "BodyLimitMiddleware",
        "ActivityMiddleware",
    ]
