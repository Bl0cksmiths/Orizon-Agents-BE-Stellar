"""Who a request is, for rate limiting — `security.client_identity`.

On Render the service sits behind Cloudflare and Render's own proxy. Measured
against the live deployment on 2026-10-06 with TRUSTED_PROXY_HOPS=0: the
`x-ratelimit-remaining` counter of one caller's consecutive requests
alternated between two buckets, so the LAST X-Forwarded-For entry — the key
the limiter used — is an address from the platform's own pool, not the
caller's. Every visitor shared a couple of budgets.

`client_identity` walks the chain from the right instead, past the hops our
infrastructure appends — non-public addresses (Render's internal network) and
at most one Cloudflare edge — and takes the first entry nobody downstream of
the caller could have written. It never reads the leftmost entries, which the
caller controls. When no such entry exists it answers None, and callers then
apply no per-client budget at all (per-wallet budgets and service-wide
ceilings still hold) rather than lumping strangers together.

Our own frontend proves itself with FRONTEND_PROXY_TOKEN and names the visitor
in X-Orizon-Client-Ip, because Vercel's egress addresses are shared and
unpublished.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.security import client_identity

TOKEN = "frontend-proxy-token-" + "k" * 24

# Public, globally routable addresses (documentation ranges are not global and
# would read as infrastructure).
VISITOR = "81.2.69.160"
OTHER_VISITOR = "2.125.160.216"
VERCEL_EGRESS = "76.76.21.9"
CLOUDFLARE_EDGE = "172.70.81.12"
RENDER_INTERNAL = "10.201.3.4"


def _scope(forwarded: str | None, *, peer: str | None = "127.0.0.1", headers: dict[str, str] | None = None) -> dict:
    raw = []
    if forwarded is not None:
        raw.append((b"x-forwarded-for", forwarded.encode("latin-1")))
    for name, value in (headers or {}).items():
        raw.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    return {"type": "http", "headers": raw, "client": (peer, 1234) if peer else None}


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(settings, "frontend_proxy_token", TOKEN)
    return TOKEN


# ── behind Render's proxies ─────────────────────────────────────


@pytest.mark.parametrize(
    "chain",
    [
        f"{VISITOR}",
        f"{VISITOR}, {CLOUDFLARE_EDGE}",
        f"{VISITOR}, {RENDER_INTERNAL}",
        f"{VISITOR}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL}",
        f"{VISITOR}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL}, 10.0.0.7",
    ],
    ids=["bare", "cloudflare", "render-internal", "both", "two-internal"],
)
def test_the_visitor_is_found_behind_our_proxies(chain) -> None:
    assert client_identity(_scope(chain)) == VISITOR


def test_a_spoofed_prefix_never_becomes_the_identity() -> None:
    # The caller writes whatever it likes on the LEFT; Cloudflare appends the
    # address it actually saw. Rotating the prefix must not mint new buckets.
    for spoof in ("1.1.1.1", "8.8.8.8, 9.9.9.9", "10.0.0.1", "garbage", "::1"):
        assert client_identity(_scope(f"{spoof}, {VISITOR}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL}")) == VISITOR


def test_two_visitors_behind_the_same_edge_are_two_identities() -> None:
    a = client_identity(_scope(f"{VISITOR}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL}"))
    b = client_identity(_scope(f"{OTHER_VISITOR}, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL}"))

    assert a == VISITOR and b == OTHER_VISITOR


@pytest.mark.parametrize(
    "chain",
    [
        f"{RENDER_INTERNAL}",
        f"{CLOUDFLARE_EDGE}, {RENDER_INTERNAL}",
        # A Cloudflare Worker calling us arrives FROM a Cloudflare address, so
        # the entry left of the edge is one more Cloudflare hop: whatever is
        # left of that is the caller's own writing, so there is no identity.
        f"{VISITOR}, 104.16.0.9, {CLOUDFLARE_EDGE}, {RENDER_INTERNAL}",
        f"not-an-ip, {CLOUDFLARE_EDGE}",
        f"{VISITOR}:443, {CLOUDFLARE_EDGE}",
        " , , ",
    ],
    ids=["internal-only", "edge-only", "worker", "garbage", "with-port", "empty-entries"],
)
def test_no_trustworthy_entry_is_no_identity(chain) -> None:
    assert client_identity(_scope(chain)) is None


def test_without_a_forwarded_header_the_direct_peer_is_the_identity() -> None:
    # Local runs and tests: nothing proxies, so the socket peer is the caller.
    assert client_identity(_scope(None, peer="127.0.0.1")) == "127.0.0.1"
    assert client_identity(_scope(None, peer=None)) is None


def test_the_hop_count_setting_does_not_move_the_identity(monkeypatch) -> None:
    # TRUSTED_PROXY_HOPS keeps its meaning for the access log only; a stale
    # dashboard value can no longer make the limiter read a caller-written entry.
    monkeypatch.setattr(settings, "trusted_proxy_hops", 3)

    assert client_identity(_scope(f"1.1.1.1, 8.8.8.8, {VISITOR}, {CLOUDFLARE_EDGE}")) == VISITOR


# ── our frontend ────────────────────────────────────────────────


def test_the_frontend_names_its_visitor(token) -> None:
    scope = _scope(
        f"{VERCEL_EGRESS}, {CLOUDFLARE_EDGE}",
        headers={"x-frontend-proxy-token": token, "x-orizon-client-ip": VISITOR},
    )

    assert client_identity(scope) == VISITOR


def test_many_visitors_behind_the_frontend_are_many_identities(token) -> None:
    ids = {
        client_identity(
            _scope(
                f"{VERCEL_EGRESS}, {CLOUDFLARE_EDGE}",
                headers={"x-frontend-proxy-token": token, "x-orizon-client-ip": f"81.2.69.{i}"},
            )
        )
        for i in range(1, 51)
    }

    assert len(ids) == 50


def test_a_frontend_read_with_no_visitor_has_no_identity(token) -> None:
    # A cached route handler's fetch serves everyone; no per-client budget.
    scope = _scope(f"{VERCEL_EGRESS}, {CLOUDFLARE_EDGE}", headers={"x-frontend-proxy-token": token})

    assert client_identity(scope) is None


@pytest.mark.parametrize("supplied", ["wrong-token-" + "x" * 30, "", TOKEN + "x", TOKEN[:-1]])
def test_without_the_right_token_the_visitor_header_is_ignored(token, supplied) -> None:
    scope = _scope(
        f"{VISITOR}, {CLOUDFLARE_EDGE}",
        headers={"x-frontend-proxy-token": supplied, "x-orizon-client-ip": OTHER_VISITOR},
    )

    assert client_identity(scope) == VISITOR


def test_with_no_token_configured_nobody_is_the_frontend(monkeypatch) -> None:
    monkeypatch.setattr(settings, "frontend_proxy_token", "")
    scope = _scope(
        f"{VISITOR}, {CLOUDFLARE_EDGE}", headers={"x-frontend-proxy-token": "", "x-orizon-client-ip": OTHER_VISITOR}
    )

    assert client_identity(scope) == VISITOR


@pytest.mark.parametrize("named", ["10.0.0.1", "127.0.0.1", "not-an-ip", CLOUDFLARE_EDGE, ""])
def test_the_frontend_cannot_name_an_infrastructure_address(token, named) -> None:
    scope = _scope(
        f"{VERCEL_EGRESS}, {CLOUDFLARE_EDGE}",
        headers={"x-frontend-proxy-token": token, "x-orizon-client-ip": named},
    )

    assert client_identity(scope) is None
