from __future__ import annotations

import asyncio
import functools
import logging
import random
import re
import secrets
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from .. import money
from ..agents.model_factory import claude_workers
from ..agents.orchestrator import draft_plan, orchestrator_agent
from ..agents.registry import get_worker
from ..agents.role_cards import card_for
from ..agents.workers.base import ModelWorker
from ..agents.workers.claude_only import ClaudeOnlyWorker
from ..agents.workers.prompt_safety import fence_user_input, sanitize_untrusted
from ..agents.workers.vision_input import has_image_input
from ..config import settings
from ..demo_kits import DemoKit, detect_kit
from ..llm import provider
from ..llm.errors import LLMError, LLMRefused, SpendCapReached
from ..llm.tiers import TIERS, Tier, display_name, effort_for, model_for, planner_model
from ..schemas import (
    Agent,
    DecomposeResponse,
    GuardSummary,
    Plan,
    PlanFloorNotice,
    PlanModels,
    PlanStage,
    PlanStep,
    StoredPlan,
    TierModels,
    UnderstoodSpec,
)
from ..security import redact_secrets
from ..state import state
from . import intent_screening, reachability, reputation_svc
from .binding_registry import is_dispatchable
from .plan_notices import (
    below_floor_exclusion,
    external_policy_notice,
    no_image_exclusion,
    no_input_exclusion,
    provider_exclusion,
    relaxation,
    simulated_exclusion,
    substitution,
    unbound_exclusions,
    unreachable_exclusion,
    unreachable_exclusions,
)
from .prompt_improver import Spec
from .registry_sync import MAX_AGENT_NAME_CHARS
from .request_signals import has_translation_target

logger = logging.getLogger(__name__)

# The reputation floor may never starve the planner of choices.
_MIN_ROUTABLE_AGENTS = 3


class NoRoutableAgentsError(RuntimeError):
    """No agent can take a step: nothing is both listed and dispatchable.

    The starvation backstop can relax the reputation floor, but it cannot
    conjure an agent — it only re-admits from the listed, dispatchable set, and
    when that set is empty (every operator delisted, every endpoint unbound)
    there is nothing honest to plan with. The one thing this path must never do
    about it is route to an agent it did not offer, which is what the old
    hardcoded fallback did. So the request is refused instead, before any LLM
    call is spent on a prompt with no agents in it.

    A distinct type because it is not a planner failure and not a bad request:
    it is the service being temporarily unable to serve (503, retryable once an
    agent is relisted or bound), and a caller has to be able to tell it apart
    from an upstream fault to say so.
    """


class PlannerBusyError(RuntimeError):
    """Every planning slot is taken and the wait queue is full.

    The request was fine and the condition clears as soon as a planning call
    finishes, so this is a retryable 503 — refused at once, before it holds a
    connection in a queue it would most likely time out of anyway.
    """


class _PlanGate:
    """Concurrency gate on the free-form planning call, with a BOUNDED wait queue.

    `limit` calls run at once; up to `max_waiting` more wait for a slot, first
    come first served, and anything past that raises `PlannerBusyError`. The
    queue used to be unbounded: every waiter held a connection for up to the
    whole decompose budget, so a burst bought a pile of stalled requests and
    then a pile of planning calls.

    Not an `asyncio.Semaphore`, which binds itself to the first event loop a
    waiter contends on and then raises in any other — harmless in production's
    single loop, and a trap for every test that plans under contention in a
    second `asyncio.run`. The waiters here are futures created on whichever
    loop is running at the time, so nothing outlives the loop it waited on.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def waiting(self) -> int:
        return sum(1 for w in self._waiters if not w.done())

    def locked(self) -> bool:
        """Whether a new caller would have to wait for a slot."""
        return self.in_flight >= self.limit or self.waiting > 0

    def _release(self) -> None:
        # Hand the slot straight to the next live waiter, so a newcomer can
        # never jump the queue between a release and the waiter waking up.
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self.in_flight -= 1

    @asynccontextmanager
    async def slot(self, max_waiting: int) -> AsyncIterator[None]:
        if self.locked():
            if self.waiting >= max_waiting:
                raise PlannerBusyError(f"{self.in_flight} planning call(s) running and {self.waiting} waiting")
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            except BaseException:
                # Cancelled or timed out while queued. If a release had
                # already handed this caller the slot, pass it on; otherwise
                # just leave the queue.
                if waiter.done() and not waiter.cancelled():
                    self._release()
                elif waiter in self._waiters:
                    self._waiters.remove(waiter)
                raise
        else:
            self.in_flight += 1
        try:
            yield
        finally:
            self._release()


# Gate on the free-form planning LLM call. /execute's fan-out is bounded by
# orchestrator_max_concurrent (execution_svc); this is the same protection for
# /decompose, whose non-kit path makes a real LLM call per request. Sized at
# call time from settings like execute_plan's ceiling — rebuilt only when the
# configured limit changes (production never does; tests tune it).
_plan_gate: _PlanGate | None = None


def _decompose_gate() -> _PlanGate:
    global _plan_gate
    limit = max(1, settings.decompose_max_concurrent)
    if _plan_gate is None or _plan_gate.limit != limit:
        _plan_gate = _PlanGate(limit)
    return _plan_gate


# ── Curated 6-step pipeline used when the intent matches a DemoKit ─────────
# (agent_id, rationale-template). Prices come from the registry at build time.
_KIT_PIPELINE: list[tuple[str, str]] = [
    ("agt_09l5", "extract feature brief + edge cases for the build"),
    ("agt_05x7", "produce brand identity: name, tagline, audience"),
    ("agt_02k2", "lock design tokens: palette, typography, motion"),
    ("agt_11c0", "implement single-file HTML using brief + tokens"),
    ("agt_12r0", "polish pass: a11y, motion, persistence, edge cases"),
    ("agt_08j2", "seal artifact + record on-chain proof"),
]

# ETAs are rough but realistic per agent for a kit run.
_KIT_ETAS: dict[str, float] = {
    "agt_09l5": 0.6,  # research (deterministic from kit)
    "agt_05x7": 0.5,  # seo brief (deterministic from kit)
    "agt_02k2": 0.4,  # design tokens (deterministic from kit)
    "agt_11c0": 2.6,  # code.gen (real LLM call — heaviest step)
    "agt_12r0": 1.8,  # code.critic (real LLM call)
    "agt_08j2": 0.4,  # deploy (deterministic seal)
}

# The model tier each kit ROLE runs on, before the request's own tier caps it.
# The deterministic roles are low: they read the kit, not a model. code.gen
# serves a baked artifact where the kit has one and code.critic polishes it —
# real model work, but bounded by a fixed brief, so moderate rather than the
# planner-grade tier.
_KIT_TIERS: dict[str, Tier] = {
    "agt_09l5": "low",
    "agt_05x7": "low",
    "agt_02k2": "low",
    "agt_11c0": "moderate",
    "agt_12r0": "moderate",
    "agt_08j2": "low",
}


def _is_listed(agent: Agent) -> bool:
    """Whether this agent's operator still wants work routed to it.

    `AgentRegistry.set_active(id, false)` is the on-chain delisting control, and
    `registry_sync` maps it to `status == "offline"` — the only producer of that
    value anywhere. Until now nothing in routing read the field, so the one
    control an operator has for taking an agent out of service did nothing; this
    predicate is what makes it real.

    The rule is deliberately NEGATIVE — offline is withdrawn, anything else is
    available — and not `status == "online"`, which is a different and wrong
    rule. The seeded catalog ships two agents as "idle" (`agt_04m1`, `agt_06q4`
    in app/seed.py), which means "nothing in flight right now", not "withdrawn".
    Routing on equality would drop two working agents out of the twelve-agent
    demo catalog to enforce a flag neither of their operators ever set.

    A seeded agent can only ever carry the status `seed.py` gave it: the sync
    loop skips the `agt_` namespace outright, so nothing on-chain can delist a
    worker-backed catalog agent.
    """
    return agent.status != "offline"


class _RegistrySnapshot(NamedTuple):
    """The registry as ONE decompose sees it: read once, handed to every stage.

    `agents` is every entry, in registry order; `routable` is the listed,
    dispatchable subset — the only agents any planning path may put in a plan,
    and so the only ones whose reputation is worth reading.

    Taken once, before the reputation read, and passed down rather than
    re-read by each stage. The registry is live — `registry_sync` indexes
    permissionless registrations and operators bind, unbind and delist while a
    plan is being built — and a stage that read it again could meet an agent
    the reputation snapshot never covered. That agent had no entry, so it
    passed the floor on "no entry" and was offered, ranked and even promoted to
    a kit slot on the rating its own registrant wrote. With one snapshot, every
    stage agrees on the agent set the reputation read was taken for.

    The two point-of-use checks — the clamp and `_fallback_agent` — still ask
    the live registry, deliberately: they only ever NARROW this set, dropping
    an agent delisted or unbound while the planner ran, and never widen it.

    `unreachable` is the listed, dispatchable agents a FRESH failed health
    check stands against (D-084): bound, so not unbound, but with nothing
    answering at the endpoint. They are kept out of `routable` — no backstop
    may re-admit them, since relaxing a rule cannot make a dead endpoint
    answer — and reported under their own reason code.
    """

    agents: tuple[Agent, ...]
    routable: tuple[Agent, ...]
    unreachable: tuple[Agent, ...] = ()
    # Listed and dispatchable, but held out by routing policy before anything
    # else judged them (`_plannable`): built-in agents whose worker only
    # simulates its output, and — while `PLANNER_ROUTE_EXTERNAL` is off —
    # external operator agents. No backstop re-admits either: relaxing the
    # floor cannot make a simulation real or reverse the owner's policy.
    simulated: tuple[Agent, ...] = ()
    external: tuple[Agent, ...] = ()
    # `PLANNER_ROUTE_EXTERNAL` as this snapshot applied it, so the notices
    # describe the same policy the routable set was cut with.
    route_external: bool = False
    # Built-in agents that run on Claude only, held out while the workers are
    # not on Claude (`_needs_absent_provider`).
    off_provider: tuple[Agent, ...] = ()


def _with_executor(step: PlanStep) -> PlanStep:
    """`step` stamped with who runs it and, for our own workers, on which model.

    Built-in is exactly what `/execute` will treat as first-party: the agent
    id has a local worker (`get_worker`). Everything else a plan can hold is a
    bound operator endpoint, whose model is the operator's business, so it is
    named as external with no model rather than with a guess.

    The model is the one the step's tier routes to on the active provider —
    `model_for(tier)` on Claude, the agno worker model on OpenAI. A step with
    no tier on the Claude path (a kit planned while the check was paused)
    falls back to the worker's own default tier, which is what it will run on.
    Stamped once, in each builder's last pass over its steps, so the kit path,
    the free-form clamp and the fallback cannot disagree about it.
    """
    worker = get_worker(step.agent_id)
    if worker is None:
        return step.model_copy(update={"executor": "external", "model": None})
    if provider.active_provider() != "anthropic":
        return step.model_copy(update={"executor": "built_in", "model": settings.worker_model})
    # A built-in LLM worker reports the tier it will really run on — its
    # default when the step names none, capped at its `max_tier` (code.gen and
    # code.critic never run on Opus) — so the card never claims a model the
    # step will not use.
    tier = worker.effective_tier(step.tier) if isinstance(worker, ModelWorker) else step.tier
    return step.model_copy(update={"executor": "built_in", "model": model_for(tier) if tier else None})


def _is_simulated(agent_id: str) -> bool:
    """Whether a built-in worker exists for `agent_id` but only simulates its output.

    Read off the worker itself (`Worker.real`), so the day an agent's real
    worker replaces its simulation in `app/agents/registry.py`, the agent
    becomes plannable with no change here.
    """
    worker = get_worker(agent_id)
    return worker is not None and worker.real is not True


def _needs_absent_provider(agent_id: str, on_claude: bool | None = None) -> bool:
    """Whether `agent_id`'s built-in worker runs on Claude only while the workers
    are not on Claude — so every step it took would fail unattempted
    (`claude_step.MODEL_NOT_CONFIGURED`). Asked of the worker's class, so a new
    Claude-only worker is covered with no change here."""
    claude = claude_workers() if on_claude is None else on_claude
    return isinstance(get_worker(agent_id), ClaudeOnlyWorker) and not claude


def _is_external(agent_id: str) -> bool:
    """Whether `agent_id` runs anywhere but on a built-in worker (a bound operator endpoint)."""
    return get_worker(agent_id) is None


def plannable(agent: Agent, *, on_claude: bool | None = None) -> bool:
    """Whether routing POLICY lets a plan use `agent` at all — before the floor,
    the endpoint checks and the planner have any say.

    Three rules, all enforced here in code rather than asked of the model:

      * a built-in agent whose worker only simulates its output is never
        planned — a buyer must never be charged for simulated work;
      * a built-in agent that runs on Claude only is not planned while the
        workers are off Claude — its step could not run;
      * an external operator agent is planned only while
        `PLANNER_ROUTE_EXTERNAL` is on (owner decision: plans use the
        platform's own agents).

    Public so the evals harness offers the planner exactly the set decompose
    would; `on_claude` lets it ask as the Claude pipeline would, whatever
    provider the harness process happens to resolve (None: the active one).
    """
    if _is_simulated(agent.id) or _needs_absent_provider(agent.id, on_claude):
        return False
    return not _is_external(agent.id) or settings.planner_route_external


def _snapshot_registry() -> _RegistrySnapshot:
    """Read the registry once and split out what planning may route to."""
    agents = tuple(state.list_agents())
    dispatchable = [a for a in agents if _is_listed(a) and is_dispatchable(a.id)]
    eligible = [a for a in dispatchable if plannable(a)]
    return _RegistrySnapshot(
        agents,
        tuple(a for a in eligible if not reachability.is_failing(a.id)),
        tuple(a for a in eligible if reachability.is_failing(a.id)),
        tuple(a for a in dispatchable if _is_simulated(a.id)),
        tuple(a for a in dispatchable if _is_external(a.id) and not plannable(a)),
        settings.planner_route_external,
        tuple(a for a in dispatchable if not _is_simulated(a.id) and _needs_absent_provider(a.id)),
    )


def _still_routable(agent_id: str) -> bool:
    """Point-of-use check against the LIVE registry: still listed and dispatchable.

    Only ever narrows the snapshot. The kit path pauses before it plans, and
    an operator can delist or unbind an agent in that pause; the snapshot
    would still say it was routable, so a step is placed only if the live
    registry agrees — the same question the free-form clamp asks after the
    planning call.
    """
    agent = state.agents.get(agent_id)
    return (
        agent is not None
        and _is_listed(agent)
        and is_dispatchable(agent_id)
        and plannable(agent)
        and not reachability.is_failing(agent_id)
    )


def _rep_fields(info: reputation_svc.RepInfo | None) -> dict[str, Any]:
    """PlanStep reputation stamp — empty when the agent has no rep entry.

    The one place a step's reputation is stamped, on every path (kit step,
    substitute, re-admission, model step, fallback), so a card can never show
    a lower bound on one path and not another. Everything here comes from the
    snapshot the floor was applied with: a card that re-read reputation later
    could show a bound the plan was never judged on.
    """
    if info is None:
        return {}
    return {
        "rep_bps": info.smoothed_bps,
        "rep_source": info.source,
        "rep_lower_bound_bps": info.lower_bound_bps,
        "rep_count": info.count,
        "rep_dispute_rate_bps": info.dispute_rate_bps,
        "rep_degraded": info.degraded,
    }


def _reputation_degraded(reps: dict[str, reputation_svc.RepInfo]) -> bool:
    """Whether ANY reputation read in this snapshot fell back to the prior.

    `RepInfo.degraded` means the on-chain read FAILED and the Bayesian prior was
    served instead, so every floor verdict in this plan rests on an estimate.
    With the shipped config the prior clears the floor, which means an outage
    fails OPEN and the buyer is otherwise shown a trust gate that did not run.

    Not to be confused with the other two `degraded`s in this payload:
    `PlanStep.degraded` and `PlanFloorNotice.kind == "degraded"` both mean
    "re-admitted BELOW the floor by the starvation backstop" — a verdict that
    was reached, not one that could not be. A plan can carry either without the
    other. `PlanStep.rep_degraded` is this same fact per step, stamped by
    `_rep_fields`, so a card can say WHICH agent's numbers are an estimate.
    """
    return any(info.degraded for info in reps.values())


def _smoothed_score(agent: Agent, reps: dict[str, reputation_svc.RepInfo]) -> int:
    """An agent's smoothed score for ranking and display, in bps.

    The PRIOR when the snapshot has no entry, and never `Agent.rep`: for an
    on-chain agent that is a number its own registrant wrote, so ranking or
    displaying it let an unscored agent claiming 5.0 outrank one with real
    evidence. Every caller already holds only scored agents (see `_scored`),
    so the fallback is a totality guard, and it is the one number the
    service itself would serve an agent with no evidence.
    """
    info = reps.get(agent.id)
    return info.smoothed_bps if info is not None else settings.reputation_prior_bps


def _scored(agents: list[Agent], reps: dict[str, reputation_svc.RepInfo]) -> list[Agent]:
    """The agents the snapshot actually scored; the rest are not offered at all.

    `passes_floor(None)` admits an agent without consulting the floor, so an
    agent with no entry would reach a plan on no evidence whatsoever. With one
    registry snapshot per decompose and reputation read for exactly its
    routable agents, a missing entry means the read did not return one — and
    an agent the floor could not judge is not one a plan may route to.
    """
    kept = [a for a in agents if a.id in reps]
    if len(kept) < len(agents):
        missing = sorted(a.id for a in agents if a.id not in reps)
        logger.warning("%d routable agent(s) had no reputation entry and were not offered: %s", len(missing), missing)
    return kept


def _backstop_rank(agent: Agent, reps: dict[str, reputation_svc.RepInfo]) -> tuple[int, str]:
    """Sort key for the starvation backstop: best smoothed score first, id breaking ties.

    ONE rule for both planning paths. They used to rank separately — the kit
    path by `(-smoothed, id)`, the free-form path by smoothed score alone, ties
    left to registry insertion order and self-declared `Agent.rep` standing in
    for a missing entry — so one reputation snapshot could re-admit different
    agents depending on which path planned the intent, which is exactly the
    divergence `plan_notices` exists to rule out for the notices themselves.

    The backstops only ever rank sub-floor agents, and a sub-floor agent always
    has an entry (`passes_floor(None)` admits the agent outright). The one
    other caller, `_fallback_agent`, uses it as a last tie-breaker, and
    `fetch_reps` returns an entry for every agent it is asked about. So the 0
    for a missing entry is a totality guard, not a policy — and it is
    deliberately not `Agent.rep`, a number an on-chain registrant writes about
    itself, which has no place in a ranking that stands in for evidence.
    """
    info = reps.get(agent.id)
    return (-(info.smoothed_bps if info is not None else 0), agent.id)


# Kit-pipeline agent ids. Substitutes are drawn from OUTSIDE this set —
# borrowing one kit role's agent to fill another is itself a silent reshuffle,
# which the product rules forbid.
_KIT_AGENT_IDS: frozenset[str] = frozenset(aid for aid, _ in _KIT_PIPELINE)

# The kit role that produces the deliverable. Every other role feeds it (brief,
# brand, tokens) or refines and seals what it made, and code.gen is the worker
# that serves the kit's artifact — so a kit plan without it hands the buyer no
# artifact at all, only the paid-for inputs to a build no step performs.
_KIT_BUILDER_ID = "agt_11c0"


def _kit_step(
    agent: Agent,
    rationale: str,
    eta: float,
    reps: dict[str, reputation_svc.RepInfo],
    substituted_for: str | None = None,
    degraded: bool = False,
    tier: Tier | None = None,
) -> PlanStep:
    """One curated-pipeline step, priced from the registry and rep-stamped.

    `eta` is the ROLE's eta (from _KIT_ETAS), not the agent's, so a substitute
    inherits the timing of the step it fills. Price is the acting agent's own
    rate — the buyer pays whoever actually does the work. `degraded` marks a
    step the starvation backstop re-admitted below the floor.
    """
    return PlanStep(
        agent_id=agent.id,
        agent_name=agent.name,
        rationale=rationale,
        price_stroops=money.to_stroops(agent.price),
        est_eta_seconds=eta,
        substituted_for=substituted_for,
        degraded=degraded,
        tier=tier,
        **_rep_fields(reps.get(agent.id)),
    )


def _floor_substitute(
    designated: Agent,
    reps: dict[str, reputation_svc.RepInfo],
    taken: set[str],
    registry: _RegistrySnapshot | None = None,
) -> Agent | None:
    """Deterministically pick a floor-clearing replacement for a sub-floor kit
    agent: dispatchable, OFF the kit pipeline, sharing >=1 skill, not already
    used in this plan. Highest smoothed score wins, id breaks ties — a pure
    function of the reputation snapshot, so the plan stays reproducible.

    "Dispatchable" is a local worker OR a bound external endpoint (story 2.01).
    The floor is unchanged and still applied here: a bound agent stands in for
    a sub-floor kit agent only if it clears the floor on the same arithmetic.

    Delisted agents are excluded from the pool as well. Promoting one INTO a
    kit slot is the same defect as offering one to the planner, only harder to
    spot: the agent is not merely tolerated in a candidate list, it is chosen,
    and it lands in the plan with a `substituted_for` badge implying we picked
    the best available stand-in. An agent whose operator withdrew it is not
    available at all.

    The pool is the decompose's own registry snapshot (listed and dispatchable
    already), narrowed to the agents it scored: a stand-in is chosen on
    evidence, so an agent with no entry is never one.
    """
    pool = _scored(list((registry or _snapshot_registry()).routable), reps)
    wanted = set(designated.skills)
    candidates = [
        a
        for a in pool
        if a.id not in taken
        and a.id not in _KIT_AGENT_IDS
        and reputation_svc.passes_floor(reps.get(a.id))
        and wanted.intersection(a.skills)
        and _still_routable(a.id)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda a: (-_smoothed_score(a, reps), a.id))


# Line-break characters that could split one agent's entry into two. Not in
# `sanitize_untrusted`'s control-character class, which deliberately preserves
# newlines because a fenced free-text blob legitimately contains them — this
# block's one-line-per-agent grammar is a local concern, so it is handled here.
_LINE_BREAKS = re.compile(r"[\r\n\u2028\u2029]+")


def _prompt_name(name: str) -> str:
    """Render an agent's name as a safe FIELD inside AVAILABLE_AGENTS.

    The name is attacker-controlled: `AgentRegistry.register` is permissionless
    and stores `name: String` verbatim, with no length or content check, so our
    API's max_length=100 is validation on the wrong side of the trust boundary.
    Since story 2.01 a registered-and-bound external agent is dispatchable, and
    `_routable_registry` filters on exactly that — which is what puts an
    operator's text into the TRUSTED half of the planning prompt.

    `sanitize_untrusted`, not `fence_untrusted`: AVAILABLE_AGENTS is the half
    of the prompt the planner must parse and obey, so its contents cannot be
    wrapped in a block that tells the model to ignore them, and a multi-line
    BEGIN/END fence would destroy the line grammar besides. The field-level
    primitive is the right one — it defuses marker forgery, strips control
    characters, and clamps.

    Two structural defences are layered on top, because this block's grammar is
    one `- id=… name=… price=… rep=… skills=…` line per agent:

      * line breaks collapse to spaces, so a name can never forge a second
        `- id=` entry and offer the planner an agent that does not exist;
      * the value is quoted (and inner double quotes become single ones), so a
        name such as `x price=0.000 rep=5.00` cannot forge the FIELDS beside
        it — the payload stays visibly inside one quoted string.

    This is applied here rather than merely trusted from `registry_sync`
    because prompt defence belongs at the prompt-construction site by this
    repo's convention (see every `worker_prompt` call site), and because an
    `Agent` can reach `state` by paths that never crossed the registry mirror.
    """
    safe = sanitize_untrusted(name, max_chars=MAX_AGENT_NAME_CHARS)
    safe = _LINE_BREAKS.sub(" ", safe).replace('"', "'")
    return f'"{safe}"'


class _Shortlist(NamedTuple):
    """What the free-form planner may route to, and what the buyer is told.

    `offered` is the same set `block` lists, carried as data so the clamp can
    hold the model to it: the block is what the planner was SHOWN, the model's
    plan is what it RETURNED, and only ids in the first may survive into the
    second. Re-deriving the set from the prompt string would be parsing our own
    output back, and re-deriving it from state after the LLM call would ask a
    different question of a registry that may have moved in the meantime.
    """

    block: str
    notices: list[PlanFloorNotice]
    offered: frozenset[str]


def _registry_notices(registry: _RegistrySnapshot) -> list[PlanFloorNotice]:
    """The registry's own exclusion notices as it stands — both paths.

    Unbound on-chain agents are a registry fact, not a floor verdict, so they
    are read from the whole catalog rather than from a routable subset (which
    they are by definition absent from), and the fact does not depend on which
    path planned the intent. One selection, called by both, so the two cannot
    disagree about which agents it names.

    Seeded agents are skipped: every one ships with a local worker, so an
    unbound seeded agent is a deployment defect to fix, not a buyer-facing
    exclusion. Delisted agents are skipped as well — a withdrawn agent gets no
    notice under any reason code, argued at the call in `_routable_registry`.

    Reads registry and binding state, never reputation, and `unbound_exclusions`
    orders by id before it caps, so the kit path's determinism promise holds:
    the same registry yields the same notices.

    Listed and not routable is listed and not dispatchable, read off the same
    snapshot the plan was built from — less the agents left out for a dead
    endpoint, which ARE bound and are reported first, under their own code
    (D-084). Calling one "unbound" would send its operator to fix the wrong
    thing.

    Built-in agents whose worker only simulates its output come first, under
    `simulated_worker`, then Claude-only agents while the workers are off
    Claude, under `provider_unavailable` — whichever way the external switch is
    set. With the
    switch OFF, ONE `external_not_routed` notice states the policy whenever
    any outside agent is listed — naming none of them (the set is the whole
    permissionless registry; the ids go to the log) — and no unbound or
    unreachable notice is given, since the policy is why they are absent.
    """
    simulated = [simulated_exclusion(a) for a in registry.simulated]
    simulated += [provider_exclusion(a) for a in registry.off_provider]
    if not registry.route_external:
        outside = sorted(
            a.id for a in registry.agents if a.source == "onchain" and _is_listed(a) and _is_external(a.id)
        )
        if not outside:
            return simulated
        # The card says the policy once; which agents it covered is ops detail.
        logger.info("external routing is off: %d listed outside agent(s) not offered: %s", len(outside), outside[:50])
        return [*simulated, external_policy_notice()]
    unreachable = {a.id for a in registry.unreachable}
    routable = {a.id for a in registry.routable} | unreachable
    return (
        simulated
        + unreachable_exclusions(registry.unreachable)
        + unbound_exclusions(
            a for a in registry.agents if a.source == "onchain" and _is_listed(a) and a.id not in routable
        )
    )


def _routable_registry(
    reps: dict[str, reputation_svc.RepInfo],
    registry: _RegistrySnapshot | None = None,
) -> _Shortlist:
    """The AVAILABLE_AGENTS block, the floor actions that shaped it, and its ids.

    The routable set is a subtraction, and until story 3.02 only the remainder
    survived: the complement was dropped on the floor of a list comprehension
    and the starvation relaxation went to `logger.warning`, where no buyer will
    ever see it. Both halves are returned now, because the thing the buyer
    needs to know is precisely what was taken away.

    The block's bytes are unchanged by this — they are pinned in
    tests/test_model_path_floor.py, since a one-character drift changes what
    the planner plans and would surface as unrelated assertions failing
    downstream.
    """
    # Two subtractions, both on the ASSIGNMENT rather than on `cleared`,
    # because the floor-starvation backstop below re-admits from THIS list:
    #
    #   * an indexed on-chain agent (story 1.02) is marketplace-visible but only
    #     planner-routable once an operator binds it an endpoint (story 2.01) —
    #     until then it has nothing to execute a step with;
    #   * a delisted agent has been withdrawn by its own operator, and that is
    #     the one exclusion the backstop may never undo. The floor is OUR rule
    #     and we are entitled to relax it when relaxing keeps the product
    #     working; `set_active(id, false)` is someone else's decision about
    #     their own service, and re-admitting on starvation would route paid
    #     work to an operator who asked us to stop.
    registry = registry or _snapshot_registry()
    agents = _scored(list(registry.routable), reps)
    cleared = [a for a in agents if reputation_svc.passes_floor(reps.get(a.id))]
    # Prompt cap. Every listed agent is prompt tokens on every planner call,
    # and binding is open to any registrant, so the block is bounded: past the
    # cap, the best-scored agents that cleared the floor are listed, by the
    # backstop's own rule. The rest cleared the floor and were simply not
    # listed, which is not a floor verdict, so it carries no notice — the same
    # reasoning that keeps `not_selected_by_planner` out of the vocabulary.
    cap = max(_MIN_ROUTABLE_AGENTS, settings.decompose_prompt_max_agents)
    if len(cleared) > cap:
        listed = {a.id for a in sorted(cleared, key=lambda a: _backstop_rank(a, reps))[:cap]}
        logger.info("%d agents cleared the floor; listing the best-scored %d to the planner", len(cleared), cap)
        cleared = [a for a in cleared if a.id in listed]
    # Starvation backstop: TOP UP the agents that cleared the floor, never
    # replace them. Re-ranking the whole dispatchable set and keeping the top
    # _MIN_ROUTABLE_AGENTS used to push agents that PASSED the floor out of the
    # prompt in favour of better-scored ones that failed it — the floor then
    # made the shortlist less trustworthy than no floor at all, and every
    # `floor_relaxed` notice claimed a shortfall the plan had manufactured.
    # Only the deficit is re-admitted, in the order the kit path uses too.
    deficit = max(0, _MIN_ROUTABLE_AGENTS - len(cleared))
    readmitted = sorted(
        (a for a in agents if not reputation_svc.passes_floor(reps.get(a.id))),
        key=lambda a: _backstop_rank(a, reps),
    )[:deficit]
    if readmitted:
        logger.warning(
            "reputation floor left only %d/%d agents routable; re-admitting %d below it by smoothed score",
            len(cleared),
            len(agents),
            len(readmitted),
        )

    offered = {a.id for a in cleared} | {a.id for a in readmitted}
    # Registry order whichever rule admitted an agent, so a re-admission never
    # reads to the planner as a promotion to the top of the list.
    routable = [a for a in agents if a.id in offered]
    # Order is part of the contract — the plan card renders these in sequence,
    # and a list that reshuffles between two identical requests reads as the
    # system changing its mind. Registry order drives the first two groups and
    # `unbound_exclusions` sorts the third, so the whole list is a pure
    # function of the registry and the reputation snapshot.
    #
    # Every agent that CLEARED the floor is offered — the backstop only ever
    # adds to them — so the only floor exclusions are sub-floor agents the
    # backstop did not reach, and no agent is both offered and excluded.
    #
    # Deliberately uncapped, unlike the unbound group: every entry here is a
    # verdict the floor reached against an agent the buyer could otherwise have
    # been routed to, and story 3.02 forbids a floor verdict going unsaid. A
    # cap would silence exactly the agents past the cut.
    notices = [
        below_floor_exclusion(a, reps.get(a.id))
        for a in agents
        if a.id not in offered and not reputation_svc.passes_floor(reps.get(a.id))
    ]
    notices += [
        relaxation(a, reps.get(a.id), min_routable=_MIN_ROUTABLE_AGENTS)
        for a in routable
        if not reputation_svc.passes_floor(reps.get(a.id))
    ]
    # Unbound on-chain agents, which `_registry_notices` selects the same way
    # for both paths, come last.
    #
    # Delisted agents are skipped there for a related but distinct reason, and
    # it is the reporting half of this lane's decision: a withdrawn agent gets
    # NO notice at all, under any reason code.
    #
    # ADR 0006 D2 left `inactive` out of the closed `ExclusionReason` vocabulary
    # because routing could not produce that state. This function just changed
    # that premise — so the question is live again, and the answer is still no,
    # on different grounds. `below_floor` is a verdict we reached, `floor_relaxed`
    # is our own rule bending, `unbound_endpoint` is a setup step the operator
    # has not finished (stories 2.05/2.06 exist to get it finished). All three
    # explain a gap between what the marketplace lists and what the plan drew
    # from. A delisting is none of those: the operator asked to be absent, got
    # what they asked for, and `status` already says so on their own
    # `GET /api/agents` row. Announcing it on every buyer's plan card, on every
    # request, for as long as they stay withdrawn, publishes a business decision
    # the buyer was never protected from — and the withdrawn set only grows over
    # a deployment's life, so it would drown the notices this list exists for
    # exactly the way D3 says `not_selected_by_planner` would.
    #
    # Concretely, that means a delisted-AND-unbound agent is filtered rather
    # than reported: "no endpoint bound" is true of it but is not why it is
    # absent, and it is advice nobody wants acted on.
    notices += _registry_notices(registry)

    return _Shortlist(render_agents_block(routable, reps), notices, frozenset(offered))


def render_agents_block(agents: Sequence[Agent], reps: dict[str, reputation_svc.RepInfo]) -> str:
    """The AVAILABLE_AGENTS block for `agents`, one entry per agent, in order.

    An entry is one `- id=…` line, then — for a built-in agent — its role card
    on an indented line of its own.

    The planner's whole view of who it may route to. Public so the evals
    harness can hand `draft_plan` the same block decompose would; decompose
    itself only ever renders the shortlist `_routable_registry` chose.
    """
    lines = ["AVAILABLE_AGENTS:"]
    for a in agents:
        # Live smoothed score on the 0–5 scale the prompt already uses. Never
        # the agent's self-declared `rep`: the planner reads this number as
        # evidence, and an on-chain registrant writes that one about itself.
        rep_display = _smoothed_score(a, reps) / 2000
        # Only `name` is treated. `id` is a Soroban Symbol
        # ([A-Za-z0-9_]{1,32}), so it can hold no space, quote, newline or
        # fence marker; price and rep are floats this line formats itself.
        #
        # `skills` is a Vec<Symbol> for an ON-CHAIN agent, and the same
        # reasoning holds there — but the seeded catalog is plain Python and
        # does contain a space (`agt_10b6` has "42 langs", app/seed.py:17), so
        # the constraint is a property of the chain rather than of this field.
        # It stays untreated because the seed is trusted first-party data, not
        # because nothing here can contain a separator; an untrusted writer
        # into `skills` would change that.
        lines.append(
            f"- id={a.id} name={_prompt_name(a.name)} price={a.price:.3f} "
            f"rep={rep_display:.2f} skills={','.join(a.skills)}"
        )
        # A built-in agent's role card, indented under its entry: what it
        # does, reads and hands on, so the planner can compose a pipeline
        # whose handoffs exist. Static first-party text (`role_cards`), so the
        # block stays as cacheable as before; an external agent has none.
        card = card_for(a.id)
        if card is not None and not _is_external(a.id):
            lines.append(f"  {card.render()}")
    return "\n".join(lines)


def _registry_prompt_fragment(reps: dict[str, reputation_svc.RepInfo]) -> str:
    """The prompt block alone, for callers that only assert on the string.

    Kept as a named view rather than folded away because the prompt-safety and
    routability suites exercise the block itself — what the planner is shown —
    and reading a notices list they never use out of a tuple would obscure
    exactly the thing they are about. Planning uses `_routable_registry`.
    """
    return _routable_registry(reps).block


def build_planning_prompt(registry_block: str, intent: str) -> str:
    """Registry facts, then the FENCED intent, then the ask.

    The intent is free-form and attacker-controllable, so it is never spliced
    bare next to AVAILABLE_AGENTS — it arrives as a delimited data block the
    planner is told not to obey. The trusted instruction goes last.
    """
    return "\n\n".join([registry_block, fence_user_input(intent), "Return the Plan."])


async def _kit_thinking() -> None:
    """The kit path's randomized "thinking time" — see `_build_kit_plan`.

    Its own function so a test can skip the pause by name, rather than paying
    1.4–2.4 s of real sleep per kit plan or patching `asyncio.sleep` for the
    whole process.
    """
    await asyncio.sleep(1.4 + random.random() * 1.0)


class _DroppedKitRole(NamedTuple):
    """A sub-floor kit role with no substitute, held for the starvation backstop."""

    position: int  # index in _KIT_PIPELINE — where the step goes if re-admitted
    agent: Agent
    rationale: str
    info: reputation_svc.RepInfo | None


async def _build_kit_plan(
    intent: str,
    kit: DemoKit,
    reps: dict[str, reputation_svc.RepInfo],
    registry: _RegistrySnapshot | None = None,
    pipeline: _PipelineFacts | None = None,
) -> DecomposeResponse:
    """Deterministic 6-step plan for a curated demo intent. No LLM call.

    The reputation floor is applied to every pipeline agent, exactly as on the
    free-form path: a sub-floor agent is replaced by a floor-clearing worker
    that shares a skill, or dropped, and every such action is recorded on the
    response so the buyer never sees a silently reshuffled pipeline (story
    3.02). Given the same registry and reputation snapshot the plan — steps
    and notices — is identical, with no LLM call. The registry counts because
    it always did: listing, bindings and substitute candidates are read from
    it, and the unbound-endpoint notices both paths share are a fact about it.

    Every notice is built by `plan_notices`, the same module `_routable_registry`
    uses, so "exactly as on the free-form path" is true by construction rather
    than for as long as both paths are remembered together. The builders are
    pure, which is what lets this path keep its determinism promise.

    A short randomized sleep up front mimics orchestrator "thinking time" so
    the Decompose UX feels like real LLM planning instead of a hardcoded dict
    being unpacked. It changes timing only, never plan content.
    """
    # Snapshot before the pause, not after: an agent that lands during it was
    # never covered by the reputation read this plan is judged on.
    registry = registry or _snapshot_registry()
    routable = {a.id for a in registry.routable}
    by_id = {a.id: a for a in registry.agents}
    plan_tier = pipeline.screening.tier if pipeline is not None else None

    def role_tier(role_id: str) -> Tier | None:
        # A stand-in inherits the ROLE's tier, as it inherits the role's eta.
        return _capped_tier(_KIT_TIERS.get(role_id, "low"), plan_tier)

    await _kit_thinking()

    # (pipeline position, step). Execution runs steps in list order and later
    # roles read earlier ones' output from the run context — code.gen takes its
    # design tokens from design.figma, code.critic polishes code.gen's draft —
    # so a step is only coherent at its own position, however late it was
    # admitted.
    placed: list[tuple[int, PlanStep]] = []
    notices: list[PlanFloorNotice] = []
    taken: set[str] = set()
    # Sub-floor agents with no substitute — held until after the loop so the
    # starvation backstop can choose which to re-admit before the rest are
    # recorded as plain exclusions.
    dropped: list[_DroppedKitRole] = []

    for position, (agent_id, rationale) in enumerate(_KIT_PIPELINE):
        agent = by_id.get(agent_id)
        if agent is None:
            # The kit pipeline references an agent that isn't seeded — this
            # is a programmer error. Skip the step rather than crash the
            # whole pipeline.
            continue

        if not _is_listed(agent):
            # Delisted by its operator: for routing purposes as absent as an id
            # that is not in the registry at all, so it is handled the same way
            # — the step is dropped, with no substitute and no notice.
            #
            # The placement is the load-bearing part. It sits BEFORE the floor
            # check and outside `dropped`, which is the list the starvation
            # backstop re-admits from, so no combination of bad ratings can put
            # a withdrawn agent back into a kit slot.
            #
            # No substitute, because a substitution is a FLOOR action: it emits
            # `kind="substituted"` with `reason_code="below_floor"` and a
            # sentence naming the bps the designated agent failed on. A delisted
            # agent failed nothing, so the only honest notice here is none —
            # which is also this lane's reporting decision (see
            # `_routable_registry`). Quietly promoting a stand-in with no notice
            # would be the silently reshuffled pipeline story 3.02 forbids.
            continue

        if agent.id not in routable or not _still_routable(agent.id):
            # Listed but with nothing to execute it — a seeded worker missing
            # from this deployment — or delisted or unbound during the pause.
            # No step can run it, so it is dropped the same silent way.
            continue

        info = reps.get(agent.id)
        if info is None:
            # Routable but unscored: the floor cannot judge it, so it is not
            # offered — the same rule `_scored` applies to the free-form path.
            logger.warning("kit agent %s had no reputation entry and was not planned", agent.id)
            continue

        eta = _KIT_ETAS.get(agent_id, 1.0)
        if reputation_svc.passes_floor(info):
            placed.append((position, _kit_step(agent, rationale, eta, reps, tier=role_tier(agent_id))))
            taken.add(agent.id)
            continue

        # Sub-floor: substitute with a floor-clearing off-pipeline worker that
        # shares a skill, else drop the step. Either way the buyer is told.
        sub = _floor_substitute(agent, reps, taken, registry)
        if sub is not None:
            placed.append(
                (position, _kit_step(sub, rationale, eta, reps, substituted_for=agent.id, tier=role_tier(agent_id)))
            )
            taken.add(sub.id)
            notices.append(substitution(agent, sub, info))
        else:
            dropped.append(_DroppedKitRole(position, agent, rationale, info))

    # Starvation backstop — reuse _MIN_ROUTABLE_AGENTS rather than invent a
    # second rule. If the floor left too few steps, re-admit dropped kit agents
    # to cover the deficit and record the degradation; the remainder are
    # recorded as exclusions. A re-admitted step goes back to its own pipeline
    # position, never onto the end: appended, design tokens would run AFTER the
    # code.gen step that needs them, and it would build without them.
    #
    # The builder goes first whatever its score, then `_backstop_rank` — the
    # free-form path's rule. Score alone once turned a battered tetris kit into
    # research + brand + tokens with no code.gen: three paid steps preparing
    # inputs for a build nobody was asked to do, and no artifact at the end.
    by_priority = sorted(dropped, key=lambda d: (d.agent.id != _KIT_BUILDER_ID, _backstop_rank(d.agent, reps)))
    deficit = max(0, _MIN_ROUTABLE_AGENTS - len(placed))
    readmit_ids = {d.agent.id for d in by_priority[:deficit]}
    if readmit_ids:
        logger.warning(
            "reputation floor left only %d kit step(s); re-admitting %d dropped agent(s), builder first",
            len(placed),
            len(readmit_ids),
        )
    for position, agent, rationale, info in dropped:
        if agent.id in readmit_ids:
            step = _kit_step(
                agent, rationale, _KIT_ETAS.get(agent.id, 1.0), reps, degraded=True, tier=role_tier(agent.id)
            )
            placed.append((position, step))
            taken.add(agent.id)
            notices.append(relaxation(agent, info, min_routable=_MIN_ROUTABLE_AGENTS))
        else:
            notices.append(below_floor_exclusion(agent, info))
    # Last, as on the free-form path, so both lists group the same way: what
    # the floor did first, then registry entries nothing could dispatch. This
    # path used to report none, so a demo intent showed a marketplace with
    # agents its plan card never accounted for.
    notices += _registry_notices(registry)
    steps = [_with_executor(step) for _, step in sorted(placed, key=lambda p: p[0])]

    plan_id = f"pln_{secrets.token_hex(4)}"
    total_eta = sum(s.est_eta_seconds for s in steps)

    stored = StoredPlan(
        id=plan_id,
        intent=intent,
        # Priced in stroops step by step; the plan's total and the legacy
        # `total_usdc` are derived from those integers (ADR 0015).
        plan=Plan(steps=steps, tier=plan_tier),
        total_eta=total_eta,
        # What the buyer is about to be shown, kept with the plan so
        # `/execute` judges the plan the buyer actually authorised.
        notices=notices,
        floor_bps=settings.reputation_floor_bps,
        reputation_degraded=_reputation_degraded(reps),
        stages=pipeline.stages_with(False) if pipeline else [],
    )
    state.add_plan(stored)

    return DecomposeResponse(
        plan_id=plan_id,
        intent=intent,
        steps=steps,
        total_eta=round(total_eta, 2),
        notices=notices,
        # Both paths answer the same two questions, because a buyer cannot tell
        # which one planned their intent and should not have to.
        floor_bps=settings.reputation_floor_bps,
        reputation_degraded=_reputation_degraded(reps),
        **(pipeline.response_fields(False) if pipeline else {}),
    )


# The empty-plan fallback's preferred agent. A copywriter can produce something
# for any intent, which no other seeded role can promise.
_FALLBACK_AGENT_ID = "agt_01h8"


def _fallback_agent(offered: frozenset[str], reps: dict[str, reputation_svc.RepInfo]) -> Agent | None:
    """The agent an emptied or failed model plan falls back to — from `offered` only.

    The fallback exists so the UI never gets stuck on a plan the clamp emptied
    or a planner that produced none, but it is still a routing decision, and it used to be the one routing
    decision that skipped the floor: `agt_01h8` was hardcoded, so a copywriter
    the floor had just excluded took the whole job the moment the model's picks
    were clamped away. It is held to the clamp's rule now — offered, and still
    listed and dispatchable at the point of use.

    One deterministic key, so the same snapshot always falls back to the same
    agent:

      1. `agt_01h8` whenever it is offered, as before;
      2. an agent that CLEARED the floor ahead of one the backstop re-admitted —
         a relaxation is a last resort, not a tie-breaker;
      3. `_backstop_rank`: best smoothed score, id breaking ties.

    None when nothing offered is still routable; the caller refuses the plan.
    """
    candidates = [a for a in state.list_agents() if a.id in offered and _still_routable(a.id)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda a: (
            a.id != _FALLBACK_AGENT_ID,
            not reputation_svc.passes_floor(reps.get(a.id)),
            _backstop_rank(a, reps),
        ),
    )


def authorizable_total_usdc(steps: list[PlanStep]) -> float:
    """The plan's total as the legacy float: `Σ price_stroops`, exactly.

    The console signs the authorization's `max_amount` from the plan's total,
    and escrow v2 pays each delivered step its own price in stroops, refusing
    the whole settle when their sum passes the max (S6). Since ADR 0015 every
    step IS priced in stroops, so the total is their integer sum and this float
    is only its legacy spelling — `DecomposeResponse.total_usdc` derives the
    same number itself. Sign `total_stroops`, never a float.
    """
    return money.stroops_to_float(sum(s.price_stroops or 0 for s in steps))


# The most steps a free-form plan may carry, matching the "1–6 ordered steps"
# the planner is instructed to return (app/agents/orchestrator.py). Every step
# is a paid dispatch and `/execute` runs them all, so the count is the buyer's
# bill: the model is asked for six, and the clamp is what makes six a limit
# rather than a request. A 200-step plan was storable before this.
_MAX_PLAN_STEPS = 6


# Enough of a failure message to say what went wrong, never a whole body.
_FAILURE_EXCERPT_CHARS = 200


def _loggable(text: str) -> str:
    """Third-party text made fit for a log line: no key in it, and bounded.

    Neither source of a planner failure message is ours. An OpenAI 401 quotes
    back the key it rejected, only partly masked, and an answer that did not
    parse is whatever the model wrote, at whatever length. So every configured
    secret and anything shaped like a key is redacted — by the same rule the
    process-wide log filter applies (`app/security.py`), so the two cannot
    drift — and the excerpt is clamped.
    """
    return redact_secrets(text)[:_FAILURE_EXCERPT_CHARS]


@functools.cache
def _failed_runs() -> frozenset[Any]:
    """Run states in which agno itself reports that the planner call did not
    finish. Read from agno on first use: importing it at module scope put agno
    on the boot path (app/agents/model_factory.py)."""
    from agno.run.base import RunStatus

    return frozenset({RunStatus.error, RunStatus.cancelled})


def _planner_plan(result: Any) -> Plan | None:
    """The planner's own Plan, or None when its run produced none to use.

    agno does not raise when the model call fails. `Agent.arun` catches the
    provider's exception, marks the run `RunStatus.error` and returns the
    message as the run's `content` — the field that holds the Plan on success.
    A missing API key, a refused connection or an upstream 5xx therefore
    arrives here looking like an answer, and was read as one: the clamp asked
    the error string for `.steps`, and the router turned that AttributeError
    into a 502 on every free-form intent (BLO-121). A provider error is not
    model output, so the result is checked before anything reads it as a plan.

    Two checks, because they answer different questions. The status is agno's
    own verdict on the run, and the only thing that can reject one that failed
    AFTER its answer parsed — an output guardrail refusing the plan, say —
    where `content` would still hold a Plan. The type check covers what the
    status does not: a run that completed with text that never parsed as a
    Plan (agno leaves the raw string in `content`), or with no answer at all.
    There is no dict branch: agno returns a dict only for a dict
    `output_schema`, and this agent's is the `Plan` model. Both fields are read
    with `getattr`, so a result of any other shape degrades here too, instead
    of raising the AttributeError this function exists to prevent.

    The failure is logged here, while the run is still in hand, and goes no
    further: the caller serves the fallback plan, and the buyer is told only
    that it is one.
    """
    status = getattr(result, "status", None)
    content = getattr(result, "content", None)
    if status not in _failed_runs() and isinstance(content, Plan):
        return content
    excerpt = f": {_loggable(content)!r}" if isinstance(content, str) else ""
    logger.warning(
        "planner %s gave no usable plan (run status %s, %s content%s); serving the fallback plan",
        settings.orchestrator_model,
        getattr(status, "value", status),
        type(content).__name__,
        excerpt,
    )
    return None


class _ProposedStep(Protocol):
    """A step as a planner proposed it: the legacy `PlanStep` or Claude's `PlannedStep`."""

    @property
    def agent_id(self) -> str: ...
    @property
    def rationale(self) -> str: ...
    @property
    def est_eta_seconds(self) -> float: ...
    @property
    def tier(self) -> Tier | None: ...


def _capped_tier(step_tier: Tier | None, plan_tier: Tier | None) -> Tier | None:
    """A step's tier held at or below its plan's; the plan's when the step has none.

    None without a plan tier: only the Claude pipeline rates a request, and a
    tier the legacy planner happened to write into its output is not one
    anything judged.
    """
    if plan_tier is None:
        return None
    if step_tier is None:
        return plan_tier
    return min(step_tier, plan_tier, key=TIERS.index)


class _Clamped(NamedTuple):
    steps: list[PlanStep]
    # Offered agents whose endpoint was found dead WHILE the planner ran.
    went_unreachable: dict[str, Agent]


def _clamp(
    proposed: Sequence[_ProposedStep],
    shortlist: _Shortlist,
    reps: dict[str, reputation_svc.RepInfo],
    *,
    plan_tier: Tier | None = None,
) -> _Clamped:
    """Hold a planner's proposal to the shortlist; backfill names, snap prices to registry truth.

    A planner that produced no plan proposes no steps, so it lands in the
    empty-plan fallback by the same road as a plan the clamp emptied. Every
    planner goes through here — the legacy one and Claude — so the floor, the
    allowlist and the dead-endpoint check cannot differ between them.
    """
    cleaned: list[PlanStep] = []
    # (agent, rationale) pairs already kept. A repeated pair is the same paid
    # work bought twice — whitespace and case are the model's, not the task's.
    seen: set[tuple[str, str]] = set()
    # Offered agents whose endpoint was found dead WHILE the planner ran — a
    # background probe (`reachability.refresh_stale`) landing mid-call. Their
    # steps are dropped like any other no-longer-routable pick, and the buyer
    # is told, so the card accounts for every agent it lost (D-084).
    went_unreachable: dict[str, Agent] = {}
    for step in proposed:
        if len(cleaned) >= _MAX_PLAN_STEPS:
            # Capped on the steps KEPT, so an invented id the clamp drops
            # never costs the plan a legitimate step.
            break
        if step.agent_id not in shortlist.offered:
            # The planner may only route to what it was OFFERED. The block is
            # what it was SHOWN and this is what it RETURNED, and the two are
            # not the same set: the model invents ids, repeats ones from an
            # earlier turn, and names agents it knows by reputation or from its
            # instructions even when the floor just removed them. Any of those
            # would be stored, dispatched and paid for — a sub-floor agent
            # sailing past the trust gate with a `below_floor` notice about it
            # on the very same plan card. Holding the plan to `offered` is what
            # makes the floor a gate rather than a suggestion, and it is also
            # why no step can ever share an agent with an exclusion notice:
            # every excluded agent is, by construction, not offered.
            continue
        agent = state.agents.get(step.agent_id)
        if not agent or not _is_listed(agent) or not is_dispatchable(agent.id) or not plannable(agent):
            # Offered, but no longer routable at the point of use. The
            # shortlist was built BEFORE the planning call, and that call can
            # take tens of seconds, during which an operator can delist the
            # agent or unbind its endpoint (or a worker swap or the external
            # switch can change `plannable`). This is the last gate before the
            # step is stored and later dispatched, so the registry is asked
            # again here rather than trusted from the snapshot: a delisted
            # agent reaching /execute is the whole bug, and a step with nothing
            # to execute it would only reach /execute's unknown-agent skip.
            continue
        if reachability.is_failing(agent.id):
            went_unreachable[agent.id] = agent
            continue
        rationale = step.rationale.strip()
        if (agent.id, rationale.casefold()) in seen:
            continue
        seen.add((agent.id, rationale.casefold()))
        info = reps.get(agent.id)
        cleaned.append(
            PlanStep(
                agent_id=agent.id,
                agent_name=agent.name,
                rationale=rationale,
                price_stroops=money.to_stroops(agent.price),
                est_eta_seconds=max(0.3, min(step.est_eta_seconds, 3.0)),
                # The model's own tier, never above the plan's: a step cannot be
                # harder than the request it is part of, and the cap is what keeps
                # an over-eager plan off the most expensive model.
                tier=_capped_tier(step.tier, plan_tier),
                # An OFFERED agent below the floor can only be one the
                # starvation backstop re-admitted, which already carries a
                # `floor_relaxed` notice — so the inline flag and the notice
                # agree, as they do on the kit path. Recomputed from the same
                # snapshot rather than trusted from the model's output, whose
                # copy of this field is whatever it chose to write.
                degraded=not reputation_svc.passes_floor(info),
                **_rep_fields(info),
            )
        )
    return _Clamped(cleaned, went_unreachable)


# ── Plan composition: the handoffs a step depends on must exist ───────────
#
# A pipeline step that refines or ships another step's output is paid work on
# nothing when that output is missing: code.critic reviews a code builder's
# draft and deploy.v0 seals one, and without a builder in the plan both
# answer "nothing to do" — for the buyer's money. vision.ocr likewise reads an
# image the request must supply. The planner is told all of this (its role
# cards); these rules hold the plan to it in code, after the clamp, on both
# planners. They only ever DROP or REORDER the model's own steps — never add
# one, so they cannot put an agent in a plan the floor or the policy kept out.
_CODE_BUILDER_IDS: frozenset[str] = frozenset({"agt_11c0", "agt_03d9"})  # code.gen, code.next
_CRITIC_ID = "agt_12r0"
_DEPLOY_ID = "agt_08j2"
_OCR_ID = "agt_06q4"
_TRANSLATE_ID = "agt_10b6"

# A step dropped unless the plan holds at least one of these, with what the
# buyer is told it lacked. code.critic reviews code.gen's single-file HTML
# only: it declines a code.next project (`code_critic_worker.UNSUPPORTED_ARTIFACT`),
# so code.next alone gives it nothing to review. deploy.v0 seals either build.
_NEEDS_ONE_OF: dict[str, tuple[frozenset[str], str]] = {
    _CRITIC_ID: (frozenset({"agt_11c0"}), "there is no code.gen build to review (it does not review code.next)"),
    _DEPLOY_ID: (_CODE_BUILDER_IDS, "there is no build to seal"),
}

# A step moved after every one of these the plan holds: the critic after the
# draft it reviews, the seal after the build it seals and the review of it.
_RUNS_AFTER: dict[str, frozenset[str]] = {
    _CRITIC_ID: _CODE_BUILDER_IDS,
    _DEPLOY_ID: _CODE_BUILDER_IDS | {_CRITIC_ID},
}


def _handoff_order(steps: list[PlanStep]) -> list[PlanStep]:
    """`steps` reordered so each runs after what `_RUNS_AFTER` says it reads.

    A stable topological sort: at every position, the earliest step (in the
    model's order) whose producers are all placed goes next, so a plan that
    already flows forward comes back unchanged and any other moves only the
    steps it must. Always total: the edges only point from builders to the
    critic and from both to the seal, so there is no cycle to stall on.
    """
    waiting = list(range(len(steps)))
    after = [
        {j for j in waiting if steps[j].agent_id in _RUNS_AFTER.get(steps[i].agent_id, frozenset())} for i in waiting
    ]
    placed: set[int] = set()
    order: list[PlanStep] = []
    while waiting:
        nxt = next(i for i in waiting if after[i] <= placed)
        waiting.remove(nxt)
        placed.add(nxt)
        order.append(steps[nxt])
    return order


class _Composed(NamedTuple):
    steps: list[PlanStep]
    # One notice per agent whose proposed steps were dropped for want of their
    # input (`no_image_input`, `no_step_input`), in id order — the buyer is
    # told, as for an agent found unreachable mid-plan.
    notices: list[PlanFloorNotice]


def _missing_input(agent_id: str, present: set[str], intent: str) -> str | None:
    """What a proposed step on `agent_id` would lack — None when it has its input.

    Every check is one the run loop would otherwise make at dispatch, failing
    the step unbilled; made here, before the buyer authorizes its price. All
    pure: `has_image_input` judges an image link without fetching it (and with
    no upload context, since decompose takes none), and the translation check
    is `request_signals.has_translation_target`, which only says no when sure.
    """
    needs = _NEEDS_ONE_OF.get(agent_id)
    if needs is not None and not needs[0] & present:
        return needs[1]
    if agent_id == _OCR_ID and not has_image_input(intent, None):
        return _NO_IMAGE
    if agent_id == _TRANSLATE_ID and not has_translation_target(intent):
        return "the request names no language to translate into"
    return None


_NO_IMAGE = "no image"


def _compose(steps: list[PlanStep], intent: str) -> _Composed:
    """The clamped plan held to its handoffs: steps with nothing to work on are
    dropped (and the buyer told), and steps that read another step's output
    run after it."""
    present = {s.agent_id for s in steps}
    kept: list[PlanStep] = []
    dropped: dict[str, str] = {}
    for step in steps:
        missing = _missing_input(step.agent_id, present, intent)
        if missing is None:
            kept.append(step)
        else:
            dropped[step.agent_id] = missing
    notices = []
    for agent_id, missing in sorted(dropped.items()):
        agent = state.agents.get(agent_id)
        if agent is None:
            continue
        logger.info("dropped a %s step: %s", agent.name, missing)
        notices.append(no_image_exclusion(agent) if missing is _NO_IMAGE else no_input_exclusion(agent, missing))
    return _Composed(_handoff_order(kept), notices)


def _finish_free_form(
    intent: str,
    shortlist: _Shortlist,
    reps: dict[str, reputation_svc.RepInfo],
    clamped: _Clamped,
    *,
    plan_tier: Tier | None = None,
    pipeline: _PipelineFacts | None = None,
) -> DecomposeResponse:
    """Serve a clamped free-form plan: the fallback if it is empty, then store and answer.

    `pipeline` carries what the Claude pipeline adds to the answer (the
    request check, the understood request, the models, the stage lines); the
    legacy planner passes none and its answer is exactly what it always was.
    """
    composed = _compose(clamped.steps, intent)
    cleaned = composed.steps
    # Whatever left `cleaned` empty — a planner that failed, or one whose every
    # step the clamp or the composition rules discarded — the steps served
    # from here on are not the model's plan, and the response has to say so.
    planner_fallback = not cleaned
    if planner_fallback:
        # Fall back to a minimal safe plan so the UI never gets stuck — drawn
        # from the shortlist like any model step, never from outside it. The
        # copywriter used to be hardcoded here on the grounds that nothing
        # on-chain can delist it; true, but the FLOOR can exclude it, and the
        # fallback then routed to an agent the plan card was simultaneously
        # reporting as below the floor.
        fallback = _fallback_agent(shortlist.offered, reps)
        if fallback is None:
            # Everything offered was delisted or unbound while the planner ran.
            raise NoRoutableAgentsError("every offered agent left the registry during planning")
        info = reps.get(fallback.id)
        cleaned = [
            PlanStep(
                agent_id=fallback.id,
                agent_name=fallback.name,
                rationale=(
                    "fallback: generate copy for the intent"
                    if fallback.id == _FALLBACK_AGENT_ID
                    else "fallback: the planner returned no usable step, so the top-ranked shortlisted agent takes it"
                ),
                price_stroops=money.to_stroops(fallback.price),
                est_eta_seconds=0.8,
                tier=plan_tier,
                degraded=not reputation_svc.passes_floor(info),
                **_rep_fields(info),
            )
        ]

    cleaned = [_with_executor(step) for step in cleaned]
    notices = (
        shortlist.notices
        + [unreachable_exclusion(a) for _, a in sorted(clamped.went_unreachable.items())]
        + composed.notices
    )

    plan_id = f"pln_{secrets.token_hex(4)}"
    total_eta = sum(s.est_eta_seconds for s in cleaned)

    stored = StoredPlan(
        id=plan_id,
        intent=intent,
        # Totals derive from the steps' stroops (ADR 0015), as on the kit path.
        plan=Plan(steps=cleaned, tier=plan_tier),
        total_eta=total_eta,
        notices=notices,
        floor_bps=settings.reputation_floor_bps,
        reputation_degraded=_reputation_degraded(reps),
        planner_fallback=planner_fallback,
        stages=pipeline.stages_with(planner_fallback) if pipeline else [],
    )
    state.add_plan(stored)

    return DecomposeResponse(
        plan_id=plan_id,
        intent=intent,
        steps=cleaned,
        total_eta=round(total_eta, 2),
        # The floor acted BEFORE the planner was asked anything, so these
        # describe the shortlist the model chose from, not the model's choice.
        # An agent that cleared the floor and simply was not picked is absent
        # from `notices` by construction — see `_routable_registry`. The one
        # addition is an offered agent whose endpoint was found dead during
        # the call, which the clamp dropped.
        notices=notices,
        floor_bps=settings.reputation_floor_bps,
        reputation_degraded=_reputation_degraded(reps),
        planner_fallback=planner_fallback,
        **(pipeline.response_fields(planner_fallback) if pipeline else {}),
    )


@dataclass(frozen=True)
class _PipelineFacts:
    """What the Claude pipeline adds to a plan: how it was screened and who planned it.

    Built once the planner has answered (or not), and read by both plan
    builders, so a kit plan and a free-form plan describe themselves the same
    way. `fallback_stage` is the plan line when the served plan turns out to be
    the fallback — only `_finish_free_form` knows that, after the clamp.
    """

    screening: intent_screening.Screening
    plan_stage: str
    fallback_stage: str = ""
    planner: str | None = None
    understood_as: UnderstoodSpec | None = None

    def stages_with(self, planner_fallback: bool) -> list[PlanStage]:
        msg = self.fallback_stage if planner_fallback and self.fallback_stage else self.plan_stage
        return [*self.screening.stages, PlanStage(stage="plan", msg=msg)]

    def response_fields(self, planner_fallback: bool) -> dict[str, Any]:
        decision = self.screening.decision
        return {
            "tier": self.screening.tier,
            "understood_as": self.understood_as,
            "guard": (
                GuardSummary(verdict=decision.verdict, tier=decision.tier, reasons=list(decision.reasons))
                if decision is not None and decision.tier is not None
                else None
            ),
            "models": PlanModels(
                planner=self.planner,
                improver=self.screening.improved_by,
                guard=self.screening.guard_model,
                tiers=TierModels(low=model_for("low"), moderate=model_for("moderate"), complex=model_for("complex")),
            ),
            "stages": self.stages_with(planner_fallback),
        }


def _understood(spec: Spec | None) -> UnderstoodSpec | None:
    """The spec the plan was built from, as the buyer may correct and resubmit it."""
    if spec is None:
        return None
    try:
        return UnderstoodSpec.model_validate(spec.model_dump())
    except ValidationError:
        # Both models carry the same bounds, so this is drift between them, not
        # a buyer's problem: the plan stands, the panel is just not offered.
        logger.warning("an accepted spec does not fit the understood-request shape; not echoing it")
        return None


async def decompose(intent: str, *, spec: UnderstoodSpec | None = None) -> DecomposeResponse:
    """Plan an intent: a curated kit's fixed pipeline, or a planner's clamped plan.

    `ORCHESTRATOR_PROVIDER` picks the planner. On `anthropic` the request is
    screened first (`intent_screening`) and planned on Claude; on `openai` the
    legacy agno planner runs exactly as before and `spec` is ignored, since
    nothing on that path ever offered one to correct.
    """
    # What the plan's stroops are stroops OF (`Plan.asset`). Decided from the
    # config alone on testnet; a non-native SAC is read once and remembered,
    # so the plan built below can name it (ADR 0015).
    await money.current_asset()
    if provider.active_provider() == "anthropic":
        return await _decompose_claude(intent, spec)
    return await _decompose_legacy(intent)


async def _decompose_claude(intent: str, user_spec: UnderstoodSpec | None) -> DecomposeResponse:
    """The Claude pipeline: screen, then plan on Opus, then the same clamp as ever.

    The reputation read runs beside the screening rather than after it: both
    are bounded waits on someone else, and a request the check refuses only
    wastes a read the cache will serve the next one anyway. The registry
    snapshot is still taken first, for the reason `_RegistrySnapshot` gives.
    """
    registry = _snapshot_registry()
    reachability.refresh_stale([a.id for a in registry.routable if a.source == "onchain"])
    kit = detect_kit(intent)
    if kit is None and not registry.routable:
        # Before any paid call, as on the legacy path: no agent, nothing to screen for.
        raise NoRoutableAgentsError("no listed, dispatchable agent to plan with")
    reading = asyncio.create_task(reputation_svc.fetch_reps([a.id for a in registry.routable]))
    try:
        if kit is not None:
            screening = await asyncio.wait_for(intent_screening.screen_kit(intent), settings.decompose_timeout_seconds)
            pipeline = _PipelineFacts(screening=screening, plan_stage=f"Curated demo plan: {kit.brand.name}")
            return await _build_kit_plan(intent, kit, await reading, registry, pipeline)
        return await asyncio.wait_for(
            _screen_and_plan(intent, user_spec, registry, reading), timeout=settings.decompose_timeout_seconds
        )
    finally:
        if not reading.done():
            reading.cancel()


async def _screen_and_plan(
    intent: str,
    user_spec: UnderstoodSpec | None,
    registry: _RegistrySnapshot,
    reading: asyncio.Task[dict[str, reputation_svc.RepInfo]],
) -> DecomposeResponse:
    """The free-form half, inside the planning gate: every call in it is a paid model call."""
    async with _decompose_gate().slot(max(0, settings.decompose_max_queued)):
        edited = Spec.model_validate(user_spec.model_dump()) if user_spec is not None else None
        screening = await intent_screening.screen_free_form(intent, user_spec=edited)
        tier = screening.tier
        # screen_free_form refuses an untiered verdict instead of returning it.
        assert tier is not None
        reps = await reading
        shortlist = _routable_registry(reps, registry)
        if not shortlist.offered:
            raise NoRoutableAgentsError("no listed, dispatchable agent to plan with")
        request: str | Spec = screening.spec if screening.spec is not None else intent
        try:
            result = await draft_plan(request, tier=tier, agents_block=shortlist.block)
        except SpendCapReached as err:
            raise intent_screening.paused(err) from err
        except LLMRefused as err:
            # The planner's own safety classifiers declined what the guard let
            # through. That is a refusal, not a planner fault: no fallback plan
            # is dressed up as an answer to a request the model would not touch.
            logger.warning("planner declined a screened request (category=%s)", err.category)
            raise intent_screening.IntentBlocked(intent_screening.PLANNER_DECLINED_MESSAGE) from err
        except LLMError as err:
            # Unavailable, truncated, invalid output, a rejected request: the
            # buyer gets the fallback plan, flagged, as on the legacy path.
            logger.warning(
                "planner gave no usable plan (%s: %s); serving the fallback plan",
                type(err).__name__,
                _loggable(str(err)),
            )
            result = None

    effort = effort_for(tier)
    planner = (result.served_by or result.model) if result is not None else None
    pipeline = _PipelineFacts(
        screening=screening,
        plan_stage=f"Planned by {display_name(planner or planner_model())} (effort {effort})",
        fallback_stage=(
            "The planner could not answer; a fallback plan was served"
            if result is None
            else "None of the planner's steps could be used; a fallback plan was served"
        ),
        planner=planner,
        understood_as=_understood(screening.spec),
    )
    proposed = result.value.steps if result is not None else []
    return _finish_free_form(
        intent, shortlist, reps, _clamp(proposed, shortlist, reps, plan_tier=tier), plan_tier=tier, pipeline=pipeline
    )


async def _decompose_legacy(intent: str) -> DecomposeResponse:
    # One registry snapshot and one live reputation snapshot per decompose,
    # shared by the kit path, the routing prompt and the per-step stamps.
    #
    # Reputation is read for the ROUTABLE agents only. The registry is
    # permissionless and indexes every registration with no cap, and unbound
    # or delisted agents are unroutable by definition — reading them spent
    # the batch deadline on agents no plan could use, so enough spam
    # registrations timed out every read and pushed every plan onto the prior.
    registry = _snapshot_registry()
    # Ask, in the background, after every bound endpoint this plan could route
    # to that has no fresh health verdict (D-084). It never delays this plan;
    # it is what lets the next one leave a dead endpoint out.
    reachability.refresh_stale([a.id for a in registry.routable if a.source == "onchain"])
    reps = await reputation_svc.fetch_reps([a.id for a in registry.routable])

    # ── Demo-kit short circuit ─────────────────────────────────────────────
    # If the intent matches a curated kit (tetris / calculator / snake /
    # pomodoro), bypass the LLM orchestrator entirely and return the
    # deterministic 6-step pipeline. Reliable for live demos; no LLM cost.
    kit = detect_kit(intent)
    if kit is not None:
        return await _build_kit_plan(intent, kit, reps, registry)

    # ── Free-form path: LLM orchestrator decides the plan ──────────────────
    shortlist = _routable_registry(reps, registry)
    if not shortlist.offered:
        # Checked before the gate, not after the call: an empty AVAILABLE_AGENTS
        # block can only produce steps the clamp discards, so the LLM call would
        # be paid for, hold a planning slot, and change nothing.
        raise NoRoutableAgentsError("no listed, dispatchable agent to plan with")
    prompt = build_planning_prompt(shortlist.block, intent)

    async def _bounded_plan() -> Any:
        # The kit short circuit above never takes this gate; every request
        # here is a real LLM call, so concurrency is capped the same way
        # /execute's fan-out is. Queue time counts against the budget below,
        # and a full queue refuses the request outright (PlannerBusyError).
        async with _decompose_gate().slot(max(0, settings.decompose_max_queued)):
            return await orchestrator_agent.arun(prompt)

    # Hard end-to-end budget for the planning call — without it a hung
    # upstream would pin this request for the OpenAI client's full
    # timeout x retry envelope. The router maps TimeoutError to a 504.
    try:
        result = await asyncio.wait_for(
            _bounded_plan(),
            timeout=settings.decompose_timeout_seconds,
        )
    except (TimeoutError, PlannerBusyError):
        # Kept out of the degradation below on purpose: a hung planner has
        # already cost the caller the whole budget, and 504 `decompose_timeout`
        # is the answer the router and its clients already speak for that. A
        # full queue never reached the planner at all — a fallback plan would
        # dress a refusal up as an answer — so it is the router's 503.
        raise
    except Exception as e:
        # agno hands provider errors back as a failed run, which
        # `_planner_plan` reads, so what raises here failed around the model
        # call rather than inside it. The buyer's answer is the same either
        # way, and the `async with` in `_bounded_plan` has already given the
        # planning slot back.
        logger.warning(
            "planner %s call raised %s: %r; serving the fallback plan",
            settings.orchestrator_model,
            type(e).__name__,
            _loggable(str(e)),
        )
        plan = None
    else:
        plan = _planner_plan(result)

    return _finish_free_form(intent, shortlist, reps, _clamp(plan.steps if plan is not None else [], shortlist, reps))
