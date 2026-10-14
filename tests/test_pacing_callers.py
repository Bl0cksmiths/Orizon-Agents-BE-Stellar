"""Every pause and jitter that moved onto `app.pacing` kept its exact range.

Each caller used to inline `low + random.random() * span`; these pin the
`(low, span)` each one now hands the helper, so a range can't drift unnoticed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import pacing
from app.agents.registry import WORKERS
from app.agents.workers.mock import MockWorker
from app.demo_kits.registry import ALL_KITS
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
