"""The real guard, improver and planner — over FakeJev/FakeClaude, or live.

Nothing here re-implements a model call. Each case goes through the app's own
entry points, in the order `/api/orchestrator/decompose` uses them:

    intent_guard.check_intent           jev battery (Haiku fallback on a jev outage)
    prompt_improver.improve             Sonnet 5.5 -> Spec          ┐ only when the guard
    prompt_improver.recheck / resolve   jev re-check of the Spec    │ allowed, with
    agents.orchestrator.draft_plan      Opus 5.5 RAW plan           ┘ --stages all

This is `intent_screening.screen_free_form`'s sequence with one difference:
the improver runs after the guard rather than beside it. The outcome is the
same (the app discards the improver's work on a refusal), the eval does not
pay for specs nobody plans from, and calling the functions directly keeps the
guard's scores and reasons, which a refusal exception does not carry.

Accounting happens at the transport seam (`claude.set_transport`,
`jev.set_transport`): every request and completion, refusals and truncations
included, is recorded against the case that made it, with its usage, its cost
from the app's own price table, and the model that actually served it. The raw
complexity choice is read from the guard's jev answer there too, so the sweep
can replay the round-up rule.

Fake mode installs `app.llm.testing.FakeJev` / `FakeClaude` scripted from each
case's LABEL (the same label-derived scores as the oracle, plus seeded noise):
it exercises the real code paths — decision rule, re-check, resolve, schema
validation, the planner's structured output — not the models. Live mode keeps
the app's real transports and gives the process its own in-memory spend
ledger, so an eval neither reads nor spends production's daily budget record
(the app's daily cap, `LLM_DAILY_SPEND_CAP_USD`, still applies to this run's
own total).
"""

from __future__ import annotations

import contextvars
import importlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.llm import claude, jev, spend
from app.llm.errors import JevUnavailable, LLMError, LLMInvalidOutput, LLMRefused, LLMTruncated, LLMUnavailable
from app.llm.errors import SpendCapReached as AppSpendCapReached
from app.services import intent_guard, prompt_improver
from app.services.intent_screening import PROVISIONAL_IMPROVER_TIER

from .contract import CaseRun, GuardObservation, PipelineUnavailable, PlanObservation, SpendCapReached, StageCall
from .dataset import Case
from .synthetic import noisy_scores

# Planner calls, by the planner lane's contract: draft_plan(request, *, tier, agents_block) -> LLMResult[ModelPlan].
PlanFn = Callable[..., Awaitable[Any]]


@dataclass
class _CaseLog:
    calls: list[StageCall] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)
    raw_tier: str | None = None


_current: contextvars.ContextVar[_CaseLog | None] = contextvars.ContextVar("eval_case_log", default=None)

_STAGE = {
    "guard.intent": "guard",
    "guard.intent.fallback": "guard_fallback",
    "improve.spec": "improve",
    "guard.spec": "recheck",
    "guard.spec.same": "recheck",
    "guard.spec.fallback": "recheck",
    "planner": "plan",
}


def _stage(purpose: str) -> str:
    return _STAGE.get(purpose, purpose)


class RecordingClaude:
    """Passes every request through and books it to the case that made it."""

    def __init__(self, inner: claude.ClaudeTransport) -> None:
        self.inner = inner

    async def complete(self, request: claude.ClaudeRequest) -> claude.Completion:
        log = _current.get()
        started = time.perf_counter()
        completion = await self.inner.complete(request)
        if log is not None:
            usage = completion.usage
            log.calls.append(
                StageCall(
                    stage=_stage(request.purpose),
                    model=request.model,
                    served_by=completion.served_by,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_input_tokens=usage.cache_read_tokens,
                    cache_creation_input_tokens=usage.cache_write_tokens,
                    cost_usd=completion.cost_usd(),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    stop_reason=completion.stop_reason,
                )
            )
            log.transcript.append({"role": "tool_call", "name": request.purpose, "content": request.user})
            log.transcript.append({"role": "tool_result", "name": request.purpose, "content": completion.text})
            if request.purpose == "guard.intent.fallback":
                try:
                    log.raw_tier = json.loads(completion.text).get("complexity")
                except (ValueError, AttributeError):
                    pass
        return completion


class RecordingJev:
    def __init__(self, inner: jev.JevTransport) -> None:
        self.inner = inner

    async def system_one(self, request: jev.JevRequest) -> jev.JevReply:
        log = _current.get()
        started = time.perf_counter()
        reply = await self.inner.system_one(request)
        if log is not None:
            log.calls.append(
                StageCall(
                    stage=_stage(request.purpose),
                    model=request.model,
                    served_by=reply.model if reply.model and reply.model != request.model else None,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    cost_usd=spend.jev_cost_usd(reply.input_tokens),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )
            answers = {k: _answer_json(v) for k, v in reply.answers.items()}
            log.transcript.append({"role": "tool_call", "name": request.purpose, "content": request.state})
            log.transcript.append({"role": "tool_result", "name": request.purpose, "content": json.dumps(answers)})
            if request.purpose == "guard.intent":
                choice = reply.answers.get("complexity")
                log.raw_tier = getattr(choice, "choice", None)
        return reply


def _answer_json(answer: Any) -> Any:
    for attr in ("noul", "choice", "score"):
        if hasattr(answer, attr):
            out = {attr: getattr(answer, attr)}
            if hasattr(answer, "confidence"):
                out["confidence"] = answer.confidence
            return out
    return repr(answer)


class _JevDown:
    """A jev transport in outage: every call fails before anything is billed."""

    async def system_one(self, request: jev.JevRequest) -> jev.JevReply:
        raise JevUnavailable("forced_fallback")


def seeded_agents() -> list[Any]:
    """The built-in catalog as `Agent`s — what AVAILABLE_AGENTS lists on a fresh boot."""
    from app.schemas import Agent
    from app.seed import _SEED

    return [
        Agent(id=i, name=n, skills=s, price=p, rep=r, status=st, runs=runs, real=real)  # type: ignore[arg-type]
        for i, n, s, p, r, st, runs, real in _SEED
    ]


def _default_planner() -> tuple[PlanFn, str, frozenset[str]]:
    """The planner lane's raw planner and the AVAILABLE_AGENTS block decompose
    would show it for the seeded catalog (no reputation history: the prior)."""
    try:
        draft_plan = importlib.import_module("app.agents.orchestrator").draft_plan
        render_agents_block = importlib.import_module("app.services.orchestrator_svc").render_agents_block
    except AttributeError as e:  # the planner lane's raw planner is not on this branch yet
        raise ImportError(str(e)) from e
    agents = seeded_agents()
    return draft_plan, render_agents_block(agents, {}), frozenset(a.id for a in agents)


@dataclass
class AppPipeline:
    name: str
    live: bool
    planner: PlanFn | None = None
    agents_block: str = ""
    offered: frozenset[str] = frozenset()

    @classmethod
    def create(
        cls,
        *,
        live: bool,
        cases: list[Case],
        stages: str = "guard",
        noise: float = 0.1,
        seed: int = 0,
        force_fallback: bool = False,
    ) -> AppPipeline:
        """Install the recording transports (around fakes unless `live`).

        With `stages == "all"` the planner is resolved here, so a branch that
        does not have it yet fails before the run, not once per case. With
        `force_fallback` every jev call fails as an outage would, so the guard
        takes its Claude Haiku 4.5 fallback path — the backup's own quality,
        measured through the guard's real fallback code."""
        planner, block, offered = _default_planner() if stages == "all" else (None, "", frozenset[str]())
        if live:
            spend.set_ledger(spend.SpendLedger(spend.InMemorySpendStore()))
            claude_inner: Any = claude.get_transport()
            jev_inner: Any = jev.get_transport()
        else:
            claude_inner, jev_inner = scripted_fakes(cases, noise=noise, seed=seed)
        if force_fallback:
            jev_inner = _JevDown()
        claude.set_transport(RecordingClaude(claude_inner))
        jev.set_transport(RecordingJev(jev_inner))
        name = ("app-live" if live else "app-fake") + ("-fallback" if force_fallback else "")
        return cls(name=name, live=live, planner=planner, agents_block=block, offered=offered)

    def _planner(self) -> PlanFn:
        if self.planner is None:
            self.planner, self.agents_block, self.offered = _default_planner()
        return self.planner

    async def run(self, intent: str, *, stages: str) -> CaseRun:
        log = _CaseLog(transcript=[{"role": "user", "content": intent}])
        token = _current.set(log)
        try:
            return await self._run(intent, stages, log)
        except AppSpendCapReached as e:
            raise SpendCapReached(str(e)) from e
        except (LLMUnavailable, JevUnavailable) as e:
            raise PipelineUnavailable("model_unavailable", f"{type(e).__name__}: {e}", log.calls) from e
        finally:
            _current.reset(token)

    async def _run(self, intent: str, stages: str, log: _CaseLog) -> CaseRun:
        decision = await intent_guard.check_intent(intent)
        if decision.verdict == "unavailable":
            raise PipelineUnavailable("guard_unavailable", "jev and the fallback guard both failed", log.calls)
        scores = dict(decision.scores)
        reasons = list(decision.reasons)
        verdict, tier = decision.verdict, decision.tier
        spec_dump: dict[str, Any] | None = None
        plan: PlanObservation | None = None

        if stages == "all" and verdict == "allow" and tier is not None:
            spec = None
            try:
                # The app writes the spec before the guard's tier is known, on
                # its provisional tier; the eval asks for the same spec.
                spec = await prompt_improver.improve(intent, PROVISIONAL_IMPROVER_TIER)
            except AppSpendCapReached:
                raise
            except LLMError as e:  # the app plans from the original on any improver failure
                reasons.append(f"no_spec:{type(e).__name__}")
            check = await prompt_improver.recheck(intent, spec) if spec is not None else None
            if check is not None:
                scores.update({f"recheck_{k}": v for k, v in check.scores.items() if k != "same_request"})
                if "same_request" in check.scores:
                    scores["same_request"] = check.scores["same_request"]
            resolution = prompt_improver.resolve(decision, check)
            if resolution.action == "unavailable":
                raise PipelineUnavailable("guard_unavailable", "the spec re-check could not run", log.calls)
            if resolution.action in ("block", "needs_detail"):
                verdict, tier = resolution.action, None
                reasons.extend(resolution.reasons)
            else:
                planned_from = spec if resolution.action == "use_spec" else intent
                spec_dump = spec.model_dump() if spec is not None else None
                plan = await self._plan(planned_from, tier)

        log.transcript.append(
            {"role": "assistant", "content": json.dumps({"verdict": verdict, "tier": tier, "reasons": reasons})}
        )
        guard = GuardObservation(verdict, tier, log.raw_tier, tuple(reasons), scores)
        return CaseRun(guard=guard, spec=spec_dump, plan=plan, calls=log.calls, transcript=log.transcript)

    async def _plan(self, request: Any, tier: str) -> PlanObservation:
        planner = self._planner()
        try:
            result = await planner(request, tier=tier, agents_block=self.agents_block)
        except LLMRefused as e:
            return PlanObservation(offered=self.offered, raw=None, refused=e.category or "refused")
        except LLMTruncated:
            return PlanObservation(offered=self.offered, raw=None, truncated=True)
        except LLMInvalidOutput:
            # The answer did not validate as the planner's own schema: a plan
            # that fails every check, not a missing one.
            return PlanObservation(offered=self.offered, raw=None)
        value = result.value
        raw = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return PlanObservation(offered=self.offered, raw=raw)


# ── fake mode: FakeJev / FakeClaude answering from the labels ──────────────

_CASE_TAG = re.compile(r"eval case ([A-Za-z0-9_.-]+)")
_OFFERED = re.compile(r"\bid=(\S+)")
_TIER = re.compile(r"overall complexity is (low|moderate|complex)")
_STEPS_FOR_TIER = {"low": 1, "moderate": 3, "complex": 5}


def scripted_fakes(cases: list[Case], *, noise: float, seed: int) -> tuple[Any, Any]:
    """FakeClaude and FakeJev that answer each case from its label."""
    from app.agents.workers.prompt_safety import sanitize_untrusted
    from app.llm import testing
    from app.services.intent_guard import classifier_state

    by_state = {classifier_state(c.intent): c for c in cases}
    by_id = {c.id: c for c in cases}
    fake_jev = testing.FakeJev()
    fake_claude = testing.FakeClaude()

    def guard_answers(request: Any) -> dict[str, Any]:
        case = by_state[request.state]
        s, tier = noisy_scores(case, noise, seed)
        return {
            "injection": s["injection"],
            "harmful": s["harmful"],
            "severity": testing.score(s["severity"]),
            "real_request": s["real_request"],
            "complexity": testing.choice(tier, confidence=s.get("complexity_confidence", 0.9)),
        }

    def spec_answers(request: Any) -> dict[str, Any]:
        found = _CASE_TAG.search(request.state)
        case = by_id[found.group(1)] if found else None
        s, _ = noisy_scores(case, noise, seed + 1) if case else ({}, "low")
        answers: dict[str, Any] = {
            "injection": s.get("injection", 0.0),
            "harmful": s.get("harmful", 0.0),
            "severity": testing.score(s.get("severity", 0.0)),
            "same_request": 0.9,
        }
        return {q: answers[q] for q in request.questions}

    def improve(request: Any) -> dict[str, Any]:
        # The improver reads the fenced, sanitized intent; match on that form.
        case = max(
            (c for c in cases if sanitize_untrusted(c.intent)[:80] in request.user),
            key=lambda c: len(c.intent),
        )
        return {
            "goal": case.intent.strip()[:250],
            "deliverable": f"What eval case {case.id} asks for",
            "constraints": [],
            "done_criteria": ["The request is met"],
            "summary": case.intent.strip()[:250],
        }

    def plan(request: Any) -> dict[str, Any]:
        offered = sorted(set(_OFFERED.findall(request.system)))
        found = _TIER.search(request.user)
        tier = found.group(1) if found else "complex"
        steps = offered[: _STEPS_FOR_TIER.get(tier, 1)]
        return {
            "steps": [
                {"agent_id": a, "rationale": "scripted step", "est_eta_seconds": 1.0, "tier": tier} for a in steps
            ]
        }

    def fallback_assessment(request: Any) -> dict[str, Any]:
        # The fallback reads the fenced intent; answer from the case it names.
        case = max(
            (c for c in cases if sanitize_untrusted(c.intent)[:80] in request.user),
            key=lambda c: len(c.intent),
        )
        s, tier = noisy_scores(case, noise, seed)
        return {
            "injection": s["injection"],
            "harmful": s["harmful"],
            "severity": int(round(s["severity"])),
            "real_request": s["real_request"],
            "complexity": tier,
            "complexity_confidence": s.get("complexity_confidence", 0.9),
        }

    fake_claude.respond_with(fallback_assessment, purpose="guard.intent.fallback")
    fake_jev.respond_with(guard_answers, purpose="guard.intent")
    fake_jev.respond_with(spec_answers, purpose="guard.spec")
    fake_jev.respond_with(spec_answers, purpose="guard.spec.same")
    fake_claude.respond_with(improve, purpose="improve.spec")
    fake_claude.respond_with(plan, purpose="planner")
    return fake_claude, fake_jev
