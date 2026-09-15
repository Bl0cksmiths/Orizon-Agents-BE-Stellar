from __future__ import annotations

import asyncio
import logging
import random
import secrets
from typing import Any

from ..agents.orchestrator import orchestrator_agent
from ..agents.workers.prompt_safety import fence_user_input
from ..config import settings
from ..demo_kits import DemoKit, detect_kit
from ..schemas import Agent, DecomposeResponse, Plan, PlanFloorNotice, PlanStep, StoredPlan
from ..state import state
from . import reputation_svc
from .binding_registry import is_dispatchable

logger = logging.getLogger(__name__)

# The reputation floor may never starve the planner of choices.
_MIN_ROUTABLE_AGENTS = 3

# Gate on the free-form planning LLM call. /execute's fan-out is bounded by
# orchestrator_max_concurrent (execution_svc); this is the same protection for
# /decompose, whose non-kit path makes a real LLM call per request while the
# rate limiter spends one shared bucket. Sized at call time from settings like
# execute_plan's ceiling — the semaphore is rebuilt only when the configured
# limit changes (production never does; tests tune it).
_plan_gate: asyncio.Semaphore | None = None
_plan_gate_limit: int | None = None


def _decompose_gate() -> asyncio.Semaphore:
    global _plan_gate, _plan_gate_limit
    limit = max(1, settings.decompose_max_concurrent)
    if _plan_gate is None or _plan_gate_limit != limit:
        _plan_gate = asyncio.Semaphore(limit)
        _plan_gate_limit = limit
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


def _rep_fields(info: reputation_svc.RepInfo | None) -> dict[str, Any]:
    """PlanStep reputation stamp — empty when the agent has no rep entry."""
    if info is None:
        return {}
    return {"rep_bps": info.smoothed_bps, "rep_source": info.source}


# Kit-pipeline agent ids. Substitutes are drawn from OUTSIDE this set —
# borrowing one kit role's agent to fill another is itself a silent reshuffle,
# which the product rules forbid.
_KIT_AGENT_IDS: frozenset[str] = frozenset(aid for aid, _ in _KIT_PIPELINE)


def _floor_reason(info: reputation_svc.RepInfo | None) -> str:
    """Why the floor acted on an agent, with the deciding lower-bound bps."""
    lb = info.lower_bound_bps if info is not None else 0
    return f"below routing floor ({lb} < {settings.reputation_floor_bps} bps)"


def _kit_step(
    agent: Agent,
    rationale: str,
    eta: float,
    reps: dict[str, reputation_svc.RepInfo],
    substituted_for: str | None = None,
    degraded: bool = False,
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
        est_price_usdc=agent.price,
        est_eta_seconds=eta,
        substituted_for=substituted_for,
        degraded=degraded,
        **_rep_fields(reps.get(agent.id)),
    )


def _floor_substitute(
    designated: Agent,
    reps: dict[str, reputation_svc.RepInfo],
    taken: set[str],
) -> Agent | None:
    """Deterministically pick a floor-clearing replacement for a sub-floor kit
    agent: dispatchable, OFF the kit pipeline, sharing >=1 skill, not already
    used in this plan. Highest smoothed score wins, id breaks ties — a pure
    function of the reputation snapshot, so the plan stays reproducible.

    "Dispatchable" is a local worker OR a bound external endpoint (story 2.01).
    The floor is unchanged and still applied here: a bound agent stands in for
    a sub-floor kit agent only if it clears the floor on the same arithmetic.
    """
    wanted = set(designated.skills)
    candidates = [
        a
        for a in state.list_agents()
        if a.id not in taken
        and a.id not in _KIT_AGENT_IDS
        and is_dispatchable(a.id)
        and reputation_svc.passes_floor(reps.get(a.id))
        and wanted.intersection(a.skills)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda a: (
            -(reps[a.id].smoothed_bps if a.id in reps else round(a.rep * 2000)),
            a.id,
        )
    )
    return candidates[0]


def _registry_prompt_fragment(reps: dict[str, reputation_svc.RepInfo]) -> str:
    # An indexed on-chain agent (story 1.02) is marketplace-visible but only
    # planner-routable once an operator binds it an endpoint (story 2.01) —
    # until then it has nothing to execute a step with. The filter sits on the
    # assignment so the floor-starvation fallback below (which sorts this list,
    # not `routable`) can never admit an unbound one either.
    agents = [a for a in state.list_agents() if is_dispatchable(a.id)]
    routable = [a for a in agents if reputation_svc.passes_floor(reps.get(a.id))]
    if len(routable) < _MIN_ROUTABLE_AGENTS:
        logger.warning(
            "reputation floor left only %d/%d agents routable; keeping top %d by smoothed score",
            len(routable),
            len(agents),
            _MIN_ROUTABLE_AGENTS,
        )
        routable = sorted(
            agents,
            key=lambda a: reps[a.id].smoothed_bps if a.id in reps else round(a.rep * 2000),
            reverse=True,
        )[:_MIN_ROUTABLE_AGENTS]

    lines = ["AVAILABLE_AGENTS:"]
    for a in routable:
        info = reps.get(a.id)
        # Live smoothed score on the 0–5 scale the prompt already uses;
        # seeded rep only when the agent has no reputation entry.
        rep_display = info.smoothed_bps / 2000 if info is not None else a.rep
        lines.append(f"- id={a.id} name={a.name} price={a.price:.3f} rep={rep_display:.2f} skills={','.join(a.skills)}")
    return "\n".join(lines)


def build_planning_prompt(registry_block: str, intent: str) -> str:
    """Registry facts, then the FENCED intent, then the ask.

    The intent is free-form and attacker-controllable, so it is never spliced
    bare next to AVAILABLE_AGENTS — it arrives as a delimited data block the
    planner is told not to obey. The trusted instruction goes last.
    """
    return "\n\n".join([registry_block, fence_user_input(intent), "Return the Plan."])


async def _build_kit_plan(intent: str, kit: DemoKit, reps: dict[str, reputation_svc.RepInfo]) -> DecomposeResponse:
    """Deterministic 6-step plan for a curated demo intent. No LLM call.

    The reputation floor is applied to every pipeline agent, exactly as on the
    free-form path: a sub-floor agent is replaced by a floor-clearing worker
    that shares a skill, or dropped, and every such action is recorded on the
    response so the buyer never sees a silently reshuffled pipeline (story
    3.02). Given the same reputation snapshot the plan — steps and notices — is
    identical, with no LLM call.

    A short randomized sleep up front mimics orchestrator "thinking time" so
    the Decompose UX feels like real LLM planning instead of a hardcoded dict
    being unpacked. It changes timing only, never plan content.
    """
    await asyncio.sleep(1.4 + random.random() * 1.0)

    steps: list[PlanStep] = []
    notices: list[PlanFloorNotice] = []
    taken: set[str] = set()
    # Sub-floor agents with no substitute — held until after the loop so the
    # starvation backstop can re-admit the strongest before the rest are
    # recorded as plain exclusions.
    dropped: list[tuple[Agent, str, reputation_svc.RepInfo | None]] = []

    for agent_id, rationale in _KIT_PIPELINE:
        agent = state.agents.get(agent_id)
        if agent is None:
            # The kit pipeline references an agent that isn't seeded — this
            # is a programmer error. Skip the step rather than crash the
            # whole pipeline.
            continue

        eta = _KIT_ETAS.get(agent_id, 1.0)
        info = reps.get(agent.id)
        if reputation_svc.passes_floor(info):
            steps.append(_kit_step(agent, rationale, eta, reps))
            taken.add(agent.id)
            continue

        # Sub-floor: substitute with a floor-clearing off-pipeline worker that
        # shares a skill, else drop the step. Either way the buyer is told.
        sub = _floor_substitute(agent, reps, taken)
        if sub is not None:
            steps.append(_kit_step(sub, rationale, eta, reps, substituted_for=agent.id))
            taken.add(sub.id)
            notices.append(
                PlanFloorNotice(
                    kind="substituted",
                    agent_id=agent.id,
                    agent_name=agent.name,
                    replacement_id=sub.id,
                    replacement_name=sub.name,
                    reason=_floor_reason(info),
                )
            )
        else:
            dropped.append((agent, rationale, info))

    # Starvation backstop — reuse _MIN_ROUTABLE_AGENTS rather than invent a
    # second rule. If the floor left too few steps, re-admit the highest-scored
    # dropped kit agents (top-N by smoothed score, id breaking ties) and record
    # the degradation; the remainder are recorded as exclusions. Re-admitted
    # steps are appended in pipeline order for a coherent plan.
    by_score = sorted(
        dropped,
        key=lambda d: (-(d[2].smoothed_bps if d[2] is not None else 0), d[0].id),
    )
    deficit = max(0, _MIN_ROUTABLE_AGENTS - len(steps))
    readmit_ids = {d[0].id for d in by_score[:deficit]}
    if readmit_ids:
        logger.warning(
            "reputation floor left only %d kit step(s); re-admitting %d dropped agent(s) by smoothed score",
            len(steps),
            len(readmit_ids),
        )
    for agent, rationale, info in dropped:
        if agent.id in readmit_ids:
            steps.append(_kit_step(agent, rationale, _KIT_ETAS.get(agent.id, 1.0), reps, degraded=True))
            taken.add(agent.id)
            notices.append(
                PlanFloorNotice(
                    kind="degraded",
                    agent_id=agent.id,
                    agent_name=agent.name,
                    reason=(
                        "re-admitted below the floor to keep the plan workable "
                        f"(fewer than {_MIN_ROUTABLE_AGENTS} agents cleared it)"
                    ),
                )
            )
        else:
            notices.append(
                PlanFloorNotice(
                    kind="excluded",
                    agent_id=agent.id,
                    agent_name=agent.name,
                    reason=_floor_reason(info),
                )
            )

    plan_id = f"pln_{secrets.token_hex(4)}"
    total_price = sum(s.est_price_usdc for s in steps)
    total_eta = sum(s.est_eta_seconds for s in steps)

    stored = StoredPlan(
        id=plan_id,
        intent=intent,
        plan=Plan(steps=steps),
        total_usdc=total_price,
        total_eta=total_eta,
    )
    state.add_plan(stored)

    return DecomposeResponse(
        plan_id=plan_id,
        intent=intent,
        steps=steps,
        total_usdc=round(total_price, 4),
        total_eta=round(total_eta, 2),
        notices=notices,
    )


async def decompose(intent: str) -> DecomposeResponse:
    # One live reputation snapshot per decompose — timeout-bounded and never
    # raises (prior fallback), shared by the kit path, the routing prompt,
    # and the per-step reputation stamps.
    reps = await reputation_svc.fetch_reps([a.id for a in state.list_agents()])

    # ── Demo-kit short circuit ─────────────────────────────────────────────
    # If the intent matches a curated kit (tetris / calculator / snake /
    # pomodoro), bypass the LLM orchestrator entirely and return the
    # deterministic 6-step pipeline. Reliable for live demos; no LLM cost.
    kit = detect_kit(intent)
    if kit is not None:
        return await _build_kit_plan(intent, kit, reps)

    # ── Free-form path: LLM orchestrator decides the plan ──────────────────
    prompt = build_planning_prompt(_registry_prompt_fragment(reps), intent)

    async def _bounded_plan() -> Any:
        # The kit short circuit above never takes this gate; every request
        # here is a real LLM call, so concurrency is capped the same way
        # /execute's fan-out is. Queue time counts against the budget below.
        async with _decompose_gate():
            return await orchestrator_agent.arun(prompt)

    # Hard end-to-end budget for the planning call — without it a hung
    # upstream would pin this request for the OpenAI client's full
    # timeout x retry envelope. The router maps TimeoutError to a 504.
    result = await asyncio.wait_for(
        _bounded_plan(),
        timeout=settings.decompose_timeout_seconds,
    )
    plan: Plan = result.content

    # Clamp to known agents; backfill names + snap price to registry truth.
    cleaned: list[PlanStep] = []
    for step in plan.steps:
        agent = state.agents.get(step.agent_id)
        if not agent or not is_dispatchable(agent.id):
            # Drop unknown ids silently — the model sometimes invents — or
            # names an indexed agent that nothing can execute: no local worker
            # and no operator binding. Dropping it here means /execute can
            # never reach the unknown-agent skip path for a planned step. A
            # bound external agent survives this filter, which is the point.
            continue
        cleaned.append(
            PlanStep(
                agent_id=agent.id,
                agent_name=agent.name,
                rationale=step.rationale.strip(),
                est_price_usdc=agent.price,
                est_eta_seconds=max(0.3, min(step.est_eta_seconds, 3.0)),
                **_rep_fields(reps.get(agent.id)),
            )
        )

    if not cleaned:
        # Fall back to a minimal safe plan so the UI never gets stuck.
        copy_agent = state.agents["agt_01h8"]
        cleaned = [
            PlanStep(
                agent_id=copy_agent.id,
                agent_name=copy_agent.name,
                rationale="fallback: generate copy for the intent",
                est_price_usdc=copy_agent.price,
                est_eta_seconds=0.8,
                **_rep_fields(reps.get(copy_agent.id)),
            )
        ]

    plan_id = f"pln_{secrets.token_hex(4)}"
    total_price = sum(s.est_price_usdc for s in cleaned)
    total_eta = sum(s.est_eta_seconds for s in cleaned)

    stored = StoredPlan(
        id=plan_id,
        intent=intent,
        plan=Plan(steps=cleaned),
        total_usdc=total_price,
        total_eta=total_eta,
    )
    state.add_plan(stored)

    return DecomposeResponse(
        plan_id=plan_id,
        intent=intent,
        steps=cleaned,
        total_usdc=round(total_price, 4),
        total_eta=round(total_eta, 2),
    )
