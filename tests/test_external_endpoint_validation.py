"""SSRF rules for operator-supplied endpoint URLs (audit finding B2).

`ExternalHttpWorker` dispatches to a URL an operator will one day register, so
the URL is attacker-controlled input to an outbound request made from inside
our network. These tests pin the guard that keeps such a dispatch off cloud
instance metadata, loopback and the private ranges.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.agents.workers.external_http import ExternalDispatchError, ExternalHttpWorker, validate_endpoint_url


@pytest.mark.parametrize(
    "url",
    [
        "https://operator.example/run",
        "https://agents.operator.example:8443/v1/dispatch?x=1",
        "https://93.184.216.34/run",  # a public IP literal is fine
    ],
)
def test_public_https_endpoints_are_accepted(url: str) -> None:
    validate_endpoint_url(url)  # does not raise


@pytest.mark.parametrize(
    "url",
    [
        "http://operator.example/run",  # plaintext
        "file:///etc/passwd",
        "gopher://operator.example:70/_payload",
        "ftp://operator.example/run",
        "operator.example/run",  # no scheme at all
    ],
)
def test_non_https_schemes_are_rejected(url: str) -> None:
    with pytest.raises(ExternalDispatchError):
        validate_endpoint_url(url)


def test_cloud_metadata_ip_is_rejected() -> None:
    # The one that matters most: 169.254.169.254 hands out instance
    # credentials to anything that can reach it from the host.
    with pytest.raises(ExternalDispatchError, match="non-public address"):
        validate_endpoint_url("https://169.254.169.254/latest/meta-data/iam/security-credentials/")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/run",  # loopback
        "https://127.0.0.1:8000/run",
        "https://10.0.0.5/run",  # private
        "https://192.168.1.1/run",  # private
        "https://172.16.0.9/run",  # private
        "https://[::1]/run",  # v6 loopback
        "https://[fe80::1]/run",  # v6 link-local
        "https://[fd00::1]/run",  # v6 unique-local
        "https://0.0.0.0/run",  # unspecified
        "https://224.0.0.1/run",  # multicast
        "https://240.0.0.1/run",  # reserved
    ],
)
def test_non_public_ip_literals_are_rejected(url: str) -> None:
    with pytest.raises(ExternalDispatchError, match="non-public address"):
        validate_endpoint_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/run",
        "https://localhost:9000/run",
        "https://LOCALHOST/run",  # case is not an escape
        "https://localhost./run",  # nor is a trailing-dot FQDN
        "https://api.localhost/run",  # RFC 6761 reserves the whole suffix
    ],
)
def test_loopback_hostnames_are_rejected(url: str) -> None:
    with pytest.raises(ExternalDispatchError, match="loopback host"):
        validate_endpoint_url(url)


@pytest.mark.parametrize("url", ["https://", "https:///run", "https://user@/run"])
def test_missing_host_is_rejected(url: str) -> None:
    with pytest.raises(ExternalDispatchError, match="no host"):
        validate_endpoint_url(url)


def test_userinfo_does_not_disguise_a_blocked_host() -> None:
    # The classic "https://operator.example@169.254.169.254/" trick: everything
    # before the @ is credentials, the real host is the metadata service.
    with pytest.raises(ExternalDispatchError, match="non-public address"):
        validate_endpoint_url("https://operator.example@169.254.169.254/latest/meta-data/")


def test_worker_refuses_to_dispatch_to_a_blocked_endpoint() -> None:
    # The guard is wired into the dispatch path, not just available as a
    # helper: no request is ever handed to the transport, and the failure is
    # the ordinary ExternalDispatchError execution_svc already degrades on.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"summary": "should never be reached"})

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            worker = ExternalHttpWorker("ext_evil", "external.evil", "http://169.254.169.254/", client=client)
            with pytest.raises(ExternalDispatchError, match="external dispatch"):
                await worker.run("x", "y")

    asyncio.run(go())
    assert calls["n"] == 0  # the endpoint was never contacted


def test_endpoint_rebound_after_construction_is_revalidated() -> None:
    # endpoint_url is a plain attribute: validating per dispatch (not once in
    # __init__) is what catches a URL swapped out after the worker was built.
    worker = ExternalHttpWorker("ext_demo1", "external.demo", "https://operator.example/run")
    worker.endpoint_url = "https://169.254.169.254/latest/meta-data/"

    with pytest.raises(ExternalDispatchError, match="non-public address"):
        asyncio.run(worker.run("x", "y"))
