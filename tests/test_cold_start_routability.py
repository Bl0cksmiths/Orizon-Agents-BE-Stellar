"""Cold-start routability — a newly registered agent is hireable (story 3.06, BLO-28).

The registry is permissionless: an agent registers on-chain, has never been
rated, and must still be routable, or the marketplace has no way in and the
oldest incumbents keep every job by default. `reputation_svc` implements that
with a Bayesian prior whose lower bound (5677 bps) sits just above the shipped
routing floor (5500 bps), and `test_rating_economics.py` pins the arithmetic.

What was untested is whether the guarantee survives the trip through the
PLANNER. Every decompose-level test in this repo models a cold-start agent as
a MISSING reputation entry, and `passes_floor(None)` returns True without ever
comparing a lower bound to the floor — so those tests take a branch on which
`reputation_floor_bps` is dead config. Raise it to 9000, above the prior bound,
and they all still pass while no newcomer in the product can be hired again.

So every snapshot here gives the cold-start agent the RepInfo
`reputation_svc._prior_info` actually serves an unrated agent — source
"prior", count 0, weight 0, smoothed 7000, lower bound 5677 — and drives it
through the public `decompose()` entry point on both planning paths. The two
hostile-floor tests below are the ones that hold the invariant at the routing
level rather than the arithmetic level: they fail the moment the configured
floor stops admitting newcomers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import binding_registry, orchestrator_svc, reputation_svc
from app.services.reputation_svc import RepInfo
from app.state import state

# Matches a DemoKit, so decompose() takes the curated kit path.
KIT_INTENT = "tetris game in html"
# Matches none, so decompose() takes the free-form model path.
FREE_FORM_INTENT = "write a haiku about databases"

# The brand-new registrant: indexed from the chain, no ratings, no local
# worker, bound to an operator endpoint. `ext_` namespace on purpose — `agt_`
# is the seeded catalog.
NEWCOMER = "ext_new1"

# The kit pipeline's designer slot. Chosen because no other seeded agent shares
# a skill with it, so when it fails the floor the slot is DROPPED rather than
# quietly refilled — the exclusion is the floor's own verdict, unmixed with a
# substitution. The newcomer below shares its skills and is the only candidate
# that can ever stand in for it.
COLD_KIT_AGENT = "agt_02k2"

# A floor above the prior's lower bound (5677 bps). This is the hostile config
# the suite is blind to today: legal, one env var away, and fatal to the
# permissionless guarantee.
HOSTILE_FLOOR_BPS = 9000


async def _no_sleep(*_a: object, **_k: object) -> None:
    """Stands in for the kit path's simulated thinking time."""
    return None


class _FakeBindingStore:
    """Stands in for the durable BindingStore. Only `list_agent_ids` is
    exercised — `refresh_bound_ids` is the one public way to load the set of
    agents the planner considers dispatchable."""

    def __init__(self, *agent_ids: str) -> None:
        self._ids = frozenset(agent_ids)

    async def list_agent_ids(self) -> frozenset[str]:
        return self._ids


def _load_bound(monkeypatch: pytest.MonkeyPatch, *agent_ids: str) -> None:
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _FakeBindingStore(*agent_ids))
    asyncio.run(binding_registry.refresh_bound_ids())


def _newcomer() -> Agent:
    """A freshly indexed on-chain agent, built the way `registry_sync` builds
    one: prior-derived display rep, zero runs, no local worker, `source`
    "onchain". Its skills mirror the kit designer's so it is a plausible
    stand-in for that slot rather than an agent no plan could ever want."""
    return Agent(
        id=NEWCOMER,
        name="newcomer.design",
        skills=["ui", "tokens", "figma"],
        price=0.02,
        rep=settings.reputation_prior_bps / 2000,
        status="online",
        runs=0,
        real=False,
        owner="GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        source="onchain",
    )


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> object:
    """The seeded catalog plus one brand-new on-chain agent, BOUND.

    The binding is load-bearing. `_routable_registry` filters candidates on
    `is_dispatchable` BEFORE the floor is consulted, so an unbound on-chain
    agent is dropped for want of an endpoint and never reaches the floor at
    all — a cold-start test built on one would pass while asserting nothing
    about reputation, and would keep passing with the floor at 10000. Binding
    it puts it on the floor's side of that filter, which is the only place the
    guarantee under test can be observed.

    Registry and bound set are both restored afterwards, and the kit path's
    randomized thinking sleep is no-op'd so the curated tests stay fast.
    """
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    state.add_agent(_newcomer())
    _load_bound(monkeypatch, NEWCOMER)
    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _no_sleep)
    yield
    _load_bound(monkeypatch)
    state.agents.clear()
    state.agents.update(saved)


def _cold_start(agent_id: str) -> RepInfo:
    """What the service itself serves an agent with no on-chain evidence.

    Deliberately not a hand-written RepInfo: a literal would go on asserting
    5677 after the prior, the prior weight or the Wilson constant moved, and
    the test would then be protecting a number instead of the code that
    produces it.
    """
    return reputation_svc._prior_info(agent_id)


def _rated(agent_id: str, *, lower: int = 9600) -> RepInfo:
    """An agent with real on-chain evidence, at a chosen lower bound.

    The default clears every floor used in this module, so a rated agent is
    never the reason a plan changes shape and the starvation backstop never
    fires — whatever happens to the cold-start agent is the floor acting on it
    alone.
    """
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=9800,
        lower_bound_bps=lower,
        avg_bps=9800,
        count=40,
        weight=40 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


def _snapshot(*cold: str) -> dict[str, RepInfo]:
    """A reputation snapshot covering EVERY agent in the registry.

    No key is left out, and that is the point: a missing key takes
    `passes_floor`'s `info is None` short circuit, which answers True without
    looking at the floor, so a suite that models cold start as an absent entry
    passes under any floor whatsoever. Here the cold agents carry the real
    prior object and are judged by the same comparison a rated agent is.
    """
    return {a.id: (_cold_start(a.id) if a.id in cold else _rated(a.id)) for a in state.list_agents()}


def _cold_start_snapshot() -> dict[str, RepInfo]:
    """One snapshot for both planning paths: two unrated agents — the on-chain
    newcomer and the kit pipeline's designer — and ten rated ones. Shared so
    the kit and model tests reach the same conclusion about the same world."""
    return _snapshot(NEWCOMER, COLD_KIT_AGENT)


def _freeze_reps(monkeypatch: pytest.MonkeyPatch, reps: dict[str, RepInfo]) -> None:
    """Pin the per-decompose reputation read to `reps` — no chain, no clock."""

    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return reps

    monkeypatch.setattr(reputation_svc, "fetch_reps", _fake_reps)


def _offered(prompt: str) -> set[str]:
    """The agent ids in the AVAILABLE_AGENTS block the planner was handed.

    This is the candidate set: an agent absent from this block cannot be
    chosen, whatever the model decides. Read off the prompt rather than from a
    helper's return value because it is what the planner actually saw.
    """
    return {line.split()[1].removeprefix("id=") for line in prompt.splitlines() if line.startswith("- id=")}


def _run_model_path(
    monkeypatch: pytest.MonkeyPatch,
    reps: dict[str, RepInfo],
    *picks: str,
) -> tuple[DecomposeResponse, str]:
    """decompose() on a free-form intent; returns the response and the prompt.

    The stub planner returns a plan naming `picks`, so the response reflects a
    model choice rather than the empty-plan fallback.
    """
    seen: list[str] = []

    async def _arun(prompt: str) -> object:
        seen.append(prompt)
        steps = [
            PlanStep(agent_id=aid, rationale="model-chosen step", est_price_usdc=0.05, est_eta_seconds=1.0)
            for aid in picks
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    _freeze_reps(monkeypatch, reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _arun)
    resp = asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))
    assert len(seen) == 1
    return resp, seen[0]


def _run_kit_path(monkeypatch: pytest.MonkeyPatch, reps: dict[str, RepInfo]) -> DecomposeResponse:
    """decompose() on a curated intent, with the LLM boobytrapped — the kit
    path is the demo safety net and must reach its verdict with no model
    call at all, so any call fails the test loudly."""

    # Recorded rather than raised: decompose degrades a planner call that
    # raises to its fallback plan (BLO-121), which would swallow the raise.
    llm_calls: list[object] = []

    async def _record(*a: object, **_k: object) -> object:
        llm_calls.append(a)
        return None

    _freeze_reps(monkeypatch, reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _record)
    resp = asyncio.run(orchestrator_svc.decompose(KIT_INTENT))
    assert llm_calls == [], "the kit path must never call the LLM"
    return resp


# ── the premise every assertion below rests on ──────────────────


def test_an_unrated_agent_has_zero_ratings_and_zero_evidence_weight(registry: object) -> None:
    """AC-1's "zero ratings and zero evidence weight", as the service serves it.

    If this drifts — a prior that arrives carrying evidence, or `degraded`
    defaulting True — every test below keeps passing while no longer
    describing a cold start, and the guarantee they claim to hold goes
    untested. The last two assertions are the distinction the rest of the
    suite misses: this entry clears the floor by ARITHMETIC, on a real object,
    not by the `info is None` short circuit a missing entry takes.
    """
    info = _cold_start(NEWCOMER)

    assert info.count == 0
    assert info.weight == 0
    assert info.source == "prior"
    # Not a failed read. `degraded` means the ledger could not be reached; a
    # newcomer's prior is the answer, not a fallback for one, and a buyer told
    # otherwise would read an honest verdict as an outage.
    assert info.degraded is False

    assert info.lower_bound_bps >= settings.reputation_floor_bps
    assert reputation_svc.passes_floor(info) is True


# ── AC-1: a brand-new agent is routable (model path) ────────────


def test_cold_start_agent_is_routable_on_the_model_path(registry: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1. A newly registered agent reaches the planner and is never reported
    as excluded.

    Both halves matter and the second is the one story 3.02 made assertable.
    An agent that never reaches the candidate set is one nobody can hire,
    whatever the marketplace page says about it — permissionless registration
    that cannot be followed by a job is a listing, not a market. And a
    cold-start agent NAMED in a `below_floor` notice tells the buyer we
    protected them from a newcomer, which is the opposite of the guarantee:
    the plan card would narrate an exclusion the floor never made.
    """
    reps = _cold_start_snapshot()

    resp, prompt = _run_model_path(monkeypatch, reps, NEWCOMER, "agt_11c0")

    # Offered to the planner, and picked: routable end to end.
    assert {NEWCOMER, COLD_KIT_AGENT} <= _offered(prompt)
    assert NEWCOMER in [s.agent_id for s in resp.steps]

    step = next(s for s in resp.steps if s.agent_id == NEWCOMER)
    assert step.rep_source == "prior"
    assert step.rep_bps == settings.reputation_prior_bps

    # Nothing was taken away from this plan at all — so in particular nothing
    # was taken away from either unrated agent.
    assert resp.notices == []
    # A cold start is not a degraded read: conflating the two would tell the
    # buyer the trust gate ran on an estimate when it ran on the real answer.
    assert resp.reputation_degraded is False


# ── AC-4: the same guarantee on the curated kit path ────────────


def test_cold_start_agent_keeps_its_kit_pipeline_slot(registry: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4. An unrated agent holding a kit slot keeps it, with no LLM call.

    The kit path applies the floor itself rather than filtering a candidate
    list, so it can lose the guarantee independently of the model path — and
    it is the path every demo takes. If this stops holding, the curated
    pipeline silently reshuffles around its own designer for want of ratings
    it was never given a chance to earn, and the plan card explains the swap
    to the buyer as a trust verdict.
    """
    reps = _cold_start_snapshot()

    resp = _run_kit_path(monkeypatch, reps)

    ids = [s.agent_id for s in resp.steps]
    assert COLD_KIT_AGENT in ids
    assert len(ids) == 6  # the full curated pipeline, nothing dropped or swapped

    step = next(s for s in resp.steps if s.agent_id == COLD_KIT_AGENT)
    assert step.rep_source == "prior"
    assert step.rep_bps == settings.reputation_prior_bps
    assert step.substituted_for is None
    assert step.degraded is False

    assert resp.notices == []
    assert resp.reputation_degraded is False
    # Same snapshot, same conclusion as the model path: the floor this plan was
    # built under is the shipped one, and it admits newcomers.
    assert resp.floor_bps == settings.reputation_floor_bps


def test_cold_start_newcomer_can_take_a_kit_slot_from_a_sub_floor_agent(
    registry: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4's hiring half: zero ratings is not a reason to pass an agent over.

    "Not excluded" is a weak guarantee if a newcomer can still never be
    SELECTED. `_floor_substitute` is the kit path's hiring decision, and it
    applies the same floor — so an agent with no history is eligible to take
    real paid work away from a rated agent that fell below it. That is the
    mechanism the reputation system exists to provide: if it stopped holding,
    a sub-floor incumbent's slot would be dropped rather than filled, and the
    buyer would get a shorter pipeline instead of a working newcomer.
    """
    reps = _snapshot(NEWCOMER)
    reps[COLD_KIT_AGENT] = _rated(COLD_KIT_AGENT, lower=100)

    resp = _run_kit_path(monkeypatch, reps)

    ids = [s.agent_id for s in resp.steps]
    assert COLD_KIT_AGENT not in ids
    assert NEWCOMER in ids
    assert len(ids) == 6  # the substitution keeps the pipeline full

    step = next(s for s in resp.steps if s.agent_id == NEWCOMER)
    assert step.substituted_for == COLD_KIT_AGENT
    assert step.rep_source == "prior"

    note = next(n for n in resp.notices if n.agent_id == COLD_KIT_AGENT)
    assert note.kind == "substituted"
    assert note.reason_code == "below_floor"
    assert note.replacement_id == NEWCOMER
    # The notice is about the agent the floor removed. The newcomer doing the
    # work is never the subject of one.
    assert not any(n.agent_id == NEWCOMER for n in resp.notices)


# ── the tests the rest of the suite cannot do ───────────────────


def test_a_floor_above_the_prior_excludes_the_newcomer_from_model_routing(
    registry: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A floor raised above the prior lower bound must FAIL this suite, at the
    ROUTING level rather than the arithmetic level.

    Today only one assertion on a pure function (`test_rating_economics`'s
    `prior_clears_floor`) notices a hostile `reputation_floor_bps`. Every
    decompose-level test models cold start as a missing entry, takes
    `passes_floor`'s None branch, and passes with the floor at 9000 — so the
    config that decides whether the marketplace is open to newcomers could be
    changed without a single planning test objecting. This drives the real
    prior object through `decompose()`, so the 5677-versus-floor comparison
    actually runs: with the floor above the bound the newcomer vanishes from
    the planner's candidate set and is reported as `below_floor`, which is
    precisely the shipped behaviour AC-1 forbids.
    """
    monkeypatch.setattr(settings, "reputation_floor_bps", HOSTILE_FLOOR_BPS)
    reps = _cold_start_snapshot()

    resp, prompt = _run_model_path(monkeypatch, reps, "agt_11c0")

    assert NEWCOMER not in _offered(prompt)
    assert NEWCOMER not in [s.agent_id for s in resp.steps]

    note = next(n for n in resp.notices if n.agent_id == NEWCOMER)
    assert note.kind == "excluded"
    assert note.reason_code == "below_floor"
    assert note.lower_bound_bps == reps[NEWCOMER].lower_bound_bps
    assert note.floor_bps == HOSTILE_FLOOR_BPS
    assert resp.floor_bps == HOSTILE_FLOOR_BPS

    # Both unrated agents, and only them: the ten rated agents still clear the
    # floor, so the starvation backstop never fires and nothing here is the
    # backstop reshaping the plan.
    assert [n.agent_id for n in resp.notices] == [COLD_KIT_AGENT, NEWCOMER]


def test_a_floor_above_the_prior_drops_the_cold_start_kit_slot(
    registry: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same proof on the curated path, because a floor is configured once
    and applied by two independent implementations.

    The kit path never consults the candidate-set filter the model path uses:
    it calls `passes_floor` per pipeline agent and again per substitute. A
    hostile floor therefore has to be caught here on its own evidence, and
    this is the demo path — the failure mode is a curated pipeline that comes
    back a step short, with the missing designer explained away as
    under-qualified when its only fault is being new.
    """
    monkeypatch.setattr(settings, "reputation_floor_bps", HOSTILE_FLOOR_BPS)
    reps = _cold_start_snapshot()

    resp = _run_kit_path(monkeypatch, reps)

    ids = [s.agent_id for s in resp.steps]
    assert COLD_KIT_AGENT not in ids
    assert len(ids) == 5  # five rated agents clear the floor; the slot is gone
    # The one agent that could have filled the slot is itself a newcomer, so
    # the same floor removes the substitute too — a floor above the prior does
    # not merely re-rank the market, it closes it.
    assert NEWCOMER not in ids

    note = next(n for n in resp.notices if n.agent_id == COLD_KIT_AGENT)
    assert note.kind == "excluded"
    assert note.reason_code == "below_floor"
    assert note.lower_bound_bps == reps[COLD_KIT_AGENT].lower_bound_bps
    assert note.floor_bps == HOSTILE_FLOOR_BPS
    assert resp.floor_bps == HOSTILE_FLOOR_BPS


def test_a_cold_start_step_reads_as_new_not_as_a_failed_read(registry: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """The enriched step fields must let a card tell a newcomer from an outage.

    A newcomer and an unreadable ledger are both served the prior, so both
    arrive as `rep_source="prior"` with a count of 0. `rep_degraded` is the one
    field that separates them, and it has to be False here on both paths: a
    newcomer shown as a failed read looks broken on the one plan it finally
    got picked for, and a card that cannot tell the two apart has to hedge on
    every unrated agent in the market.
    """
    reps = _cold_start_snapshot()

    model_resp, _ = _run_model_path(monkeypatch, reps, NEWCOMER)
    kit_resp = _run_kit_path(monkeypatch, reps)

    for resp, agent_id in ((model_resp, NEWCOMER), (kit_resp, COLD_KIT_AGENT)):
        step = next(s for s in resp.steps if s.agent_id == agent_id)
        assert step.rep_source == "prior"
        assert step.rep_count == 0
        assert step.rep_dispute_rate_bps == 0
        assert step.rep_lower_bound_bps == reps[agent_id].lower_bound_bps
        assert step.rep_degraded is False
