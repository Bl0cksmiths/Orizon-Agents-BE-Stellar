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
