"""Two ways a dispatch could still be turned against us after story 2.03.

1. DNS REBINDING. The SSRF rules were enforced against the URL as written on
   every dispatch, and against the RESOLVED addresses only at bind time. So an
   operator could bind `https://evil.example/run` while its A record was
   public, wait for the bind to be recorded, and repoint the name at
   169.254.169.254 or at a 10/8 neighbour. Every dispatch after that carried a
   signed envelope — the buyer's intent and every prior agent's output — to
   that address from inside our own network. These tests drive the real worker
   with the resolver replaced, and pin both halves of the fix: the address
   check now runs per dispatch, and the connect goes to the address that was
   checked rather than to a name the transport would resolve a second time.

2. THE SIZE CAP WAS APPLIED AFTER DECOMPRESSION. httpx offers gzip by default
   and counts only what it has already decoded, so ~200 KB of gzip expanding to
   200 MiB was refused as `oversize_response` — after a measured 142 MiB traced
   peak over a real socket, which on a 512 MB instance is enough to take out
   other buyers' in-flight settlements. The dispatch now asks for `identity`
   and refuses any other content-coding before reading the body, and the cap
   counts bytes as received.

Hermetic: no socket is ever opened and no name is ever really resolved. The
resolver is replaced on the running loop (the house idiom — see
tests/test_endpoint_policy.py) and every transport answers in process.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.agents.workers import external_http as eh
from app.services.endpoint_policy import EndpointPolicyError, resolve_and_check, resolve_checked_addresses

# A credential in the query string is an ordinary way to write an operator
# endpoint, and execution_svc logs an ExternalDispatchError's message verbatim.
# Every refusal below is checked for it.
SECRET = "s3cr3t-bearer-token"
ENDPOINT = f"https://operator.example/dispatch?token={SECRET}"
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _addrinfo(*addresses: str) -> list[tuple[Any, ...]]:
    """getaddrinfo's 5-tuples for `addresses`, v4 or v6 by their spelling."""
    infos: list[tuple[Any, ...]] = []
    for address in addresses:
        if ":" in address:
            infos.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0, 0, 0)))
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0)))
    return infos


class _Resolver:
    """A scripted stand-in for `loop.getaddrinfo`, recording what it was asked.

    `answers` is consumed one query at a time and the last entry repeats, which
    is what lets one resolver play a name that was public when it was bound and
    private by the time it is dispatched to.
    """

    def __init__(self, *answers: list[tuple[Any, ...]], error: BaseException | None = None) -> None:
        self.answers = list(answers)
        self.error = error
        self.hosts: list[str] = []

    async def __call__(self, host: str, port: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        self.hosts.append(host)
        if self.error is not None:
            raise self.error
        return self.answers[min(len(self.hosts), len(self.answers)) - 1]


def _install(resolver: _Resolver) -> None:
    """Replace DNS for the running loop only.

    On the loop instance, not the asyncio module: every `asyncio.run` builds a
    fresh loop and closes it on the way out, so nothing leaks into another test.
    """
    asyncio.get_running_loop().getaddrinfo = resolver  # type: ignore[method-assign]


def _worker(endpoint: str = ENDPOINT, *, transport: httpx.AsyncBaseTransport | None = None) -> eh.ExternalHttpWorker:
    """A worker that builds its own client unless `transport` is supplied.

    The no-client form is the production wiring: `binding_registry.resolve_worker`
    constructs the worker exactly this way, so a refusal it produces proves the
    pinned transport is actually installed on the dispatch path rather than
    merely importable. `transport` is for the cases that must let a dispatch
    through to inspect what went on the wire.
    """
    client = None if transport is None else httpx.AsyncClient(transport=transport)
    return eh.ExternalHttpWorker("ext_demo1", "external.demo", endpoint, client=client)


def _pinned(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncBaseTransport:
    """The real pinned transport in front of an in-process responder."""
    return eh._PinnedAddressTransport(httpx.MockTransport(handler))


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"summary": "built it"})


async def _failing(worker: eh.ExternalHttpWorker) -> eh.ExternalDispatchError:
    """Drive one real dispatch and hand back the error it failed with."""
    with pytest.raises(eh.ExternalDispatchError) as caught:
        await worker.run("build it", "because")
    return caught.value


def _assert_leaks_nothing(error: eh.ExternalDispatchError, endpoint: str = ENDPOINT) -> None:
    """No refusal that leaves this module may carry the endpoint URL.

    `execution_svc` logs the message verbatim, and an operator endpoint's query
    string is an ordinary place to find a bearer token.
    """
    message = str(error)
    assert SECRET not in message
    assert endpoint not in message
    assert "token=" not in message


# --- 1. the address check now runs per dispatch ------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback — this process' own admin surface
        "10.0.0.5",  # private — Render's internal network
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local — the cloud metadata service
        "fe80::1",  # v6 link-local
        "::1",  # v6 loopback
    ],
)
def test_a_host_resolving_into_a_blocked_range_is_refused_at_dispatch(address: str) -> None:
    # The URL is spelled with an ordinary public-looking name, so the pure
    # check has nothing to object to; only resolving it finds the problem.
    resolver = _Resolver(_addrinfo(address))

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker())

    error = asyncio.run(go())

    assert error.rule == "endpoint_refused"
    assert address in str(error)  # the refusal names WHICH address
    assert resolver.hosts == ["operator.example"]  # asked once: a refusal is not retried


def test_an_endpoint_public_at_bind_time_and_private_at_dispatch_is_refused() -> None:
    """The actual attack, start to finish, against the real code on both sides.

    The bind API's `resolve_and_check` sees a public answer and records the
    binding. The name is then repointed — a one-second TTL is all it takes —
    and the dispatch that follows must not go there. Before the fix the
    dispatch re-ran only the URL check, which cannot see a DNS answer at all,
    so this connected to the metadata service with a signed envelope.
    """
    resolver = _Resolver(_addrinfo(PUBLIC_V4), _addrinfo("169.254.169.254"))

    async def go() -> tuple[tuple[str, ...], eh.ExternalDispatchError]:
        _install(resolver)
        bound = await resolve_and_check(ENDPOINT)  # bind time: passes, as it did then
        return bound, await _failing(_worker())  # dispatch time: must not

    bound, error = asyncio.run(go())

    assert bound == (PUBLIC_V4,)  # the binding really was accepted
    assert error.rule == "endpoint_refused"
    assert "169.254.169.254" in str(error)
    assert resolver.hosts == ["operator.example", "operator.example"]


def test_one_blocked_address_among_public_ones_still_refuses_the_dispatch() -> None:
    # A public A record in front of a metadata-service one is the obvious dodge:
    # the connect may use either, so one blocked address refuses the whole host.
    resolver = _Resolver(_addrinfo(PUBLIC_V4, "169.254.169.254"))

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker())

    error = asyncio.run(go())

    assert error.rule == "endpoint_refused"
    assert "169.254.169.254" in str(error)


def test_a_host_that_stops_resolving_fails_the_step_rather_than_crashing() -> None:
    resolver = _Resolver(error=socket.gaierror(-2, "Name or service not known"))

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker())

    error = asyncio.run(go())

    assert error.rule == "endpoint_refused"
    assert "could not be resolved" in str(error)


def test_a_rebinding_refusal_leaks_no_url() -> None:
    # The refusal becomes the message execution_svc logs verbatim, and the
    # endpoint's query string is an ordinary place for an operator credential.
    resolver = _Resolver(_addrinfo("169.254.169.254"))

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker())

    error = asyncio.run(go())

    _assert_leaks_nothing(error)
    assert "169.254.169.254" in str(error)  # the useful half of the prose survives


# --- 2. the connect goes to the address that was checked ---------------------


def test_the_dispatch_connects_to_the_address_it_checked() -> None:
    """Resolve-then-pin: checking the name and then handing httpx the NAME
    would let the transport resolve it a second time, and a rebind landing in
    that window is dialled without ever having been checked."""
    seen: dict[str, Any] = {}
    resolver = _Resolver(_addrinfo(PUBLIC_V4))

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host_header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return _ok(request)

    async def go() -> dict[str, Any]:
        _install(resolver)
        return await _worker(transport=_pinned(handler)).run("build it", "because")

    out = asyncio.run(go())

    assert out["summary"] == "built it"
    # The IP literal, not the name: there is nothing left for httpx to resolve.
    assert seen["url"] == f"https://{PUBLIC_V4}/dispatch?token={SECRET}"
    # …while the operator's front end still sees the hostname it was bound
    # under, and the TLS handshake still verifies the certificate against it.
    assert seen["host_header"] == "operator.example"
    assert seen["sni"] == "operator.example"
    assert resolver.hosts == ["operator.example"]


def test_a_v6_answer_is_pinned_in_brackets_and_keeps_the_port() -> None:
    # A bare v6 address spliced into a URL without brackets is a malformed URL,
    # and the port must survive the rewrite or the dispatch goes to 443.
    seen: dict[str, Any] = {}
    resolver = _Resolver(_addrinfo(PUBLIC_V6))

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host_header"] = request.headers.get("host")
        return _ok(request)

    async def go() -> None:
        _install(resolver)
        await _worker("https://operator.example:8443/run", transport=_pinned(handler)).run("x", "y")

    asyncio.run(go())

    assert seen["url"] == f"https://[{PUBLIC_V6}]:8443/run"
    assert seen["host_header"] == "operator.example:8443"


def test_an_idna_endpoint_is_resolved_and_pinned_in_its_ascii_form() -> None:
    # `request.url.host` hands back the unicode spelling of an IDNA name, which
    # neither the resolver nor the SNI extension accepts. raw_host is the
    # punycode, and pinning the wrong one would fail every such dispatch.
    seen: dict[str, Any] = {}
    resolver = _Resolver(_addrinfo(PUBLIC_V4))

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sni"] = request.extensions.get("sni_hostname")
        return _ok(request)

    async def go() -> None:
        _install(resolver)
        await _worker("https://bücher.example/run", transport=_pinned(handler)).run("x", "y")

    asyncio.run(go())

    assert resolver.hosts == ["xn--bcher-kva.example"]
    assert seen["sni"] == "xn--bcher-kva.example"


def test_the_pin_is_re_resolved_on_the_connection_retry() -> None:
    # The retry is a fresh connection, so it deserves a fresh check: a name
    # that turns private between the first attempt and the second must not be
    # dialled on the strength of the first answer.
    resolver = _Resolver(_addrinfo(PUBLIC_V4), _addrinfo("10.0.0.5"))
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker(transport=_pinned(handler)))

    error = asyncio.run(go())

    assert attempts["n"] == 1  # the retry never reached the transport
    assert error.rule == "endpoint_refused"
    assert "10.0.0.5" in str(error)


# --- 3. the size cap counts bytes as received --------------------------------


def _streaming(headers: dict[str, str], chunks: AsyncIterator[bytes]) -> Callable[[httpx.Request], httpx.Response]:
    """A handler whose body is a live stream, so `_once` takes the raw path.

    An `httpx.Response` built from in-memory bytes has already consumed its
    stream before the worker sees it; only a generator body reproduces what
    comes off a socket.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=chunks)

    return handler


def test_the_dispatch_asks_for_no_compression() -> None:
    # httpx's default is "gzip, deflate, br, zstd". Asking for identity is what
    # keeps a compressed reply off the happy path for an honest operator; the
    # refusal below is what makes it hold against a hostile one.
    seen: dict[str, Any] = {}
    resolver = _Resolver(_addrinfo(PUBLIC_V4))

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        return _ok(request)

    async def go() -> None:
        _install(resolver)
        await _worker(transport=_pinned(handler)).run("x", "y")

    asyncio.run(go())

    assert seen["accept_encoding"] == "identity"


def test_a_gzip_bomb_is_refused_before_a_byte_of_it_is_read() -> None:
    """The finding, exactly: ~2 KB of gzip that decompresses past the 1 MiB cap.

    The old loop capped DECODED bytes, so the refusal arrived only after the
    decoder had already materialised the expansion. Here the generator is never
    pulled at all — `pulled` stays 0 — which is the whole point: the peak is
    bounded by the headers, not by the ratio the operator chose.
    """
    bomb = gzip.compress(b"x" * (8 * eh.MAX_RESPONSE_BYTES), 9)
    pulled = {"n": 0}

    async def body() -> AsyncIterator[bytes]:
        pulled["n"] += 1
        yield bomb

    resolver = _Resolver(_addrinfo(PUBLIC_V4))
    handler = _streaming({"Content-Type": "application/json", "Content-Encoding": "gzip"}, body())

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker(transport=_pinned(handler)))

    error = asyncio.run(go())

    assert len(bomb) < 64 * 1024  # a single socket read's worth of wire bytes…
    assert error.rule == "invalid_response"
    assert "gzip-encoded" in str(error)
    assert pulled["n"] == 0  # …and not one of them was ever decoded
    _assert_leaks_nothing(error)


@pytest.mark.parametrize("coding", ["gzip", "br", "zstd", "deflate", "gzip, br", "  GZIP  "])
def test_any_content_coding_we_did_not_accept_is_refused(coding: str) -> None:
    # The body here is valid JSON as it stands, so only the FRAMING can refuse
    # it — which is what stops this passing for the wrong reason if the coding
    # check is ever removed and the bytes are simply read as-is.
    resolver = _Resolver(_addrinfo(PUBLIC_V4))
    pulled = {"n": 0}

    async def body() -> AsyncIterator[bytes]:
        pulled["n"] += 1
        yield b'{"summary": "built it"}'

    handler = _streaming({"Content-Encoding": coding}, body())

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker(transport=_pinned(handler)))

    error = asyncio.run(go())

    assert error.rule == "invalid_response"
    assert "-encoded but the request accepted identity only" in str(error)
    assert pulled["n"] == 0


@pytest.mark.parametrize("header", ["", "identity", " identity "])
def test_an_uncompressed_response_is_not_refused(header: str) -> None:
    # `identity` is what we asked for and an absent header means the same
    # thing; neither may be turned into a failed step.
    resolver = _Resolver(_addrinfo(PUBLIC_V4))

    async def body() -> AsyncIterator[bytes]:
        yield b'{"summary": "built it"}'

    handler = _streaming({"Content-Encoding": header} if header else {}, body())

    async def go() -> dict[str, Any]:
        _install(resolver)
        return await _worker(transport=_pinned(handler)).run("x", "y")

    assert asyncio.run(go())["summary"] == "built it"


def test_the_cap_counts_wire_bytes_and_cuts_the_stream_off_early() -> None:
    # The belt behind the coding refusal: whatever the framing says, a body is
    # abandoned once it has sent more than the cap, so one dispatch cannot
    # allocate an unbounded amount whatever the operator does.
    chunk = b"x" * 64 * 1024
    sent = {"bytes": 0}

    async def body() -> AsyncIterator[bytes]:
        while sent["bytes"] < 64 * eh.MAX_RESPONSE_BYTES:  # bounded so a regression fails rather than hangs
            sent["bytes"] += len(chunk)
            yield chunk

    resolver = _Resolver(_addrinfo(PUBLIC_V4))
    handler = _streaming({"Content-Type": "application/json"}, body())

    async def go() -> eh.ExternalDispatchError:
        _install(resolver)
        return await _failing(_worker(transport=_pinned(handler)))

    error = asyncio.run(go())

    assert error.rule == "oversize_response"
    # Cut off within one chunk of the cap, not after the operator finished.
    assert sent["bytes"] <= eh.MAX_RESPONSE_BYTES + len(chunk)
    _assert_leaks_nothing(error)


def test_a_hostless_endpoint_is_refused_without_a_dns_query() -> None:
    # The host-level helper is reachable on its own, so it carries its own
    # floor rather than trusting every caller to have run the URL check first:
    # "" is what getaddrinfo turns into a lookup of the local host.
    resolver = _Resolver(_addrinfo(PUBLIC_V4))

    async def go() -> EndpointPolicyError:
        _install(resolver)
        with pytest.raises(EndpointPolicyError) as caught:
            await resolve_checked_addresses(".")
        return caught.value

    error = asyncio.run(go())

    assert error.rule == "unresolvable_host"
    assert resolver.hosts == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", None),
        ("identity", None),
        ("IDENTITY, identity", None),
        ("gzip", "gzip"),
        ("identity, gzip", "gzip"),
        ("GZip, br", "gzip"),
    ],
)
def test_unacceptable_coding_reads_the_header_the_way_a_sender_writes_it(header: str, expected: str | None) -> None:
    # Case and whitespace are not an escape, and a multi-coding header is
    # judged on its first non-identity entry.
    assert eh._unacceptable_coding(header) == expected
