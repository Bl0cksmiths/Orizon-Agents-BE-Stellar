"""Endpoint policy — the single source of truth for which operator-supplied
URLs this service is willing to touch (SSRF).

An operator endpoint is an outbound request made by *our* server from *inside*
our network, so an unchecked URL turns the orchestrator into a proxy for
anything the endpoint can reach: cloud instance metadata at 169.254.169.254
(credentials), 127.0.0.1 (this process' own admin surface), and the private
ranges holding the database and the internal services.

The rules started life inside `app/agents/workers/external_http.py`, where the
1.06 spike needed them at dispatch time. Story 2.01 gives them a second caller
— the bind API, which must refuse a bad URL *before* it is ever stored — and a
block-list with two copies is a block-list that drifts, with the stale copy
always being the one nobody reviews. So they live here, once, and the worker
imports them (see docs/decisions/0003-operator-endpoint-binding.md).

Two entry points, deliberately split by whether they do I/O:

  * `validate_endpoint_url` is pure — no DNS, no sockets, no clock. It judges
    the URL *as written*, which is what lets the bind handler run it before any
    chain read or write and prove that a blocked URL stored nothing.
  * `resolve_and_check` additionally resolves the hostname and applies the same
    address predicate to every address that comes back, closing the "ordinary
    name that resolves into a blocked range" hole the pure check concedes.

Every refusal is an `EndpointPolicyError` carrying a `rule` from
`ENDPOINT_RULES`. The rule — not the prose — is the machine-readable part: it
is what a caller maps to an API error code and what a test asserts on, so the
message stays free to say something useful to a human without a test pinning
its wording.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# The closed vocabulary of refusal reasons. Closed on purpose: EndpointPolicyError
# rejects anything outside it, so a typo at a raise site fails loudly here rather
# than reaching a caller as an error code nobody handles.
ENDPOINT_RULES: frozenset[str] = frozenset(
    {
        "malformed_url",
        "scheme_not_https",
        "no_host",
        "non_public_address",
        "loopback_host",
        "metadata_host",
        "unresolvable_host",
    }
)

# Only https: an operator endpoint is a third party across the public internet,
# and http would put the envelope (and the artifact coming back) on the wire in
# the clear. Restricting the scheme also kills file://, gopher:// and the rest
# of the SSRF-classic schemes in the same line.
ALLOWED_SCHEMES = frozenset({"https"})
# Hostnames that resolve to the local machine without ever touching an IP
# literal. ".localhost" is reserved for exactly this by RFC 6761.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})
# Names the cloud providers resolve to the link-local metadata service. Blocking
# 169.254.169.254 as a literal does nothing about these: they are ordinary names
# that only resolve inside the VM, so nothing short of a name check stops them
# before DNS. `resolve_and_check` is what makes the range unreachable by ANY
# name; this list is the floor that still holds when only the pure check runs.
_METADATA_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog", "instance-data"})


class EndpointPolicyError(ValueError):
    """An operator endpoint was refused, naming the rule that refused it.

    `rule` is one of `ENDPOINT_RULES`; the constructor refuses any other value
    so the vocabulary cannot quietly grow a synonym. ValueError rather than a
    bespoke base because callers that do not care about the rule — a router
    catching bad input, a test — still get a sensible except clause.
    """

    def __init__(self, rule: str, message: str) -> None:
        if rule not in ENDPOINT_RULES:
            raise ValueError(f"unknown endpoint policy rule {rule!r}")
        super().__init__(message)
        self.rule = rule


def _as_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` denotes, or None if it is a name.

    `ipaddress.ip_address` parses only the canonical dotted-quad, but the
    resolver is far more permissive: getaddrinfo reads "2130706433", "127.1"
    and "0177.0.0.1" as 127.0.0.1, and "2852039166" as the metadata service.
    Parsed by ip_address alone, every one of those spellings falls through to
    the hostname rules — which only know `localhost` — and is then handed to
    the connect as the blocked address after all.

    socket.inet_aton IS the resolver's own parser, so it recognises exactly the
    spellings getaddrinfo would go on to honour: decimal, octal, hex and the
    short a.b / a.b.c forms. A name that inet_aton accepts is all-numeric and
    therefore cannot be a public FQDN, so nothing legitimate is caught here.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError):
        return None  # a real name, not a literal in disguise


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether `ip` is somewhere we refuse to send a dispatch.

    THE address predicate: both the literal in a URL and every address the
    resolver hands back are judged by this one function, because a range that
    is unreachable by literal and reachable by name is not blocked at all.

    An IPv4-mapped v6 address is unwrapped first so the v4 rules are applied to
    the address that actually gets dialled. Note what this is and is not: on
    CPython 3.12 `ipaddress.ip_address("::ffff:127.0.0.1").is_loopback` is
    False, but `.is_private` and `.is_reserved` are both True, so the mapped
    spelling was already refused before the unwrap existed — measured, not
    assumed. The unwrap is defence in depth and intent-made-explicit, NOT a
    patched hole: do not treat it as licence to drop is_private or is_reserved
    from the predicate below, which is what actually catches these today.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def validate_endpoint_url(url: str) -> None:
    """Reject an operator endpoint that is not safe to dispatch to (SSRF).

    Pure: no DNS, no sockets, no I/O of any kind. The rules, in order:
      * the URL must parse — see `malformed_url`;
      * scheme must be https — see ALLOWED_SCHEMES;
      * a host must be present;
      * an IP literal must be publicly routable — private, loopback,
        link-local (which is what 169.254.169.254 is), reserved, multicast and
        unspecified addresses are all refused, v4 and v6 alike. "IP literal"
        means any spelling the RESOLVER treats as one, not just the canonical
        dotted-quad — see `_as_ip_literal`;
      * a hostname must not be a loopback name (`localhost`, `*.localhost`)
        or a known cloud metadata name (`metadata.google.internal`, …).

    Deliberately NOT covered here: this validates the URL as written, so an
    ordinary name that RESOLVES into a blocked range still passes — the
    metadata names above are a hand-listed floor, not a general answer. That is
    `resolve_and_check`'s job, and it is separate precisely because this half
    must stay callable before any I/O has happened. DNS rebinding between a
    check and a connect remains untouched by either; closing it needs
    resolve-then-pin at socket level. Redirects cannot launder the check
    because the worker never follows them.

    Raises EndpointPolicyError. The worker re-raises it as ExternalDispatchError
    so a bad binding fails its step like any other dispatch failure.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as e:  # malformed IPv6 bracket, non-numeric port, …
        raise EndpointPolicyError("malformed_url", f"endpoint URL {url!r} could not be parsed") from e

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EndpointPolicyError(
            "scheme_not_https",
            f"endpoint URL {url!r} uses scheme {scheme or '(none)'!r}; only {sorted(ALLOWED_SCHEMES)} allowed",
        )

    host = (host or "").rstrip(".")  # a trailing-dot FQDN names the same host
    if not host:
        raise EndpointPolicyError("no_host", f"endpoint URL {url!r} has no host")

    ip = _as_ip_literal(host)
    if ip is not None:
        if _is_blocked_address(ip):
            # Report the canonical form: "2130706433" is not obviously 127.0.0.1
            # in a log line, and the operator needs to see what we resolved it to.
            raise EndpointPolicyError(
                "non_public_address",
                f"endpoint URL {url!r} points at non-public address {ip} — "
                "private, loopback, link-local, reserved, multicast and unspecified "
                "ranges are not dispatchable",
            )
        return

    if host in _LOOPBACK_HOSTNAMES or any(host.endswith(f".{name}") for name in _LOOPBACK_HOSTNAMES):
        raise EndpointPolicyError("loopback_host", f"endpoint URL {url!r} points at loopback host {host!r}")

    if host in _METADATA_HOSTNAMES:
        raise EndpointPolicyError("metadata_host", f"endpoint URL {url!r} points at cloud metadata host {host!r}")


async def resolve_and_check(url: str) -> tuple[str, ...]:
    """`validate_endpoint_url`, then resolve the host and judge what comes back.

    The pure check can only see the URL as written, so `evil.example` — an
    ordinary, perfectly public-looking name whose A record is 127.0.0.1 or
    169.254.169.254 — walks straight through it. The hand-listed metadata names
    are a floor for the handful of spellings we know; this is the general
    answer: resolve the name and apply `_is_blocked_address`, the SAME
    predicate the literal rule uses, to EVERY address returned. One public
    address among five does not make a host dispatchable — the resolver is free
    to hand any of them to the connect, so one blocked address refuses the lot.

    Returns the resolved addresses (deduplicated, resolver order preserved) so
    a caller can log or pin them.

    A DNS lookup is an outbound *query*, not an outbound *request to the
    endpoint*, so this still satisfies "no outbound request to the endpoint at
    bind time". It does reach the network, which is why it is a separate
    function: the bind handler runs the pure check first, before any chain read
    or write, and only calls this once the caller has proved ownership — an
    unauthenticated resolver is a service someone else will happily use.

    Raises EndpointPolicyError with rule `unresolvable_host` (the name does not
    resolve, or resolves to nothing usable) or `non_public_address` (naming the
    offending address), plus anything `validate_endpoint_url` raises.
    """
    validate_endpoint_url(url)
    host = (urlsplit(url).hostname or "").rstrip(".")

    # The loop's resolver, not socket.getaddrinfo: the blocking call would stall
    # the whole event loop for the length of a DNS timeout, and this runs inside
    # a request handler.
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:  # socket.gaierror and friends are all OSError
        raise EndpointPolicyError(
            "unresolvable_host", f"endpoint URL {url!r} host {host!r} could not be resolved"
        ) from e

    addresses: list[str] = []
    for info in infos:
        # sockaddr[0] is the address for both AF_INET and AF_INET6; a v6 result
        # can carry a "%eth0" scope suffix that ip_address will not parse.
        address = str(info[4][0]).partition("%")[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise EndpointPolicyError("unresolvable_host", f"endpoint URL {url!r} host {host!r} resolved to no addresses")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as e:
            # A resolver that answers with something unparseable is not an
            # answer we can judge, and an unjudgeable address is not dispatchable.
            raise EndpointPolicyError(
                "unresolvable_host", f"endpoint URL {url!r} host {host!r} resolved to unparseable address {address!r}"
            ) from e
        if _is_blocked_address(ip):
            raise EndpointPolicyError(
                "non_public_address",
                f"endpoint URL {url!r} host {host!r} resolves to non-public address {ip} — "
                "private, loopback, link-local, reserved, multicast and unspecified "
                "ranges are not dispatchable",
            )

    return tuple(addresses)
