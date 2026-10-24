"""The activity tracker that pauses database-reading background work while the API is idle."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import settings
from app.services import activity
from app.services.activity import ActivityMiddleware, ActivityTracker


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _scope(path: str, method: str = "GET", kind: str = "http") -> dict[str, Any]:
    return {"type": kind, "method": method, "path": path}


@pytest.mark.parametrize(
    "path",
    ["/api/metrics/overview", "/api/agents", "/api/adoption", "/api/tasks/abc", "/api/stellar/reputation"],
)
def test_a_request_to_the_api_counts(path: str) -> None:
    assert activity.is_meaningful(_scope(path))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_write_counts(method: str) -> None:
    assert activity.is_meaningful(_scope("/api/orchestrator/execute", method))


@pytest.mark.parametrize(
    "path",
    ["/health", "/api/health", "/readiness", "/", "/docs", "/redoc", "/openapi.json", "/wp-login.php", "/apix"],
)
def test_probes_docs_and_strays_do_not_count(path: str) -> None:
    assert not activity.is_meaningful(_scope(path))


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_a_pinger_or_a_preflight_does_not_count(method: str) -> None:
    assert not activity.is_meaningful(_scope("/api/metrics/overview", method))


def test_a_websocket_or_lifespan_scope_does_not_count() -> None:
    assert not activity.is_meaningful(_scope("/api/trace", kind="websocket"))
    assert not activity.is_meaningful({"type": "lifespan"})


def test_boot_counts_as_activity_and_the_pause_starts_after_the_idle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "keep_warm_idle_pause_seconds", 600.0)
    clock = Clock()
    tracker = ActivityTracker(clock)

    assert not tracker.idle()
    clock.now += 599.0
    assert not tracker.idle()
    assert tracker.seconds_since_last_request() == 599.0
    clock.now += 1.0
    assert tracker.idle()


def test_a_request_resumes_and_restarts_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "keep_warm_idle_pause_seconds", 600.0)
    clock = Clock()
    tracker = ActivityTracker(clock)
    clock.now += 3_600.0
    assert tracker.idle()

    tracker.note()
    assert not tracker.idle()
    assert tracker.seconds_since_last_request() == 0.0
    clock.now += 599.0
    assert not tracker.idle()


def test_the_window_is_read_from_the_settings_each_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    tracker = ActivityTracker(clock)
    clock.now += 120.0
    monkeypatch.setattr(settings, "keep_warm_idle_pause_seconds", 600.0)
    assert not tracker.idle()
    monkeypatch.setattr(settings, "keep_warm_idle_pause_seconds", 60.0)
    assert tracker.idle()


def test_the_middleware_notes_a_meaningful_request_and_passes_every_request_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    tracker = ActivityTracker(clock)
    monkeypatch.setattr(activity, "tracker", tracker)
    reached: list[str] = []

    async def inner(scope: Any, _receive: Any, _send: Any) -> None:
        reached.append(scope["path"])

    middleware = ActivityMiddleware(inner)

    clock.now += 500.0
    asyncio.run(middleware(_scope("/health"), None, None))
    assert tracker.seconds_since_last_request() == 500.0

    asyncio.run(middleware(_scope("/api/metrics/overview"), None, None))
    assert tracker.seconds_since_last_request() == 0.0
    assert reached == ["/health", "/api/metrics/overview"]


def test_the_app_counts_an_api_request_and_not_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    clock = Clock()
    tracker = ActivityTracker(clock)
    monkeypatch.setattr(activity, "tracker", tracker)
    client = TestClient(app)

    clock.now += 900.0
    for path in ("/health", "/api/health", "/readiness", "/"):
        client.get(path)
    client.head("/api/agents")
    assert tracker.seconds_since_last_request() == 900.0

    client.get("/api/agents")
    assert tracker.seconds_since_last_request() == 0.0
