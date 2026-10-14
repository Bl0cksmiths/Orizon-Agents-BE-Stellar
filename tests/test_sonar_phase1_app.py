"""Behaviour pinned while clearing SonarQube's phase-1 security and bug findings."""

from __future__ import annotations

from stellar_sdk import Network

from app import config
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
