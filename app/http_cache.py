"""HTTP caching for the public read routes: validators, conditional GETs, Cache-Control.

Two audiences read these headers. Vercel's CDN sits in front of every browser
request (the frontend rewrites `/api/*` to this service) and caches a response
for `s-maxage` seconds, then serves it for `stale-while-revalidate` more while
it fetches a new one in the background — so one origin read answers every
visitor in that window. Any client that keeps a copy can revalidate it with
`If-None-Match` and get a bodiless 304 when nothing changed.

`max-age`/`s-maxage` are the snapshot's REMAINING freshness, never the full TTL:
a cache must not hold a snapshot past the point the service itself would stop
calling it fresh. A response that must not be cached at all — a registry mirror
still filling after a restart, whose counts are a prefix of the registry —
says `no-cache`: a cache may keep it but has to revalidate it on every use.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from email.utils import formatdate
from typing import Any

from fastapi import Request, Response

from .services.snapshots import Snapshot

JSON = "application/json"

# Read by a cross-origin client only when listed in CORS `expose_headers`.
SNAPSHOT_AGE_HEADER = "X-Snapshot-Age"
SNAPSHOT_SOURCE_HEADER = "X-Snapshot-Source"


def cache_control(*, fresh_for: float, stale_while_revalidate: int, cacheable: bool = True) -> str:
    """`public, max-age=N, s-maxage=N, stale-while-revalidate=M`, or `no-cache`."""
    if not cacheable:
        return "no-cache"
    seconds = max(0, math.floor(fresh_for))
    return f"public, max-age={seconds}, s-maxage={seconds}, stale-while-revalidate={stale_while_revalidate}"


def etag_matches(request: Request, etag: str) -> bool:
    """RFC 9110 weak comparison of `If-None-Match` against `etag`."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    if header.strip() == "*":
        return True
    ours = etag.removeprefix("W/")
    return any(candidate.strip().removeprefix("W/") == ours for candidate in header.split(","))


def accepts_gzip(request: Request) -> bool:
    """Whether the client listed gzip with a non-zero q-value."""
    for part in request.headers.get("accept-encoding", "").split(","):
        coding, _, params = part.strip().partition(";")
        if coding.strip().lower() not in ("gzip", "*"):
            continue
        q = params.strip()
        if q.startswith("q="):
            try:
                return float(q[2:]) > 0
            except ValueError:
                return False
        return True
    return False


def conditional_json(
    request: Request,
    *,
    body: bytes,
    etag: str,
    cache_control_value: str,
    gzip_body: bytes | None = None,
    last_modified: float | None = None,
    headers: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """A JSON response that honours `If-None-Match`, with its validators set.

    `gzip_body`, when given, is sent to a client that accepts gzip with
    `Content-Encoding: gzip`; the app's GZipMiddleware passes an already-encoded
    response through untouched, so the body is never compressed twice.
    """
    out = {
        "ETag": etag,
        "Cache-Control": cache_control_value,
        "Vary": "Accept-Encoding",
        **(headers or {}),
    }
    if last_modified is not None:
        out["Last-Modified"] = formatdate(last_modified, usegmt=True)
    if etag_matches(request, etag):
        return Response(status_code=304, headers=out)
    if gzip_body is not None and accepts_gzip(request):
        out["Content-Encoding"] = "gzip"
        return Response(content=gzip_body, status_code=status_code, media_type=JSON, headers=out)
    return Response(content=body, status_code=status_code, media_type=JSON, headers=out)


def snapshot_response(
    request: Request,
    snap: Snapshot[Any],
    *,
    fresh_seconds: float,
    stale_while_revalidate: int,
    cacheable: bool = True,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Serve a snapshot: its pre-encoded body, ETag, Last-Modified and age."""
    age = snap.age_seconds()
    return conditional_json(
        request,
        body=snap.body,
        gzip_body=snap.gzip_body,
        etag=snap.etag,
        last_modified=snap.generated_at,
        cache_control_value=cache_control(
            fresh_for=fresh_seconds - age, stale_while_revalidate=stale_while_revalidate, cacheable=cacheable
        ),
        headers={
            SNAPSHOT_AGE_HEADER: str(math.floor(age)),
            SNAPSHOT_SOURCE_HEADER: snap.source,
            **(headers or {}),
        },
    )
