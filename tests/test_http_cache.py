"""app/http_cache.py: validators, conditional GETs and Cache-Control for the read routes.

Driven through a throwaway FastAPI app so the real request/response objects,
and the app's own GZipMiddleware, are what is exercised.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from app import http_cache
from app.services import snapshots

BODY = b'{"items": [' + b",".join(b'{"n": %d}' % i for i in range(400)) + b"]}"


def _snap(age: float = 0.0, source: snapshots.SnapshotSource = "live") -> snapshots.Snapshot[None]:
    return snapshots.encode(None, BODY, time.time() - age, source=source, age_seconds=age)


def _app(snap: snapshots.Snapshot[None], *, cacheable: bool = True) -> TestClient:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/snap")
    async def serve(request: Request) -> Response:
        return http_cache.snapshot_response(
            request, snap, fresh_seconds=15, stale_while_revalidate=60, cacheable=cacheable
        )

    return TestClient(app)


def test_a_snapshot_goes_out_with_its_validators_and_age() -> None:
    snap = _snap(age=5)
    r = _app(snap).get("/snap", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.content == BODY
    assert r.headers["etag"] == snap.etag
    assert r.headers["last-modified"].endswith("GMT")
    assert r.headers["x-snapshot-age"] in ("4", "5")
    assert r.headers["x-snapshot-source"] == "live"
    assert r.headers["content-type"] == "application/json"
    assert "content-encoding" not in r.headers


def test_cache_control_is_the_remaining_freshness_not_the_whole_ttl() -> None:
    r = _app(_snap(age=5)).get("/snap")
    assert r.headers["cache-control"] in (
        "public, max-age=10, s-maxage=10, stale-while-revalidate=60",
        "public, max-age=9, s-maxage=9, stale-while-revalidate=60",
    )


def test_a_snapshot_past_its_freshness_is_max_age_zero() -> None:
    r = _app(_snap(age=600, source="persisted")).get("/snap")
    assert r.headers["cache-control"] == "public, max-age=0, s-maxage=0, stale-while-revalidate=60"
    assert r.headers["x-snapshot-source"] == "persisted"


def test_an_uncacheable_snapshot_says_no_cache() -> None:
    r = _app(_snap(), cacheable=False).get("/snap")
    assert r.headers["cache-control"] == "no-cache"
    assert "etag" in r.headers  # still revalidatable


def test_a_gzip_client_gets_the_prebuilt_gzip_once_compressed() -> None:
    snap = _snap()
    r = _app(snap).get("/snap", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["content-length"] == str(len(snap.gzip_body))
    assert r.content == BODY  # httpx decodes exactly once: the middleware did not compress again
    assert "Accept-Encoding" in r.headers["vary"]


@pytest.mark.parametrize("header", ["br", "identity"])
def test_a_client_that_refuses_gzip_gets_plain_json(header: str) -> None:
    r = _app(_snap()).get("/snap", headers={"Accept-Encoding": header})
    assert "content-encoding" not in r.headers
    assert r.content == BODY


@pytest.mark.parametrize(
    "if_none_match",
    ["{etag}", "{bare}", '"nope", {etag}', "*"],
)
def test_a_matching_if_none_match_is_a_bodiless_304(if_none_match: str) -> None:
    snap = _snap()
    header = if_none_match.format(etag=snap.etag, bare=snap.etag.removeprefix("W/"))
    r = _app(snap).get("/snap", headers={"If-None-Match": header})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == snap.etag
    assert r.headers["cache-control"].startswith("public, max-age=")


def test_a_stale_validator_gets_the_full_body() -> None:
    r = _app(_snap()).get("/snap", headers={"If-None-Match": 'W/"something-else"'})
    assert r.status_code == 200
    assert r.content == BODY


@pytest.mark.parametrize(
    ("header", "accepted"),
    [
        ("gzip, deflate, br", True),
        ("br;q=1.0, gzip;q=0.8", True),
        ("gzip;q=0", False),
        ("gzip;q=bogus", False),
        ("*", True),
        ("", False),
        ("deflate", False),
    ],
)
def test_accepts_gzip_reads_q_values(header: str, accepted: bool) -> None:
    scope = {"type": "http", "headers": [(b"accept-encoding", header.encode())]}
    assert http_cache.accepts_gzip(Request(scope)) is accepted


def test_conditional_json_without_gzip_or_last_modified() -> None:
    app = FastAPI()

    @app.get("/plain")
    async def plain(request: Request) -> Response:
        return http_cache.conditional_json(
            request, body=b"[]", etag='W/"e"', cache_control_value="no-cache", headers={"X-Extra": "1"}
        )

    r = TestClient(app).get("/plain", headers={"Accept-Encoding": "gzip"})
    assert r.content == b"[]"
    assert "content-encoding" not in r.headers
    assert "last-modified" not in r.headers
    assert r.headers["x-extra"] == "1"
