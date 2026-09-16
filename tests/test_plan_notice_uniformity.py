"""Both planning paths must report the reputation floor the same way (story 3.02).

Story 3.01 taught the curated demo-kit path to apply the floor and say so. The
free-form path applies the same floor — `passes_floor` filters the routing
prompt — and says nothing at all. So the feature exists only on the one intent
a reviewer is most likely to type: "tetris game in html" returns exclusions,
and every other intent returns a plan with the same agents quietly missing
from it. A buyer cannot tell a plan that considered an agent and rejected it
from one that never saw it.

These are the CONTRACT tests for the fix. They drive `decompose()` — the
public entry point both paths share — and assert on the RESPONSE only, never
on the helpers that build it, so the free-form path's prompt construction can
be restructured underneath them without touching this file.

What they pin:

  * the two paths report the same floor action, in the same shape, with the
    same closed reason vocabulary, from one shared reputation snapshot;
  * the floor a plan was actually built against travels on the response, read
    from settings rather than assumed by the client;
  * an agent the planner simply did not choose is NOT an exclusion;
  * every field this story adds is optional, so a pre-3.02 client still
    validates and a full payload round-trips unchanged;
  * a notice can never be internally contradictory — no substitution without a
    replacement, no degradation that fails to say the floor was relaxed.

Hermetic throughout: `reputation_svc.fetch_reps` is the only thing on this
path that would reach the chain and it is always replaced with a fixed
snapshot, and the planning LLM is either stubbed or booby-trapped.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.demo_kits import detect_kit
from app.schemas import DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

# A curated intent, and one that must never match a kit. Which path each takes
# is decompose()'s business, not this file's — that is the point of the story.
KIT_INTENT = "tetris game in html"
FREE_FORM_INTENT = "write a launch announcement for a neighbourhood bakery"

# Sub-floor lower bound. Deliberately absurd rather than marginal so no floor
# an operator could plausibly configure lets it through, and so the number can
# be asserted on when it reappears as data on the notice.
SUB_FLOOR_LOWER_BPS = 100

# agt_02k2 (design.figma) holds "ui"/"tokens"/"figma", which no off-pipeline
# agent shares — so the kit path DROPS it rather than substituting, which is
# the one floor action the free-form path can also produce. That makes it the
# only agent the two paths can be compared on directly.
UNSUBSTITUTABLE_KIT_AGENT = "agt_02k2"


async def _noop(*_a: object, **_k: object) -> None:
    return None


def _rep(agent_id: str, *, smoothed: int, lower: int, degraded: bool = False) -> RepInfo:
    """An on-chain reputation entry with the two numbers routing actually reads."""
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower,
        avg_bps=smoothed,
        count=5,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
        degraded=degraded,
    )


def _sub_floor(agent_id: str, *, smoothed: int = 4000) -> RepInfo:
    """A rep entry that FAILS the routing floor on both paths."""
    return _rep(agent_id, smoothed=smoothed, lower=SUB_FLOOR_LOWER_BPS)


def _clears_floor(agent_id: str) -> RepInfo:
    """A rep entry comfortably above any shipped floor."""
    return _rep(agent_id, smoothed=8000, lower=8000)


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> object:
    """Fresh 12-agent registry, restored after; kit thinking-sleep no-op'd.

    Mirrors tests/test_kit_floor.py rather than importing from it: a fixture
    reached across test modules is a fixture two lanes can break each other
    with, and this file must keep failing for story reasons only.
    """
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _noop)
    yield
    state.agents.clear()
    state.agents.update(saved)


def _freeze_reps(monkeypatch: pytest.MonkeyPatch, reps: dict[str, RepInfo]) -> None:
    """Pin the reputation snapshot decompose() reads — and keep it offline."""

    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return reps

    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _fake_reps)


def _run_kit(monkeypatch: pytest.MonkeyPatch, reps: dict[str, RepInfo]) -> DecomposeResponse:
    """decompose() on the curated path, with the planning LLM booby-trapped.

    The kit path is deterministic by design; a call to the model here means
    the short circuit broke, and that must fail the test rather than quietly
    cost money on every demo.
    """

    async def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the kit path must never call the LLM")

    _freeze_reps(monkeypatch, reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _boom)
    return asyncio.run(orchestrator_svc.decompose(KIT_INTENT))


def _run_free_form(
    monkeypatch: pytest.MonkeyPatch,
    reps: dict[str, RepInfo],
    picks: list[str],
) -> DecomposeResponse:
    """decompose() on the free-form path with the planner LLM stubbed.

    `picks` is what the model "returns". Nothing here inspects the prompt: the
    prompt is an implementation detail this story is actively rewriting, and a
    test that reads it would fail for reasons that are not the contract.
    """
    # Guard: a new kit trigger that happened to match this string would turn
    # the free-form half of every comparison below into a second kit run, and
    # the suite would go green while the story stayed unimplemented.
    assert detect_kit(FREE_FORM_INTENT) is None

    async def _arun(*_a: object, **_k: object) -> SimpleNamespace:
        steps = [
            PlanStep(
                agent_id=agent_id,
                rationale="model-chosen step",
                est_price_usdc=0.05,
                est_eta_seconds=1.0,
            )
            for agent_id in picks
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    _freeze_reps(monkeypatch, reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _arun)
    return asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))


def _reported(resp: DecomposeResponse) -> list[tuple[str, str, str, int | None, int]]:
    """Every floor action on a response, as comparable data.

    Deliberately includes the deciding numbers: two paths that agree on the
    verdict but disagree on `lower_bound_bps` or `floor_bps` render two
    different sentences on the same plan card, which is the defect this story
    exists to remove.
    """
    return sorted((n.kind, n.agent_id, n.reason_code, n.lower_bound_bps, n.floor_bps) for n in resp.notices)


def test_both_paths_report_the_same_floor_action(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6. One reputation snapshot, two intents, identical reporting.

    If this stops holding, the product has a trust gate that only narrates
    itself on demo intents: the free-form path drops a sub-floor agent from
    its candidate pool exactly as the kit path does, and a buyer reading the
    response cannot tell the agent was ever considered. Every downstream
    renderer — plan card, integration guide, dispute evidence — then has to
    special-case which intent produced the plan.
    """
    # One agent under the floor, everyone else silent (no entry == cold start
    # == routable), so each path has exactly one action to report and the two
    # responses can be compared element for element.
    reps = {UNSUBSTITUTABLE_KIT_AGENT: _sub_floor(UNSUBSTITUTABLE_KIT_AGENT)}

    kit = _run_kit(monkeypatch, reps)
    free_form = _run_free_form(monkeypatch, reps, ["agt_11c0", "agt_01h8"])

    # The agent really is gone from both plans — without this the comparison
    # below could be satisfied by two paths that both report nothing.
    assert UNSUBSTITUTABLE_KIT_AGENT not in [s.agent_id for s in kit.steps]
    assert UNSUBSTITUTABLE_KIT_AGENT not in [s.agent_id for s in free_form.steps]

    expected = [
        (
            "excluded",
            UNSUBSTITUTABLE_KIT_AGENT,
            "below_floor",
            SUB_FLOOR_LOWER_BPS,
            kit.floor_bps,
        )
    ]
    assert _reported(kit) == expected
    assert _reported(free_form) == expected

    # Same floor, same snapshot health: the two responses describe one world.
    assert kit.floor_bps == free_form.floor_bps
    assert kit.reputation_degraded == free_form.reputation_degraded

    # The prose differs (it names a different plan), but both paths owe the
    # buyer a sentence — a reason_code with no reason is an unrenderable card.
    for resp in (kit, free_form):
        assert all(n.reason.strip() for n in resp.notices)


def test_both_paths_report_the_configured_floor(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2. `floor_bps` is the floor this plan was built against, from settings.

    The threshold is per-deployment config. A client that hardcodes 5500 to
    render "4.10 against a 3.00 floor" narrates the wrong number the moment an
    operator tunes REPUTATION_FLOOR_BPS — silently, and in the one sentence
    the buyer uses to judge whether the exclusion was fair. Two different
    floors are exercised so a constant baked into the response cannot pass.
    """
    reps = {UNSUBSTITUTABLE_KIT_AGENT: _sub_floor(UNSUBSTITUTABLE_KIT_AGENT)}

    for floor in (4200, 3300):
        # Both below the shipped 5500 default, so a prior-only agent still
        # clears them and the only floor action stays the one this test set up.
        monkeypatch.setattr(settings, "reputation_floor_bps", floor)

        kit = _run_kit(monkeypatch, reps)
        free_form = _run_free_form(monkeypatch, reps, ["agt_11c0"])

        assert kit.floor_bps == floor
        assert free_form.floor_bps == floor

        # And the same number reaches the notice, so a card rendering one
        # exclusion never has to reach back to the envelope to find the floor.
        assert [n.floor_bps for n in kit.notices] == [floor]
        assert [n.floor_bps for n in free_form.notices] == [floor]


def test_unpicked_agents_are_not_reported_as_excluded(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3. Not being chosen is not an exclusion.

    The whole point of a notice is that it is rare enough to read. A registry
    of twelve agents produces a plan of one to six, so reporting "everyone we
    didn't hire" would put five to eleven lines on every single plan card and
    bury the one line that says an agent failed the trust gate. Product rules
    forbid it, and `not_selected_by_planner` is deliberately not in the reason
    vocabulary — this test is what stops it being reintroduced by behaviour
    instead of by a Literal.
    """
    # Every agent well clear of the floor: nothing here is a floor action.
    reps = {a.id: _clears_floor(a.id) for a in state.list_agents()}

    kit = _run_kit(monkeypatch, reps)
    free_form = _run_free_form(monkeypatch, reps, ["agt_11c0"])

    # Both plans genuinely left agents on the table — otherwise there is
    # nothing to under-report and the assertions below are vacuous.
    assert len(kit.steps) < len(state.agents)
    assert len(free_form.steps) == 1

    assert [n.agent_id for n in kit.notices] == []
    assert [n.agent_id for n in free_form.notices] == []

    # Named explicitly because agt_04m1 (sol-audit) is dispatchable, clears
    # the floor, and is on neither plan — the exact shape of agent a
    # list-everything implementation would report.
    for resp in (kit, free_form):
        assert "agt_04m1" not in [s.agent_id for s in resp.steps]
        assert not any(n.reason_code == "below_floor" for n in resp.notices)
