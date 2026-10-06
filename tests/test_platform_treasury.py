"""The platform treasury: the one account the built-in agents are registered to (ADR 0016).

Pinned here:

  * the committed team register declares exactly one platform treasury, so the
    account the escrow pays for built-in work is never counted as an outside
    operator, and the backend has one place to read it from;
  * the arguments each built-in agent is registered with are the seeded
    catalog's own name, skills and price, so the on-chain record says what a
    plan charges.
"""

from __future__ import annotations

import re

import pytest
from stellar_sdk import StrKey

from app import money
from app.agents.registry import WORKERS
from app.schemas import AGENT_ID_PATTERN
from app.seed import seed_registry
from app.services import adoption_svc, platform_treasury
from app.services.adoption_svc import TeamWallet, load_team_register
from app.state import state

ROLE = "platform treasury (built-in agents' payee)"


@pytest.fixture(autouse=True)
def _seeded() -> None:
    seed_registry()


# ── the register ────────────────────────────────────────────────────────
def test_the_register_declares_exactly_one_platform_treasury() -> None:
    treasuries = [w for w in load_team_register() if w.role == ROLE]

    assert len(treasuries) == 1
    assert StrKey.is_valid_ed25519_public_key(treasuries[0].address)
    assert platform_treasury.TREASURY_ROLE == ROLE
    assert platform_treasury.treasury_address() == treasuries[0].address


def test_a_register_without_a_treasury_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    others = tuple(w for w in adoption_svc.TEAM_REGISTER if w.role != ROLE)
    monkeypatch.setattr(adoption_svc, "TEAM_REGISTER", others)

    assert platform_treasury.treasury_address() is None


def test_a_register_with_two_treasuries_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    second = TeamWallet(address="GBWMD26IB6CMG3JO3HU7SD7ZJSTF4BIJ5JS77ANMLJ52M6FV6K3J7BQJ", role=ROLE, evidence="x")
    monkeypatch.setattr(adoption_svc, "TEAM_REGISTER", (*adoption_svc.TEAM_REGISTER, second))

    with pytest.raises(platform_treasury.TreasuryError, match="2 entries"):
        platform_treasury.treasury_address()


def test_the_treasury_is_never_an_external_owner() -> None:
    """The adoption report and the overview share this rule: an agent the
    treasury owns is ours, listed under `excluded` with the treasury's role."""
    rule = adoption_svc.OwnerRule(
        register={w.address: w for w in adoption_svc.TEAM_REGISTER}, platform=adoption_svc._PlatformKeys()
    )

    assert rule.classify(platform_treasury.treasury_address() or "") == ("team_wallet", ROLE)


# ── the registrations ───────────────────────────────────────────────────
# What each built-in agent is registered with: the seeded catalog's name,
# skills as Symbols, and its price in stroops. Pinned literally so a change to
# the catalog is a deliberate change here, and to the on-chain records.
EXPECTED = [
    ("agt_01h8", "copywrite.v3", ("copy", "seo", "en"), 120_000),
    ("agt_02k2", "design.figma", ("ui", "tokens", "figma"), 180_000),
    ("agt_03d9", "code.next", ("ts", "react", "next"), 660_000),
    ("agt_04m1", "sol-audit", ("solidity", "security"), 1_800_000),
    ("agt_05x7", "seo.brief", ("seo", "research"), 90_000),
    ("agt_06q4", "vision.ocr", ("vision", "ocr"), 140_000),
    ("agt_07w3", "ads.meta", ("ads", "meta"), 220_000),
    ("agt_08j2", "deploy.v0", ("deploy", "ci", "seal"), 110_000),
    ("agt_09l5", "research.pro", ("research", "citations"), 240_000),
    ("agt_10b6", "translate.42", ("i18n", "42_langs"), 70_000),
    ("agt_11c0", "code.gen", ("code", "html", "js", "build"), 540_000),
    ("agt_12r0", "code.critic", ("a11y", "polish", "review"), 520_000),
]


def test_every_built_in_agent_is_registered_with_its_seeded_terms() -> None:
    got = [(r.agent_id, r.name, r.skills, r.price_stroops) for r in platform_treasury.registrations()]

    assert got == EXPECTED
    assert {r[0] for r in got} == set(WORKERS)


def test_a_registration_charges_what_a_plan_charges() -> None:
    for registration in platform_treasury.registrations():
        assert registration.price_stroops == money.to_stroops(state.agents[registration.agent_id].price)


def test_every_registered_skill_is_a_symbol() -> None:
    for registration in platform_treasury.registrations():
        for skill in registration.skills:
            assert re.fullmatch(AGENT_ID_PATTERN, skill), (registration.agent_id, skill)


@pytest.mark.parametrize(
    ("skill", "symbol"),
    [("42 langs", "42_langs"), ("a11y", "a11y"), ("x-y.z", "x_y_z"), ("s" * 40, "s" * 32)],
)
def test_a_skill_is_made_a_symbol(skill: str, symbol: str) -> None:
    assert platform_treasury.onchain_skill(skill) == symbol
