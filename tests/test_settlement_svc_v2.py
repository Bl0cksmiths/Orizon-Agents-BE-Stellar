"""Operator earnings under escrow v2 (ADR 0010).

v2 emits one `charged` per payout, topic'd with the agent ACTUALLY paid, and
its payer is the buyer whose custody funded it — so a third-party buyer's
payout is revenue for that agent. And because v2's settler can be rotated,
a payout this deployment itself funded is excluded even when the chain's
current `settler()` no longer names the key that paid.
"""

from __future__ import annotations

from typing import Any

import pytest
from stellar_sdk import Keypair
from test_settlement_svc import (
    AGENT_ID,
    BUYER,
    ESCROW_ID,
    OWNER,
    REGISTRY_ID,
    SAC_ID,
    SETTLER,
    _auth_id,
    _charged_event,
    _FakeRpc,
    _job_id,
    _reader,
    _run,
)

from app.config import settings
from app.stellar import cache as rcache
from app.stellar import client as sc

PLATFORM_SIGNER = Keypair.random()


@pytest.fixture(autouse=True)
def configured(hermetic_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    rcache.clear()
    monkeypatch.setattr(sc, "simulate_read", _reader())
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)
    monkeypatch.setattr(settings, "stellar_payment_escrow", ESCROW_ID)
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC_ID)
    yield settings
    rcache.clear()


def _v2_reader(payers: dict[str, str]) -> Any:
    """`_reader`, but `authorization` answers in v2's shape (`settled` added)."""
    base = _reader(payers=payers)

    def read(contract_id: str, function_name: str, args: Any = None, source: Any = None, **_kw: Any) -> Any:
        value = base(contract_id, function_name, args, source)
        if function_name == "authorization":
            value = {**value, "settled": True}
        return value

    return read


def test_a_buyer_funded_v2_payout_is_the_paid_agents_revenue(monkeypatch):
    """Two payouts out of one settle, both topic'd with this agent: both are
    decoded, attributed to the buyer who funded the custody, and counted."""
    events = [
        _charged_event(1_000_010, _auth_id(1), _job_id(1), 120_000),
        _charged_event(1_000_010, _auth_id(1), _job_id(1), 30_000),
    ]

    evidence = _run(monkeypatch, _FakeRpc(events=events), _v2_reader({_auth_id(1).hex(): BUYER}))

    assert evidence.unavailable is None
    assert [(e.amount_stroops, e.payer, e.exclusion) for e in evidence.entries] == [
        (120_000, BUYER, None),
        (30_000, BUYER, None),
    ]
    assert (evidence.agent_id, evidence.total_stroops, evidence.self_payment_stroops) == (AGENT_ID, 150_000, 0)


def test_a_payout_funded_by_this_deployments_own_key_is_not_revenue(monkeypatch):
    """After a settler rotation the chain's `settler()` names another key, but
    the key this deployment signs with paying is still the platform paying."""
    monkeypatch.setattr(settings, "stellar_signing_key", PLATFORM_SIGNER.secret)
    monkeypatch.setattr(sc, "signer_public_key", lambda: PLATFORM_SIGNER.public_key)
    events = [_charged_event(1_000_010, _auth_id(1), _job_id(1), 120_000)]

    evidence = _run(monkeypatch, _FakeRpc(events=events), _v2_reader({_auth_id(1).hex(): PLATFORM_SIGNER.public_key}))

    [entry] = evidence.entries
    assert (entry.exclusion, entry.self_payment) == ("settler", True)
    assert (evidence.total_stroops, evidence.self_payment_stroops) == (0, 120_000)


def test_a_payout_funded_by_the_admin_is_not_revenue(monkeypatch):
    admin = Keypair.random().public_key
    monkeypatch.setattr(settings, "stellar_admin_address", admin)
    events = [_charged_event(1_000_010, _auth_id(1), _job_id(1), 120_000)]

    evidence = _run(monkeypatch, _FakeRpc(events=events), _v2_reader({_auth_id(1).hex(): admin}))

    assert evidence.entries[0].exclusion == "settler"


def test_the_owner_rule_still_outranks_the_platform_rule(monkeypatch):
    """`owner` before `settler`, as `_exclusion` orders them: the precise
    sentence for the operator's own wallet."""
    monkeypatch.setattr(settings, "stellar_admin_address", OWNER)
    events = [_charged_event(1_000_010, _auth_id(1), _job_id(1), 120_000)]

    evidence = _run(monkeypatch, _FakeRpc(events=events), _v2_reader({_auth_id(1).hex(): OWNER}))

    assert evidence.entries[0].exclusion == "owner"
    assert SETTLER != OWNER
