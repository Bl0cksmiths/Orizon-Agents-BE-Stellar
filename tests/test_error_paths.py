"""Error-path behavior: the 500 envelope, non-ASCII auth headers, rate-limit
keying on the trusted proxy hop, and X-Request-ID propagation."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.config import settings
from app.main import app
from app.security import RateLimitMiddleware

CHARGE_BODY = {
    "auth_id_hex": "ab" * 16,
    "amount_usdc": 1.0,
    "job_id_hex": "cd" * 16,
}


def _install_boom_route() -> None:
    """Register a route that always raises, once, on the shared app."""
    if any(getattr(r, "path", None) == "/boom-test" for r in app.routes):
        return

    @app.get("/boom-test", include_in_schema=False)
    async def boom() -> None:
        raise RuntimeError("boom")


def test_forced_500_returns_envelope_with_nosniff():
    _install_boom_route()
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom-test")
    assert r.status_code == 500
    assert r.json() == {
        "detail": "internal server error",
        "error": {
            "code": "internal_error",
            "message": "internal server error",
            "request_id": r.headers["x-request-id"],
        },
    }
    # The global handler runs outside the security-header middleware, so it
    # must stamp the hardening headers itself.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_non_ascii_api_key_is_401_not_500(client, hermetic_settings):
    hermetic_settings.api_key = "secret-key"
    r = client.post(
        "/api/stellar/server/charge",
        json=CHARGE_BODY,
        # Bytes value: Starlette decodes header values latin-1, so this
        # reaches the guard as a non-ASCII str — must be a clean 401, not a
        # compare_digest TypeError turned 500.
        headers={b"x-api-key": "sécret-key".encode("latin-1")},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_api_key"


def test_rate_limiter_keys_on_last_forwarded_hop():
    """Pins the shipped default end-to-end: TRUSTED_PROXY_HOPS=0 drops no
    trailing entries, so the limiter buckets on the LAST hop exactly as it
    always has. tests/test_proxy_trust.py covers the tuned settings; this
    guards the "merging changes nothing" promise through real middleware."""
    assert settings.trusted_proxy_hops == 0

    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/hit", ok)])
    limited = TestClient(RateLimitMiddleware(inner, limit=1, window_seconds=60))

    # Rotating the client-controlled leftmost hop must NOT mint fresh buckets.
    assert limited.get("/hit", headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}).status_code == 200
    assert limited.get("/hit", headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"}).status_code == 429
    # A genuinely different trusted (last) hop gets its own bucket.
    assert limited.get("/hit", headers={"X-Forwarded-For": "1.1.1.1, 8.8.8.8"}).status_code == 200


def test_request_id_echoed_when_supplied(client):
    r = client.get("/", headers={"X-Request-ID": "trace-me-42"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-me-42"


def test_request_id_generated_when_absent(client):
    r = client.get("/")
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    assert len(rid) == 16
    int(rid, 16)  # generated ids are uuid4 hex prefixes


@pytest.mark.parametrize(
    "supplied",
    [
        "has space",
        '"><script>alert(1)</script>',
        "x" * 65,
        "line\\nbreak",
        "café",
        "{rid}%0d%0aSet-Cookie:a=b",
    ],
    ids=["space", "markup", "too-long", "escaped-newline", "non-ascii", "crlf-encoded"],
)
def test_a_request_id_outside_the_safe_shape_is_replaced_not_echoed(client, supplied):
    # The id is echoed in a header, written into every log line of the request
    # and quoted back in every error body, so only a plain token is taken from
    # the caller; anything else gets a fresh id, never a trimmed copy of theirs.
    r = client.get("/", headers={"X-Request-ID": supplied.encode("utf-8")})

    rid = r.headers["x-request-id"]
    assert rid != supplied and supplied[:64] != rid
    assert re.fullmatch(r"[0-9a-f]{16}", rid)


@pytest.mark.parametrize("supplied", ["trace-me-42", "a1b2c3d4e5f60718", "req_01:edge.7", "x" * 64])
def test_a_plain_token_request_id_is_kept(client, supplied):
    assert client.get("/", headers={"X-Request-ID": supplied}).headers["x-request-id"] == supplied
