"""The /decompose router: how each planning failure reaches the caller.

`decompose` itself is replaced here — these tests are about the router's
mapping from a failure to a status and code, not about planning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import orchestrator as router
from app.schemas import DecomposeResponse
from app.security import KeyedRateLimiter
from app.services import orchestrator_svc

INTENT = "write a haiku about databases"
KIT_INTENT = "pomodoro timer app"


@pytest.fixture(autouse=True)
def fresh_planner_budget(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A per-test planner budget, so no test spends another's — or the suite's."""
    monkeypatch.setattr(
        router,
        "_planner_limiter",
        KeyedRateLimiter(lambda: settings.decompose_rate_limit_per_minute),
        raising=False,
    )
    yield


def _answering(calls: list[str]) -> Callable[[str], Awaitable[DecomposeResponse]]:
    async def _decompose(intent: str) -> DecomposeResponse:
        calls.append(intent)
        return DecomposeResponse(plan_id="pln_test", intent=intent, steps=[], total_usdc=0.0, total_eta=0.0)

    return _decompose


def _raising(exc: BaseException) -> Callable[[str], Awaitable[object]]:
    async def _decompose(_intent: str) -> object:
        raise exc

    return _decompose


def _post(client: TestClient, intent: str = INTENT, *, caller: str = "203.0.113.7") -> tuple[int, str]:
    r = client.post("/api/orchestrator/decompose", json={"intent": intent}, headers={"X-Forwarded-For": caller})
    return r.status_code, r.json().get("detail")


def test_a_full_planner_queue_is_a_retryable_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "decompose", _raising(orchestrator_svc.PlannerBusyError("1 running, 1 waiting")))

    assert _post(client) == (503, "planner_busy")


# ── the planner's own per-client budget ─────────────────────────


def test_free_form_decompose_is_limited_per_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every free-form decompose is an LLM call, and the global limiter is a
    # polling budget: 1200 a minute of planner calls is a bill, not a limit.
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 2)
    calls: list[str] = []
    monkeypatch.setattr(router, "decompose", _answering(calls))

    assert _post(client)[0] == 200
    assert _post(client)[0] == 200
    r = client.post("/api/orchestrator/decompose", json={"intent": INTENT}, headers={"X-Forwarded-For": "203.0.113.7"})

    assert r.status_code == 429
    assert r.json()["detail"] == "decompose_rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # Refused before planning: the limited call never reached the planner.
    assert len(calls) == 2
    # Per client: another caller still has its whole budget.
    assert _post(client, caller="198.51.100.9")[0] == 200


def test_kit_intents_do_not_spend_the_planner_budget(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The demo path makes no LLM call, so it is never throttled by this budget.
    monkeypatch.setattr(settings, "decompose_rate_limit_per_minute", 1)
    monkeypatch.setattr(router, "decompose", _answering([]))

    assert [_post(client, KIT_INTENT)[0] for _ in range(3)] == [200, 200, 200]
    assert _post(client)[0] == 200
    assert _post(client)[0] == 429


def test_the_planner_budget_slides_and_can_be_switched_off() -> None:
    budget = 2
    limiter = KeyedRateLimiter(lambda: budget, window_seconds=60.0)

    assert limiter.hit("a", now=0.0) is None
    assert limiter.hit("a", now=10.0) is None
    assert limiter.hit("a", now=20.0) == 40  # until the first hit leaves the window
    assert limiter.hit("a", now=60.5) is None  # it has
    assert limiter.hit("b", now=20.0) is None

    budget = 0
    assert all(limiter.hit("a", now=61.0) is None for _ in range(50))


# ── what counts as an intent ────────────────────────────────────


@pytest.mark.parametrize("intent", ["   ", "\n\t \n", "  hi  ", "a" * 501, " " + "a" * 501])
def test_an_intent_that_is_blank_short_or_long_is_refused_before_planning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    # Whitespace is stripped before the 3–500 bounds apply: a blank intent
    # used to pass validation and spend one planner call on nothing.
    calls: list[str] = []
    monkeypatch.setattr(router, "decompose", _answering(calls))

    assert _post(client, intent)[0] == 422
    assert calls == []


def test_an_intent_is_planned_stripped_and_up_to_500_characters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(router, "decompose", _answering(calls))

    assert _post(client, "  " + "a" * 500 + "\n")[0] == 200
    assert _post(client, "  write a haiku  ")[0] == 200
    assert calls == ["a" * 500, "write a haiku"]


# ── what each failure answers, and what it logs ─────────────────

SECRET_INTENT = "draft a takeover offer for Acme Corp at 41 dollars a share"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError(), (504, "decompose_timeout")),
        (orchestrator_svc.NoRoutableAgentsError("nothing listed"), (503, "no_routable_agents")),
        (RuntimeError("something nobody anticipated"), (502, "decompose_failed")),
    ],
)
def test_each_planning_failure_maps_to_its_status_and_never_logs_the_intent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException,
    expected: tuple[int, str],
) -> None:
    # The buyer's intent used to be logged verbatim on every refusal and fault.
    # The line names it by length and hash instead, so repeats still correlate.
    monkeypatch.setattr(router, "decompose", _raising(exc))
    caplog.set_level("WARNING", logger="app.routers.orchestrator")

    assert _post(client, SECRET_INTENT) == expected

    logged = [r.getMessage() for r in caplog.records if r.name == "app.routers.orchestrator"]
    assert logged, "the failure was not logged at all"
    assert not any("Acme" in line or "takeover" in line for line in logged)
    assert any(router._intent_ref(SECRET_INTENT) in line for line in logged)
    assert router._intent_ref(SECRET_INTENT).startswith(f"len={len(SECRET_INTENT)} sha256=")


def test_an_unknown_plan_is_a_404(client: TestClient) -> None:
    r = client.post("/api/orchestrator/execute", json={"plan_id": "pln_nowhere"})

    assert r.status_code == 404
