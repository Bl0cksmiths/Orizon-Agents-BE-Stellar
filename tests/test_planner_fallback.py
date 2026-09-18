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
import logging
from collections.abc import Awaitable, Callable

import pytest
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from fastapi.testclient import TestClient

from app.demo_kits import detect_kit
from app.schemas import DecomposeResponse, Plan, PlanStep
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


def test_a_planner_call_that_raises_serves_the_fallback_plan(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # agno turns the provider's own errors into a failed run (above), so what
    # can still raise out of `arun` is everything around the model call. It
    # gets the answer a failed run gets, not a 502.
    async def _refused(_prompt: str) -> object:
        raise ConnectionRefusedError(111, "Connection refused")

    resp = _decompose(monkeypatch, _refused)

    assert resp.planner_fallback is True
    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert _stored_ids(resp) == ["agt_01h8"]


def _code_gen_plan() -> Plan:
    """The plan a real model returns for a build, so a fallback cannot pass for it."""
    step = PlanStep(agent_id="agt_11c0", rationale="model-chosen step", est_price_usdc=0.05, est_eta_seconds=1.0)
    return Plan(steps=[step])


# What agno's `arun` really returns when the planner produced nothing usable —
# a RunOutput, as in production, rather than a shape invented for the test.
@pytest.mark.parametrize(
    "run",
    [
        pytest.param(RunOutput(status=RunStatus.error, content="Connection error."), id="provider-error"),
        pytest.param(RunOutput(status=RunStatus.cancelled, content="Run was cancelled"), id="cancelled"),
        pytest.param(RunOutput(status=RunStatus.completed, content="Sure! Step 1: ..."), id="unparsed-answer"),
        pytest.param(RunOutput(status=RunStatus.completed, content=None), id="no-answer"),
        # agno's verdict wins over a plan-shaped content: an output guardrail
        # that rejects a parsed plan leaves it in `content` on a failed run.
        pytest.param(RunOutput(status=RunStatus.error, content=_code_gen_plan()), id="plan-on-failed-run"),
    ],
)
def test_a_planner_run_with_no_usable_plan_serves_the_fallback_plan(
    seeded: object, monkeypatch: pytest.MonkeyPatch, run: RunOutput
) -> None:
    async def _arun(_prompt: str) -> RunOutput:
        return run

    resp = _decompose(monkeypatch, _arun)

    assert resp.planner_fallback is True
    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]
    assert _stored_ids(resp) == ["agt_01h8"]


def test_the_provider_message_is_logged_redacted_and_never_returned(
    seeded: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    hermetic_settings: object,
) -> None:
    # An OpenAI 401 once agno has made it the run's content. It quotes the
    # rejected key back: the configured one whole, as it does a short key, and
    # another only partly masked.
    configured = "sk-configured-0123456789"
    monkeypatch.setattr(hermetic_settings, "openai_api_key", configured)
    message = f"Incorrect API key provided: {configured}. Also tried sk-proj-abc*****wxyz."

    async def _arun(_prompt: str) -> RunOutput:
        return RunOutput(status=RunStatus.error, content=message)

    with caplog.at_level(logging.WARNING, logger=orchestrator_svc.logger.name):
        resp = _decompose(monkeypatch, _arun)

    # The buyer is told a fallback was served, never why the provider failed.
    assert "Incorrect API key" not in resp.model_dump_json()
    logged = [r.getMessage() for r in caplog.records if r.name == orchestrator_svc.logger.name]
    assert len(logged) == 1
    assert "serving the fallback plan" in logged[0]
    # What is worth debugging survives; the keys do not.
    assert "Incorrect API key provided" in logged[0]
    assert "sk-configured" not in logged[0]
    assert "abc*****wxyz" not in logged[0]


def test_a_hung_planner_still_times_out_and_mints_no_plan(
    seeded: object, monkeypatch: pytest.MonkeyPatch, hermetic_settings: object
) -> None:
    # The one planner failure that is NOT degraded. The caller has already
    # waited out the whole budget, and the router answers it with 504
    # `decompose_timeout` (tests/test_orchestrator_timeout.py) — never with a
    # fallback plan served after the deadline it was promised.
    monkeypatch.setattr(hermetic_settings, "decompose_timeout_seconds", 0.05)
    # One slot, so a slot the timeout failed to give back would show.
    monkeypatch.setattr(hermetic_settings, "decompose_max_concurrent", 1)

    async def _hangs(_prompt: str) -> RunOutput:
        await asyncio.sleep(30)
        return RunOutput(status=RunStatus.completed, content=_code_gen_plan())

    before = set(state.plans)
    with pytest.raises(TimeoutError):
        _decompose(monkeypatch, _hangs)
    assert set(state.plans) == before
    assert not orchestrator_svc._decompose_gate().locked()


def test_a_failed_planner_call_gives_its_planning_slot_back(
    seeded: object, monkeypatch: pytest.MonkeyPatch, hermetic_settings: object
) -> None:
    # One slot, and a budget short enough to fail fast: a slot kept by any
    # failure below would leave the next request queued behind it until the
    # budget ran out, and every free-form intent after that a 504.
    monkeypatch.setattr(hermetic_settings, "decompose_max_concurrent", 1)
    monkeypatch.setattr(hermetic_settings, "decompose_timeout_seconds", 2.0)

    async def _refused(_prompt: str) -> RunOutput:
        raise ConnectionRefusedError(111, "Connection refused")

    async def _failed_run(_prompt: str) -> RunOutput:
        return RunOutput(status=RunStatus.error, content="Connection error.")

    for planner in (_refused, _failed_run, _refused, _failed_run):
        assert _decompose(monkeypatch, planner).planner_fallback is True
    assert not orchestrator_svc._decompose_gate().locked()
