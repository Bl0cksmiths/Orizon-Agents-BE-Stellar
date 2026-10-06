"""Each built-in worker builds on what the steps before it produced.

On FakeClaude only: the right upstream content reaches each worker's prompt,
inside the UPSTREAM_OUTPUTS fence and after the fenced request, and a step
with nothing upstream asks exactly what it asked before. The handoff itself
(map, bounds, scrub) is pinned in tests/test_upstream_handoff.py; the run
trace's "uses output from" line in tests/test_handoff_trace.py.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from app.agents.registry import WORKERS
from app.agents.workers.base import ModelWorker
from app.agents.workers.mock import MockWorker
from app.config import settings
from app.llm.testing import FakeClaude

INTENT = "a landing page for a neighbourhood bike repair co-op"
RATIONALE = "the buyer needs it"
FENCE_BEGIN = "BEGIN UPSTREAM_OUTPUTS"
FENCE_END = "END UPSTREAM_OUTPUTS"


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def research() -> dict[str, Any]:
    return {
        "summary": "Co-ops thrive on volunteer stands.",
        "findings": [{"claim": "RESEARCH-CLAIM riders want same-week fixes.", "confidence": 0.8}],
        "sources": ["community survey"],
        "source": "llm",
    }


def seo() -> dict[str, Any]:
    return {
        "summary": "Local repair intent.",
        "keywords": ["SEO-KEYWORD bike repair co-op"],
        "audiences": ["SEO-AUDIENCE commuters"],
        "source": "llm",
    }


def ctx(**outputs: Any) -> dict[str, Any]:
    return {"kit": None, "intent": INTENT, **outputs}


def fenced_body(prompt: str) -> str:
    """The text inside the prompt's UPSTREAM_OUTPUTS fence."""
    assert prompt.count(FENCE_BEGIN) == 1, prompt
    return prompt[prompt.index(FENCE_BEGIN) : prompt.index(FENCE_END)]


def run(agent_id: str, context: dict[str, Any] | None) -> dict[str, Any]:
    return asyncio.run(WORKERS[agent_id].run(INTENT, RATIONALE, context=context))


# ── the base hook ───────────────────────────────────────────────────────────


class _Reader(ModelWorker):
    id = "agt_test"
    name = "copywrite.v3"  # reads what copywrite reads
    real = True
    default_tier = "low"
    reads_upstream: ClassVar[bool] = True
    deterministic = False

    def _deterministic(self, context: dict[str, Any] | None) -> bool:
        return self.deterministic

    async def run(self, intent: str, rationale: str, context: Any = None, *, tier: Any = None) -> dict[str, Any]:
        return {}


def test_a_reading_worker_reports_the_roles_its_handoff_carries() -> None:
    assert _Reader().upstream_sources(ctx(**{"research.pro": research(), "seo.brief": seo()})) == [
        "seo.brief",
        "research.pro",
    ]


def test_a_deterministic_step_reports_no_upstream() -> None:
    reader = _Reader()
    reader.deterministic = True
    assert reader.upstream_sources(ctx(**{"seo.brief": seo()})) == []


def test_a_worker_that_does_not_read_upstream_never_claims_to() -> None:
    mock = MockWorker("agt_03d9", "code.next")  # code.next's role reads a lot; the mock reads nothing
    assert mock.upstream_sources(ctx(**{"seo.brief": seo(), "design.figma": {"palette": {"bg": "#000"}}})) == []
    assert not mock.handoff(ctx(**{"seo.brief": seo()}))
