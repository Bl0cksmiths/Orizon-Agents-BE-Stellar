"""Every pause and jitter that moved onto `app.pacing` kept its exact range.

Each caller used to inline `low + random.random() * span`; these pin the
`(low, span)` each one now hands the helper, so a range can't drift unnoticed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import pacing
from app.agents.registry import WORKERS
from app.agents.workers.mock import MockWorker
from app.demo_kits.registry import ALL_KITS
from app.pdax import resilience
from app.pdax.errors import PdaxError
from app.services import orchestrator_svc


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    """Record each `pacing.pause(low, span)` instead of sleeping."""
    seen: list[tuple[float, float]] = []

    async def record(low_s: float, span_s: float, source: object = None) -> None:
        seen.append((low_s, span_s))

    monkeypatch.setattr(pacing, "pause", record)
    return seen


def _kit() -> dict[str, Any]:
    """A demo kit that ships a baked artifact, as the run hands it to workers."""
    return next(k for k in ALL_KITS if k.artifact_path and k.load_artifact()).model_dump()


def _run(agent_id: str, context: dict[str, Any] | None) -> dict[str, Any]:
    return asyncio.run(WORKERS[agent_id].run("a pomodoro timer", "r", context=context))


def test_the_mock_worker_pauses_point_two_to_point_six(pauses: list[tuple[float, float]]) -> None:
    asyncio.run(MockWorker("agt_mock", "mock.v1").run("i", "r"))
    assert pauses == [(0.2, 0.4)]


def test_design_tokens_kit_path_pauses_as_before(pauses: list[tuple[float, float]]) -> None:
    _run("agt_02k2", {"kit": _kit()})
    assert pauses == [(0.25, 0.15)]


def test_seo_brief_kit_path_pauses_as_before(pauses: list[tuple[float, float]]) -> None:
    _run("agt_05x7", {"kit": _kit()})
    assert pauses == [(0.3, 0.2)]


def test_deploy_pauses_as_before(pauses: list[tuple[float, float]]) -> None:
    _run("agt_08j2", {})
    assert pauses == [(0.28, 0.18)]


def test_research_kit_path_pauses_as_before(pauses: list[tuple[float, float]]) -> None:
    _run("agt_09l5", {"kit": _kit()})
    assert pauses == [(0.35, 0.25)]


def test_code_gen_and_critic_baked_paths_pause_as_before(pauses: list[tuple[float, float]]) -> None:
    kit = _kit()
    draft = _run("agt_11c0", {"kit": kit})
    assert draft["source"] == "baked"
    _run("agt_12r0", {"kit": kit, "code.gen": draft})
    assert pauses == [(0.4, 0.6), (0.4, 0.6)]


def test_the_kit_thinking_time_is_one_point_four_to_two_point_four(pauses: list[tuple[float, float]]) -> None:
    asyncio.run(orchestrator_svc._kit_thinking())
    assert pauses == [(1.4, 1.0)]


def _always_503(_attempt: int) -> Any:
    raise PdaxError("boom", http_status=503)


def test_pdax_retry_backoff_keeps_its_half_to_one_and_a_half_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[float, float]] = []
    slept: list[float] = []

    def fixed(low: float = 0.5, span: float = 1.0, source: object = None) -> float:
        asked.append((low, span))
        return 1.0

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    async def fn(attempt: int) -> str:
        return _always_503(attempt)

    monkeypatch.setattr(pacing, "jitter", fixed)
    monkeypatch.setattr(resilience, "asyncio", SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))
    with pytest.raises(PdaxError):
        asyncio.run(resilience.with_retries(fn, attempts=4, base_delay=0.3, max_delay=1.0))
    assert asked == [(0.5, 1.0)] * 3
    assert slept == [pytest.approx(0.3), pytest.approx(0.6), pytest.approx(1.0)]  # capped at max_delay


def test_pdax_retry_delays_stay_inside_the_jittered_window(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    async def fn(attempt: int) -> str:
        return _always_503(attempt)

    monkeypatch.setattr(resilience, "asyncio", SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))
    for _ in range(200):
        with pytest.raises(PdaxError):
            asyncio.run(resilience.with_retries(fn, attempts=2, base_delay=0.3))
    assert len(slept) == 200
    assert all(0.15 <= d < 0.45 for d in slept)
