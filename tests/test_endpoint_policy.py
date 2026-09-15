"""The shared endpoint policy — SSRF rules for operator-supplied URLs.

`tests/test_external_endpoint_validation.py` still proves the rules behave
through the worker's dispatch path, unchanged, which is the regression proof
that moving them here was faithful. This suite pins what is new in 2.01 and what
the bind API is built on: every refusal names a machine-readable rule from
`ENDPOINT_RULES`, so a router can map it to an error code and a test can assert
it without regexing English prose.
"""

from __future__ import annotations

import pytest

from app.services.endpoint_policy import ENDPOINT_RULES, EndpointPolicyError, validate_endpoint_url

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
