from __future__ import annotations

from typing import Any

from ... import pacing
from .base import Worker


class MockWorker(Worker):
    real = False

    def __init__(self, id_: str, name: str) -> None:
        self.id = id_
        self.name = name

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await pacing.pause(0.2, 0.4)
        return {
            "summary": f"{self.name} simulated output for intent",
            "mock": True,
            "intent_excerpt": intent[:80],
        }
