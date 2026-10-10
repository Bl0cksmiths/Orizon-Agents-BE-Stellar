"""A curated kit intent and the per-client /decompose budget.

On Claude every kit intent is screened (`intent_screening.screen_kit`): the
paid jev guard, and the Claude fallback guard when jev is down — calls the
daily spend cap does not stop. So there a kit spends the same per-client
budget a free-form request does, and is refused with the same 429 before it
reaches the guard. On the legacy provider a kit's plan reads no model at all,
and it stays outside the budget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.demo_kits import detect_kit
from app.llm.testing import FakeClaude, FakeJev, choice, score
from app.routers import orchestrator as router
from app.schemas import DecomposeResponse
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.state import state

DECOMPOSE = "/api/orchestrator/decompose"
KIT_INTENT = "make me a tetris game"
FREE_FORM = "write a haiku about databases"
CALLER = "81.2.69.160"
OTHER_CALLER = "2.125.160.216"


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A fresh seeded registry and no kit thinking pause, for the real pipeline."""

    async def _no_pause() -> None:
        return None

    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_pause)
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


def _answering(calls: list[str]) -> Callable[[str], Awaitable[DecomposeResponse]]:
    async def _decompose(intent: str) -> DecomposeResponse:
        calls.append(intent)
        return DecomposeResponse(plan_id="pln_test", intent=intent, steps=[], total_usdc=0.0, total_eta=0.0)

    return _decompose


def _post(client: TestClient, intent: str, *, caller: str = CALLER) -> Any:
    return client.post(DECOMPOSE, json={"intent": intent}, headers={"X-Forwarded-For": caller})


def _guard_allows(fake_jev: FakeJev) -> None:
    fake_jev.answer(
        {
            "injection": 0.02,
            "harmful": 0.01,
            "severity": score(0),
            "real_request": 0.96,
            "complexity": choice("moderate", confidence=0.9),
        },
        purpose="guard.intent",
    )


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    error = {k: v for k, v in body["error"].items() if k != "request_id"}
    return {**body, "error": error}


def test_the_kit_intent_here_is_a_kit() -> None:
    assert detect_kit(KIT_INTENT) is not None
    assert detect_kit(FREE_FORM) is None


# ── Claude: a kit is screened, so it is counted ─────────────────


def test_a_kit_intent_spends_the_planner_budget_on_claude_and_never_reaches_the_guard_past_it(
    client: TestClient,
    seeded: None,
    fake_claude: FakeClaude,
    fake_jev: FakeJev,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 2)
    _guard_allows(fake_jev)
    _guard_allows(fake_jev)

    assert _post(client, KIT_INTENT).status_code == 200
    assert _post(client, KIT_INTENT).status_code == 200
    r = _post(client, KIT_INTENT)

    assert r.status_code == 429
    assert r.json()["detail"] == "decompose_rate_limited"
    assert r.json()["error"]["code"] == "decompose_rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # Refused before screening: two guard passes for two plans, none for the third.
    assert [c.purpose for c in fake_jev.calls] == ["guard.intent", "guard.intent"]
    assert fake_claude.calls == []
    assert fake_jev.pending == 0


def test_a_kit_429_is_the_free_form_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 1)
    monkeypatch.setattr(router, "decompose", _answering([]))

    assert _post(client, KIT_INTENT).status_code == 200
    kit = _post(client, KIT_INTENT)
    assert _post(client, FREE_FORM, caller=OTHER_CALLER).status_code == 200
    free_form = _post(client, FREE_FORM, caller=OTHER_CALLER)

    assert kit.status_code == free_form.status_code == 429
    assert _without_request_id(kit.json()) == _without_request_id(free_form.json())
    assert 1 <= int(kit.headers["retry-after"]) <= 60
    assert 1 <= int(free_form.headers["retry-after"]) <= 60


def test_kits_and_free_form_share_one_budget_on_claude(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 2)
    calls: list[str] = []
    monkeypatch.setattr(router, "decompose", _answering(calls))

    assert _post(client, KIT_INTENT).status_code == 200
    assert _post(client, FREE_FORM).status_code == 200
    assert _post(client, KIT_INTENT).status_code == 429
    assert _post(client, FREE_FORM).status_code == 429

    assert calls == [KIT_INTENT, FREE_FORM]
    # Per client: another caller still has its whole budget.
    assert _post(client, KIT_INTENT, caller=OTHER_CALLER).status_code == 200


# ── both providers: free-form is limited as it always was ───────


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_free_form_is_limited_on_either_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", provider)
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 1)
    calls: list[str] = []
    monkeypatch.setattr(router, "decompose", _answering(calls))

    assert _post(client, FREE_FORM).status_code == 200
    r = _post(client, FREE_FORM)

    assert r.status_code == 429
    assert r.json()["detail"] == "decompose_rate_limited"
    assert calls == [FREE_FORM]


# ── legacy: a kit reads no model, so it stays free ──────────────


def test_a_kit_on_the_legacy_provider_makes_no_model_call_and_is_not_counted(
    client: TestClient,
    seeded: None,
    fake_claude: FakeClaude,
    fake_jev: FakeJev,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 1)

    async def _no_planner(_prompt: str) -> object:
        raise AssertionError("a kit never reaches the legacy planner")

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _no_planner)

    assert [_post(client, KIT_INTENT).status_code for _ in range(3)] == [200, 200, 200]
    assert fake_claude.calls == [] and fake_jev.calls == []
