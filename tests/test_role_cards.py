"""The planner's role cards: one per built-in agent, static, and one safe line each."""

from __future__ import annotations

import re

import pytest

from app.agents.role_cards import ROLE_CARDS, card_for
from app.seed import _SEED

SEEDED_IDS = [row[0] for row in _SEED]
SEEDED_NAMES = {row[1] for row in _SEED}


def test_every_built_in_agent_has_a_card_and_nothing_else_does() -> None:
    assert sorted(ROLE_CARDS) == sorted(SEEDED_IDS)
    assert card_for("ext_operator_agent") is None


@pytest.mark.parametrize("agent_id", SEEDED_IDS)
def test_a_card_renders_as_one_line_that_cannot_pass_for_an_entry(agent_id: str) -> None:
    line = ROLE_CARDS[agent_id].render()

    assert "\n" not in line and "\r" not in line
    # AVAILABLE_AGENTS entries are `key=value` fields; a card has none.
    assert "=" not in line
    assert not line.startswith("-")
    assert line.startswith("does: ") and "; reads: " in line and "; hands on: " in line and "; use when: " in line


@pytest.mark.parametrize("agent_id", SEEDED_IDS)
def test_a_card_names_only_agents_that_exist(agent_id: str) -> None:
    # A card that taught a handoff to an agent the catalog does not have would
    # teach the planner a pipeline it cannot build.
    named = set(re.findall(r"\b[a-z]+(?:\.[a-z0-9]+)+\b", ROLE_CARDS[agent_id].render()))

    assert named <= SEEDED_NAMES, named - SEEDED_NAMES


def test_a_card_renders_in_its_pinned_shape() -> None:
    assert ROLE_CARDS["agt_08j2"].render() == (
        "does: seals the finished build and issues a preview link; "
        "reads: the latest code artifact (after code.critic when it ran); "
        "hands on: the sealed build and its preview link, as the last step; "
        "use when: a build should be live, shared or deployed; never without a code builder before it"
    )


def test_the_block_sets_a_built_in_agents_card_under_its_entry_and_gives_an_external_agent_none() -> None:
    from app.schemas import Agent
    from app.services.orchestrator_svc import render_agents_block

    built_in = Agent(id="agt_11c0", name="code.gen", skills=["code"], price=0.054, rep=4.0, status="online", runs=1)
    external = Agent(
        id="ext_op",
        name="operator agent",
        skills=["code"],
        price=0.01,
        rep=4.0,
        status="online",
        runs=1,
        source="onchain",
    )

    lines = render_agents_block([built_in, external], {}).splitlines()

    assert lines == [
        "AVAILABLE_AGENTS:",
        lines[1],
        lines[2],
        "  " + ROLE_CARDS["agt_11c0"].render(),
        lines[4],
    ]
    assert lines[2].startswith("- id=agt_11c0 ") and lines[4].startswith("- id=ext_op ")


# ── prices in the block: exact, in the real asset ───────────────


def _priced(price: float):
    from app.schemas import Agent

    return Agent(id="agt_01h8", name="copywrite.v3", skills=["copy"], price=price, rep=4.0, status="online", runs=1)


@pytest.mark.parametrize(
    ("price", "shown"),
    [(0.054, "0.054"), (0.0125, "0.0125"), (0.0000001, "0.0000001"), (1.5, "1.500"), (0.0, "0.000")],
)
def test_the_block_shows_each_price_exactly(price: float, shown: str) -> None:
    from app.services.orchestrator_svc import render_agents_block

    entry = render_agents_block([_priced(price)], {}).splitlines()[2]

    assert f" price={shown} " in entry


def test_the_block_names_the_networks_asset_never_an_assumed_usdc() -> None:
    from app.services.orchestrator_svc import render_agents_block

    block = render_agents_block([_priced(0.054)], {})

    assert block.splitlines()[:2] == [
        "AVAILABLE_AGENTS:",
        "(price = what one step costs the buyer, exactly, in XLM)",
    ]
    assert "USDC" not in block


def test_the_block_names_whatever_asset_the_deployment_settles_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import money
    from app.services.orchestrator_svc import render_agents_block

    monkeypatch.setattr(money, "asset_code", lambda: "USDC")

    assert "exactly, in USDC)" in render_agents_block([_priced(0.054)], {})


def test_a_price_the_ledger_cannot_hold_is_shown_unpriced_not_raised() -> None:
    from app.services.orchestrator_svc import render_agents_block

    agent = _priced(0.054).model_copy(update={"price": float("nan")})

    assert " price=unpriced " in render_agents_block([agent], {})


def test_the_block_is_byte_stable_between_calls() -> None:
    from app.services.orchestrator_svc import render_agents_block

    agents = [_priced(0.0125)]

    assert render_agents_block(agents, {}) == render_agents_block(agents, {})
