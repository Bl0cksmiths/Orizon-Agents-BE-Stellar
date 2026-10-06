"""The hardening headers every response carries, errors and short-circuits included.

Two were missing. HSTS: the middleware's own comment said Render's edge sets
it, and a live response from the deployment (2026-10-06) carries none. And a
Content-Security-Policy: this API renders nothing, so `default-src 'none'`
costs it nothing and turns any response a browser is tricked into rendering
— an error body echoing input, an artifact — into inert text. The docs pages
are the one exception, because Swagger UI and ReDoc are pages that load
scripts; they keep `frame-ancestors 'none'` so they cannot be framed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

API_CSP = "default-src 'none'; frame-ancestors 'none'"
HSTS = "max-age=63072000; includeSubDomains"


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _assert_hardened(headers, *, csp: str = API_CSP) -> None:
    assert headers["strict-transport-security"] == HSTS
    assert headers["content-security-policy"] == csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"


def test_an_api_response_carries_every_header(client) -> None:
    r = client.get("/api/agents")

    assert r.status_code == 200
    _assert_hardened(r.headers)


@pytest.mark.parametrize("path", ["/api/agents/nope", "/no-such-route"])
def test_an_error_response_carries_every_header(client, path) -> None:
    r = client.get(path)

    assert r.status_code == 404
    _assert_hardened(r.headers)


def test_a_413_short_circuit_carries_every_header(client) -> None:
    r = client.post("/api/orchestrator/decompose", content=b"x" * 2_000_000)

    assert r.status_code == 413
    _assert_hardened(r.headers)


def test_the_500_handler_carries_every_header() -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/boom-headers-test")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    app.include_router(router)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/boom-headers-test")
    finally:
        app.router.routes[:] = [rt for rt in app.router.routes if getattr(rt, "path", "") != "/boom-headers-test"]

    assert r.status_code == 500
    assert "kaboom" not in r.text
    _assert_hardened(r.headers)


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_the_docs_pages_can_still_load_their_scripts(client, path) -> None:
    r = client.get(path)

    assert r.status_code == 200
    _assert_hardened(r.headers, csp="frame-ancestors 'none'")
