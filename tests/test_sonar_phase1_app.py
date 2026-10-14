"""Behaviour pinned while clearing SonarQube's phase-1 security and bug findings."""

from __future__ import annotations

import ipaddress

from stellar_sdk import Network

from app import config, security
from app.config import MAINNET_PASSPHRASE, TESTNET_PASSPHRASE, Settings

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
