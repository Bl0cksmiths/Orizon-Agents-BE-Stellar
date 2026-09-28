"""The readiness probe (story 5.02): what it fetches, what guards it, and what
it is allowed to say about the answer.

The probe is the one part of the readiness check that makes our server send a
request, so these tests pin it from three sides:

  * it fetches the bound URL and nothing else, through the dispatch path's own
    pinned transport — the SSRF guard is reused, not re-implemented, and a host
    that resolves into a blocked range is never dialled;
  * its classification of a failure is taken from real sockets where the
    distinction is the whole point (refused vs TLS vs timeout);
  * its result carries a coarse outcome and a status code — never a URL, a
    header or a body byte.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.agents.workers import external_http
from app.services import operator_readiness as readiness
from app.services.endpoint_policy import EndpointPolicyError

SECRET = "s3cr3t-probe-token"
BOUND = f"https://agent.example/run?token={SECRET}"
PUBLIC_V4 = "93.184.216.34"


def _pin_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[str]:
    """Answer the pinned transport's resolve-and-check with `addresses`."""
    asked: list[str] = []

    async def _resolve(host: str) -> tuple[str, ...]:
        asked.append(host)
        return addresses

    monkeypatch.setattr(external_http, "resolve_checked_addresses", _resolve)
    return asked


def _respond_with(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Put an in-process responder UNDER the real pin, and record what reaches it."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(readiness, "_inner_transport", lambda: httpx.MockTransport(_record))
    return seen


def _probe(url: str = BOUND) -> readiness.ProbeResult:
    return asyncio.run(readiness.probe_bound_endpoint(url))


def _assert_coarse(result: readiness.ProbeResult) -> None:
    """Nothing but an outcome, a status code and a policy rule name."""
    assert set(vars(result)) == {"outcome", "status_code", "rule"}
    text = repr(result)
    assert "agent.example" not in text
    assert SECRET not in text


# --- what is fetched ----------------------------------------------------------


def test_probe_fetches_the_bound_url_through_the_pinned_address(monkeypatch):
    asked = _pin_to(monkeypatch, PUBLIC_V4)
    seen = _respond_with(monkeypatch, lambda _r: httpx.Response(200, text="ignored body"))

    result = _probe()

    assert result == readiness.ProbeResult("ok", status_code=200)
    assert asked == ["agent.example"]  # the bound host, resolved by the guard
    [request] = seen
    assert request.method == "GET"
    # Dialled at the address the guard checked, never a name left to re-resolve…
    assert request.url.host == PUBLIC_V4
    assert request.url.scheme == "https"
    # …with the bound path and query exactly, and the name kept for TLS and vhosting.
    assert request.url.raw_path == f"/run?token={SECRET}".encode()
    assert request.extensions["sni_hostname"] == "agent.example"
    assert request.headers["host"] == "agent.example"
    assert request.headers["accept-encoding"] == "identity"
    _assert_coarse(result)


def test_probe_reuses_the_dispatch_ssrf_guard_and_never_dials_a_blocked_address(monkeypatch):
    """No resolver stub here: the REAL resolve-and-check runs against a loop
    whose DNS answers with a private address, and refuses before any request."""
    seen = _respond_with(monkeypatch, lambda _r: httpx.Response(200))

    async def _run() -> readiness.ProbeResult:
        async def _getaddrinfo(host: str, port: Any, **_kw: Any) -> list[tuple[Any, ...]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 0))]

        asyncio.get_running_loop().getaddrinfo = _getaddrinfo  # type: ignore[method-assign]
        return await readiness.probe_bound_endpoint(BOUND)

    result = asyncio.run(_run())

    assert result == readiness.ProbeResult("endpoint_refused", rule="non_public_address")
    assert seen == []
    _assert_coarse(result)


def test_the_probe_goes_through_the_pinned_transport_with_the_dispatch_connect_bound(monkeypatch):
    monkeypatch.setattr(readiness, "PROBE_TIMEOUT_SECONDS", 9.0)  # apart from the connect bound
    transport = readiness._probe_transport()
    try:
        assert isinstance(transport, external_http._PinnedAddressTransport)
    finally:
        asyncio.run(transport.aclose())
    timeout = readiness._probe_request(BOUND).extensions["timeout"]
    assert timeout["connect"] == external_http.CONNECT_TIMEOUT_SECONDS
    assert timeout["read"] == 9.0


def test_the_probe_logs_nothing_that_names_the_endpoint(monkeypatch, caplog):
    """httpx.AsyncClient logs "HTTP Request: GET <full URL>" at INFO on every
    request; the probe must not be the thing that writes a bound URL's
    query-string credential into our log."""
    caplog.set_level(logging.DEBUG)
    _pin_to(monkeypatch, PUBLIC_V4)
    _respond_with(monkeypatch, lambda _r: httpx.Response(200))

    assert _probe().outcome == "ok"
    assert SECRET not in caplog.text
    assert "agent.example" not in caplog.text
    assert PUBLIC_V4 not in caplog.text


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("http://agent.example/run", "scheme_not_https"),
        ("https://169.254.169.254/latest/meta-data", "non_public_address"),
        ("https://localhost/run", "loopback_host"),
        ("https://metadata.google.internal/", "metadata_host"),
    ],
)
def test_a_stored_url_that_fails_the_policy_today_is_never_fetched(monkeypatch, url, rule):
    """The stored URL passed the policy at bind time; the rules may have
    tightened since, so it is judged again before anything is resolved."""
    asked = _pin_to(monkeypatch, PUBLIC_V4)
    seen = _respond_with(monkeypatch, lambda _r: httpx.Response(200))

    assert _probe(url) == readiness.ProbeResult("endpoint_refused", rule=rule)
    assert asked == []
    assert seen == []


def test_a_redirect_is_reported_not_followed(monkeypatch):
    _pin_to(monkeypatch, PUBLIC_V4)
    seen = _respond_with(
        monkeypatch, lambda _r: httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})
    )

    assert _probe() == readiness.ProbeResult("http_status", status_code=302)
    assert len(seen) == 1


@pytest.mark.parametrize("code", [201, 204, 299])
def test_any_2xx_is_ok(monkeypatch, code):
    _pin_to(monkeypatch, PUBLIC_V4)
    _respond_with(monkeypatch, lambda _r: httpx.Response(code))

    assert _probe() == readiness.ProbeResult("ok", status_code=code)


@pytest.mark.parametrize("code", [301, 403, 404, 405, 500, 502, 530])
def test_a_non_2xx_is_its_status_code_and_nothing_else(monkeypatch, code):
    _pin_to(monkeypatch, PUBLIC_V4)
    _respond_with(
        monkeypatch,
        lambda _r: httpx.Response(code, text=f"stack trace mentioning {BOUND}", headers={"x-debug": BOUND}),
    )

    result = _probe()

    assert result == readiness.ProbeResult("http_status", status_code=code)
    _assert_coarse(result)


def test_an_unresolvable_host_is_its_own_outcome(monkeypatch):
    async def _gone(host: str) -> tuple[str, ...]:
        raise EndpointPolicyError("unresolvable_host", f"host {host!r} could not be resolved")

    monkeypatch.setattr(external_http, "resolve_checked_addresses", _gone)

    assert _probe() == readiness.ProbeResult("unresolvable", rule="unresolvable_host")


# --- failure classification, on real loopback sockets --------------------------
#
# The pin is pointed at 127.0.0.1 (the resolver stub stands in for DNS), so the
# real httpx transport dials a real socket and the exception chain under test
# is the one production sees.


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_a_closed_port_is_connection_refused(monkeypatch):
    _pin_to(monkeypatch, "127.0.0.1")

    assert _probe(f"https://agent.example:{_free_port()}/run") == readiness.ProbeResult("connection_refused")


def test_a_server_that_does_not_speak_tls_is_a_tls_error(monkeypatch):
    _pin_to(monkeypatch, "127.0.0.1")

    async def _run() -> readiness.ProbeResult:
        async def _plain_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(_plain_http, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await readiness.probe_bound_endpoint(f"https://agent.example:{port}/run")

    assert asyncio.run(_run()) == readiness.ProbeResult("tls_error")


def test_a_tls_handshake_that_never_completes_is_a_timeout(monkeypatch):
    """httpx bounds the handshake with its connect timeout and raises its own
    ConnectTimeout, which is not an asyncio TimeoutError."""
    _pin_to(monkeypatch, "127.0.0.1")
    monkeypatch.setattr(readiness, "PROBE_CONNECT_TIMEOUT_SECONDS", 0.3)

    async def _run() -> tuple[readiness.ProbeResult, float]:
        async def _silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(10)

        server = await asyncio.start_server(_silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            result = await readiness.probe_bound_endpoint(f"https://agent.example:{port}/run")
        finally:
            server.close()
        return result, loop.time() - started

    result, elapsed = asyncio.run(_run())

    assert result == readiness.ProbeResult("timeout")
    assert elapsed < 2.0


def test_the_whole_probe_is_bounded_even_where_httpx_timeouts_are_not(monkeypatch):
    """httpx's timeouts are idle gaps, not a total; an endpoint that trickles
    never trips them. The probe's own deadline is what caps it."""
    _pin_to(monkeypatch, PUBLIC_V4)
    monkeypatch.setattr(readiness, "PROBE_TIMEOUT_SECONDS", 0.2)

    async def _never(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    monkeypatch.setattr(readiness, "_inner_transport", lambda: httpx.MockTransport(_never))

    async def _run() -> tuple[readiness.ProbeResult, float]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await readiness.probe_bound_endpoint(BOUND)
        return result, loop.time() - started

    result, elapsed = asyncio.run(_run())

    assert result == readiness.ProbeResult("timeout")
    assert elapsed < 2.0


# --- the quick-tunnel flag ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://random-words-here.trycloudflare.com/run", True),
        ("https://RANDOM.TRYCLOUDFLARE.COM./run", True),
        ("https://trycloudflare.com.attacker.example/run", False),
        ("https://my-agent.onrender.com/run", False),
        ("https://[::1/run", False),  # unparseable is not a tunnel, and does not raise
    ],
)
def test_quick_tunnel_detection(url, expected):
    assert readiness.is_quick_tunnel(url) is expected
