"""The X-Forwarded-For trust boundary: which entry of the chain becomes the
rate-limit bucket and the access log's `client=`.

`TRUSTED_PROXY_HOPS` is the only knob, it cannot be inferred from outside the
deployment, and both ways of setting it wrong are silent — too low and every
visitor shares one global budget, too high and any caller can mint a fresh
bucket per request. These tests pin the shipped default (unchanged behaviour),
the resolution for each tuned value, and that a malformed or over-short chain
degrades toward the safe side rather than toward a bypass.
"""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.config import settings
from app.security import (
    CHAIN_TOO_SHORT_KEY,
    RateLimitMiddleware,
    client_key,
    forwarded_chain_sampler,
)


def _scope(forwarded: str | None = None, peer: str | None = "127.0.0.1") -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode("latin-1")))
    scope: dict = {"type": "http", "headers": headers}
    if peer is not None:
        scope["client"] = (peer, 12345)
    return scope


# A plausible production chain: what the visitor sent, the visitor's own
# address as the first proxy saw it, Vercel's egress, then Render's edge.
CHAIN = "203.0.113.50, 198.51.100.7, 76.76.21.9, 10.201.3.4"


def test_default_setting_is_zero_hops():
    """The default must reproduce the pre-existing behaviour exactly, so that
    merging the setting changes nothing until it is deliberately tuned."""
    assert settings.trusted_proxy_hops == 0


def test_default_resolves_the_last_hop():
    assert client_key(_scope(CHAIN)) == "10.201.3.4"


@pytest.mark.parametrize(
    ("hops", "expected"),
    [
        (0, "10.201.3.4"),  # today: the entry our own edge appended
        (1, "76.76.21.9"),  # skip Render's edge
        (2, "198.51.100.7"),  # skip Render + Vercel — the real visitor
        (3, "203.0.113.50"),  # one hop too far: now caller-controlled
    ],
)
def test_each_hop_count_resolves_the_expected_entry(hops, expected):
    assert client_key(_scope(CHAIN), hops=hops) == expected


def test_hop_count_is_read_from_settings(monkeypatch):
    """The middleware passes no explicit hop count, so the live setting — not
    an import-time snapshot — is what production resolution follows."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert client_key(_scope(CHAIN)) == "198.51.100.7"


def test_spoofed_leading_entries_cannot_influence_the_key_at_the_default():
    """A caller controls everything to the LEFT of the address our edge saw.
    At the default they may rotate it freely without ever moving the key."""
    keys = {
        client_key(_scope(f"{spoof}, 198.51.100.7, 76.76.21.9, 10.201.3.4"))
        for spoof in ("1.1.1.1", "2.2.2.2", "not-an-ip", "", "   ")
    }
    assert keys == {"10.201.3.4"}


def test_spoofed_entries_cannot_reach_the_key_at_the_intended_hop_count():
    """The same guarantee has to survive tuning: with two infrastructure hops
    trusted, injected entries stay to the left of the resolved visitor."""
    for spoof in ("1.1.1.1", "2.2.2.2", "9.9.9.9"):
        scope = _scope(f"{spoof}, 198.51.100.7, 76.76.21.9, 10.201.3.4")
        assert client_key(scope, hops=2) == "198.51.100.7"


def test_short_chain_resolves_to_the_sentinel_not_the_leftmost_entry():
    """Too-high a hop count must fail loudly-but-safely. Clamping to index 0
    would hand the key to the caller — the exact bypass the setting exists to
    avoid — so a chain with nothing left to resolve gets a literal instead."""
    assert client_key(_scope("198.51.100.7, 10.201.3.4"), hops=2) == CHAIN_TOO_SHORT_KEY
    assert client_key(_scope("10.201.3.4"), hops=5) == CHAIN_TOO_SHORT_KEY
    # The sentinel is not an address, so it can never collide with a client.
    assert CHAIN_TOO_SHORT_KEY.count(".") == 0


def test_malformed_chains_do_not_crash_and_fall_back_safely():
    # Empty entries are dropped rather than selected; a trailing comma must
    # not push resolution onto scope["client"], which uvicorn rewrites to the
    # caller-supplied LEFTMOST entry under --forwarded-allow-ips='*'.
    assert client_key(_scope("198.51.100.7, 10.201.3.4,")) == "10.201.3.4"
    assert client_key(_scope("198.51.100.7,,, 10.201.3.4")) == "10.201.3.4"
    assert client_key(_scope("  ,  198.51.100.7 ,  10.201.3.4  ")) == "10.201.3.4"
    # Nothing usable in the header at all → the transport peer, as before.
    assert client_key(_scope(" , , ")) == "127.0.0.1"
    assert client_key(_scope("")) == "127.0.0.1"
    # Header absent, and then no peer either.
    assert client_key(_scope(None)) == "127.0.0.1"
    assert client_key(_scope(None, peer=None)) == "unknown"
    # Non-address junk is keyed on verbatim rather than raising.
    assert client_key(_scope("a, b, ../../etc/passwd")) == "../../etc/passwd"


def test_negative_hop_count_is_floored_not_wrapped(monkeypatch):
    """A negative value would index from the caller-controlled end of the
    chain; it degrades to the default instead."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", -3)
    assert client_key(_scope(CHAIN)) == "10.201.3.4"
    assert client_key(_scope(CHAIN), hops=-1) == "10.201.3.4"


def _limited_client(limit: int = 1) -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/hit", ok)])
    return TestClient(RateLimitMiddleware(inner, limit=limit, window_seconds=60))


# The limiter keys on `client_identity`, not on the hop count above: Render's
# own hops (non-public, then one Cloudflare edge) are recognised by address.
_EDGE = "172.70.81.12, 10.201.3.4"


@pytest.mark.parametrize("hops", [0, 2])
def test_visitors_behind_our_edge_get_their_own_buckets_whatever_the_hop_count(monkeypatch, hops):
    """The point of the identity: two visitors behind Render's proxies stop
    sharing one budget, with no tuning — and no TRUSTED_PROXY_HOPS value can
    make the limiter read an entry the caller wrote."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", hops)
    limited = _limited_client()
    assert limited.get("/hit", headers={"X-Forwarded-For": f"1.1.1.1, 81.2.69.160, {_EDGE}"}).status_code == 200
    # Same visitor, rotated spoof prefix — still the same bucket.
    assert limited.get("/hit", headers={"X-Forwarded-For": f"2.2.2.2, 81.2.69.160, {_EDGE}"}).status_code == 429
    # A different visitor behind the same edge gets its own.
    assert limited.get("/hit", headers={"X-Forwarded-For": f"1.1.1.1, 2.125.160.216, {_EDGE}"}).status_code == 200


def test_requests_nobody_can_be_told_apart_share_only_the_service_ceiling(monkeypatch):
    """A chain of infrastructure alone names nobody. Those requests are held by
    one service-wide ceiling bucket rather than by a per-client budget, and
    never spend an identified visitor's."""
    limited = _limited_client()
    assert limited.get("/hit", headers={"X-Forwarded-For": _EDGE}).status_code == 200
    assert limited.get("/hit", headers={"X-Forwarded-For": "10.0.0.9"}).status_code == 429
    assert limited.get("/hit", headers={"X-Forwarded-For": f"81.2.69.160, {_EDGE}"}).status_code == 200


# ── the forwarded-chain sample ─────────────────────────────────────────
# How the hop count above is meant to be determined: a bounded dump of the raw
# chain into Render's log stream, rather than a diagnostic route that would
# expose visitors' addresses to anyone.


@pytest.fixture()
def armed_sampler():
    """Re-arm the process-wide sampler for one test and disarm it after, so
    the budget cannot leak into (or be spent by) another test."""

    def arm(budget: int) -> None:
        forwarded_chain_sampler.reset(budget)

    yield arm
    forwarded_chain_sampler.reset(0)


def _sample_lines(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if "forwarded chain sample" in rec.getMessage()]


def test_sample_reports_the_raw_chain_and_the_resolved_key(client, armed_sampler, caplog):
    armed_sampler(1)
    with caplog.at_level(logging.INFO, logger="app.security"):
        r = client.get("/api/agents", headers={"X-Forwarded-For": CHAIN})
    assert r.status_code == 200

    lines = _sample_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    # Every entry of the chain, so the operator can COUNT the trailing hops.
    for entry in CHAIN.split(", "):
        assert entry in line
    assert "entries=4" in line
    # And what the current setting makes of it, so the two can be compared.
    assert f"TRUSTED_PROXY_HOPS={settings.trusted_proxy_hops}" in line
    assert "client=10.201.3.4" in line
    # And the identity the rate limiters actually key on, read past our own
    # hops by address — here the first public entry from the right.
    assert "identity=76.76.21.9" in line


def test_sample_stops_after_the_budget(client, armed_sampler, caplog):
    """Cheap by construction: a 512 MB shared-CPU instance must not pay for
    this, and visitor addresses must not accumulate in the log."""
    armed_sampler(2)
    with caplog.at_level(logging.INFO, logger="app.security"):
        for _ in range(5):
            assert client.get("/api/agents", headers={"X-Forwarded-For": CHAIN}).status_code == 200

    lines = _sample_lines(caplog)
    assert len(lines) == 2
    assert "sample 1/2" in lines[0]
    assert "sample 2/2" in lines[1]


def test_sampling_is_off_when_the_budget_is_zero(client, armed_sampler, caplog):
    armed_sampler(0)
    with caplog.at_level(logging.INFO, logger="app.security"):
        assert client.get("/api/agents", headers={"X-Forwarded-For": CHAIN}).status_code == 200
    assert _sample_lines(caplog) == []


def test_probe_paths_never_spend_the_budget(client, armed_sampler, caplog):
    """Render's health checker and uptime monitors reach the exempt paths from
    a different chain than a browser does; letting them consume the sample
    would answer the wrong question."""
    armed_sampler(2)
    with caplog.at_level(logging.INFO, logger="app.security"):
        for path in ("/health", "/api/health", "/readiness", "/"):
            client.get(path, headers={"X-Forwarded-For": CHAIN})
        assert _sample_lines(caplog) == []
        # The budget is intact for the first request that actually matters.
        assert client.get("/api/agents", headers={"X-Forwarded-For": CHAIN}).status_code == 200
    assert len(_sample_lines(caplog)) == 1


def test_sample_is_correlated_with_the_access_line(client, armed_sampler, caplog):
    """Both lines carry the same request id, so the raw chain and the key it
    produced can be read together in Render's log viewer."""
    armed_sampler(1)
    with caplog.at_level(logging.INFO, logger="app.security"):
        client.get("/api/agents", headers={"X-Forwarded-For": CHAIN, "X-Request-ID": "sample-rid-1"})

    tagged = [rec for rec in caplog.records if getattr(rec, "request_id", "-") == "sample-rid-1"]
    messages = [rec.getMessage() for rec in tagged]
    assert any("forwarded chain sample" in m for m in messages)
    assert any("GET /api/agents ->" in m for m in messages)


def test_sample_is_taken_even_when_the_request_is_throttled(armed_sampler, caplog):
    """The sample is taken before dispatch, so a service that is already 429ing
    everyone — the failure mode that prompts the investigation — still reports
    the chain that caused it."""
    from app.security import RequestContextMiddleware

    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/hit", ok)])
    stack = TestClient(RequestContextMiddleware(RateLimitMiddleware(inner, limit=1, window_seconds=60)))

    armed_sampler(2)
    with caplog.at_level(logging.INFO, logger="app.security"):
        assert stack.get("/hit", headers={"X-Forwarded-For": CHAIN}).status_code == 200
        assert stack.get("/hit", headers={"X-Forwarded-For": CHAIN}).status_code == 429

    assert len(_sample_lines(caplog)) == 2
