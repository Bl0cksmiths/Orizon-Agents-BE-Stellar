"""Reputation-floor notices — the one place a floor action becomes a payload.

Story 3.02 (BLO-24) promises the buyer an honest account of what the routing
floor did to their plan, and the orchestrator has TWO planning paths that have
to keep that promise the same way: the curated demo-kit pipeline and the
free-form LLM plan. Building the payload inline in both is how the same
exclusion ends up reading "below routing floor" on one path and something
else on the other, and a client switching on `reason_code` then has two
vocabularies to learn for one event. So every notice either path emits is
constructed here, which makes acceptance criterion 6 — identical shape,
identical vocabulary, whichever path planned — true by construction instead of
true for as long as both paths are edited together.

Two properties keep that from rotting:

  * Nothing here takes the floor as an argument. Each builder reads
    `settings.reputation_floor_bps` itself, so the number stamped on a notice
    cannot disagree with the number the floor was actually applied with, and a
    caller cannot hand in a stale one it cached earlier in the request.
  * Everything here is pure — no I/O, no clock, no registry lookup. A notice is
    a function of its agent, its reputation entry and settings. The kit path is
    the demo safety net and must render the same plan card twice from the same
    reputation snapshot; a notice builder that read anything live would be the
    one thing in that path that could not promise it.

`kind` (what happened to the plan) and `reason_code` (why) are orthogonal, and
every construction site below sets both explicitly rather than leaning on the
schema default — see `PlanFloorNotice` for why the pair exists at all.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..config import settings
from ..schemas import Agent, PlanFloorNotice
from .reputation_svc import RepInfo

# How many unbound agents a single plan will name before it stops listing them.
#
# The registry is permissionless: anyone can index an agent on-chain, and most
# of them will never be bound to an endpoint, so the unbound set grows without
# limit while the number of agents a plan actually wanted stays around six.
# Listing all of them would bury the two or three notices the buyer needs to
# read — the exact signal this story exists to create — under a roll-call of
# agents they never asked for, and would grow the /decompose payload with it.
# Eight is the compromise: comfortably more than one plan's worth of genuine
# near-misses, few enough to stay scannable on the plan card.
UNBOUND_REPORT_CAP: int = 8

# Deliberately not phrased as a failure. An unbound agent is never dispatched
# to in the first place, so it has failed nothing — the same distinction the
# executor already makes when it refuses to rate an undispatched step ("did not
# deliver" and "was never asked" are different facts, ADR 0005 D5). Wording
# that blamed the agent here would put a slur on a plan card for an operator
# whose only omission is a bind that has not happened yet.
_UNBOUND_REASON = (
    "registered on-chain but no endpoint bound (nothing to dispatch a step to, so the planner passed it over)"
)


def _floor_reason(info: RepInfo | None) -> str:
    """Why the floor acted on an agent, with the deciding lower-bound bps.

    Carried over verbatim from the wording `orchestrator_svc` already ships,
    including the 0 it prints when there is no reputation entry. A frontend and
    a test suite both assert on this exact sentence, so it is a compatibility
    surface rather than prose to improve — a better sentence here breaks them
    silently, at read time, in a plan card nobody is watching.

    In practice the None case never reaches this function: an agent with no
    entry PASSES the floor (`reputation_svc.passes_floor`), so it is never the
    subject of a below-floor notice. The parameter stays optional only because
    the callers hold `dict.get` results and should not have to narrow them.
    """
    lb = info.lower_bound_bps if info is not None else 0
    return f"below routing floor ({lb} < {settings.reputation_floor_bps} bps)"


def _lower_bound(info: RepInfo | None) -> int | None:
    """The deciding bound as data — None, never 0, when there is no rep entry.

    "This agent has no reputation entry" and "this agent's lower bound is zero"
    are different facts, and they do not even carry the same verdict: a missing
    entry passes the floor, a zero bound fails it under any sane config. A 0
    substituted here would therefore render as the worst possible score beside
    an agent the planner happily routed to, and the buyer has no way to tell
    that apart from a genuinely terrible agent.

    The prose from `_floor_reason` is stuck with its historical 0; this field
    is new (story 3.02) and is not, so it tells the truth.
    """
    return None if info is None else info.lower_bound_bps


def below_floor_exclusion(agent: Agent, info: RepInfo | None) -> PlanFloorNotice:
    """A sub-floor agent dropped from the plan outright, with no stand-in."""
    return PlanFloorNotice(
        kind="excluded",
        agent_id=agent.id,
        agent_name=agent.name,
        reason=_floor_reason(info),
        reason_code="below_floor",
        lower_bound_bps=_lower_bound(info),
        floor_bps=settings.reputation_floor_bps,
    )


def unbound_exclusion(agent: Agent) -> PlanFloorNotice:
    """An agent left out because there is no endpoint to dispatch a step to.

    Not a reputation verdict, which is the whole reason `reason_code` exists
    separately from `kind`: an on-chain indexed agent is marketplace-visible
    from the moment it registers but only routable once an operator binds it an
    endpoint (story 2.01). Its reputation may be excellent and irrelevant, so
    `lower_bound_bps` stays None — there is no deciding number to show.

    `floor_bps` is stamped anyway, as on every other notice, because it states
    the threshold THIS PLAN was built under rather than anything about this
    agent; a renderer that wants to print the floor should not have to branch
    on reason_code to find out whether the field is populated.
    """
    return PlanFloorNotice(
        kind="excluded",
        agent_id=agent.id,
        agent_name=agent.name,
        reason=_UNBOUND_REASON,
        reason_code="unbound_endpoint",
        lower_bound_bps=None,
        floor_bps=settings.reputation_floor_bps,
    )


def substitution(designated: Agent, replacement: Agent, info: RepInfo | None) -> PlanFloorNotice:
    """A sub-floor agent whose step a floor-clearing agent took over.

    The notice is about the DESIGNATED agent — it is the one the floor acted on
    and the one the buyer expected to see — with the replacement named
    alongside. `info` is likewise the designated agent's entry: `lower_bound_bps`
    has to be the number that lost the step, not the substitute's, or the card
    reads as an accusation against the agent that is actually doing the work.
    """
    return PlanFloorNotice(
        kind="substituted",
        agent_id=designated.id,
        agent_name=designated.name,
        replacement_id=replacement.id,
        replacement_name=replacement.name,
        reason=_floor_reason(info),
        reason_code="below_floor",
        lower_bound_bps=_lower_bound(info),
        floor_bps=settings.reputation_floor_bps,
    )


def relaxation(agent: Agent, info: RepInfo | None, *, min_routable: int) -> PlanFloorNotice:
    """A sub-floor agent the starvation backstop re-admitted below the floor.

    `min_routable` is passed in rather than read from a constant here because
    the backstop's arithmetic belongs to the planner that runs it. Copying the
    threshold into this module would give one number two homes, and the copy
    that goes stale after a tune is always the one printed at the buyer.
    Only the sentence is ours.
    """
    reason = f"re-admitted below the floor to keep the plan workable (fewer than {min_routable} agents cleared it)"
    return PlanFloorNotice(
        kind="degraded",
        agent_id=agent.id,
        agent_name=agent.name,
        reason=reason,
        reason_code="floor_relaxed",
        lower_bound_bps=_lower_bound(info),
        floor_bps=settings.reputation_floor_bps,
    )


def unbound_exclusions(agents: Iterable[Agent]) -> list[PlanFloorNotice]:
    """Unbound-endpoint notices for `agents`, ordered by id and capped.

    Sorted before the cap, not after, and by id rather than by anything derived
    from live state: which eight agents get named has to be a property of the
    input set alone. The kit path is the demo safety net — the same registry
    snapshot must produce the same plan card every time, and a cap applied to
    an arbitrarily ordered iterable would quietly rotate the names on the card
    between two identical requests.

    `agents` is the set the caller found unroutable for want of a binding; this
    function does not decide that (it would need the binding registry, and this
    module stays pure). It only decides how much of it the buyer is shown.
    """
    ordered = sorted(agents, key=lambda a: a.id)
    return [unbound_exclusion(a) for a in ordered[:UNBOUND_REPORT_CAP]]
