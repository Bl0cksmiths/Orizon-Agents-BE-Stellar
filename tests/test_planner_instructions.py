"""The planner's standing instructions must not route around the floor.

`INSTRUCTIONS` told the model to prefer code.gen (agt_11c0) for every build
intent, unconditionally. When the floor excluded agt_11c0 from AVAILABLE_AGENTS
that standing order still named it, and a model following its instructions
named it back — which the clamp in `decompose()` now drops, but only after an
LLM call was spent on a plan that then falls back to something the buyer did
not ask for. The instructions are the cheaper place to be right.

These pin the two sentences that matter rather than the whole prompt, so the
prose can be edited freely around them. Whitespace is normalized first, since
the constant is wrapped for reading.
"""

from __future__ import annotations

from app.agents.orchestrator import INSTRUCTIONS, orchestrator_agent


def _text() -> str:
    return " ".join(INSTRUCTIONS.split())


def test_the_planner_is_told_only_listed_ids_may_be_used() -> None:
    text = _text()

    assert "Use ONLY agent_ids listed in AVAILABLE_AGENTS" in text
    # Including ids the instructions themselves mention: the list is the
    # authority for this request, not the prose written for every request.
    assert "even one named elsewhere in these instructions" in text
    assert "any step naming it is discarded" in text
    # And the agent is actually built with them.
    assert orchestrator_agent.instructions == INSTRUCTIONS
