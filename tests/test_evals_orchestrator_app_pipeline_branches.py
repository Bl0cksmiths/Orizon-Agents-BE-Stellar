"""Characterization tests for the app pipeline's less-travelled branches.

The guard, improver, re-check and resolver are replaced with stubs so each
branch of `AppPipeline._run` and `RecordingClaude.complete` is reached on
purpose: an improver failure, a re-check without a same-request score, an
unavailable or refusing resolution, a streamed call with the caller's own
callback, and a fallback answer that is not JSON.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.llm import claude, testing
from app.llm.errors import LLMError
from app.llm.errors import SpendCapReached as AppSpendCapReached
from app.services import intent_guard, prompt_improver
from app.services.intent_guard import GuardDecision
from app.services.prompt_improver import Resolution, Spec, SpecCheck
from evals.orchestrator.app_pipeline import AppPipeline, RecordingClaude, _CaseLog, _current
from evals.orchestrator.contract import PipelineUnavailable, PlanObservation, SpendCapReached

_ALLOW = GuardDecision(verdict="allow", tier="low", reasons=["watch"], scores={"injection": 0.1})
_SPEC = Spec(goal="g", deliverable="d", summary="s")


def _pipeline(monkeypatch: pytest.MonkeyPatch, *, check: SpecCheck | None, resolution: Resolution) -> AppPipeline:
    async def check_intent(intent: str) -> GuardDecision:
        return _ALLOW

    async def recheck(original: str, spec: Spec) -> SpecCheck | None:
        return check

    monkeypatch.setattr(intent_guard, "check_intent", check_intent)
    monkeypatch.setattr(prompt_improver, "recheck", recheck)
    monkeypatch.setattr(prompt_improver, "resolve", lambda decision, chk: resolution)
    pipeline = AppPipeline(name="stub", live=False)
    planned: list[Any] = []

    async def plan(request: Any, tier: str) -> PlanObservation:
        planned.append((request, tier))
        return PlanObservation(
            offered=frozenset(), raw={"planned_from": request if isinstance(request, str) else "spec"}
        )

    monkeypatch.setattr(pipeline, "_plan", plan)
    return pipeline


def _improves(monkeypatch: pytest.MonkeyPatch) -> None:
    async def improve(intent: str, tier: str) -> Spec:
        return _SPEC

    monkeypatch.setattr(prompt_improver, "improve", improve)


def test_an_improver_failure_plans_from_the_original_and_says_why(monkeypatch):
    async def improve(intent: str, tier: str) -> Spec:
        raise LLMError("down")

    monkeypatch.setattr(prompt_improver, "improve", improve)
    pipeline = _pipeline(monkeypatch, check=None, resolution=Resolution(action="use_original", reasons=["no_spec"]))
    run = asyncio.run(pipeline.run("hello", stages="all"))
    assert run.guard.verdict == "allow"
    assert run.guard.tier == "low"
    assert run.guard.reasons == ("watch", "no_spec:LLMError")
    assert run.guard.scores == {"injection": 0.1}
    assert run.spec is None
    assert run.plan is not None
    assert run.plan.raw == {"planned_from": "hello"}


def test_a_spend_cap_in_the_improver_stops_the_run(monkeypatch):
    async def improve(intent: str, tier: str) -> Spec:
        raise AppSpendCapReached(spent_usd=5.0, cap_usd=5.0, retry_after=60)

    monkeypatch.setattr(prompt_improver, "improve", improve)
    pipeline = _pipeline(monkeypatch, check=None, resolution=Resolution(action="use_original"))
    with pytest.raises(SpendCapReached):
        asyncio.run(pipeline.run("hello", stages="all"))


def test_a_recheck_without_same_request_keeps_only_prefixed_scores(monkeypatch):
    _improves(monkeypatch)
    check = SpecCheck(verdict="clean", scores={"injection": 0.2})
    pipeline = _pipeline(monkeypatch, check=check, resolution=Resolution(action="use_spec"))
    run = asyncio.run(pipeline.run("hello", stages="all"))
    assert run.guard.scores == {"injection": 0.1, "recheck_injection": 0.2}
    assert run.spec == _SPEC.model_dump()
    assert run.plan is not None
    assert run.plan.raw == {"planned_from": "spec"}


def test_a_recheck_with_same_request_keeps_it_unprefixed(monkeypatch):
    _improves(monkeypatch)
    check = SpecCheck(verdict="clean", scores={"harmful": 0.3, "same_request": 0.9})
    pipeline = _pipeline(monkeypatch, check=check, resolution=Resolution(action="use_original", reasons=["watch"]))
    run = asyncio.run(pipeline.run("hello", stages="all"))
    assert run.guard.scores == {"injection": 0.1, "recheck_harmful": 0.3, "same_request": 0.9}
    assert run.guard.reasons == ("watch",)
    assert run.plan is not None
    assert run.plan.raw == {"planned_from": "hello"}


def test_an_unavailable_recheck_is_a_pipeline_error(monkeypatch):
    _improves(monkeypatch)
    pipeline = _pipeline(monkeypatch, check=None, resolution=Resolution(action="unavailable"))
    with pytest.raises(PipelineUnavailable) as caught:
        asyncio.run(pipeline.run("hello", stages="all"))
    assert caught.value.failure_class == "guard_unavailable"
    assert str(caught.value.detail) == "the spec re-check could not run"


@pytest.mark.parametrize("action", ["block", "needs_detail"])
def test_a_refusing_resolution_replaces_the_verdict_and_skips_the_plan(monkeypatch, action):
    _improves(monkeypatch)
    check = SpecCheck(verdict="drifted", scores={"same_request": 0.1})
    pipeline = _pipeline(monkeypatch, check=check, resolution=Resolution(action=action, reasons=["drifted"]))
    run = asyncio.run(pipeline.run("hello", stages="all"))
    assert run.guard.verdict == action
    assert run.guard.tier is None
    assert run.guard.reasons == ("watch", "drifted")
    assert run.spec is None
    assert run.plan is None
    assert run.transcript[-1]["content"] == (
        '{"verdict": "' + action + '", "tier": null, "reasons": ["watch", "drifted"]}'
    )


def _streamed(on_text: Any) -> _CaseLog:
    fake = testing.FakeClaude().reply("abc", purpose="worker.code.gen")
    claude.set_transport(RecordingClaude(fake))
    log = _CaseLog()
    token = _current.set(log)
    try:
        asyncio.run(
            claude.text(
                purpose="worker.code.gen",
                model="claude-sonnet-5-5",
                system="s",
                user="u",
                max_tokens=1_000,
                stream=True,
                on_text=on_text,
            )
        )
    finally:
        _current.reset(token)
    return log


def test_a_streamed_call_still_feeds_the_callers_sync_callback():
    seen: list[str] = []
    log = _streamed(seen.append)
    assert "".join(seen) == "abc"
    assert log.calls[0].first_token_ms is not None


def test_a_streamed_call_still_awaits_the_callers_async_callback():
    seen: list[str] = []

    async def on_text(delta: str) -> None:
        seen.append(delta)

    _streamed(on_text)
    assert "".join(seen) == "abc"


def test_a_call_outside_a_case_is_passed_through_unbooked():
    fake = testing.FakeClaude().reply("hi", purpose="worker.x")
    claude.set_transport(RecordingClaude(fake))
    result = asyncio.run(
        claude.text(purpose="worker.x", model="claude-sonnet-5-5", system="s", user="u", max_tokens=10)
    )
    assert result.value == "hi"
    assert len(fake.calls_for("worker.x")) == 1


@pytest.mark.parametrize("reply", ["not json", "[1, 2]"])
def test_a_fallback_answer_that_is_not_a_json_object_leaves_the_raw_tier_unset(reply):
    fake = testing.FakeClaude().reply(reply, purpose="guard.intent.fallback")
    claude.set_transport(RecordingClaude(fake))
    log = _CaseLog()
    token = _current.set(log)
    try:
        asyncio.run(
            claude.text(purpose="guard.intent.fallback", model="claude-haiku-4-5", system="s", user="u", max_tokens=10)
        )
    finally:
        _current.reset(token)
    assert log.raw_tier is None
    assert [c.stage for c in log.calls] == ["guard_fallback"]
    assert [t["role"] for t in log.transcript] == ["tool_call", "tool_result"]
