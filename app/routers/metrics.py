"""GET /api/metrics/overview — the dashboard's network numbers, all measured.

Every value here is read from a source a reviewer can check: the agent
registry mirror (and whether it is complete, `registry_synced`), the adoption report's owner rule, the binding set, the
durable settlement store, the in-memory task store and on-chain reputation.
Nothing is invented. A part whose source cannot be read is reported as null
(or an empty list) and sets `degraded`, and the part is named in a
rate-limited log line — it is never replaced by a number that looks measured
(ADR 0013).

The whole overview is computed at most once per OVERVIEW_CACHE_TTL_SECONDS and
shared by every concurrent caller (single-flight, `app.stellar.cache`), so the
dashboard's polling cannot turn into a chain read and a database query per
request. The app-wide rate limit (`RateLimitMiddleware`) still applies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter

from ..schemas import (
    Agent,
    OverviewAgents,
    OverviewMetrics,
    OverviewOperators,
    OverviewTasks,
    OverviewTrust,
    OverviewWorkflows,
    SettledDay,
    SkillShare,
)
from ..services import adoption_svc, binding_registry, registry_sync, reputation_svc
from ..services.dispute_store import SECONDS_PER_DAY, get_dispute_store
from ..state import state
from ..stellar import cache as rcache

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])

# One computation per this many seconds, whoever is polling. Short enough that
# a settlement shows up within a poll or two; long enough that a dashboard left
# open in several tabs costs one set of reads, not one per tab per poll.
OVERVIEW_CACHE_KEY = "metrics:overview"
OVERVIEW_CACHE_TTL_SECONDS = 15.0

# The settled-workflow sparkline: one point per UTC day, today included.
SERIES_DAYS = 14

# The skill mix names this many skills and folds the rest into "other".
TOP_SKILLS = 5
OTHER_SKILL = "other"

# Upper bound on the settlement-store read. The Postgres pool's own timeouts
# (connect, acquire, command) add up to tens of seconds, which is a hung
# dashboard; past this the part is reported unreadable and the rest of the
# overview is served.
SETTLEMENT_READ_BUDGET_SECONDS = 5.0

# Tasks the completion rate is computed over: the whole in-memory task store.
_TASK_WINDOW = state.task_order.maxlen or 200

# The dashboard polls every few seconds and an outage lasts minutes, so a
# degraded part is logged on a duty cycle: every transition (measured →
# degraded and back) at once, and a steady degraded state at most this often.
# One line per poll would bury the log on a free-tier instance; no line at all
# is what let the old baselines pass for measurements.
_DEGRADED_LOG_INTERVAL_SECONDS = 300.0


class _SourceNote:
    """Rate-limited record of whether one part of the overview was measured."""

    def __init__(self, part: str) -> None:
        self.part = part
        self.degraded: bool | None = None  # None = nothing observed yet
        self.logged_at = 0.0

    def note(self, degraded: bool, reason: str = "", exc_info: bool = False) -> None:
        changed = degraded != self.degraded
        if not degraded:
            if changed and self.degraded is not None:
                logger.info("overview %s recovered: measured again", self.part)
            self.degraded = False
            return
        now = time.monotonic()
        if changed or now - self.logged_at >= _DEGRADED_LOG_INTERVAL_SECONDS:
            logger.warning(
                "overview %s degraded: %s — served as unknown or partial, never as a stand-in value",
                self.part,
                reason,
                exc_info=exc_info,
            )
            self.logged_at = now
        self.degraded = True


_external_note = _SourceNote("agents.external")
_bound_note = _SourceNote("agents.bound")
_workflows_note = _SourceNote("workflows")
_trust_note = _SourceNote("trust")
_registry_note = _SourceNote("agents (registry mirror)")


# ── agents and operators ──────────────────────────────────────────────────
@dataclass(frozen=True)
class _External:
    agents: int | None
    wallets: int | None
    degraded: bool


async def _external() -> _External:
    """External agents and their distinct owners, by adoption_svc's own rule.

    The same mirror and the same `OwnerRule` the adoption report counts with,
    so the two can never disagree. Like that report, an on-chain agent whose
    owner is unknown is not counted, and a platform key that could not be read
    leaves an owner we cannot rule out; either one marks the part degraded.
    The report's settlement scans are not run: this needs owners, not charges.
    """
    mirror = adoption_svc.onchain_mirror()
    try:
        rule = await adoption_svc.owner_rule()
    except Exception as e:
        # owner_rule records its failed reads rather than raising, so reaching
        # this is a bug worth a traceback, not a silent zero.
        _external_note.note(True, f"owner rule raised {type(e).__name__}", exc_info=True)
        return _External(agents=None, wallets=None, degraded=True)
    unowned = sorted(a.id for a in mirror.values() if not a.owner)
    external = [a for a in mirror.values() if a.owner and rule.classify(a.owner) is None]
    reasons = []
    if rule.unreadable:
        reasons.append(f"platform keys unreadable ({', '.join(rule.unreadable)}), so an owner may be ours")
    if unowned:
        reasons.append(f"{len(unowned)} on-chain agent(s) with no known owner not counted: {', '.join(unowned[:5])}")
    _external_note.note(bool(reasons), "; ".join(reasons))
    return _External(
        agents=len(external),
        wallets=len({a.owner for a in external}),
        degraded=bool(reasons),
    )


def _bound(agents: list[Agent]) -> int | None:
    """On-chain agents with an endpoint bound, as GET /api/agents reports them.

    Only an on-chain agent can be bound (a seeded one runs in-process). None
    when the bound set has not been loaded: its emptiness then says nothing.
    """
    answers = [binding_registry.is_bound(a.id) for a in agents if a.source == "onchain"]
    if any(answer is None for answer in answers):
        _bound_note.note(True, "the binding set has not been loaded from the binding store")
        return None
    _bound_note.note(False)
    return sum(1 for answer in answers if answer)


# ── workflows ─────────────────────────────────────────────────────────────
def _day_label(day: int) -> str:
    return datetime.fromtimestamp(day * SECONDS_PER_DAY, UTC).date().isoformat()


@dataclass(frozen=True)
class _Workflows:
    settled: int | None
    series: list[SettledDay]
    degraded: bool


async def _workflows(now: float) -> _Workflows:
    """Settled workflows from the durable settlement store, all payers.

    The total is every settlement the store holds; the series is the last
    SERIES_DAYS UTC days, oldest first, with a zero for a day that saw none.
    """
    try:
        by_day = await asyncio.wait_for(
            get_dispute_store().count_settled_by_day(), timeout=SETTLEMENT_READ_BUDGET_SECONDS
        )
    except Exception as e:
        _workflows_note.note(True, f"settlement store unreadable: {type(e).__name__}: {e}")
        return _Workflows(settled=None, series=[], degraded=True)
    _workflows_note.note(False)
    today = int(now // SECONDS_PER_DAY)
    series = [
        SettledDay(date=_day_label(day), settled=by_day.get(day, 0))
        for day in range(today - SERIES_DAYS + 1, today + 1)
    ]
    return _Workflows(settled=sum(by_day.values()), series=series, degraded=False)


# ── tasks ─────────────────────────────────────────────────────────────────
def _tasks() -> OverviewTasks:
    """Counts over the in-memory task store. The rate is over decided tasks
    only: pending and running ones are not failures."""
    tasks = state.recent_tasks(limit=_TASK_WINDOW)
    complete = sum(1 for t in tasks if t.status == "complete")
    failed = sum(1 for t in tasks if t.status == "failed")
    decided = complete + failed
    return OverviewTasks(
        recent=len(tasks),
        complete=complete,
        failed=failed,
        completion_rate=round(complete / decided, 3) if decided else None,
    )


# ── trust ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Trust:
    trust: OverviewTrust
    degraded: bool


async def _trust(agents: list[Agent]) -> _Trust:
    """Mean smoothed on-chain reputation on the 0–5 scale.

    Only agents whose reputation read came back with on-chain evidence
    (`source == "onchain"`, a last-known read included) count. The flat prior
    of an unrated agent is not evidence, so with none the average is None —
    "nothing rated yet" is a measured state, not a failure. A read that
    reputation_svc had to degrade to the prior marks the part degraded: the
    average may then be missing agents.
    """
    if not agents:
        _trust_note.note(False)
        return _Trust(OverviewTrust(avg=None, rated_agents=0), degraded=False)
    try:
        infos = await reputation_svc.fetch_reps([a.id for a in agents])
    except Exception as e:
        # fetch_reps is documented never to raise; reaching this is a bug.
        _trust_note.note(True, f"reputation batch read raised {type(e).__name__}", exc_info=True)
        return _Trust(OverviewTrust(avg=None, rated_agents=None), degraded=True)
    rated = [info.smoothed_bps for info in infos.values() if info.source == "onchain"]
    unreadable = sum(1 for info in infos.values() if info.degraded)
    if unreadable:
        _trust_note.note(True, f"{unreadable}/{len(infos)} reputation reads degraded to the prior")
    else:
        _trust_note.note(False)
    avg = round(sum(rated) / len(rated) / 2000.0, 2) if rated else None
    return _Trust(OverviewTrust(avg=avg, rated_agents=len(rated)), degraded=bool(unreadable))


# ── skills ────────────────────────────────────────────────────────────────
def _largest_remainder(weights: list[int]) -> list[int]:
    """Whole percentages of `weights` that sum to exactly 100.

    Each share is floored, then the points left over go to the largest
    remainders (ties to the earlier entry), so no share is off by more than
    one point and the list never sums to 99 or 101.
    """
    total = sum(weights)
    # Integer arithmetic throughout: a float share like 28.000000000000004
    # would otherwise decide which entry gets a point.
    pcts = [w * 100 // total for w in weights]
    remainders = [w * 100 % total for w in weights]
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[: 100 - sum(pcts)]:
        pcts[i] += 1
    return pcts


def _skills(agents: list[Agent]) -> list[SkillShare]:
    """The registry's skill mix: the top TOP_SKILLS skills by agent count, then
    "other" for the rest.

    `agents` is how many agents carry the skill (for "other", how many carry
    any of the rest). `pct` is the skill's share of all skill tags in the
    registry: an agent lists several skills, so shares of agents would not sum
    to 100. A skill literally named "other" is folded into the "other" row.
    """
    holders: dict[str, set[str]] = {}
    for agent in agents:
        for skill in {s.strip().lower() for s in agent.skills if s.strip()}:
            holders.setdefault(skill, set()).add(agent.id)
    ranked = sorted((s for s in holders if s != OTHER_SKILL), key=lambda s: (-len(holders[s]), s))
    top, rest = ranked[:TOP_SKILLS], ranked[TOP_SKILLS:]
    if OTHER_SKILL in holders:
        rest.append(OTHER_SKILL)
    rows = [(name, len(holders[name]), len(holders[name])) for name in top]
    if rest:
        rows.append(
            (
                OTHER_SKILL,
                len(set().union(*(holders[s] for s in rest))),
                sum(len(holders[s]) for s in rest),
            )
        )
    if not rows:
        return []
    pcts = _largest_remainder([tags for _, _, tags in rows])
    return [SkillShare(name=name, agents=count, pct=pct) for (name, count, _), pct in zip(rows, pcts, strict=True)]


# ── the overview ──────────────────────────────────────────────────────────
async def build_overview() -> OverviewMetrics:
    """Compute the overview from its sources. Never raises for a failed read.

    The mirror's sync status is read with the agent list, no await between, so
    `registry_synced` describes exactly the agents every count is taken over.
    A mirror still filling after a restart is not a failed read: its counts are
    served, as the partial counts they are, and `degraded` says so.
    """
    now = time.time()
    synced = registry_sync.status().synced
    agents = state.list_agents()
    _registry_note.note(not synced, f"the registry mirror has not finished a full pass; {len(agents)} agents so far")
    external, workflows, trust = await asyncio.gather(_external(), _workflows(now), _trust(agents))
    bound = _bound(agents)
    return OverviewMetrics(
        generated_at=now,
        agents=OverviewAgents(
            registered=len(agents),
            onchain=sum(1 for a in agents if a.source == "onchain"),
            seeded=sum(1 for a in agents if a.source == "seeded"),
            external=external.agents,
            bound=bound,
            online=sum(1 for a in agents if a.status == "online"),
        ),
        operators=OverviewOperators(external_wallets=external.wallets),
        workflows=OverviewWorkflows(settled=workflows.settled, series=workflows.series),
        tasks=_tasks(),
        trust=trust.trust,
        skills=_skills(agents),
        registry_synced=synced,
        degraded=not synced or external.degraded or bound is None or workflows.degraded or trust.degraded,
    )


async def fetch_overview() -> OverviewMetrics:
    """The cached overview: one computation per OVERVIEW_CACHE_TTL_SECONDS,
    shared by every concurrent caller (the cache is single-flight)."""
    result = await rcache.get_or_set(OVERVIEW_CACHE_KEY, OVERVIEW_CACHE_TTL_SECONDS, build_overview)
    if not isinstance(result, OverviewMetrics):
        raise RuntimeError(f"overview cache held {type(result).__name__}")
    return result


@router.get("/metrics/overview", response_model=OverviewMetrics, summary="Dashboard overview metrics")
async def overview() -> OverviewMetrics:
    """Measured network numbers for the dashboard.

    Agents (registered, on-chain, seeded, external, bound, online), distinct
    external operator wallets, settled workflows from the durable settlement
    store with a 14-day UTC series, task completion over the in-memory task
    store, mean on-chain trust, and the registry's skill mix. Nothing is a
    baseline or a fallback: a part that could not be read is null (or []) and
    `degraded` is true. Cached for 15 s and shared across callers.
    """
    return await fetch_overview()
