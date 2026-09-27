"""Tests for the network-aware stellar.expert explorer mapping."""

from __future__ import annotations

import pytest

from app.config import MAINNET_PASSPHRASE, settings
from app.stellar import client as sc

TESTNET_PASSPHRASE = "Test SDF Network ; September 2015"


def test_explorer_network_is_testnet_by_default(monkeypatch):
    monkeypatch.setattr(settings, "stellar_network", "testnet")
    monkeypatch.setattr(settings, "stellar_network_passphrase", TESTNET_PASSPHRASE)
    assert sc.explorer_network() == "testnet"


@pytest.mark.parametrize("label", ["mainnet", "public", "pubnet", " Mainnet", "testnet"])
def test_the_mainnet_passphrase_is_the_public_explorer_whatever_the_label(monkeypatch, label):
    """D-074: the passphrase decides which chain a transaction landed on, so it
    decides which explorer can show it. `pubnet` used to yield `/explorer/pubnet/`
    and `testnet` over the mainnet passphrase the testnet explorer."""
    monkeypatch.setattr(settings, "stellar_network", label)
    monkeypatch.setattr(settings, "stellar_network_passphrase", MAINNET_PASSPHRASE)
    assert sc.explorer_network() == "public"


def test_a_mainnet_label_over_a_testnet_passphrase_links_testnet(monkeypatch):
    # Unbootable (the passphrase validator refuses it), but settings are mutable
    # afterwards: the link must still follow where the signature is valid.
    monkeypatch.setattr(settings, "stellar_network", "mainnet")
    monkeypatch.setattr(settings, "stellar_network_passphrase", TESTNET_PASSPHRASE)
    assert sc.explorer_network() == "testnet"


def test_to_jsonable_converts_scval_natives():
    from stellar_sdk import Address

    g = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
    native = {
        "owner": Address(g),
        "receipts": [b"\x00" * 16],
        "count": 3,
        "name": "Orizon Batch",
    }
    out = sc._to_jsonable(native)
    assert out["owner"] == g
    assert out["receipts"] == ["00" * 16]
    assert out["count"] == 3
    assert out["name"] == "Orizon Batch"
