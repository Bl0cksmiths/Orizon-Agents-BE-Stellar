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
        "  " + ROLE_CARDS["agt_11c0"].render(),
        lines[3],
    ]
    assert lines[1].startswith("- id=agt_11c0 ") and lines[3].startswith("- id=ext_op ")
