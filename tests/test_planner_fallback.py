"""A planner that fails is served the fallback plan, never a 502 (BLO-121).

agno does not raise when the planning model cannot be reached: `Agent.arun`
catches the provider's exception, marks the run `RunStatus.error` and returns
the error message as the run's `content` — the field that holds the `Plan` on
success. `decompose()` read that field as a Plan unconditionally, so a blank
OPENAI_API_KEY, a refused connection or an upstream 5xx surfaced as
`'str' object has no attribute 'steps'` inside the clamp, and every free-form
intent answered 502 `decompose_failed`.

Fixtures are local rather than imported from the neighbouring planner suites,
as those suites do themselves, so this file fails for its own reasons only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient

from app.demo_kits import detect_kit
from app.schemas import DecomposeResponse
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.state import state

# Matches no DemoKit, so decompose() takes the free-form path — the intent
# BLO-121 was reported with.
FREE_FORM_INTENT = "write a haiku about the sea"


@pytest.fixture()
def seeded() -> object:
    """Fresh 12-agent registry, restored after."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


def _decompose(monkeypatch: pytest.MonkeyPatch, planner: Callable[[str], Awaitable[object]]) -> DecomposeResponse:
    """Plan FREE_FORM_INTENT through the public entry point, `planner` standing
    in for the model. Reputation is the hermetic suite's prior for every agent,
    which clears the floor, so the whole seeded catalog is offered."""
    assert detect_kit(FREE_FORM_INTENT) is None
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", planner)
    return asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))


def _stored_ids(resp: DecomposeResponse) -> list[str]:
    """What /execute will dispatch — the stored plan, not the response."""
    return [s.agent_id for s in state.plans[resp.plan_id].plan.steps]


def test_a_blank_api_key_serves_the_fallback_plan_not_a_502(
    seeded: object, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BLO-121 as reported, through the real agno agent: no stand-in planner,
    # only no key. agno raises ModelAuthenticationError while building the
    # OpenAI client, before any request is made, and hands the message back as
    # the run's content — so this reaches no network.
    model = orchestrator_svc.orchestrator_agent.model
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(model, "api_key", None)
    # A client cached by an earlier run would carry a key and skip the check.
    monkeypatch.setattr(model, "async_client", None)

    r = client.post("/api/orchestrator/decompose", json={"intent": FREE_FORM_INTENT})

    assert r.status_code == 200
    body = r.json()
    assert body["planner_fallback"] is True
    # Every seeded agent clears the floor on the prior, so the copywriter —
    # the fallback's first choice — was offered and takes the intent.
    assert [s["agent_id"] for s in body["steps"]] == ["agt_01h8"]
    # The provider's message is for the log, not the buyer.
    assert "OPENAI_API_KEY" not in r.text
