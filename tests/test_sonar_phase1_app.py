"""Behaviour pinned while clearing SonarQube's phase-1 security and bug findings."""

from __future__ import annotations

import ipaddress
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_core import PydanticSerializationError, to_json
from stellar_sdk import Network

from app import config, security
from app.config import MAINNET_PASSPHRASE, TESTNET_PASSPHRASE, Settings
from app.routers import agents as agents_router
from app.schemas import Agent
from app.services import binding_registry
from app.state import state

# ── network passphrases come from the SDK ──────────────────────────────────


def test_the_passphrase_names_are_the_sdk_constants() -> None:
    assert MAINNET_PASSPHRASE == Network.PUBLIC_NETWORK_PASSPHRASE
    assert TESTNET_PASSPHRASE == Network.TESTNET_NETWORK_PASSPHRASE
    assert MAINNET_PASSPHRASE != TESTNET_PASSPHRASE


def test_the_default_passphrase_is_testnet_so_a_bare_boot_moves_no_real_value() -> None:
    assert Settings.model_fields["stellar_network_passphrase"].default == Network.TESTNET_NETWORK_PASSPHRASE


def test_is_mainnet_still_reads_the_sdk_public_passphrase() -> None:
    probe = config.settings.model_copy(update={"stellar_network_passphrase": Network.PUBLIC_NETWORK_PASSPHRASE})
    assert probe.is_mainnet() is True
    probe = config.settings.model_copy(update={"stellar_network_passphrase": Network.TESTNET_NETWORK_PASSPHRASE})
    assert probe.is_mainnet() is False


# ── Cloudflare edge ranges ──────────────────────────────────────────────────


def test_the_cloudflare_ranges_are_an_immutable_tuple_of_networks() -> None:
    nets = security._CLOUDFLARE_NETWORKS
    assert isinstance(nets, tuple)
    assert all(isinstance(n, ipaddress.IPv4Network | ipaddress.IPv6Network) for n in nets)
    assert len(set(nets)) == len(nets)


def test_the_cloudflare_ranges_match_the_published_list_size() -> None:
    """15 IPv4 + 7 IPv6 CIDRs at https://www.cloudflare.com/ips/ as of 2026-10-15."""
    nets = security._CLOUDFLARE_NETWORKS
    assert sum(n.version == 4 for n in nets) == 15
    assert sum(n.version == 6 for n in nets) == 7


def test_every_cloudflare_range_is_recognised_end_to_end() -> None:
    for net in security._CLOUDFLARE_NETWORKS:
        assert security._is_cloudflare(net.network_address), net
        assert security._is_cloudflare(net.broadcast_address), net
        assert security._visitor(net[1]) is None, net


def test_an_address_just_outside_a_cloudflare_range_is_not_an_edge() -> None:
    for net in security._CLOUDFLARE_NETWORKS:
        after = net.broadcast_address + 1
        if not any(after in other for other in security._CLOUDFLARE_NETWORKS):
            assert not security._is_cloudflare(after), net


# ── CORS: plain http only ever for the loopback ─────────────────────────────

REPO = Path(__file__).resolve().parent.parent


def _render_cors_origins() -> list[str]:
    lines = (REPO / "render.yaml").read_text().splitlines()
    at = next(i for i, line in enumerate(lines) if line.strip() == "- key: CORS_ORIGINS")
    value = lines[at + 1].strip()
    assert value.startswith("value: ")
    return [o.strip() for o in value.removeprefix("value: ").split(",") if o.strip()]


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost.evil.example",
        "http://127.0.0.1.nip.io",
        "http://localhost:3000@evil.example",
        "http://evil.example#localhost",
        "http://0.0.0.0:3000",
        "http://192.168.1.10:3000",
        "http://LOCALHOST.evil.example",
    ],
)
def test_a_loopback_lookalike_over_plain_http_is_dropped(origin: str) -> None:
    assert security.strict_cors_origins([origin]) == []


def test_the_deployed_origin_list_admits_plain_http_for_the_loopback_only() -> None:
    kept = security.strict_cors_origins(_render_cors_origins())
    assert kept, "render.yaml lists no CORS origin the filter keeps"
    for origin in kept:
        assert origin.startswith("https://") or security._LOOPBACK_ORIGIN.fullmatch(origin), origin


def test_the_vercel_origin_regex_is_https_only() -> None:
    import app.main as main

    assert main._CORS_ORIGIN_REGEX.fullmatch("https://orizon-agents-fe-stellar.vercel.app")
    assert main._CORS_ORIGIN_REGEX.fullmatch("http://orizon-agents-fe-stellar.vercel.app") is None
    assert main._CORS_ORIGIN_REGEX.fullmatch("http://orizon-agents-fe-stellar-git-x.vercel.app") is None


# ── GET /api/agents?fields=: one schema-driven serializer ───────────────────


def _listed(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        name=f'{agent_id} "quoted" é </script>',
        skills=["code"],
        price=0.1234567890123,
        rep=4.0,
        status="online",
        runs=0,
        owner="GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        source="onchain",
    )


@pytest.fixture
def listed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Agent]]:
    saved = dict(state.agents)
    monkeypatch.setattr(binding_registry, "_bound_ids", {"ag_001"})
    monkeypatch.setattr(binding_registry, "_loaded", True)
    state.agents.clear()
    agents = [_listed(f"ag_{i:03d}") for i in range(3)]
    for agent in agents:
        state.add_agent(agent)
    yield agents
    state.agents.clear()
    state.agents.update(saved)


@pytest.mark.parametrize("fields", [*agents_router.AGENT_FIELDS, ",".join(agents_router.AGENT_FIELDS)])
def test_a_field_selected_body_is_byte_for_byte_the_previous_encoding(
    client: TestClient, listed: list[Agent], fields: str
) -> None:
    """The bytes are the ETag's input, so the switch must not change them."""
    selected = {f.strip() for f in fields.split(",")} | {"id"}
    stamp = "bound" in selected
    rows = [agents_router._with_bound(a) if stamp else a for a in listed]
    before = to_json([a.model_dump(include=selected, mode="json") for a in rows])
    assert client.get("/api/agents", params={"fields": fields}).content == before


def test_a_value_the_agent_schema_cannot_encode_fails_loudly(client: TestClient, listed: list[Agent]) -> None:
    """No fallback guesses at an unknown value: the payload is refused, never shipped half-encoded."""
    state.add_agent(listed[0].model_copy(update={"id": "ag_999", "owner": object()}))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(PydanticSerializationError):
            client.get("/api/agents", params={"fields": "owner"})
