"""The shared endpoint policy — SSRF rules for operator-supplied URLs.

`tests/test_external_endpoint_validation.py` still proves the rules behave
through the worker's dispatch path, unchanged, which is the regression proof
that moving them here was faithful. This suite pins what is new in 2.01 and what
the bind API is built on: every refusal names a machine-readable rule from
`ENDPOINT_RULES`, so a router can map it to an error code and a test can assert
it without regexing English prose.

The `resolve_and_check` tests script the resolver rather than calling it: this
suite is hermetic and must pass offline, and a test whose verdict depends on
what `evil.example` resolves to today is not a test.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from app.services.endpoint_policy import ENDPOINT_RULES, EndpointPolicyError, resolve_and_check, validate_endpoint_url

# (url, the rule that must refuse it). Kept as a module constant rather than
# inline in the decorator so the coverage test below can prove that every rule
# the pure check can produce actually has a case here.
_REFUSALS: list[tuple[str, str]] = [
    ("https://[::1/run", "malformed_url"),  # unclosed IPv6 bracket — urlsplit itself gives up
    ("http://operator.example/run", "scheme_not_https"),
    ("file:///etc/passwd", "scheme_not_https"),
    ("operator.example/run", "scheme_not_https"),  # no scheme at all
    ("https://", "no_host"),
    ("https://user@/run", "no_host"),  # userinfo is not a host
    ("https://169.254.169.254/latest/meta-data/", "non_public_address"),
    ("https://127.0.0.1:8000/run", "non_public_address"),
    ("https://10.0.0.5/run", "non_public_address"),
    ("https://2130706433/run", "non_public_address"),  # decimal spelling of 127.0.0.1
    ("https://[::1]/run", "non_public_address"),
    ("https://[::ffff:127.0.0.1]/run", "non_public_address"),  # v4-mapped v6 is not a bypass
    ("https://localhost/run", "loopback_host"),
    ("https://LOCALHOST./run", "loopback_host"),  # case and a trailing dot are not escapes
    ("https://api.localhost/run", "loopback_host"),  # RFC 6761 reserves the whole suffix
    ("https://metadata.google.internal/computeMetadata/v1/", "metadata_host"),
    ("https://instance-data/latest/meta-data/", "metadata_host"),
]


@pytest.mark.parametrize(
    "url",
    [
        "https://operator.example/run",
        "https://agents.operator.example:8443/v1/dispatch?x=1",
        "https://operator.example./run",  # a trailing-dot FQDN names the same host
        "https://93.184.216.34/run",  # a public IP literal is fine
    ],
)
def test_public_https_endpoints_are_accepted(url: str) -> None:
    assert validate_endpoint_url(url) is None


@pytest.mark.parametrize(("url", "rule"), _REFUSALS)
def test_each_refusal_names_its_rule(url: str, rule: str) -> None:
    # AC-4 asks the refusal to name the rule. `.rule` is what makes that
    # assertable: the message stays free to be useful to a human.
    with pytest.raises(EndpointPolicyError) as exc:
        validate_endpoint_url(url)
    assert exc.value.rule == rule
    assert str(exc.value)  # and it still says something


def test_every_pure_rule_is_exercised() -> None:
    # A rule name nobody can produce is a dead error code that a caller will
    # nonetheless write a branch for. `unresolvable_host` is the resolver's
    # alone, so it is the only one this pure-check suite cannot reach.
    assert {rule for _, rule in _REFUSALS} == ENDPOINT_RULES - {"unresolvable_host"}


def test_rule_vocabulary_is_closed() -> None:
    # A typo at a raise site must fail here, loudly, rather than travel to a
    # caller as an error code nothing handles.
    with pytest.raises(ValueError) as exc:
        EndpointPolicyError("not_a_real_rule", "nope")
    assert not isinstance(exc.value, EndpointPolicyError)


def test_policy_error_is_a_value_error() -> None:
    # Callers that do not care which rule fired still get a sane except clause.
    with pytest.raises(ValueError):
        validate_endpoint_url("http://operator.example/run")


def _addrinfo(*addresses: str) -> list[tuple[Any, ...]]:
    """getaddrinfo's 5-tuples for `addresses`, shaped the way the real one is."""
    infos: list[tuple[Any, ...]] = []
    for address in addresses:
        if ":" in address:
            infos.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0, 0, 0)))
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0)))
    return infos


class _Resolver:
    """A scripted stand-in for `loop.getaddrinfo`, recording what it was asked."""

    def __init__(self, infos: list[tuple[Any, ...]] | None = None, error: BaseException | None = None) -> None:
        self.infos = infos if infos is not None else []
        self.error = error
        self.hosts: list[str] = []

    async def __call__(self, host: str, port: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        self.hosts.append(host)
        if self.error is not None:
            raise self.error
        return self.infos


def _check(url: str, resolver: _Resolver) -> tuple[str, ...]:
    """Run `resolve_and_check` with `resolver` standing in for DNS.

    The substitution is on the loop instance, not the asyncio module: every
    `asyncio.run` builds a fresh loop and closes it on the way out, so nothing
    leaks into another test. (The repo has no pytest-asyncio; a bare
    `asyncio.run` inside a sync test is the house idiom for an async test.)
    """

    async def go() -> tuple[str, ...]:
        asyncio.get_running_loop().getaddrinfo = resolver  # type: ignore[method-assign]
        return await resolve_and_check(url)

    return asyncio.run(go())


def test_resolve_and_check_accepts_a_public_host() -> None:
    resolver = _Resolver(_addrinfo("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"))
    assert _check("https://operator.example/run", resolver) == ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    assert resolver.hosts == ["operator.example"]


def test_resolve_and_check_strips_the_trailing_dot_before_resolving() -> None:
    resolver = _Resolver(_addrinfo("93.184.216.34", "93.184.216.34"))  # duplicates collapse
    assert _check("https://operator.example./run", resolver) == ("93.184.216.34",)
    assert resolver.hosts == ["operator.example"]


def test_a_host_that_resolves_into_a_blocked_range_is_refused() -> None:
    # The whole point of the resolver step: the URL is spelled with an ordinary
    # public-looking name, so the pure check has nothing to object to.
    resolver = _Resolver(_addrinfo("127.0.0.1"))
    with pytest.raises(EndpointPolicyError) as exc:
        _check("https://rebind.example/run", resolver)
    assert exc.value.rule == "non_public_address"
    assert "127.0.0.1" in str(exc.value)  # the refusal names WHICH address


def test_every_resolved_address_is_checked_not_just_the_first() -> None:
    # A public A record in front of a metadata-service one is the obvious dodge:
    # the connect may use either, so one blocked address refuses the whole host.
    resolver = _Resolver(_addrinfo("93.184.216.34", "169.254.169.254"))
    with pytest.raises(EndpointPolicyError) as exc:
        _check("https://operator.example/run", resolver)
    assert exc.value.rule == "non_public_address"
    assert "169.254.169.254" in str(exc.value)


def test_a_scoped_v6_answer_is_parsed_and_refused() -> None:
    # getaddrinfo hands back link-local v6 with a "%iface" scope suffix that
    # ip_address will not parse; stripping it is what lets the rule fire at all.
    resolver = _Resolver(_addrinfo("fe80::1%eth0"))
    with pytest.raises(EndpointPolicyError) as exc:
        _check("https://operator.example/run", resolver)
    assert exc.value.rule == "non_public_address"
    assert "fe80::1" in str(exc.value)


def test_a_resolver_failure_is_unresolvable_host() -> None:
    resolver = _Resolver(error=socket.gaierror(-2, "Name or service not known"))
    with pytest.raises(EndpointPolicyError) as exc:
        _check("https://nx.example/run", resolver)
    assert exc.value.rule == "unresolvable_host"


@pytest.mark.parametrize("infos", [[], _addrinfo("not-an-address")])
def test_an_unusable_answer_is_unresolvable_host(infos: list[tuple[Any, ...]]) -> None:
    # No addresses, or an address we cannot parse and therefore cannot judge —
    # either way there is nothing here we are willing to dispatch to.
    with pytest.raises(EndpointPolicyError) as exc:
        _check("https://operator.example/run", _Resolver(infos))
    assert exc.value.rule == "unresolvable_host"


def test_a_blocked_url_never_reaches_the_resolver() -> None:
    # The pure check runs first, so a URL refused on its face costs no DNS query
    # — which is what keeps the endpoint-check route from being a free resolver.
    resolver = _Resolver(_addrinfo("93.184.216.34"))
    with pytest.raises(EndpointPolicyError) as exc:
        _check("https://169.254.169.254/latest/meta-data/", resolver)
    assert exc.value.rule == "non_public_address"
    assert resolver.hosts == []
